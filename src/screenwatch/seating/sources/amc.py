"""AMC seat maps, from the public GraphQL schema.

Recon result (2026-08-02): `graph.amctheatres.com` accepts unauthenticated
POSTs and has **introspection enabled**, so the query shape did not need
reverse engineering at all - the schema describes itself. The seat surface is
`viewer.showtime(id: Int!).seatingLayout`.

Shape notes that cost real debugging:

* `id` is `Int!`, not `ID!`, despite the field returning an `ID`.
* The layout is a full rectangular grid, padded with `type: "NotASeat"` cells
  for aisles and walls. Those are dropped; the column gaps they leave behind
  are exactly what `mark_aisles` needs to find aisles.
* Row 1 is the front row, matching our own convention.
* Row numbers skip cross-aisles - Lincoln Square's IMAX has no rows 5, 11 or
  12 - so raw row numbers are passed through and `normalize_geometry`
  interpolates depth over the values.
* Seat *names* run right to left ("A15" sits at column 7, "A1" at column 22).
  Geometry therefore uses `column`; `name` is only ever a label.
* `available` is the authority on bookability, not `seatStatus`, which is
  blank for padding cells and splits real availability across
  "Available" and "Unblocked".
"""

from __future__ import annotations

import json

from curl_cffi import requests

from ..model import (
    Auditorium,
    BlockedBySource,
    ParserDrift,
    PermanentNoSeatMap,
    RateLimited,
    Seat,
    SeatKind,
    SeatStatus,
    TransientSourceFailure,
    infer_modules,
    mark_aisles,
    normalize_geometry,
)

ENDPOINT = "https://graph.amctheatres.com/"

SEATING_QUERY = """
query ScreenwatchSeats($id: Int!) {
  viewer {
    showtime(id: $id) {
      id
      showtimeId
      auditorium
      isReservedSeating
      status
      seatingLayout {
        rows
        columns
        isZoned
        seats {
          name
          row
          column
          available
          type
          seatStatus
          shouldDisplay
        }
      }
    }
  }
}
""".strip()

_KIND = {
    "CanReserve": SeatKind.STANDARD,
    "LoveSeatLeft": SeatKind.LOVESEAT,
    "LoveSeatRight": SeatKind.LOVESEAT,
    "Companion": SeatKind.COMPANION,
    "Wheelchair": SeatKind.WHEELCHAIR,
    "Recliner": SeatKind.RECLINER,
}


class AmcSeatSource:
    chain = "amc"
    source = "amc:seating-graphql"
    tier = 3

    def __init__(self, endpoint: str = ENDPOINT, impersonate: str = "chrome131") -> None:
        self.endpoint = endpoint
        self._session = requests.Session(impersonate=impersonate)

    # ------------------------------------------------------------------
    def fetch(self, showtime_id: str | int, *, venue_id: str = "", timeout: int = 30) -> Auditorium:
        payload = self._post(int(showtime_id), timeout)
        return self.parse(payload, showtime_id=str(showtime_id), venue_id=venue_id)

    def _post(self, showtime_id: int, timeout: int) -> dict:
        try:
            response = self._session.post(
                self.endpoint,
                json={"query": SEATING_QUERY, "variables": {"id": showtime_id}},
                headers={"content-type": "application/json"},
                timeout=timeout,
            )
        except Exception as exc:
            raise TransientSourceFailure(f"AMC seat fetch failed: {exc}") from exc

        if response.status_code != 200:
            message = f"AMC seat fetch returned HTTP {response.status_code}"
            if response.status_code == 429:
                failure = RateLimited(message)
            elif response.status_code in {401, 403}:
                failure = BlockedBySource(message)
            else:
                failure = TransientSourceFailure(message)
            raise failure.with_capture(
                response.text,
                content_type=getattr(response, "headers", {}).get(
                    "content-type", "text/plain"
                ),
                source_url=self.endpoint,
                status_code=response.status_code,
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            failure = ParserDrift("AMC seat fetch returned non-JSON")
            raise failure.with_capture(
                response.text,
                content_type=getattr(response, "headers", {}).get(
                    "content-type", "text/plain"
                ),
                source_url=self.endpoint,
                status_code=response.status_code,
            ) from exc

    # ------------------------------------------------------------------
    @staticmethod
    def parse(payload: dict, *, showtime_id: str, venue_id: str = "") -> Auditorium:
        """Pure. Given a GraphQL response, produce a normalized Auditorium."""
        if errors := payload.get("errors"):
            raise TransientSourceFailure(
                f"AMC GraphQL error: {errors[0].get('message', 'unknown')}"
            )

        showtime = ((payload.get("data") or {}).get("viewer") or {}).get("showtime")
        if not showtime:
            raise TransientSourceFailure(f"AMC returned no showtime {showtime_id}")

        # General-admission houses genuinely have no seat map. That is a
        # permanent property of the screening, not a transient failure, so it
        # must not read as "the fetch broke".
        if showtime.get("isReservedSeating") is False:
            raise PermanentNoSeatMap(
                f"showtime {showtime_id} is general admission - no seat map exists"
            )

        layout = showtime.get("seatingLayout")
        if not layout or not layout.get("seats"):
            raise TransientSourceFailure(
                f"AMC returned no seating layout for {showtime_id}"
            )

        seats: list[Seat] = []
        for raw in layout["seats"]:
            kind_name = raw.get("type") or ""
            if kind_name == "NotASeat":
                continue          # grid padding; its absence becomes an aisle gap
            kind = _KIND.get(kind_name, SeatKind.STANDARD)
            status = (
                SeatStatus.AVAILABLE if raw.get("available")
                else SeatStatus.SOLD if (raw.get("seatStatus") or "") == "Sold"
                else SeatStatus.UNAVAILABLE
            )
            row = int(raw["row"])
            column = int(raw["column"])
            name = raw.get("name") or ""
            seats.append(
                Seat(
                    row_label=_row_label(name, row),
                    row_index=row,
                    col_label=_col_label(name, column),
                    col_index=column,
                    status=status,
                    kind=kind,
                )
            )

        if not seats:
            raise ParserDrift(
                f"AMC layout for {showtime_id} contained only padding cells"
            )

        return Auditorium(
            venue_id=venue_id or "amc",
            screen_id=str(showtime.get("auditorium") or ""),
            seats=normalize_geometry(mark_aisles(infer_modules(seats))),
            geometry_confidence=1.0,
            name=f"Auditorium {showtime.get('auditorium')}",
        )


def _row_label(name: str, row: int) -> str:
    """Row letter from the seat name, falling back to the raw number."""
    letters = "".join(c for c in name if c.isalpha())
    return letters or str(row)


def _col_label(name: str, column: int) -> str:
    digits = "".join(c for c in name if c.isdigit())
    return digits or str(column)
