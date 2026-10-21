"""Regal seat maps from the rendered movie/showtime page.

The old JSON route was recoverable from Regal's bundle, but it is not a public
read surface: `/api/GetSeatPlan` redirects to the public site and Cloudflare
blocks the result. The public booking flow is different. Clicking a showtime
on a theatre page navigates to:

    /movies/{title-slug}-{movie-code}?date=YYYY-MM-DD&site={theatre}&id={performance}

That page renders the seat plan as ordinary buttons. Each real seat carries a
stable id such as `seat-0-2-8`, an `aria-label` such as `B4 accessible seat`, a
title describing its kind, and `disabled` when it cannot be selected. No seat
is selected here, so this remains a read-only page visit; a hold is created by
the later click that this project never makes.

The JSON parser remains for compatibility with the earlier Vista-shaped
implementation and for any deployment that still receives that payload. The
live path is the rendered HTML parser below.
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

from ..model import (
    Auditorium,
    Seat,
    SeatDataUnavailable,
    SeatKind,
    SeatStatus,
    mark_aisles,
    normalize_geometry,
)

BOOKING_API = "https://webbooking.regmovies.com"
SEAT_PLAN = "{base}/api/GetSeatPlan?theatreCode={theatre}&sessionId={session}"
REGAL = "https://www.regmovies.com"

# Vista seat status codes, as documented and as observed.
_AVAILABLE = {0, "0", "Available", "available"}
_SOLD = {1, "1", "Sold", "sold", 2, "2", "Reserved", "reserved",
         "Held", "held", "Unavailable", "unavailable"}
_HOUSE = {3, "3", "House", "house"}
_BROKEN = {4, "4", "Broken", "broken"}

_KIND = {
    0: SeatKind.STANDARD,
    1: SeatKind.WHEELCHAIR,
    2: SeatKind.COMPANION,
    3: SeatKind.LOVESEAT,
    "Standard": SeatKind.STANDARD,
    "Wheelchair": SeatKind.WHEELCHAIR,
    "Companion": SeatKind.COMPANION,
    "House": SeatKind.STANDARD,
    "Sofa": SeatKind.LOVESEAT,
    "Recliner": SeatKind.RECLINER,
}


def _pick(node: dict, *names, default=None):
    """Vista payloads ship both camelCase and PascalCase depending on proxy."""
    for name in names:
        if name in node:
            return node[name]
        lowered = name[0].lower() + name[1:]
        if lowered in node:
            return node[lowered]
    return default


def _movie_slug(title: str) -> str:
    """Match Regal's route slug: punctuation disappears, spaces become `-`."""
    ascii_title = unicodedata.normalize("NFKD", title).encode(
        "ascii", "ignore"
    ).decode()
    cleaned = re.sub(r"[^a-z0-9\s]", "", ascii_title.lower())
    return re.sub(r"\s+", "-", cleaned).strip("-")


def _movie_route(title: str, movie_code: str) -> str:
    slug = _movie_slug(title)
    code = (movie_code or "").strip().lower()
    if not slug and not code:
        raise SeatDataUnavailable("Regal seat page needs a movie title or code")
    return "-".join(part for part in (slug, code) if part)


class _RenderedSeatParser(HTMLParser):
    """Read the seat buttons Regal renders after the movie page hydrates."""

    _SEAT_ID = re.compile(r"^seat-(\d+)-(\d+)-(\d+)$")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.seats: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "button":
            return
        values = dict(attrs)
        match = self._SEAT_ID.fullmatch(values.get("id") or "")
        if not match:
            return

        aria = (values.get("aria-label") or "").strip()
        seat_label, _, description = aria.partition(" ")
        label_match = re.fullmatch(r"([A-Za-z]+)(\d+)", seat_label)
        row_label = label_match.group(1) if label_match else str(match.group(2))
        col_label = label_match.group(2) if label_match else str(match.group(3))
        self.seats.append(
            {
                "row_index": int(match.group(2)),
                "col_index": int(match.group(3)),
                "row_label": row_label,
                "col_label": col_label,
                "description": f"{description} {values.get('title') or ''}".lower(),
                "available": "disabled" not in values,
            }
        )


def _rendered_kind(description: str) -> SeatKind:
    if "companion" in description:
        return SeatKind.COMPANION
    if "accessible" in description or "wheelchair" in description:
        return SeatKind.WHEELCHAIR
    if "love" in description:
        return SeatKind.LOVESEAT
    if "recliner" in description:
        return SeatKind.RECLINER
    return SeatKind.STANDARD


