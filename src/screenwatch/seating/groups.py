"""Assembling a party's seats, and judging how good the arrangement is.

This is the part that makes the motivating scenario work. With four people at
a near-sellout, the question is never "is this showing available" - it is
"can we actually sit together, and if not, how bad is the compromise". Two
pairs stacked in adjacent rows is a genuinely different product from four
seats scattered across the room, and the ranker needs a number for that.

The solver is multi-objective by design.  It protects the worst-served person,
maximizes Nash social welfare, preserves explicit relationships, remains
robust to uncertain room geometry and avoids destroying useful inventory for
the next customer.  No single hand-tuned weight is allowed to silently erase
one of those concerns: dominated arrangements are removed first, then a
maximin decision across several transparent preference profiles selects the
default and structurally diverse alternatives.
"""

from __future__ import annotations

import itertools
import math
import threading
from collections import OrderedDict, namedtuple
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum

from .model import Auditorium, Seat, SeatKind, SeatStatus
from .quality import QualityModel

MAX_GROUPS_PER_ROW = 6      # keep enumeration bounded in large houses
MAX_SPLIT_CANDIDATES = 40
MAX_EXACT_COMBINATIONS = 25_000
MAX_CACHED_OPTIMIZATIONS = 128

CacheInfo = namedtuple("CacheInfo", "hits misses maxsize currsize")
_GROUP_CACHE: OrderedDict[tuple, tuple[SeatGroup, ...]] = OrderedDict()
_GROUP_CACHE_LOCK = threading.RLock()
_GROUP_CACHE_HITS = 0
_GROUP_CACHE_MISSES = 0

# What an ordinary member of a party may be seated in.
#
# Wheelchair spaces and their paired companion seats are deliberately absent.
# They are allocated inventory, not general stock: assigning one to a party
# that did not ask for it takes the only seat someone else can use, and most
# ticketing flows reject the booking at checkout anyway. A party that *does*
# need them says so, and `SeatRequest` routes it down a separate path.
GENERAL_KINDS = frozenset({SeatKind.STANDARD, SeatKind.RECLINER, SeatKind.LOVESEAT})
RECLINING_KINDS = frozenset({SeatKind.RECLINER, SeatKind.LOVESEAT})


class PartyKind(Enum):
    GENERIC = "generic"
    DATE = "date"
    FRIENDS = "friends"
    COWORKERS = "coworkers"
    FAMILY = "family"


@dataclass(frozen=True)
class PartyBond:
    """A social edge between zero-based party-member indices."""

    a: int
    b: int
    weight: float = 1.0
    must_adjacent: bool = False


@dataclass(frozen=True)
class SeatRequest:
    """What a party needs, expressed in seating's own vocabulary.

    Deliberately *not* `ranking.spec.SeatingPrefs`: seating sits below ranking
    and must not import it. `ranking.fine.seat_request` translates one into the
    other, and that single function is where any new preference gets wired in -
    which is the whole reason this type exists rather than a pile of keyword
    arguments that a caller can forget to pass.

    `wheelchair_spaces` and `companion_seats` are drawn *from* the party, not
    added to it: a party of two with one wheelchair user needs one wheelchair
    space and one companion seat, which is two seats in total.
    """

    party_size: int
    together: bool = True
    allow_split: bool = True
    kinds: frozenset[SeatKind] = GENERAL_KINDS
    wheelchair_spaces: int = 0
    companion_seats: int = 0
    party_kind: PartyKind = PartyKind.GENERIC
    bonds: tuple[PartyBond, ...] = ()
    max_rows: int | None = None
    avoid_strangers: bool = True
    prefer_aisle: bool = False

    @property
    def accessible_seats(self) -> int:
        return self.wheelchair_spaces + self.companion_seats

    @property
    def general_seats(self) -> int:
        """Party members with no accessible requirement. Never negative."""
        return max(self.party_size - self.accessible_seats, 0)

    @property
    def needs_accessible_seating(self) -> bool:
        return self.accessible_seats > 0

    def admits(self, seat: Seat) -> bool:
        return seat.is_open and seat.kind in self.kinds


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


