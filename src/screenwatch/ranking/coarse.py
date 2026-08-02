"""Phase A: rank every candidate screening using only free data.

No seat maps here. Seat grids cost one guarded request each, so the point of
this phase is to decide *which handful is worth paying for*.

Every component returns 0..1 where higher is better, including the ones that
are conceptually penalties - a uniform direction means the weighted sum needs
no special cases and weights stay readable.
"""

from __future__ import annotations

import math
from datetime import datetime

from ..models import Availability
from .candidate import Option, Screening
from .spec import SearchSpec, Weights

COARSE_COMPONENTS = (
    "format_fit",
    "time_fit",
    "lateness",
    "distance_fit",
    "membership_fit",
    "availability_prior",
    "venue_affinity",
)

_AVAILABILITY_PRIOR = {
    Availability.SELLABLE: 1.0,
    Availability.ALMOST_FULL: 0.72,
    Availability.UNKNOWN: 0.60,
    Availability.SOLD_OUT: 0.0,
}


def format_fit(screening: Screening, spec: SearchSpec) -> float:
    """1.0 for the top-ranked presentation, decaying down the preference list.

    An unranked presentation scores low but not zero: with everything sold
    out, a format you did not ask for still beats not going.
    """
    if spec.presentations is None:
        return 1.0
    rank = spec.presentations.rank(screening.presentation)
    if rank is None:
        return 0.25
    return round(1.0 / (1.0 + 0.45 * rank), 4)


def time_fit(screening: Screening, spec: SearchSpec) -> float:
    """How well the start time sits inside the requested windows."""
    if not spec.time_windows:
        return 1.0
    if spec.admits_time(screening.starts_at_local):
        return 1.0
    minutes = _minutes_outside(screening.starts_at_local, spec)
    return round(math.exp(-minutes / 90.0), 4)


def _minutes_outside(when: datetime, spec: SearchSpec) -> float:
    best = float("inf")
    now_min = when.hour * 60 + when.minute
    for window in spec.time_windows:
        start = window.start.hour * 60 + window.start.minute
        end = window.end.hour * 60 + window.end.minute
        if window.wraps:
            best = min(best, min(abs(now_min - start), abs(now_min - end)))
        else:
            best = min(best, 0 if start <= now_min <= end
                       else min(abs(now_min - start), abs(now_min - end)))
    return best if best != float("inf") else 0.0


def lateness(screening: Screening, spec: SearchSpec) -> float:
    """Mild, monotonic penalty for very late starts.

    Kept separate from `time_fit` on purpose. "After 6pm" admits both an
    11:30pm and a 12:45am show, but they are not equally attractive, and
    folding that into the window score would make the window do two jobs.
    """
    local = screening.starts_at_local
    minutes = local.hour * 60 + local.minute
    if 3 * 60 <= minutes < 22 * 60:
        return 1.0
    past_ten = (minutes - 22 * 60) % (24 * 60)   # wraps through midnight
    return round(max(0.25, 1.0 - past_ten / 360.0), 4)


def distance_fit(screening: Screening, spec: SearchSpec) -> float:
    if screening.distance_km is None or spec.location.origin is None:
        return 1.0
    ratio = screening.distance_km / max(spec.location.radius_km, 1.0)
    return round(math.exp(-(ratio**2)), 4)


def membership_fit(screening: Screening, spec: SearchSpec) -> float:
    """Covered by a subscription the user already pays for.

    A boost, never a filter: a 70mm print at a chain you have no pass for is
    still the thing you wanted to see.
    """
    if not spec.memberships:
        return 1.0
    return 1.0 if spec.covered_by_membership(screening.chain) else 0.55


def availability_prior(screening: Screening, spec: SearchSpec) -> float:
    return _AVAILABILITY_PRIOR.get(screening.availability, 0.5)


def venue_affinity(screening: Screening, spec: SearchSpec) -> float:
    if screening.venue_id in spec.location.allow:
        return 1.0
    return 0.85


_SCORERS = {
    "format_fit": format_fit,
    "time_fit": time_fit,
    "lateness": lateness,
    "distance_fit": distance_fit,
    "membership_fit": membership_fit,
    "availability_prior": availability_prior,
    "venue_affinity": venue_affinity,
}


def weighted(components: dict[str, float], weights: Weights, keys) -> float:
    total = sum(getattr(weights, k) for k in keys) or 1.0
    return round(
        sum(components.get(k, 0.0) * getattr(weights, k) for k in keys) / total, 4
    )


def score_screening(screening: Screening, spec: SearchSpec) -> Option:
    components = {name: fn(screening, spec) for name, fn in _SCORERS.items()}
    coarse = weighted(components, spec.weights, COARSE_COMPONENTS)
    return Option(
        screening=screening,
        score=coarse,
        coarse_score=coarse,
        components=components,
    )


def coarse_rank(screenings: list[Screening], spec: SearchSpec) -> list[Option]:
    """Filter to what the spec admits, then rank. No network access."""
    admitted = [
        s for s in screenings
        if (spec.include_sold_out or s.availability is not Availability.SOLD_OUT)
        and spec.admits_time(s.starts_at_local)
        and spec.budget.admits(s.price_hint_usd, spec.party_size)
    ]
    options = [score_screening(s, spec) for s in admitted]
    options.sort(key=lambda o: (-o.score, o.screening.starts_at_utc))
    return options
