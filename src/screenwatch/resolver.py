"""Fuse observations from independent surfaces into facts.

Rules, in order of how much they matter:

  * Group on a *bucketed* key so a one-minute disagreement between surfaces
    does not silently become two separate screenings that never corroborate.

  * Vote on the presentation *core* - (projection, brand, aspect) - not on
    the whole descriptor. Attributes are additive: one surface listing
    open-caption and another not is two views of the same screening, not a
    conflict. Attributes union across the sources that agree on the core.

  * Availability takes the most pessimistic fresh claim. Being told a
    sold-out show is open wastes a browser launch during the exact minute you
    cannot afford one; being told an open show is full costs a missed poll.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .models import Availability, Fact, Observation, Presentation

# Cheap surfaces are not less trustworthy, but guarded ones sit closer to the
# system of record, so they break ties.
TIER_WEIGHT = {0: 0.8, 1: 1.0, 2: 1.1, 3: 1.2}

_PESSIMISM = {
    Availability.SOLD_OUT: 3,
    Availability.ALMOST_FULL: 2,
    Availability.SELLABLE: 1,
    Availability.UNKNOWN: 0,
}


def core(p: Presentation) -> tuple:
    return (p.projection, p.brand, p.aspect)


def describe_core(c: tuple) -> str:
    projection, brand, aspect = c
    bits = [brand.value, projection.value]
    if aspect:
        bits.append(aspect)
    return "/".join(bits)


class Resolver:
    def __init__(
        self,
        *,
        health: dict[str, float] | None = None,
        tolerance: timedelta = timedelta(minutes=2),
        ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        # Rolling per-source accuracy. A source that has been disagreeing with
        # quorum loses vote weight before a human notices.
        self.health = health or {}
        self.tolerance = tolerance
        self.ttl = ttl

    def weight(self, obs: Observation) -> float:
        return (
            obs.confidence
            * TIER_WEIGHT.get(obs.tier, 1.0)
            * self.health.get(obs.source, 1.0)
        )

    def resolve(self, observations: list[Observation], *, now: datetime) -> list[Fact]:
        groups: dict[str, list[Observation]] = defaultdict(list)
        for obs in observations:
            if now - obs.observed_at <= self.ttl:
                groups[obs.key.bucketed(self.tolerance)].append(obs)
        return [self._resolve_group(g) for g in groups.values()]

    def _resolve_group(self, group: list[Observation]) -> Fact:
        votes: dict[tuple, float] = defaultdict(float)
        for obs in group:
            votes[core(obs.presentation)] += self.weight(obs)

        total = sum(votes.values()) or 1.0
        winner = max(votes, key=lambda c: (votes[c], describe_core(c)))
        agreement = votes[winner] / total

        agreeing = [o for o in group if core(o.presentation) == winner]
        conflicts = tuple(sorted(
            f"{o.source}={describe_core(core(o.presentation))}"
            for o in group if core(o.presentation) != winner
        ))

        best = max(agreeing, key=lambda o: (self.weight(o), o.observed_at))

        # Attributes are additive across agreeing sources; raw comes from the
        # single most trusted one, since concatenating raws helps nobody.
        attrs = frozenset().union(*(o.presentation.attrs for o in agreeing))
        resolved = best.presentation.with_(attrs=attrs)

        availability = max((o.availability for o in group), key=lambda a: _PESSIMISM[a])
        if availability is Availability.UNKNOWN:
            known = [o.availability for o in group if o.availability is not Availability.UNKNOWN]
            if known:
                availability = max(known, key=lambda a: _PESSIMISM[a])

        return Fact(
            key=best.key,
            presentation=resolved,
            availability=availability,
            sources=tuple(sorted({o.source for o in group})),
            agreement=agreement,
            title=next((o.title for o in agreeing if o.title), None),
            external_id=best.external_id,
            deeplink=best.deeplink,
            observed_at=max(o.observed_at for o in group),
            conflicts=conflicts,
        )