_COHESION_SCORES: dict[Cohesion, float] = {
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
    fairness: float = 0.0
    nash_welfare: float = 0.0
    social_score: float = 0.0
    stranger_score: float = 1.0
    robustness: float = 0.0
    inventory_score: float = 1.0
    assignment: tuple[int, ...] = ()  # member index for each seat in `seats`
    assignment_proven_optimal: bool = True
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    objective: tuple[float, ...] = ()
    robust_preference_score: float = 0.0
    pareto_optimal: bool = False
    certificate: OptimizationCertificate | None = None

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


@dataclass(frozen=True)
class OptimizationCertificate:
    """What the solver can truthfully prove about one recommendation.

    ``proven_optimal`` is intentionally strict.  It is true only when every
    feasible subset in the stated search space was evaluated.  Large houses
    use an anytime structured search and receive an admissible (occasionally
    loose) upper gap instead of an invented claim of optimality.
    """

    method: str
    proven_optimal: bool
    combinations_considered: int
    candidates_evaluated: int
    optimality_gap_upper_bound: float | None
    geometry_confidence: float
    scope: str


def _runs(
    row: list[Seat], size: int, admits: Callable[[Seat], bool] | None = None
) -> list[tuple[Seat, ...]]:
    """Every window of `size` genuinely adjacent usable seats in one row.

    Three distinctions, and an earlier version conflated the first two:

    * An **occupied** seat between two free ones breaks adjacency. You are
      not sitting together if a stranger is between you. So blocks are cut at
      any seat that is not open.
    * A **missing** seat - an aisle, a pillar - does not appear in the row at
      all, so it does not cut the block. Seats either side of an aisle really
      are next to each other, just with a walkway between; `_has_aisle_break`
      labels that case rather than rejecting it.
    * An **ineligible** seat - open, but not the kind this party may take -
      cuts the block like an occupied one. A wheelchair bay between two seats
      is a real physical gap, and calling the seats around it contiguous would
      overstate the offer.
    """
    admits = admits or (lambda seat: seat.is_open)
    blocks: list[list[Seat]] = []
    current: list[Seat] = []
    for seat in row:                      # rows() already sorts by col_index
        if admits(seat):
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
        b.col_index - a.col_index > 1 for a, b in itertools.pairwise(run)
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


def _relation(a: Seat, b: Seat) -> float:
    """How socially adjacent two physical positions are (0..1)."""
    if a.module_id and a.module_id == b.module_id:
        return 1.0
    if a.row_index == b.row_index:
        gap = abs(a.col_index - b.col_index)
        if gap == 1:
            return 1.0
        if gap <= 3 and a.aisle_adjacent and b.aisle_adjacent:
            return 0.68
        return max(0.0, 0.38 - 0.06 * gap)
    rows = abs(a.row_index - b.row_index)
    lateral = abs(a.x - b.x)
    if rows == 1 and lateral <= 0.18:
        return 0.82
    if rows == 1 and lateral <= 0.38:
        return 0.62
    if rows == 2 and lateral <= 0.25:
        return 0.42
    return max(0.0, 0.22 - 0.05 * rows - 0.15 * lateral)


def _effective_bonds(request: SeatRequest) -> tuple[PartyBond, ...]:
    if request.bonds:
        return tuple(
            bond for bond in request.bonds
            if 0 <= bond.a < request.party_size and 0 <= bond.b < request.party_size
            and bond.a != bond.b and bond.weight > 0
        )
    n = request.party_size
    if n < 2 or not request.together:
        return ()
    weight = {
        PartyKind.DATE: 1.0,
        PartyKind.FAMILY: 0.85,
        PartyKind.FRIENDS: 0.65,
        PartyKind.COWORKERS: 0.35,
        PartyKind.GENERIC: 0.55,
    }[request.party_kind]
    return tuple(PartyBond(a, b, weight,
                           n == 2 and request.together)
                 for a in range(n) for b in range(a + 1, n))


def _social_influence(request: SeatRequest) -> float:
    if not _effective_bonds(request):
        return 0.0
    return {
        PartyKind.DATE: 0.38,
        PartyKind.FAMILY: 0.32,
        PartyKind.FRIENDS: 0.25,
        PartyKind.COWORKERS: 0.14,
        PartyKind.GENERIC: 0.22,
    }[request.party_kind]


def _module_valid(seats: tuple[Seat, ...]) -> bool:
    chosen = {seat.id for seat in seats}
    modules: dict[str, list[Seat]] = {}
    for seat in seats:
        if seat.module_id:
            modules.setdefault(seat.module_id, []).append(seat)
    return all(
        not members[0].module_required
        or len(members) == (members[0].module_size or len(members))
        for members in modules.values()
    ) and len(chosen) == len(seats)


def _parts_for(seats: tuple[Seat, ...]) -> tuple[tuple[Seat, ...], ...]:
    """Connected components under strong physical/social adjacency."""
    remaining = set(range(len(seats)))
    parts: list[tuple[Seat, ...]] = []
    while remaining:
        todo = [remaining.pop()]
        component: set[int] = set(todo)
        while todo:
            i = todo.pop()
            neighbors = {
                j for j in remaining
                if _relation(seats[i], seats[j]) >= 0.68
                and (
                    seats[i].row_index == seats[j].row_index
                    or (seats[i].module_id and seats[i].module_id == seats[j].module_id)
                )
            }
            remaining -= neighbors
            component |= neighbors
            todo.extend(neighbors)
        parts.append(tuple(seats[i] for i in sorted(component)))
    return tuple(sorted(parts, key=lambda part: (part[0].row_index, part[0].col_index)))


def _assign(
    seats: tuple[Seat, ...], request: SeatRequest, qualities: tuple[float, ...]
) -> tuple[tuple[int, ...], float, float, float, bool, bool]:
    """Assign people to positions; exact for small parties, local search after."""
    n = len(seats)
    bonds = _effective_bonds(request)
    if n != request.party_size or not bonds:
        fairness = min(qualities, default=0.0)
        nash = _geometric_mean(qualities)
        return tuple(range(n)), fairness, nash, 1.0 if n else 0.0, True, True

    influence = _social_influence(request)

    # With the default complete, equal-weight relationship graph every person
    # is interchangeable. Solving a quadratic assignment cannot change the
    # answer; compute the exact symmetric result directly. This is the common
    # large-party path and keeps 15/20-person searches interactive.
    if (not request.bonds and request.party_kind is not PartyKind.DATE
            and not any(bond.must_adjacent for bond in bonds)):
        connections = [
            sum(_relation(seat, other) for j, other in enumerate(seats) if i != j)
            / max(n - 1, 1)
            for i, seat in enumerate(seats)
        ]
        utilities = [
            (1.0 - influence) * qualities[i] + influence * connections[i]
            for i in range(n)
        ]
        social = sum(connections) / n
        return (
            tuple(range(n)), round(min(utilities), 4),
            round(_geometric_mean(utilities), 4), round(social, 4), True, True,
        )

    def evaluate(
        order: tuple[int, ...],
    ) -> tuple[tuple[float, ...], float, float, float, bool]:
        position = {person: i for i, person in enumerate(order)}
        totals = [0.0] * n
        weights = [0.0] * n
        preserved = 0.0
        possible = 0.0
        mandatory = True
        for bond in bonds:
            relation = _relation(seats[position[bond.a]], seats[position[bond.b]])
            possible += bond.weight
            preserved += bond.weight * relation
            totals[bond.a] += bond.weight * relation
            totals[bond.b] += bond.weight * relation
            weights[bond.a] += bond.weight
            weights[bond.b] += bond.weight
            if bond.must_adjacent and relation < 0.95:
                mandatory = False
        connection = [totals[i] / weights[i] if weights[i] else 1.0 for i in range(n)]
        utilities = [
            (1.0 - influence) * qualities[position[p]] + influence * connection[p]
            for p in range(n)
        ]
        fairness = min(utilities)
        nash = _geometric_mean(utilities)
        social = preserved / possible if possible else 1.0
        return (
            (float(mandatory), round(fairness, 5), round(nash, 5), round(social, 5)),
            fairness, nash, social, mandatory,
        )

    seeds: list[tuple[int, ...]] = [tuple(range(n)), tuple(reversed(range(n)))]
    degree = [sum(b.weight for b in bonds if p in (b.a, b.b)) for p in range(n)]
    seat_centrality = [sum(_relation(a, b) for b in seats) for a in seats]
    people = sorted(range(n), key=lambda p: -degree[p])
    positions = sorted(range(n), key=lambda i: -seat_centrality[i])
    greedy = [0] * n
    for person, position in zip(people, positions, strict=True):
        greedy[position] = person
    seeds.append(tuple(greedy))
    if n <= 6:
        candidates = itertools.permutations(range(n))
    else:
        candidates = seeds
    best = max((tuple(order) for order in candidates), key=lambda order: evaluate(order)[0])
    # Deterministic pair-swap hill climb handles larger relationship graphs.
    improved = True
    while improved:
        improved = False
        baseline = evaluate(best)[0]
        for i in range(n):
            for j in range(i + 1, n):
                trial = list(best)
                trial[i], trial[j] = trial[j], trial[i]
                trial_tuple = tuple(trial)
                score = evaluate(trial_tuple)[0]
                if score > baseline:
                    best, baseline, improved = trial_tuple, score, True
    _, fairness, nash, social, mandatory = evaluate(best)
    return (
        best, round(fairness, 4), round(nash, 4), round(social, 4), mandatory,
        n <= 6,
    )


def _geometric_mean(values: tuple[float, ...] | list[float]) -> float:
    """Nash welfare with the mathematically correct zero boundary."""
    if not values or any(value <= 0.0 for value in values):
        return 0.0
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _stranger_score(auditorium: Auditorium, seats: tuple[Seat, ...], enabled: bool) -> float:
    if not enabled or not seats:
        return 1.0
    selected = {seat.id for seat in seats}
    occupied = [
        seat for seat in auditorium.seats
        if seat.status in (SeatStatus.SOLD, SeatStatus.HELD) and seat.kind.is_bookable
    ]
    contacts = sum(
        1 for seat in seats for other in occupied
        if other.id not in selected and _relation(seat, other) >= 0.95
    )
    # In a busy prime zone adjacency is normal; in an empty room it is avoidable.
    pressure = max(0.15, 1.0 - auditorium.occupancy)
    return round(math.exp(-0.55 * contacts * pressure / len(seats)), 4)


def _inventory_score(auditorium: Auditorium, seats: tuple[Seat, ...]) -> float:
    selected = {seat.id for seat in seats}
    orphans = 0
    for row in auditorium.rows():
        free = [seat.is_open and seat.id not in selected for seat in row]
        for i, value in enumerate(free):
            if value and (i == 0 or not free[i - 1]) and (i == len(free) - 1 or not free[i + 1]):
                orphans += 1
    return round(1.0 / (1.0 + orphans), 4)


def _build_group(
    auditorium: Auditorium, seats: tuple[Seat, ...], request: SeatRequest,
    model: QualityModel,
) -> SeatGroup | None:
    seats = tuple(sorted(seats, key=lambda seat: (seat.row_index, seat.col_index)))
    if not _module_valid(seats):
        return None
    parts = _parts_for(seats)
    qualities = tuple(model.score(seat, row_count=auditorium.row_count) for seat in seats)
    intervals = tuple(
        model.score_interval(
            seat,
            row_count=auditorium.row_count,
            geometry_confidence=auditorium.geometry_confidence,
        )
        for seat in seats
    )
    conservative_qualities = tuple(low for low, _ in intervals)
    assignment, fairness, nash, social, mandatory, assignment_exact = _assign(
        seats, request, conservative_qualities
    )
    if not mandatory:
        return None
    seat_rows = {seat.row_index for seat in seats}
    if len(seats) == 1:
        cohesion = Cohesion.SOLO
    elif len(seat_rows) == 1 and len(parts) == 1:
        cohesion = Cohesion.ACROSS_AISLE if _has_aisle_break(seats) else Cohesion.CONTIGUOUS
    elif len(seat_rows) == 1:
        cohesion = Cohesion.SAME_ROW_SEPARATED
    else:
        spread = max(seat_rows) - min(seat_rows)
        overlap = max(seat.x for seat in seats) - min(seat.x for seat in seats) <= 0.6
        consecutive = sorted(seat_rows) == list(range(min(seat_rows), max(seat_rows) + 1))
        cohesion = (
            Cohesion.STACKED if spread == 1 and overlap
            else Cohesion.ADJACENT_ROWS if consecutive
            else Cohesion.NEARBY_ROWS if spread == 2
            else Cohesion.SCATTERED
        )
    stranger = _stranger_score(auditorium, seats, request.avoid_strangers)
    quality = round(sum(qualities) / len(qualities), 4) if qualities else 0.0
    robustness = round(min(conservative_qualities, default=0.0), 4)
    inventory = _inventory_score(auditorium, seats)
    touched_modules: dict[str, tuple[int, int]] = {}
    for seat in seats:
        if seat.module_id:
            chosen, size = touched_modules.get(seat.module_id, (0, seat.module_size or 1))
            touched_modules[seat.module_id] = (chosen + 1, size)
    module_score = (
        sum(chosen / size for chosen, size in touched_modules.values()) / len(touched_modules)
        if touched_modules else 1.0
    )
    complete = len(seats) == request.party_size
    row_span = len({seat.row_index for seat in seats})
    split_score = 1.0 / (1.0 + 0.22 * (len(parts) - 1) + 0.08 * (row_span - 1))
    objective = (
        float(complete), round(module_score, 3),
        math.floor(fairness * 10 + 1e-9) / 10,
        round(social, 3), round(split_score, 3), round(fairness, 3),
        round(nash, 3),
        round(quality, 3), stranger,
        robustness, inventory,
    )
    reasons = (
        f"guarantees the worst-served person at least {fairness:.0%}",
        f"delivers {nash:.0%} Nash social welfare",
        f"preserves {social:.0%} of party proximity",
        f"uses {len(parts)} compact block{'s' if len(parts) != 1 else ''} "
        f"across {row_span} row{'s' if row_span != 1 else ''}",
    )
    warnings = () if complete else (
        f"only {len(seats)} of {request.party_size} people can be seated",
    )
    return SeatGroup(
        seats=seats,
        cohesion=cohesion,
        quality=quality,
        requested=request.party_size,
        parts=parts,
        fairness=fairness,
        nash_welfare=nash,
        social_score=social,
        stranger_score=stranger,
        robustness=robustness,
        inventory_score=inventory,
        assignment=assignment,
        assignment_proven_optimal=assignment_exact,
        reasons=reasons,
        warnings=warnings,
        objective=objective,
    )


def _metric_vector(group: SeatGroup, *, social_topology: bool = True) -> tuple[float, ...]:
    """The dimensions on which Pareto dominance is allowed."""
    fulfillment = min(group.size / max(group.requested, 1), 1.0)
    module_integrity = group.objective[1] if len(group.objective) > 1 else 1.0
    compactness = group.objective[4] if len(group.objective) > 4 else group.cohesion_score
    connected_coverage = sum(
        len(part) for part in group.parts if len(part) > 1
    ) / max(group.size, 1)
    social_metrics = (
        connected_coverage,
        group.social_score,
        compactness,
    ) if social_topology else ()
    return (
        fulfillment,
        module_integrity,
        group.fairness,
        group.nash_welfare,
        *social_metrics,
        group.robustness,
        group.quality,
        group.stranger_score,
        group.inventory_score,
    )


def _preference_profiles(
    group: SeatGroup, *, social_topology: bool = True
) -> tuple[float, ...]:
    """Utilities under distinct, plausible ways a party may value seats.

    The default is their maximin compromise.  This is much less brittle than
    asserting that one universal set of weights describes a date, a family,
    coworkers and a film enthusiast equally well.
    """
    compactness = group.objective[4] if len(group.objective) > 4 else group.cohesion_score
    if not social_topology:
        return (
            0.45 * group.robustness + 0.30 * group.quality
            + 0.15 * group.fairness + 0.10 * group.nash_welfare,
            0.42 * group.fairness + 0.38 * group.nash_welfare
            + 0.20 * group.robustness,
            0.36 * group.robustness + 0.24 * group.fairness
            + 0.20 * group.stranger_score + 0.20 * group.inventory_score,
        )
    return (
        # Sightline-first, while refusing to sacrifice the worst person.
        0.42 * group.robustness + 0.25 * group.quality
        + 0.20 * group.fairness + 0.13 * compactness,
        # Conversation/connection-first.
        0.38 * group.social_score + 0.24 * compactness
        + 0.20 * group.fairness + 0.18 * group.nash_welfare,
        # Egalitarian: Rawlsian floor plus Nash efficiency.
        0.45 * group.fairness + 0.34 * group.nash_welfare
        + 0.12 * group.social_score + 0.09 * group.robustness,
        # Low-regret operational choice in a crowded room.
        0.24 * group.robustness + 0.21 * group.fairness
        + 0.20 * compactness + 0.19 * group.stranger_score
        + 0.16 * group.inventory_score,
    )


def _robust_utility_upper_bound(
    auditorium: Auditorium,
    request: SeatRequest,
    model: QualityModel,
) -> float:
    """Admissible room-specific bound for the robust preference objective.

    Each component is allowed its own best-case arrangement, so the bound can
    be optimistic but never excludes the true optimum.  It is substantially
    tighter than the universal bound of one in ordinary rooms and makes the
    anytime certificate useful rather than merely technically valid.
    """
    eligible = [seat for seat in auditorium.open_seats() if request.admits(seat)]
    target = min(request.party_size, len(eligible))
    if target <= 0:
        return 0.0
    point = sorted(
        (model.score(seat, row_count=auditorium.row_count) for seat in eligible),
        reverse=True,
    )[:target]
    lower = sorted(
        (
            model.score_interval(
                seat,
                row_count=auditorium.row_count,
                geometry_confidence=auditorium.geometry_confidence,
            )[0]
            for seat in eligible
        ),
        reverse=True,
    )[:target]
    influence = _social_influence(request)
    utility_upper = [
        (1.0 - influence) * quality + influence for quality in lower
    ]
    fairness = min(utility_upper, default=0.0)
    nash = _geometric_mean(utility_upper)
    robustness = min(lower, default=0.0)
    quality = sum(point) / len(point)

    if not request.together:
        profile_bounds = (
            0.45 * robustness + 0.30 * quality
            + 0.15 * fairness + 0.10 * nash,
            0.42 * fairness + 0.38 * nash + 0.20 * robustness,
            0.36 * robustness + 0.24 * fairness + 0.40,
        )
    else:
        # Social continuity, compactness, stranger comfort and inventory can
        # each be at most one. Allowing all of them to reach one independently
        # keeps the bound admissible even when no one arrangement can do so.
        profile_bounds = (
            0.42 * robustness + 0.25 * quality + 0.20 * fairness + 0.13,
            0.62 + 0.20 * fairness + 0.18 * nash,
            0.45 * fairness + 0.34 * nash + 0.09 * robustness + 0.12,
            0.55 + 0.24 * robustness + 0.21 * fairness,
        )
    return round(min(1.0, min(profile_bounds)), 6)


def _pareto_frontier_ids(
    groups: list[SeatGroup], *, social_topology: bool = True
) -> set[tuple[str, ...]]:
    """Incremental skyline; avoids quadratic work against all candidates."""
    frontier: list[tuple[tuple[str, ...], tuple[float, ...]]] = []
    eps = 1e-9
    for group in groups:
        identifier = tuple(sorted(seat.id for seat in group.seats))
        vector = _metric_vector(group, social_topology=social_topology)
        if any(
            all(left >= right - eps for left, right in zip(other, vector, strict=True))
            and any(left > right + eps for left, right in zip(other, vector, strict=True))
            for _, other in frontier
        ):
            continue
        frontier = [
            (other_id, other)
            for other_id, other in frontier
            if not (
                all(left >= right - eps for left, right in zip(vector, other, strict=True))
                and any(left > right + eps for left, right in zip(vector, other, strict=True))
            )
        ]
        frontier.append((identifier, vector))
    return {identifier for identifier, _ in frontier}


def _arrangement_distance(a: SeatGroup, b: SeatGroup) -> float:
    """0..1 structural distance used to prevent cosmetic top-k results."""
    left = {seat.id for seat in a.seats}
    right = {seat.id for seat in b.seats}
    jaccard = 1.0 - len(left & right) / max(len(left | right), 1)
    a_rows = {seat.row_index for seat in a.seats}
    b_rows = {seat.row_index for seat in b.seats}
    row_distance = 1.0 - len(a_rows & b_rows) / max(len(a_rows | b_rows), 1)
    shape_distance = min(abs(len(a.parts) - len(b.parts)) / 3.0, 1.0)
    return 0.65 * jaccard + 0.23 * row_distance + 0.12 * shape_distance


def _rank_groups(
    groups: list[SeatGroup], limit: int, *, social_topology: bool = True
) -> list[SeatGroup]:
    """Pareto-prune, robustly rank, then choose genuinely different options."""
    if not groups:
        return []
    unique = {
        tuple(sorted(seat.id for seat in group.seats)): group for group in groups
    }
    groups = list(unique.values())
    frontier_ids = _pareto_frontier_ids(groups, social_topology=social_topology)
    ranked: list[SeatGroup] = []
    for group in groups:
        identifier = tuple(sorted(seat.id for seat in group.seats))
        robust = round(min(_preference_profiles(
            group, social_topology=social_topology
        )), 6)
        fulfillment = round(min(group.size / max(group.requested, 1), 1.0), 6)
        module_integrity = group.objective[1] if len(group.objective) > 1 else 1.0
        compactness = group.objective[4] if len(group.objective) > 4 else group.cohesion_score
        connected_coverage = sum(
            len(part) for part in group.parts if len(part) > 1
        ) / max(group.size, 1)
        objective = (
            fulfillment,
            round(module_integrity, 4),
            round(connected_coverage, 4) if social_topology else 1.0,
            float(identifier in frontier_ids),
            robust,
            round(group.fairness, 4),
            round(group.nash_welfare, 4),
            round(group.social_score, 4),
            round(compactness, 4),
            round(group.robustness, 4),
            round(group.quality, 4),
            round(group.stranger_score, 4),
            round(group.inventory_score, 4),
        )
        ranked.append(replace(
            group,
            robust_preference_score=robust,
            pareto_optimal=identifier in frontier_ids,
            objective=objective,
        ))
    ranked.sort(key=lambda group: group.objective, reverse=True)
    if limit <= 1:
        return ranked[:limit]

    # The first answer is the lexicographic product optimum. Subsequent answers
    # trade no more than 12% of its robust utility unless that leaves no choice,
    # then greedily maximize both merit and structural novelty.
    selected = [ranked[0]]
    threshold = ranked[0].robust_preference_score * 0.88
    pool = [
        group for group in ranked[1:]
        if group.pareto_optimal and group.robust_preference_score >= threshold
    ] or ranked[1:]
    while pool and len(selected) < limit:
        choice = max(
            pool,
            key=lambda group: (
                0.72 * group.robust_preference_score
                + 0.28 * min(_arrangement_distance(group, prior) for prior in selected),
                group.objective,
            ),
        )
        selected.append(choice)
        pool.remove(choice)
    if len(selected) < limit:
        selected_ids = {tuple(seat.id for seat in group.seats) for group in selected}
        selected.extend(
            group for group in ranked
            if tuple(seat.id for seat in group.seats) not in selected_ids
        )
    return selected[:limit]


def _exact_groups(
    auditorium: Auditorium,
    request: SeatRequest,
    model: QualityModel,
) -> tuple[list[SeatGroup], int] | None:
    """Enumerate the complete feasible subset space when it is tractable."""
    eligible = [seat for seat in auditorium.open_seats() if request.admits(seat)]
    target = min(request.party_size, len(eligible))
    if target <= 0:
        return [], 0
    combinations = math.comb(len(eligible), target)
    if combinations > MAX_EXACT_COMBINATIONS:
        return None
    evaluated = 0
    groups: list[SeatGroup] = []
    while target > 0 and not groups:
        combinations = math.comb(len(eligible), target)
        if combinations > MAX_EXACT_COMBINATIONS:
            return None
        for seats in itertools.combinations(eligible, target):
            evaluated += 1
            rows = {seat.row_index for seat in seats}
            if request.max_rows is not None and len(rows) > request.max_rows:
                continue
            group = _build_group(auditorium, seats, request, model)
            if group is None:
                continue
            if not request.allow_split and not group.cohesion.is_together:
                continue
            groups.append(group)
        if not request.allow_split:
            break
        target -= 1
    return groups, evaluated


def _search_groups(
    auditorium: Auditorium, request: SeatRequest, model: QualityModel, limit: int
) -> list[SeatGroup]:
    eligible = [seat for seat in auditorium.open_seats() if request.admits(seat)]
    if not eligible:
        return []
    n = request.party_size
    if not request.together:
        chosen = tuple(sorted(eligible, key=lambda seat: -model.score(
            seat, row_count=auditorium.row_count))[:n])
        group = _build_group(auditorium, chosen, request, model)
        return [group] if group else []

    # Candidate row-runs of every useful size. Keeping several per row/size
    # preserves alternative centers while bounding a 300-seat auditorium.
    pool: list[tuple[Seat, ...]] = []
    for row in auditorium.rows():
        for size in range(1, min(n, len(row)) + 1):
            runs = _runs(row, size, request.admits)
            runs.sort(key=lambda run: (-_mean_quality(run, model, auditorium.row_count),
                                       abs(sum(s.x for s in run) / len(run))))
            pool.extend(run for run in runs[:4] if _module_valid(run))
    # Keep the strongest options per size globally and canonicalize.
    trimmed: list[tuple[Seat, ...]] = []
    for size in range(1, n + 1):
        same = [run for run in pool if len(run) == size]
        same.sort(key=lambda run: -_mean_quality(run, model, auditorium.row_count))
        trimmed.extend(same[:20])
    pool = sorted(trimmed, key=lambda run: (run[0].row_index, run[0].col_index, len(run)))

    # Large or pod-based rooms can genuinely require five/six components
    # (e.g. five four-seat 4DX platforms for twenty people), while an open
    # conventional room should not pay that search cost.
    longest = max((len(run) for run in pool), default=1)
    needed = math.ceil(n / longest)
    max_parts = min(max(4, needed), 6 if n > 10 else 4, n)
    max_rows = request.max_rows or (2 if n <= 3 else 3 if n <= 10 else 4)
    states: list[tuple[tuple[Seat, ...], ...]] = [()]
    exact: dict[tuple[str, ...], SeatGroup] = {}
    partial_parts: dict[int, list[tuple[tuple[Seat, ...], ...]]] = {}
    for _ in range(max_parts):
        buckets: dict[int, list[tuple[tuple[Seat, ...], ...]]] = {}
        for parts in states:
            used = {seat.id for part in parts for seat in part}
            last_key = (
                (parts[-1][0].row_index, parts[-1][0].col_index, len(parts[-1]))
                if parts else None
            )
            for run in pool:
                key = (run[0].row_index, run[0].col_index, len(run))
                if last_key is not None and key <= last_key:
                    continue
                if used & {seat.id for seat in run}:
                    continue
                total = sum(len(part) for part in parts) + len(run)
                if total > n:
                    continue
                rows = {seat.row_index for part in (*parts, run) for seat in part}
                if len(rows) > max_rows or (rows and max(rows) - min(rows) > max_rows + 1):
                    continue
                proposal = (*parts, run)
                buckets.setdefault(total, []).append(proposal)
        states = []
        for total, proposals in buckets.items():
            def preliminary(parts):
                seats = tuple(seat for part in parts for seat in part)
                q = _mean_quality(seats, model, auditorium.row_count)
                row_span = max(s.row_index for s in seats) - min(s.row_index for s in seats)
                balance = max(map(len, parts)) - min(map(len, parts))
                return q - 0.025 * row_span - 0.015 * balance - 0.02 * (len(parts) - 1)
            proposals.sort(key=preliminary, reverse=True)
            states.extend(proposals[:40])
            partial_parts[total] = proposals[:60]
            if total != n:
                continue
            for parts in proposals[:60]:
                seats = tuple(seat for part in parts for seat in part)
                group = _build_group(auditorium, seats, request, model)
                if not group:
                    continue
                ids = tuple(sorted(seat.id for seat in seats))
                exact[ids] = group
    groups = list(exact.values())
    if not request.allow_split:
        groups = [group for group in groups if group.cohesion.is_together]
        if not groups:
            return []
    if not groups and partial_parts:
        largest = max(partial_parts)
        for parts in partial_parts[largest]:
            seats = tuple(seat for part in parts for seat in part)
            if group := _build_group(auditorium, seats, request, model):
                groups.append(group)
    return groups


def _certify(
    groups: list[SeatGroup],
    auditorium: Auditorium,
    *,
    method: str,
    proven_optimal: bool,
    combinations_considered: int,
    candidates_evaluated: int,
    scope: str,
    robust_utility_upper_bound: float = 1.0,
) -> list[SeatGroup]:
    certified: list[SeatGroup] = []
    for index, group in enumerate(groups):
        globally_proven = (
            proven_optimal and index == 0 and group.assignment_proven_optimal
        )
        # The upper bound independently optimizes each profile component. It
        # may be optimistic but cannot fall below the true robust optimum.
        gap = 0.0 if globally_proven else round(
            max(0.0, robust_utility_upper_bound - group.robust_preference_score), 6
        )
        certificate = OptimizationCertificate(
            method=method,
            proven_optimal=globally_proven,
            combinations_considered=combinations_considered,
            candidates_evaluated=candidates_evaluated,
            optimality_gap_upper_bound=gap,
            geometry_confidence=round(max(0.0, min(1.0, auditorium.geometry_confidence)), 4),
            scope=(
                scope
                if group.assignment_proven_optimal
                else f"{scope}; custom people-to-seat assignment is locally optimized"
            ),
        )
        certified.append(replace(group, certificate=certificate))
    return certified


def find_groups(
    auditorium: Auditorium,
    party_size: int | SeatRequest,
    model: QualityModel | None = None,
    *,
    allow_split: bool = True,
    limit: int = 8,
) -> list[SeatGroup]:
    """Best available seatings for the party, best first.

    Searches contiguous and compact multi-row/multi-module shapes together;
    a long row does not win merely because it was discovered first. Candidate
    enumeration is bounded, then ranked by completeness, physical modules,
    worst-person fairness, social continuity, compactness, and seat quality.

    An incomplete group (fewer seats than requested) is returned only if
    nothing else exists, so the caller can distinguish "sit apart" from
    "cannot fit at all".

    A bare `party_size` is still accepted and means the default request: any
    general seat, seated together if possible.
    """
    request = (
        party_size if isinstance(party_size, SeatRequest)
        else SeatRequest(party_size, allow_split=allow_split)
    )
    # Seat maps have their own geometry. Adapt the generic middle-area prior
    # to this room before comparing groups, so the same recommendation logic
    # works for a sparse art-house layout and a large multi-aisle IMAX.
    model = (model or QualityModel()).for_auditorium(auditorium)
    if request.party_size <= 0 or limit <= 0:
        return []

    # The optimizer is pure with respect to these inputs. Seat maps are often
    # revisited by search results, watches, API rendering and explanations;
    # repeating a 20-person solve adds seconds without changing the answer.
    # A bounded process-local cache retains only immutable inputs/results and
    # naturally misses as soon as availability or preferences change.
    key = (
        auditorium.seats,
        round(auditorium.geometry_confidence, 6),
        request,
        model,
        limit,
    )
    global _GROUP_CACHE_HITS, _GROUP_CACHE_MISSES
    with _GROUP_CACHE_LOCK:
        cached = _GROUP_CACHE.get(key)
        if cached is not None:
            _GROUP_CACHE_HITS += 1
            _GROUP_CACHE.move_to_end(key)
            return list(cached)
        _GROUP_CACHE_MISSES += 1

    groups = _find_groups_uncached(auditorium, request, model, limit)
    with _GROUP_CACHE_LOCK:
        _GROUP_CACHE[key] = tuple(groups)
        _GROUP_CACHE.move_to_end(key)
        while len(_GROUP_CACHE) > MAX_CACHED_OPTIMIZATIONS:
            _GROUP_CACHE.popitem(last=False)
    return groups


def seating_cache_info() -> CacheInfo:
    """Small observability surface mirroring ``functools.lru_cache``."""
    with _GROUP_CACHE_LOCK:
        return CacheInfo(
            _GROUP_CACHE_HITS,
            _GROUP_CACHE_MISSES,
            MAX_CACHED_OPTIMIZATIONS,
            len(_GROUP_CACHE),
        )


def clear_seating_cache() -> None:
    """Clear cached optimizations; primarily useful to benchmarks and tests."""
    global _GROUP_CACHE_HITS, _GROUP_CACHE_MISSES
    with _GROUP_CACHE_LOCK:
        _GROUP_CACHE.clear()
        _GROUP_CACHE_HITS = 0
        _GROUP_CACHE_MISSES = 0


def _find_groups_uncached(
    auditorium: Auditorium,
    request: SeatRequest,
    model: QualityModel,
    limit: int,
) -> list[SeatGroup]:

    if request.needs_accessible_seating:
        # Accessible allocation is its own problem, not a filter over the
        # general one: the seats are specific, paired, and scarce.
        raw = _accessible_groups(auditorium, request, model, limit=limit)
        ranked = _rank_groups(raw, limit, social_topology=request.together)
        return _certify(
            ranked,
            auditorium,
            method="joint-accessible-allocation",
            proven_optimal=False,
            combinations_considered=0,
            candidates_evaluated=len(raw),
            scope="jointly selected accessible inventory, then nearest general seats",
        )

    exact = _exact_groups(auditorium, request, model)
    if exact is not None:
        raw, evaluated = exact
        ranked = _rank_groups(
            raw,
            1 if not request.together else limit,
            social_topology=request.together,
        )
        return _certify(
            ranked,
            auditorium,
            method="exhaustive-global-enumeration",
            proven_optimal=True,
            combinations_considered=evaluated,
            candidates_evaluated=len(raw),
            scope="every feasible subset satisfying the request's hard constraints",
            robust_utility_upper_bound=_robust_utility_upper_bound(
                auditorium, request, model
            ),
        )

    raw = _search_groups(auditorium, request, model, limit)
    ranked = _rank_groups(raw, limit, social_topology=request.together)
    return _certify(
        ranked,
        auditorium,
        method="anytime-structured-search",
        proven_optimal=False,
        combinations_considered=0,
        candidates_evaluated=len(raw),
        scope="bounded row-run and multi-row candidate search; gap bounds robust utility",
        robust_utility_upper_bound=_robust_utility_upper_bound(
            auditorium, request, model
        ),
    )


def _accessible_groups(
    auditorium: Auditorium,
    request: SeatRequest,
    model: QualityModel,
    *,
    limit: int = 8,
) -> list[SeatGroup]:
    """Seat a party that needs wheelchair spaces or companion seats.

    Not a filter over the general search, because the constraint is not "which
    seats are acceptable" but "which specific seats exist". A house has a
    handful of wheelchair spaces, each with a companion seat beside it, and
    either they are free or the party cannot be seated. Returning the best
    standard seats in that case would be a lie of the most consequential kind.

    So: take the accessible seats first, then place the rest of the party as
    close to them as the room allows. Proximity is the whole point - a
    companion seated fifteen rows away is not a companion.
    """
    row_count = auditorium.row_count

    space_pool = [
        seat for seat in auditorium.open_seats() if seat.kind is SeatKind.WHEELCHAIR
    ]
    companion_pool = [
        seat for seat in auditorium.open_seats() if seat.kind is SeatKind.COMPANION
    ]
    if len(space_pool) < request.wheelchair_spaces:
        return []                       # the room cannot seat this party
    if len(companion_pool) < request.companion_seats:
        return []

    # Jointly choose spaces and companions. Picking each kind independently
    # can select two individually excellent seats that are nowhere near one
    # another—the exact failure companion inventory exists to prevent.
    space_pool.sort(key=lambda s: -model.score(s, row_count=row_count))
    companion_pool.sort(key=lambda s: -model.score(s, row_count=row_count))
    # ponytail: bounded quality pools; widen only if real venue layouts show misses.
    space_options = itertools.combinations(
        space_pool[:request.wheelchair_spaces + 4], request.wheelchair_spaces
    )
    companion_options = tuple(itertools.combinations(
        companion_pool[:request.companion_seats + 4], request.companion_seats
    ))

    def accessible_key(pair):
        spaces, companions = pair
        proximity = min(
            (max((_relation(companion, space) for space in spaces), default=0.0)
             for companion in companions),
            default=1.0,
        )
        quality = _mean_quality((*spaces, *companions), model, row_count)
        return proximity, quality

    spaces, companions = max(
        itertools.product(space_options, companion_options), key=accessible_key
    )
    spaces, companions = list(spaces), list(companions)
    space_rows = {s.row_index for s in spaces}

    accessible = tuple(spaces + companions)
    parts: list[tuple[Seat, ...]] = [accessible] if accessible else []

    remaining = request.general_seats
    if remaining:
        anchor_row = min(space_rows) if space_rows else 0
        taken = {s.id for s in accessible}

        def near(run: tuple[Seat, ...]) -> tuple[int, float]:
            return (abs(run[0].row_index - anchor_row),
                    -_mean_quality(run, model, row_count))

        runs = [
            run
            for row in auditorium.rows()
            for run in _runs(row, remaining, request.admits)
            if not ({s.id for s in run} & taken)
        ]
        if runs:
            parts.append(min(runs, key=near))
        else:
            # No block that size: fall back to the nearest individual seats,
            # which is a worse arrangement and will score as one.
            singles = sorted(
                (s for s in auditorium.open_seats()
                 if request.admits(s) and s.id not in taken),
                key=lambda s: (abs(s.row_index - anchor_row),
                               -model.score(s, row_count=row_count)),
            )[:remaining]
            if not singles:
                return []
            parts.extend((s,) for s in singles)

    seats = tuple(sorted(
        (s for part in parts for s in part), key=lambda s: (s.row_index, s.col_index)
    ))
    if not seats:
        return []
    group = _build_group(auditorium, seats, request, model)
    if (
        group
        and request.max_rows is not None
        and len({seat.row_index for seat in group.seats}) > request.max_rows
    ):
        return []
    if group and not request.allow_split and not group.cohesion.is_together:
        return []
    return [group][:limit] if group else []


def accessible_capacity(auditorium: Auditorium) -> tuple[int, int]:
    """(open wheelchair spaces, open companion seats)."""
    wheelchair = sum(
        1 for s in auditorium.seats if s.is_open and s.kind is SeatKind.WHEELCHAIR
    )
    companion = sum(
        1 for s in auditorium.seats if s.is_open and s.kind is SeatKind.COMPANION
    )
    return wheelchair, companion
