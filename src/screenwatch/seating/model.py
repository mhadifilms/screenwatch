"""Normalized auditorium model.

Every venue describes seats differently: AMC by row letter and seat number,
a rep house by a flat list, some platforms by pixel coordinates on a diagram.
Ranking and rendering both need one shape, and more importantly they need a
shape that is *comparable across auditorium sizes* - a "good seat" in a
60-row IMAX and a "good seat" in a 12-row microcinema have to score alike.

That is what `x` and `y` are for:

    y = 0.0  front row          x = -1.0  far house left
    y = 1.0  back row           x =  0.0  on the centreline
                                x = +1.0  far house right

Both are computed from the venue's own layout, so the same scoring function
and the same renderer work everywhere without knowing anything about the room.

Sources that only expose a seat *count* still produce an Auditorium, with
`geometry_confidence = 0.0` and no seats. Ranking degrades to counting rather
than pretending it knows where the seats are.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import pairwise


class SeatStatus(Enum):
    AVAILABLE = "available"
    SOLD = "sold"
    HELD = "held"            # in someone's cart; may return
    UNAVAILABLE = "unavailable"   # broken, distancing block, house seat

    @property
    def is_open(self) -> bool:
        return self is SeatStatus.AVAILABLE


class SeatKind(Enum):
    STANDARD = "standard"
    RECLINER = "recliner"
    LOVESEAT = "loveseat"
    WHEELCHAIR = "wheelchair"
    COMPANION = "companion"
    BLOCKED = "blocked"       # structurally not a seat: aisle gap, pillar

    @property
    def is_bookable(self) -> bool:
        return self is not SeatKind.BLOCKED

    @property
    def is_accessible(self) -> bool:
        return self in (SeatKind.WHEELCHAIR, SeatKind.COMPANION)


class SeatDataUnavailable(RuntimeError):
    """No seat grid obtainable for this screening.

    Distinct from "the screening is sold out" and from "the fetch failed" -
    it means this venue or format simply does not expose seat data, so the
    ranker should fall back to availability-only scoring rather than retry.
    """


@dataclass(frozen=True)
class Seat:
    row_label: str
    row_index: int          # 0 = frontmost
    col_label: str
    col_index: int          # 0 = house left
    status: SeatStatus = SeatStatus.AVAILABLE
    kind: SeatKind = SeatKind.STANDARD
    x: float = 0.0          # -1..1 lateral, 0 = centreline
    y: float = 0.0          # 0..1 depth, 0 = front row
    aisle_adjacent: bool = False
    # Physical seats are sometimes sold/experienced as a unit (loveseats,
    # sofas, four-seat motion platforms).  `module_id` is deliberately
    # separate from kind: two adjacent recliners are not necessarily a sofa.
    module_id: str | None = None
    module_position: int | None = None
    module_size: int | None = None
    module_required: bool = False

    @property
    def id(self) -> str:
        return f"{self.row_label}{self.col_label}"

    @property
    def is_open(self) -> bool:
        return self.status.is_open and self.kind.is_bookable


@dataclass
class Auditorium:
    venue_id: str
    screen_id: str
    seats: tuple[Seat, ...] = ()
    geometry_confidence: float = 1.0     # 1.0 real coords, 0.3 inferred, 0.0 none
    reported_available: int | None = None  # when the source gives a count only
    name: str | None = None
    # Room shape without occupancy. Some platforms publish the auditorium
    # layout and a seats-sold count but never say *which* seats are taken -
    # enough to estimate whether a party can sit together, not enough to draw
    # a grid. See seating/estimate.py.
    reported_capacity: int | None = None
    row_lengths: tuple[int, ...] = ()

    # ---------------------------------------------------------------- shape
    @property
    def has_grid(self) -> bool:
        return bool(self.seats)

    @property
    def capacity(self) -> int:
        if self.seats:
            return sum(1 for s in self.seats if s.kind.is_bookable)
        return self.reported_capacity or 0

    @property
    def available(self) -> int:
        if self.seats:
            return sum(1 for s in self.seats if s.is_open)
        return self.reported_available or 0

    @property
    def occupancy(self) -> float:
        """0.0 empty .. 1.0 full. Works from a bare count too."""
        cap = self.capacity
        if cap:
            return 1.0 - (self.available / cap)
        return 0.0 if self.reported_available else 1.0

    @property
    def row_count(self) -> int:
        if self.seats:
            return len({s.row_index for s in self.seats})
        return len(self.row_lengths)

    @property
    def has_shape(self) -> bool:
        """Room layout known, occupancy not. Estimable but not drawable."""
        return not self.seats and bool(self.row_lengths)

    def rows(self) -> list[list[Seat]]:
        """Seats grouped by row, front to back, each ordered house left to right."""
        buckets: dict[int, list[Seat]] = {}
        for seat in self.seats:
            buckets.setdefault(seat.row_index, []).append(seat)
        return [sorted(buckets[i], key=lambda s: s.col_index) for i in sorted(buckets)]

    def open_seats(self) -> list[Seat]:
        return [s for s in self.seats if s.is_open]


def normalize_geometry(seats: list[Seat]) -> tuple[Seat, ...]:
    """Assign x/y from row and column indices.

    Called by every source adapter that reports a grid but no coordinates,
    which is most of them. Lateral position is normalized **per row**, because
    rows differ in width - normalizing against the widest row would push
    short front rows off-centre when they are in fact centred.

    Seats that are structurally not seats are excluded from the extents so a
    wide aisle gap does not distort the centreline.
    """
    if not seats:
        return ()

    # Depth interpolates over the row *values*, not their ordinal position, so
    # cross-aisles survive. AMC's Lincoln Square IMAX skips rows 5, 11 and 12 -
    # those are walkways, and collapsing them would put row 6 closer to the
    # screen than it physically is.
    row_indices = sorted({s.row_index for s in seats})
    lo, hi = row_indices[0], row_indices[-1]
    span = max(hi - lo, 1)
    depth_of = {r: (r - lo) / span for r in row_indices}

    by_row: dict[int, list[Seat]] = {}
    for seat in seats:
        by_row.setdefault(seat.row_index, []).append(seat)

    out: list[Seat] = []
    for row_index, row_seats in by_row.items():
        bookable = [s for s in row_seats if s.kind.is_bookable] or row_seats
        lo = min(s.col_index for s in bookable)
        hi = max(s.col_index for s in bookable)
        width = max(hi - lo, 1)
        for seat in row_seats:
            centred = (seat.col_index - lo) / width      # 0..1
            out.append(
                Seat(
                    row_label=seat.row_label,
                    row_index=seat.row_index,
                    col_label=seat.col_label,
                    col_index=seat.col_index,
                    status=seat.status,
                    kind=seat.kind,
                    x=round(centred * 2 - 1, 4),
                    y=round(depth_of[row_index], 4),
                    aisle_adjacent=seat.aisle_adjacent,
                    module_id=seat.module_id,
                    module_position=seat.module_position,
                    module_size=seat.module_size,
                    module_required=seat.module_required,
                )
            )
    return tuple(sorted(out, key=lambda s: (s.row_index, s.col_index)))


def mark_aisles(seats: list[Seat], gap_threshold: int = 2) -> list[Seat]:
    """Flag seats next to an aisle, inferred from gaps in column numbering.

    Most sources do not mark aisles at all, but a jump in seat numbering is a
    reliable proxy, and aisle adjacency matters for both seat quality and for
    deciding whether two seats are really "together".
    """
    by_row: dict[int, list[Seat]] = {}
    for seat in seats:
        by_row.setdefault(seat.row_index, []).append(seat)

    flagged: list[Seat] = []
    for row_seats in by_row.values():
        ordered = sorted(row_seats, key=lambda s: s.col_index)
        aisle_cols: set[int] = set()
        for left, right in pairwise(ordered):
            if right.col_index - left.col_index >= gap_threshold:
                aisle_cols.update({left.col_index, right.col_index})
        if ordered:
            aisle_cols.update({ordered[0].col_index, ordered[-1].col_index})
        for seat in ordered:
            flagged.append(
                Seat(**{**seat.__dict__, "aisle_adjacent": seat.col_index in aisle_cols})
            )
    return flagged


def infer_modules(seats: list[Seat], *, size: int = 2) -> list[Seat]:
    """Annotate consecutive ungrouped loveseat seats as physical modules.

    Some feeds expose left/right explicitly and parsers should keep that.
    Others expose only ``loveseat`` for both halves; this conservative pass
    pairs consecutive seats within a row and never bridges a coordinate gap.
    Inference describes topology but does not invent a whole-module purchase
    rule (`module_required` remains false).
    """
    by_row: dict[int, list[Seat]] = {}
    for seat in seats:
        by_row.setdefault(seat.row_index, []).append(seat)
    out: list[Seat] = []
    for row_index, row in by_row.items():
        ordered = sorted(row, key=lambda seat: seat.col_index)
        i = 0
        while i < len(ordered):
            seat = ordered[i]
            if seat.module_id or seat.kind is not SeatKind.LOVESEAT:
                out.append(seat)
                i += 1
                continue
            block = [seat]
            j = i + 1
            while (
                j < len(ordered) and len(block) < size
                and ordered[j].kind is SeatKind.LOVESEAT
                and not ordered[j].module_id
                and ordered[j].col_index == block[-1].col_index + 1
            ):
                block.append(ordered[j])
                j += 1
            if len(block) == size:
                module_id = f"{row_index}:{block[0].col_index}-{block[-1].col_index}"
                out.extend(
                    Seat(**{**member.__dict__, "module_id": module_id,
                            "module_position": position, "module_size": size})
                    for position, member in enumerate(block)
                )
                i = j
            else:
                out.extend(block)
                i = j
    return sorted(out, key=lambda seat: (seat.row_index, seat.col_index))
