"""Phase B: re-rank the shortlist with real seats.

Only the top-K coarse survivors get here, because each one costs a guarded
seat-map request. K is `spec.max_seatmap_fetches`.

This phase is where the motivating scenario is decided. Four people, near
sellout: phase A cannot tell a showing with four seats together from one with
four scattered singles, because at screening level they look identical. Phase
B can, and group cohesion is weighted heavily enough to overturn a format
preference and a 75-minute delay - which is the judgement a person actually
makes.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models import Attribute
from ..seating.estimate import estimate
from ..seating.estimate import seat_components as estimated_components
from ..seating.groups import (
    GENERAL_KINDS,
    RECLINING_KINDS,
    PartyBond,
    PartyKind,
    SeatGroup,
    SeatRequest,
    find_groups,
)
from ..seating.model import Auditorium, SeatDataUnavailable
from ..seating.quality import QualityModel
from .candidate import Option
from .coarse import COARSE_COMPONENTS, weighted
from .spec import SearchSpec

FINE_COMPONENTS = ("group_cohesion", "seat_quality", "party_fit")

# How many options may be *attempted* per seat-map budgeted. Unsupported
# chains raise without a network call, so a generous multiple is cheap.
ATTEMPT_MULTIPLIER = 5
ALL_COMPONENTS = COARSE_COMPONENTS + FINE_COMPONENTS

SeatFetcher = Callable[[Option], Auditorium]


def party_fit(group: SeatGroup | None, party_size: int) -> float:
    """Can everyone actually sit? Weighted hardest of the seat components.

    Partial seating is not scored as a fraction of the party: getting three
    of four in is not 75% as good as getting everyone in, it is a different
    and much worse outcome.
    """
    if group is None:
        return 0.6                      # unknown, not bad - do not punish missing data
    if group.complete:
        return 1.0
    return round(0.25 * (group.size / max(party_size, 1)), 4)


# Scores for an option whose seats are unknown.
#
# Below what a *confirmed but imperfect* grid earns, and that ordering is the
# point. Set generously, ignorance beat knowledge: an option known to seat the
# party 2+2 in row three scored worse than one nobody had looked at, so the
# ranker preferred the unexamined showing and the whole two-phase design
# inverted. Missing data now sits just under a mediocre real answer and well
# above a bad one, which is what "we do not know" actually means.
UNKNOWN_SEATS = {"group_cohesion": 0.45, "seat_quality": 0.45, "party_fit": 0.55}

# Scores for a grid that was read and yielded nothing this party can take.
#
# A different fact from "unknown", and it has to score lower, or an option
# proven unseatable ranks above one nobody has checked. That was the state
# before: both went through the same neutral branch, so a room with no four
# seats in it looked exactly like a room nobody had looked at.
NO_SEATING = {"group_cohesion": 0.0, "seat_quality": 0.0, "party_fit": 0.0}


def _seat_components(
    group: SeatGroup | None, spec: SearchSpec, *, checked: bool = False
) -> dict[str, float]:
    if group is None:
        return dict(NO_SEATING if checked else UNKNOWN_SEATS)
    return {
        # A party that said it does not mind sitting apart is not penalised
        # for sitting apart. Cohesion is still recorded on the group itself,
        # so the rationale can describe the arrangement honestly.
        "group_cohesion": 1.0 if not spec.seating.together else group.cohesion_score,
        # Carry fairness across screening boundaries too. A room whose mean
        # is high because fourteen people are excellent and one is awful must
        # not beat a room that treats the whole party well.
        "seat_quality": round(
            (
                0.40 * group.fairness
                + 0.30 * group.nash_welfare
                + 0.20 * group.quality
                + 0.10 * group.robustness
            )
            if group.fairness else group.quality,
            4,
        ),
        "party_fit": party_fit(group, spec.party_size),
    }


def seat_request(spec: SearchSpec) -> SeatRequest:
    """Translate ranking's preferences into seating's vocabulary.

    The one place the two layers meet, so a preference that is not handled
    here is not handled at all.

    Only `RECLINERS` of the `require` attributes is a seat-level constraint;
    the rest (3D, Atmos, open caption) describe the *screening* and are
    already applied in phase A by `Preference`. Requiring them again here
    would filter seats by a property seats do not have.
    """
    prefs = spec.seating
    kinds = RECLINING_KINDS if Attribute.RECLINERS in prefs.require else GENERAL_KINDS
    return SeatRequest(
        party_size=spec.party_size,
        together=prefs.together,
        allow_split=prefs.allow_split,
        kinds=kinds,
        wheelchair_spaces=prefs.wheelchair_spaces,
        companion_seats=prefs.companion_seats,
        party_kind=PartyKind(prefs.party_kind),
        bonds=tuple(PartyBond(*bond) for bond in prefs.bonds),
        max_rows=prefs.max_rows,
        avoid_strangers=prefs.avoid_strangers,
        prefer_aisle=prefs.prefer_aisle,
    )


def quality_model_for(spec: SearchSpec, venue_id: str) -> QualityModel:
    prefs = spec.seating
    return QualityModel.for_venue(
        venue_id,
        ideal_depth=prefs.ideal_depth,
        adaptive_middle=False if prefs.ideal_depth is not None else None,
        avoid_front_rows=prefs.avoid_front_rows,
        max_lateral=prefs.max_lateral,
        aisle_penalty=0.15 if prefs.avoid_aisle else (-0.08 if prefs.prefer_aisle else 0.0),
    )


def apply_seats(option: Option, auditorium: Auditorium, spec: SearchSpec) -> Option:
    """Attach the best seat assignment for this spec and rescore."""
    model = quality_model_for(spec, option.screening.venue_id).for_presentation(
        option.screening.presentation
    )

    if auditorium.has_shape:
        # Counts plus room shape but no per-seat occupancy: estimate rather
        # than pretend. This is the common case - AMC is the only source that
        # publishes a real grid without entering a booking flow.
        feasibility = estimate(
            party_size=spec.party_size,
            available=auditorium.available,
            capacity=auditorium.capacity,
            rows=auditorium.row_count,
            row_lengths=list(auditorium.row_lengths),
        )
        option.auditorium = auditorium
        option.seats = None
        option.feasibility = feasibility
        option.seat_data = "estimated"
        option.components.update(estimated_components(feasibility, spec.party_size))
        option.score = weighted(option.components, spec.weights, ALL_COMPONENTS)
        return option

    group: SeatGroup | None = None
    alternatives: tuple[SeatGroup, ...] = ()
    checked = False
    if auditorium.has_grid:
        groups = find_groups(auditorium, seat_request(spec), model)
        group = groups[0] if groups else None
        alternatives = tuple(groups[1:])
        # A grid was read. If it produced nothing, that is a finding about the
        # room, not an absence of information.
        checked = True
        option.seat_data = "grid" if group else "no_seating"
    else:
        option.seat_data = "count_only" if auditorium.available else "unavailable"

    option.auditorium = auditorium
    option.seats = group
    option.seat_alternatives = alternatives
    option.components.update(_seat_components(group, spec, checked=checked))
    option.score = weighted(option.components, spec.weights, ALL_COMPONENTS)
    return option


def fine_rank(
    options: list[Option],
    spec: SearchSpec,
    fetch_seats: SeatFetcher,
    *,
    top_k: int | None = None,
) -> list[Option]:
    """Fetch seat maps for the shortlist and re-rank everything together.

    Options beyond the budget keep their coarse score and are marked
    `not_fetched`. They stay in the result rather than being dropped - a
    caller that widens the budget should see the same set, just better
    informed.
    """
    budget = spec.max_seatmap_fetches if top_k is None else top_k

    # The budget counts *successful* fetches, not attempts. A chain with no
    # seat surface raises instantly and costs nothing, so it must not consume
    # a slot - otherwise a single Regal option at the top of the list spends
    # the whole budget and no seat map gets fetched at all. Attempts are still
    # capped so a long tail of unsupported options cannot spin.
    fetched = 0
    attempts = 0
    attempt_cap = budget * ATTEMPT_MULTIPLIER

    for option in options:
        if fetched >= budget or attempts >= attempt_cap:
            break
        attempts += 1
        try:
            auditorium = fetch_seats(option)
        except SeatDataUnavailable:
            option.seat_data = "unavailable"
            option.components.update(_seat_components(None, spec))
            option.score = weighted(option.components, spec.weights, ALL_COMPONENTS)
            continue
        apply_seats(option, auditorium, spec)
        fetched += 1

    # Everything untouched is scored on the same axis set so the sort stays
    # meaningful, using the neutral seat values.
    for option in options:
        if option.seat_data == "not_fetched":
            option.components.update(_seat_components(None, spec))
            option.score = weighted(option.components, spec.weights, ALL_COMPONENTS)

    ranked = list(options)
    ranked.sort(key=lambda o: (-o.score, o.screening.starts_at_utc))
    return ranked
