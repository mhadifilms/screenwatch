"""Group feasibility when you know how many seats are free but not which.

Per-seat occupancy turned out to be reachable on exactly one source. AMC
publishes a real grid; everywhere else the seat map sits behind the booking
flow, and getting it would mean creating a hold - a write against someone
else's ticketing system, which is the line this project does not cross.

That would leave phase B useful for one chain and inert for the rest, which
makes the ranking worthless precisely when it matters: a near-sellout.

So: when a source reports *how many* seats are free and the auditorium's
shape is known, the probability that a party can sit together is computable.
The estimate answers the question the user actually asks - "can the four of us
sit together?" - with a number instead of a shrug.

Model: seats sold are treated as uniformly distributed over bookable seats.
That is not true (people cluster, and centre seats go first), so the estimate
is deliberately *pessimistic* rather than merely approximate - see
`CLUSTERING_PENALTY`. Being told "probably not together" and finding a row of
four is a good surprise; the reverse is a ruined evening.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .groups import Cohesion

# Real bookings cluster: couples and families take adjacent seats, so the gaps
# left behind are more fragmented than a uniform scatter would leave. Shrinking
# the effective free-seat count biases every estimate toward "harder than it
# looks".
CLUSTERING_PENALTY = 0.85


@dataclass(frozen=True)
class Feasibility:
    """What can be said about seating a party from counts alone."""

    party_size: int
    available: int
    capacity: int
    rows: int
    together_probability: float          # 0..1, any single row
    can_fit_at_all: bool
    confidence: float = 0.5              # never as good as a real grid

    @property
    def occupancy(self) -> float:
        return 1.0 - (self.available / self.capacity) if self.capacity else 1.0

    @property
    def likely_cohesion(self) -> Cohesion:
        """The arrangement a party would most likely end up with."""
        if not self.can_fit_at_all:
            return Cohesion.SCATTERED
        if self.party_size == 1:
            return Cohesion.SOLO
        if self.together_probability >= 0.75:
            return Cohesion.CONTIGUOUS
        if self.together_probability >= 0.35:
            return Cohesion.STACKED          # a split is the realistic outcome
        return Cohesion.SAME_ROW_SEPARATED

    def describe(self) -> str:
        if not self.can_fit_at_all:
            return f"only {self.available} seats left — cannot fit {self.party_size}"
        pct = int(round(self.together_probability * 100))
        if self.party_size == 1:
            return f"{self.available} seats left"
        if pct >= 90:
            confidence = "almost certainly"
        elif pct >= 60:
            confidence = "probably"
        elif pct >= 30:
            confidence = "possibly"
        else:
            confidence = "unlikely"
        return (
            f"{self.available} of {self.capacity} free — {self.party_size} together "
            f"{confidence} available (~{pct}%)"
        )


def run_probability(row_length: int, free_in_row: float, party_size: int) -> float:
    """P(at least one run of `party_size` free seats in one row).

    Derived from the expected number of run start positions rather than exact
    combinatorics: with `n` seats of which `k` are free at density p = k/n,
    each of the `n - party + 1` windows is all-free with probability roughly
    p**party, and the expected count of such windows maps to a probability
    through 1 - exp(-E). Cheap, monotonic, and accurate enough to rank with.
    """
    if party_size <= 0 or row_length < party_size:
        return 0.0
    density = max(0.0, min(1.0, free_in_row / row_length))
    if density >= 1.0:
        return 1.0
    if density <= 0.0:
        return 0.0
    windows = row_length - party_size + 1
    expected = windows * (density ** party_size)
    return 1.0 - math.exp(-expected)


def estimate(
    *,
    party_size: int,
    available: int,
    capacity: int,
    rows: int,
    row_lengths: list[int] | None = None,
) -> Feasibility:
    """Probability a party can sit together, from counts plus room shape."""
    capacity = max(capacity, 0)
    available = max(min(available, capacity), 0)

    if capacity == 0 or rows <= 0:
        return Feasibility(party_size, available, capacity, rows, 0.0,
                           can_fit_at_all=available >= party_size, confidence=0.2)

    if available < party_size:
        return Feasibility(party_size, available, capacity, rows, 0.0, False)

    if party_size <= 1:
        return Feasibility(party_size, available, capacity, rows, 1.0, True)

    lengths = row_lengths or [max(capacity // rows, 1)] * rows
    effective_free = available * CLUSTERING_PENALTY
    density = effective_free / capacity

    # P(no row has a run) = product over rows of (1 - p_row).
    none = 1.0
    for length in lengths:
        none *= 1.0 - run_probability(length, length * density, party_size)

    return Feasibility(
        party_size=party_size,
        available=available,
        capacity=capacity,
        rows=rows,
        together_probability=round(1.0 - none, 4),
        can_fit_at_all=True,
    )


def seat_components(feasibility: Feasibility | None, party_size: int) -> dict[str, float]:
    """Phase-B component scores from an estimate rather than a real grid.

    Deliberately capped below what a confirmed grid can score: an option we
    have actually verified should outrank one we have only estimated, all else
    equal. A guess must never beat a fact.
    """
    if feasibility is None:
        return {"group_cohesion": 0.7, "seat_quality": 0.6, "party_fit": 0.6}

    if not feasibility.can_fit_at_all:
        return {"group_cohesion": 0.1, "seat_quality": 0.3, "party_fit": 0.1}

    together = feasibility.together_probability
    return {
        # Blend of certainty and the value of sitting together, capped at 0.95
        # so a confirmed contiguous block (1.0) always wins a tie.
        "group_cohesion": round(min(0.95, 0.35 + 0.6 * together), 4),
        # A near-empty room means free choice of seat; a full one does not.
        "seat_quality": round(0.35 + 0.45 * (1.0 - feasibility.occupancy), 4),
        "party_fit": round(min(0.95, 0.45 + 0.5 * together), 4),
    }
