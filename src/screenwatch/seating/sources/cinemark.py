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
    Seat,
    SeatDataUnavailable,
    SeatKind,
    SeatStatus,
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

    def url(self, theater_id: str, showtime_id: str) -> str:
        return SEAT_MAP.format(theater=theater_id, showtime=showtime_id)

    # ------------------------------------------------------------------
    @staticmethod
    def parse(html: str, *, venue_id: str, screen_id: str = "") -> Auditorium:
        if _CHALLENGE in html:
            raise SeatDataUnavailable("Cloudflare challenge instead of the seat map")

        seats: list[Seat] = []
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
            # `available` is the authority; the class name merely mirrors it,
            # and physical-distance buffers are unavailable with a normal type.
            available = (attrs.get("available") or "").lower() == "true"
            status = SeatStatus.AVAILABLE if available else SeatStatus.SOLD

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
            raise SeatDataUnavailable(
                "no seat buttons in the Cinemark seat map - markup changed, or "
                "this showing is general admission"
            )

        return Auditorium(
            venue_id=venue_id,
            screen_id=screen_id,
            seats=normalize_geometry(mark_aisles(seats)),
            geometry_confidence=1.0,
        )
