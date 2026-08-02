"""Venue directory: the join between a SearchSpec's geography and adapters.

Reads the same `data/venue_hardware.json` that the presentation oracle uses,
so screen hardware, coordinates and chain membership stay in one file. A venue
missing from it is not an error - it just cannot be distance-filtered, and
`LocationSpec.admits` is written to let those through rather than silently
drop them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..presentation import _VENUES  # single source of venue truth
from ..ranking.spec import GeoPoint, LocationSpec


@dataclass(frozen=True)
class Venue:
    venue_id: str
    name: str
    chain: str
    tz: str | None = None
    point: GeoPoint | None = None
    market: str | None = None
    ticketing_platform: str | None = None

    def distance_km(self, origin: GeoPoint | None) -> float | None:
        if origin is None or self.point is None:
            return None
        return round(origin.km_to(self.point), 2)


def _to_venue(venue_id: str, info: dict) -> Venue:
    point = (
        GeoPoint(info["lat"], info["lon"])
        if info.get("lat") is not None and info.get("lon") is not None
        else None
    )
    return Venue(
        venue_id=venue_id,
        name=info.get("name", venue_id),
        chain=info.get("chain", "unknown"),
        tz=info.get("tz"),
        point=point,
        market=info.get("market"),
        ticketing_platform=info.get("ticketing_platform"),
    )


class VenueDirectory:
    def __init__(self, venues: dict[str, dict] | None = None) -> None:
        source = _VENUES if venues is None else venues
        self._venues = {vid: _to_venue(vid, info) for vid, info in source.items()}

    def get(self, venue_id: str) -> Venue | None:
        return self._venues.get(venue_id)

    def register(self, venues: list[Venue]) -> None:
        """Merge provider-discovered venues in.

        Seed-table entries win on conflict: the shipped file carries screen
        hardware an API will not tell you (which IMAX house is 1.43:1), and a
        discovery pass must not overwrite that with a thinner record.
        """
        for venue in venues:
            self._venues.setdefault(venue.venue_id, venue)

    def all(self) -> list[Venue]:
        return list(self._venues.values())

    def matching(self, location: LocationSpec, *, chain: str | None = None) -> list[Venue]:
        """Venues the spec admits, nearest first.

        Explicitly allowed venues are always included and sorted first even
        when they are outside the radius - naming a venue means you want it,
        which is exactly the drive-two-hours-for-70mm case.
        """
        out = [
            v for v in self._venues.values()
            if (chain is None or v.chain == chain)
            and location.admits(v.venue_id, v.point)
        ]
        out.sort(
            key=lambda v: (
                0 if v.venue_id in location.allow else 1,
                v.distance_km(location.origin) if v.distance_km(location.origin) is not None
                else float("inf"),
                v.name,
            )
        )
        return out
