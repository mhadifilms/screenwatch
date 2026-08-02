"""Regal seat plans.

Endpoint recovered from Regal's own bundle rather than guessed:

    GET {booking_api}/api/GetSeatPlan?theatreCode={code}&sessionId={id}
    GET .../api/GetSeatPlan?...&bypass=true        (the app's own variant)

`booking_api` comes from `NEXT_PUBLIC_BOOKING_API` in the page's env block,
which resolves to `https://webbooking.regmovies.com`.

**Status: implemented from evidence, not yet verified against a live
response.** Cloudflare denies `/api/*` on the regmovies hosts to every client
tried, `curl_cffi` and a real browser alike. It is a *path* rule rather than
an IP ban: measured in a single session, `experience.regmovies.com/about`
returns 200 while `/api/tickets` returns 403, and showtime scraping across all
402 theatres works throughout. There is therefore nothing to wait out, and a
response most likely needs whatever the real booking flow carries that a bare
GET does not - an order session token being the obvious candidate.

So `parse` is written against the Vista seat-plan schema that endpoint
returns, and is defensive about which of the two common field spellings
arrives.

Vista (which Regal runs) returns rows of `SeatsInRow`, each seat carrying a
status code where 0 means available. Both the modern camelCase and the older
PascalCase spellings are accepted because Regal's proxy has shipped each.
"""

from __future__ import annotations

from typing import Any

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


class RegalSeatSource:
    chain = "regal"
    source = "regal:seatplan"
    tier = 3

    def url(self, theatre_code: str, session_id: str, *, base: str = BOOKING_API,
            bypass: bool = True) -> str:
        url = SEAT_PLAN.format(base=base, theatre=theatre_code, session=session_id)
        return url + "&bypass=true" if bypass else url

    # ------------------------------------------------------------------
    @staticmethod
    def parse(payload: Any, *, venue_id: str, screen_id: str = "") -> Auditorium:
        if isinstance(payload, str):
            raise SeatDataUnavailable("Regal seat plan came back as HTML, not JSON")
        if not isinstance(payload, dict):
            raise SeatDataUnavailable("unexpected Regal seat plan payload")
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
