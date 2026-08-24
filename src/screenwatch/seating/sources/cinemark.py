"""Cinemark seat maps, from the rendered seat-picker page.

A plain GET of `/TicketSeatMap/?TheaterId=&ShowtimeId=&…` returns the full
grid with per-seat availability. No login, no cart, no hold — the hold is
created when a seat is *selected*, which this never does.

Each seat is a button carrying everything needed:

    <button available="True" class="seatAvailable seatBlock" id="row0col0"
            info="A,10,0,0,546676" seatType="seat" selected="False"
            title="Available Seat A10">

`info` is `rowLabel,seatNumber,rowIndex,colIndex,showtimeId`, which gives both
the human label and the grid position — so no inference is needed about where
a seat sits.

Seat classes seen in the wild: `seatAvailable`, `seatUnavailable`, `seatBlank`
(structural gap), `companionAvailable`, `wheelchairAvailable`,
`designatedaisleAvailable`, and `physicalDistanceBuffer seatUnavailable` for
blocked spacing seats.
"""

from __future__ import annotations

import html as html_lib
import re

from ..model import (
    Auditorium,
    BlockedBySource,
    ParserDrift,
    Seat,
    SeatKind,
    SeatStatus,
    infer_modules,
    mark_aisles,
    normalize_geometry,
)

BASE = "https://www.cinemark.com"
SEAT_MAP = (
    BASE + "/TicketSeatMap/?TheaterId={theater}&ShowtimeId={showtime}"
)

_CHALLENGE = "Just a moment"
_SEAT = re.compile(r"<button([^>]*\bclass=\"[^\"]*seatBlock[^\"]*\"[^>]*)>", re.IGNORECASE)
_ATTR = re.compile(r'([a-zA-Z\-]+)="([^"]*)"')
_AUDITORIUM = re.compile(
    r'<div[^>]*class="[^"]*auditoriumNumber[^"]*"[^>]*>\s*([^<]+?)\s*</div>',
    re.IGNORECASE,
)
_AUDITORIUM_SIZE = re.compile(r'"auditorium_size"\s*:\s*"?(\d+)', re.IGNORECASE)

_KIND = {
    "seat": SeatKind.STANDARD,
    "companion": SeatKind.COMPANION,
    "wheelchair": SeatKind.WHEELCHAIR,
    "recliner": SeatKind.RECLINER,
    "loveseat": SeatKind.LOVESEAT,
    "designatedaisle": SeatKind.STANDARD,
}


def _attrs(raw: str) -> dict[str, str]:
    return {k.lower(): html_lib.unescape(v) for k, v in _ATTR.findall(raw)}


class CinemarkSeatSource:
    chain = "cinemark"
    source = "cinemark:seatmap"
    tier = 3

    def url(self, theater_id: str, showtime_id: str, *, page_url: str = "") -> str:
        """Prefer the URL the site itself wrote.

        Cinemark's own link carries four parameters; the two-parameter form
        this can build from ids alone is answered with a redirect to the
        homepage, which parses to zero seats and reads as "sold out".
        """
        if "/TicketSeatMap/" in page_url:
            return page_url
        return SEAT_MAP.format(theater=theater_id, showtime=showtime_id)

    # ------------------------------------------------------------------
    @staticmethod
    def parse(html: str, *, venue_id: str, screen_id: str = "") -> Auditorium:
        if _CHALLENGE in html:
            raise BlockedBySource("Cloudflare challenge instead of the seat map")

        decoded = html_lib.unescape(html)
        auditorium_match = _AUDITORIUM.search(decoded)
        auditorium_name = (
            html_lib.unescape(auditorium_match.group(1)).strip()
            if auditorium_match else None
        )
        parsed_screen_id = screen_id
        if auditorium_name:
            number = re.search(r"\b(\d+)\b", auditorium_name)
            if number:
                parsed_screen_id = number.group(1)
        size_match = _AUDITORIUM_SIZE.search(decoded)
        reported_capacity = int(size_match.group(1)) if size_match else None

        seats: list[Seat] = []
        source_capacity = 0
        for match in _SEAT.finditer(html):
            attrs = _attrs(match.group(1))
            info = (attrs.get("info") or "").split(",")
            classes = attrs.get("class", "")

            if "seatblank" in classes.lower().replace(" ", ""):
                continue          # structural gap; its absence becomes an aisle

            if len(info) < 4:
                continue          # legend swatches and zoom controls
            row_label, seat_number, row_index, col_index = info[0], info[1], info[2], info[3]
            if not row_index.isdigit() or not col_index.isdigit():
                continue

            kind = _KIND.get((attrs.get("seattype") or "seat").lower(), SeatKind.STANDARD)
            # Cinemark's auditorium_size is a fixed-seat count: wheelchair
            # spaces are excluded, while a temporarily blocked fixed seat is
            # still part of the room's physical capacity.
            if kind is not SeatKind.WHEELCHAIR:
                source_capacity += 1
            # `available` is the authority; the class name merely mirrors it,
            # and physical-distance buffers are unavailable with a normal type.
            available = (attrs.get("available") or "").lower() == "true"
            physical_buffer = (
                (attrs.get("physicaldistancebuffer") or "").lower() == "true"
                or "physicaldistancebuffer" in classes.lower()
            )
            if physical_buffer:
                kind = SeatKind.BLOCKED
                status = SeatStatus.UNAVAILABLE
            else:
                # Cinemark exposes available/unavailable, not a reliable
                # sold/house/broken distinction. Do not manufacture "sold".
                status = SeatStatus.AVAILABLE if available else SeatStatus.UNAVAILABLE

            seats.append(
                Seat(
                    row_label=row_label or str(row_index),
                    row_index=int(row_index),
                    col_label=seat_number or str(col_index),
                    col_index=int(col_index),
                    status=status,
                    kind=kind,
                )
            )

        if not seats:
            raise ParserDrift(
                "no seat buttons in the Cinemark seat map - markup changed, or "
                "this showing is general admission"
            )

        room = Auditorium(
            venue_id=venue_id,
            screen_id=parsed_screen_id,
            seats=normalize_geometry(mark_aisles(infer_modules(seats))),
            geometry_confidence=1.0,
            name=auditorium_name,
            reported_capacity=reported_capacity,
        )
        if reported_capacity is not None and source_capacity != reported_capacity:
            raise ParserDrift(
                "Cinemark seat-map capacity mismatch: "
                f"parsed {source_capacity}, source reported {reported_capacity}",
                context={
                    "parsed_capacity": source_capacity,
                    "reported_capacity": reported_capacity,
                    "screen_id": parsed_screen_id,
                },
            )
        return room