class RegalSeatSource:
    chain = "regal"
    source = "regal:seatplan"
    tier = 3

    def url(self, theatre_code: str, session_id: str, *, base: str = BOOKING_API,
            bypass: bool = True) -> str:
        url = SEAT_PLAN.format(base=base, theatre=theatre_code, session=session_id)
        return url + "&bypass=true" if bypass else url

    def movie_url(
        self,
        *,
        title: str,
        movie_code: str,
        date: str,
        theatre_code: str,
        performance_id: str,
        base: str = REGAL,
    ) -> str:
        """Build the same route Regal creates when a showtime is clicked."""
        route = _movie_route(title, movie_code)
        query = urlencode({
            "date": date,
            "site": theatre_code,
            "id": performance_id,
        })
        return f"{base}/movies/{route}?{query}"

    # ------------------------------------------------------------------
    @staticmethod
    def parse(payload: Any, *, venue_id: str, screen_id: str = "") -> Auditorium:
        if isinstance(payload, str):
            return RegalSeatSource._parse_html(
                payload, venue_id=venue_id, screen_id=screen_id
            )
        if not isinstance(payload, dict):
            raise SeatDataUnavailable("unexpected Regal seat plan payload")
        return RegalSeatSource._parse_json(
            payload, venue_id=venue_id, screen_id=screen_id
        )

    @staticmethod
    def _parse_html(html: str, *, venue_id: str, screen_id: str) -> Auditorium:
        if any(marker in html for marker in ("Attention Required", "Sorry, you have been blocked")):
            raise SeatDataUnavailable("Regal seat page was blocked by Cloudflare")
        parser = _RenderedSeatParser()
        parser.feed(html)
        if not parser.seats:
            raise SeatDataUnavailable(
                "Regal seat page HTML had no rendered seat buttons - page shape "
                "changed, or this showing is general admission"
            )

        seats = [
            Seat(
                row_label=seat["row_label"],
                row_index=seat["row_index"],
                col_label=seat["col_label"],
                col_index=seat["col_index"],
                status=(
                    SeatStatus.AVAILABLE
                    if seat["available"]
                    else SeatStatus.SOLD
                ),
                kind=_rendered_kind(seat["description"]),
            )
            for seat in parser.seats
        ]
        return Auditorium(
            venue_id=venue_id,
            screen_id=screen_id,
            seats=normalize_geometry(mark_aisles(seats)),
            geometry_confidence=1.0,
        )

    @staticmethod
    def _parse_json(payload: dict, *, venue_id: str, screen_id: str) -> Auditorium:
        if payload.get("errorCode") or payload.get("ErrorCode"):
            raise SeatDataUnavailable(
                f"Regal seat plan error: "
                f"{payload.get('errorMessage') or payload.get('ErrorCode')}"
            )

        areas = _pick(payload, "SeatLayoutData", "seatLayoutData") or payload
        areas = _pick(areas, "Areas", "areas") or []
        if not areas:
            raise SeatDataUnavailable("Regal seat plan contains no seating areas")

        seats: list[Seat] = []
        for area in areas:
            for row in _pick(area, "Rows", "rows") or []:
                row_label = str(
                    _pick(row, "PhysicalName", "physicalName", default="") or ""
                ).strip()
                row_index = int(_pick(row, "RowIndex", "rowIndex", default=0) or 0)
                for seat in _pick(row, "Seats", "seats") or []:
                    status = _pick(seat, "Status", "status")
                    kind_raw = _pick(seat, "SeatType", "seatType", default=0)
                    col_index = int(_pick(seat, "ColumnIndex", "columnIndex",
                                          default=0) or 0)
                    number = str(
                        _pick(seat, "Id", "id", "SeatId", default=col_index) or col_index
                    )
                    if status in _HOUSE or status in _BROKEN:
                        seat_status = SeatStatus.UNAVAILABLE
                    elif status in _AVAILABLE:
                        seat_status = SeatStatus.AVAILABLE
                    elif status in _SOLD:
                        seat_status = SeatStatus.SOLD
                    else:
                        # Not defaulted to SOLD. An unrecognised code most
                        # likely means Vista added one, and quietly calling it
                        # sold would hide every seat behind it - the failure
                        # this whole module exists to avoid. Raising loses the
                        # showing instead, which is visible and recoverable.
                        raise SeatDataUnavailable(
                            f"unrecognised Regal seat status {status!r} in row "
                            f"{row_label or row_index} - schema changed"
                        )

                    seats.append(
                        Seat(
                            row_label=row_label or str(row_index),
                            row_index=row_index,
                            col_label=number,
                            col_index=col_index,
                            status=seat_status,
                            kind=_KIND.get(kind_raw, SeatKind.STANDARD),
                        )
                    )

        if not seats:
            raise SeatDataUnavailable(
                "Regal seat plan parsed to zero seats - schema changed, or this "
                "showing is general admission"
            )

        return Auditorium(
            venue_id=venue_id,
            screen_id=screen_id,
            seats=normalize_geometry(mark_aisles(seats)),
            geometry_confidence=1.0,
        )
