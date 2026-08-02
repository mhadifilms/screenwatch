"""Assembling a party's seats, and judging how good the arrangement is.

This is the part that makes the motivating scenario work. With four people at
a near-sellout, the question is never "is this showing available" - it is
"can we actually sit together, and if not, how bad is the compromise". Two
pairs stacked in adjacent rows is a genuinely different product from four
seats scattered across the room, and the ranker needs a number for that.

Cohesion values are deliberately coarse and hand-set rather than derived.
They encode a judgement about how people actually experience a split, and
pretending to compute them would be false precision.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

from .model import Auditorium, Seat, SeatKind
from .quality import QualityModel

MAX_GROUPS_PER_ROW = 6      # keep enumeration bounded in large houses
MAX_SPLIT_CANDIDATES = 40


class Cohesion(Enum):
    """How the party ends up distributed.

    Identities are strings, not scores. An earlier version used the score as
    the enum value, which silently made SOLO an *alias* of CONTIGUOUS - they
    both scored 1.00, so Python collapsed them into one member and every
    `is Cohesion.SOLO` check matched contiguous groups too. Two concepts that
    happen to score the same are still two concepts.
    """

    CONTIGUOUS = "contiguous"                  # N seats side by side
    ACROSS_AISLE = "across_aisle"              # side by side, aisle between
    STACKED = "stacked"                        # adjacent rows, columns overlap
    ADJACENT_ROWS = "adjacent_rows"            # adjacent rows, offset
    NEARBY_ROWS = "nearby_rows"                # within two rows
    SAME_ROW_SEPARATED = "same_row_separated"  # same row, apart
    SCATTERED = "scattered"
    SOLO = "solo"                              # party of one

    @property
    def score(self) -> float:
        return _COHESION_SCORES[self]

    @property
    def is_together(self) -> bool:
        return self in (Cohesion.CONTIGUOUS, Cohesion.ACROSS_AISLE, Cohesion.SOLO)


_COHESION_SCORES: dict["Cohesion", float] = {
    Cohesion.CONTIGUOUS: 1.00,
    Cohesion.SOLO: 1.00,
    Cohesion.ACROSS_AISLE: 0.85,
    Cohesion.STACKED: 0.78,
    Cohesion.ADJACENT_ROWS: 0.66,
    Cohesion.NEARBY_ROWS: 0.50,
    Cohesion.SAME_ROW_SEPARATED: 0.42,
    Cohesion.SCATTERED: 0.20,
}


@dataclass(frozen=True)
class SeatGroup:
    seats: tuple[Seat, ...]
    cohesion: Cohesion
    quality: float                      # mean per-seat quality, 0..1
    requested: int = 0
    parts: tuple[tuple[Seat, ...], ...] = field(default_factory=tuple)

    @property
    def size(self) -> int:
        return len(self.seats)

    @property
    def complete(self) -> bool:
        return self.size >= self.requested

    @property
    def cohesion_score(self) -> float:
        return self.cohesion.score

    @property
    def labels(self) -> str:
        by_row: dict[str, list[str]] = {}
        for seat in sorted(self.seats, key=lambda s: (s.row_index, s.col_index)):
            by_row.setdefault(seat.row_label, []).append(seat.col_label)
        return ", ".join(f"{row} {'+'.join(cols)}" for row, cols in by_row.items())

    def describe(self) -> str:
        if self.cohesion is Cohesion.SOLO:
            return f"1 seat at {self.labels}"
        if self.cohesion is Cohesion.CONTIGUOUS:
            return f"{self.size} together at {self.labels}"
        shape = "+".join(str(len(p)) for p in self.parts) if self.parts else str(self.size)
        return f"{self.size} as {shape} at {self.labels}"


def _runs(row: list[Seat], size: int) -> list[tuple[Seat, ...]]:
    """Every window of `size` genuinely adjacent open seats in one row.

    Two distinctions that both matter, and that an earlier version conflated:

    * An **occupied** seat between two free ones breaks adjacency. You are
      not sitting together if a stranger is between you. So blocks are cut at
      any seat that is not open.
    * A **missing** seat - an aisle, a wheelchair bay, a pillar - does not
      appear in the row at all, so it does not cut the block. Seats either
      side of an aisle really are next to each other, just with a walkway
      between; `_has_aisle_break` labels that case rather than rejecting it.
    """
    blocks: list[list[Seat]] = []
    current: list[Seat] = []
    for seat in row:                      # rows() already sorts by col_index
        if seat.is_open:
            current.append(seat)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    return [
        tuple(block[i : i + size])
        for block in blocks
        for i in range(len(block) - size + 1)
    ]


def _has_aisle_break(run: tuple[Seat, ...]) -> bool:
    return any(
        b.col_index - a.col_index > 1 for a, b in zip(run, run[1:])
    )


def _classify_split(parts: tuple[tuple[Seat, ...], ...]) -> Cohesion:
    rows = {s.row_index for part in parts for s in part}
    if len(rows) == 1:
        return Cohesion.SAME_ROW_SEPARATED

    spread = max(rows) - min(rows)
    if spread == 1:
        # Stacked means you can talk over your shoulder: the column ranges of
        # the two parts actually overlap.
        ranges = [
            range(min(s.col_index for s in p), max(s.col_index for s in p) + 1)
            for p in parts
        ]
        overlap = all(
            set(a) & set(b) for a, b in itertools.combinations(ranges, 2)
        )
        return Cohesion.STACKED if overlap else Cohesion.ADJACENT_ROWS
    if spread == 2:
        return Cohesion.NEARBY_ROWS
    return Cohesion.SCATTERED


def _mean_quality(seats: tuple[Seat, ...], model: QualityModel, rows: int) -> float:
    if not seats:
        return 0.0
    return round(sum(model.score(s, row_count=rows) for s in seats) / len(seats), 4)


def find_groups(
    auditorium: Auditorium,
    party_size: int,
    model: QualityModel | None = None,
    *,
    allow_split: bool = True,
    limit: int = 8,
) -> list[SeatGroup]:
    """Best available seatings for `party_size`, best first.

    Returns contiguous options when they exist. When they do not - the
    interesting case - it falls back to two-part splits, which is what real
    groups actually accept. Three-way splits are not enumerated: past two
    parts the arrangement is bad enough that ranking a later showing higher
    is the right answer anyway.

    An incomplete group (fewer seats than requested) is returned only if
    nothing else exists, so the caller can distinguish "sit apart" from
    "cannot fit at all".
    """
    model = model or QualityModel()
    rows = auditorium.rows()
    row_count = auditorium.row_count

    if party_size <= 0:
        return []

    if party_size == 1:
        singles = sorted(
            auditorium.open_seats(),
            key=lambda s: -model.score(s, row_count=row_count),
        )[:limit]
        return [
            SeatGroup((s,), Cohesion.SOLO, model.score(s, row_count=row_count), 1)
            for s in singles
        ]

    # ---- contiguous ---------------------------------------------------
    contiguous: list[SeatGroup] = []
    for row in rows:
        scored = [
            SeatGroup(
                run,
                Cohesion.ACROSS_AISLE if _has_aisle_break(run) else Cohesion.CONTIGUOUS,
                _mean_quality(run, model, row_count),
                party_size,
                (run,),
            )
            for run in _runs(row, party_size)
        ]
        scored.sort(key=lambda g: -(g.quality * g.cohesion_score))
        contiguous.extend(scored[:MAX_GROUPS_PER_ROW])

    contiguous.sort(key=lambda g: -(g.quality * g.cohesion_score))
    if contiguous:
        return contiguous[:limit]

    if not allow_split:
        return []

    # ---- two-part splits ----------------------------------------------
    # Balanced splits first: 4 -> 2+2 beats 3+1, because two pairs is a much
    # more acceptable arrangement than stranding someone alone.
    partitions = sorted(
        {tuple(sorted((k, party_size - k), reverse=True))
         for k in range(1, party_size)},
        key=lambda p: p[0] - p[1],
    )

    candidates: list[SeatGroup] = []
    for big, small in partitions:
        big_runs = [r for row in rows for r in _runs(row, big)][:MAX_SPLIT_CANDIDATES]
        small_runs = [r for row in rows for r in _runs(row, small)][:MAX_SPLIT_CANDIDATES]
        for a, b in itertools.product(big_runs, small_runs):
            if {s.id for s in a} & {s.id for s in b}:
                continue
            parts = (a, b)
            seats = tuple(sorted(a + b, key=lambda s: (s.row_index, s.col_index)))
            candidates.append(
                SeatGroup(
                    seats,
                    _classify_split(parts),
                    _mean_quality(seats, model, row_count),
                    party_size,
                    parts,
                )
            )
        if candidates:
            break  # a balanced split exists; do not bother with worse shapes

    if candidates:
        candidates.sort(key=lambda g: -(g.quality * g.cohesion_score))
        return candidates[:limit]

    # ---- cannot fit the party -----------------------------------------
    # Return the largest block that *does* exist rather than the N
    # best-scoring seats in the room. Three adjacent seats is a materially
    # different offer from three singles, and reporting the latter when the
    # former is true both misdescribes the room and understates the option.
    for size in range(party_size - 1, 0, -1):
        blocks = [r for row in rows for r in _runs(row, size)]
        if not blocks:
            continue
        best = max(
            blocks,
            key=lambda run: _mean_quality(run, model, row_count)
            * (Cohesion.ACROSS_AISLE if _has_aisle_break(run) else Cohesion.CONTIGUOUS).score,
        )
        cohesion = (
            Cohesion.SOLO if size == 1
            else Cohesion.ACROSS_AISLE if _has_aisle_break(best)
            else Cohesion.CONTIGUOUS
        )
        return [
            SeatGroup(best, cohesion, _mean_quality(best, model, row_count),
                      party_size, (best,))
        ]
    return []


def accessible_capacity(auditorium: Auditorium) -> tuple[int, int]:
    """(open wheelchair spaces, open companion seats)."""
    wheelchair = sum(
        1 for s in auditorium.seats if s.is_open and s.kind is SeatKind.WHEELCHAIR
    )
    companion = sum(
        1 for s in auditorium.seats if s.is_open and s.kind is SeatKind.COMPANION
    )
    return wheelchair, companion
