"""Venue directory: the join between a SearchSpec's geography and adapters.

The directory is a source-backed graph. It starts with explicit independent
routing overrides and is populated by provider discovery, including the
OpenStreetMap independent-cinema directory, then persisted locally with the
source and source URL that supplied each row. There is deliberately no
packaged hardware overlay hiding inside it.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..ranking.spec import GeoPoint, LocationSpec


@dataclass(frozen=True)
class Venue:
    venue_id: str
    name: str
    chain: str
    tz: str | None = None
    point: GeoPoint | None = None
    market: str | None = None
    city: str | None = None
    state: str | None = None
    ticketing_platform: str | None = None
    url: str | None = None
    venue_type: str = "cinema"
    markup: str | None = None
    notes: str | None = None
    source: str = "directory"
    source_url: str | None = None
    observed_at: str | None = None

    def distance_km(self, origin: GeoPoint | None) -> float | None:
        if origin is None or self.point is None:
            return None
        return round(origin.km_to(self.point), 2)

    def today(self) -> date:
        return local_today(self.tz)

    @property
    def has_coordinates(self) -> bool:
        return self.point is not None

    @property
    def display_type(self) -> str:
        """A human-facing category, stable enough to filter in the local app."""
        return self.venue_type.replace("_", " ").title()

    def to_dict(self, *, origin: GeoPoint | None = None) -> dict:
        """JSON-safe directory record used by HTTP, MCP, and the local app."""
        curated = self.source == "independent-registry"
        return {
            "id": self.venue_id,
            "name": self.name,
            "chain": self.chain,
            "type": self.venue_type,
            "type_label": self.display_type,
            "timezone": self.tz,
            "market": self.market,
            "city": self.city,
            "state": self.state,
            "ticketing_platform": self.ticketing_platform,
            "url": self.url,
            "markup": self.markup,
            "notes": self.notes,
            "source": self.source,
            "source_url": self.source_url,
            "observed_at": self.observed_at,
            "provenance": {
                "status": "curated" if curated else "observed",
                "scope": "routing-config" if curated else "directory",
                "source": self.source,
                "source_url": self.source_url,
                "observed_at": self.observed_at,
            },
            "coordinates": (
                {"lat": self.point.lat, "lon": self.point.lon}
                if self.point else None
            ),
            "distance_km": self.distance_km(origin),
        }


def local_today(tz: str | None) -> date:
    """The current date *where the cinema is*.

    Every relative search window ("the next 7 days") is measured from a day,
    and that day has to be the venue's. Anchoring on UTC put every US venue a
    day ahead for the last hours of its evening - 5pm in San Francisco is
    already tomorrow in UTC - so a search run at exactly the time someone
    would run one dated tonight's showings to a day outside the window and
    returned nothing.

    Falls back to UTC for a venue with no zone on file, which is the same
    behaviour as before for those and no worse.
    """
    try:
        return datetime.now(ZoneInfo(tz) if tz else UTC).date()
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(UTC).date()


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
        city=info.get("city"),
        state=info.get("state"),
        ticketing_platform=info.get("ticketing_platform"),
        url=info.get("url"),
        venue_type=info.get("venue_type") or _venue_type(
            info.get("chain", "unknown"), info.get("name", venue_id), info
        ),
        markup=info.get("markup"),
        notes=info.get("notes"),
        source=info.get("source", "directory"),
        source_url=info.get("source_url"),
        observed_at=info.get("observed_at"),
    )


def _venue_type(chain: str, name: str, info: dict | None = None) -> str:
    """Classify a venue without pretending the source supplied a taxonomy.

    The type is intentionally coarse. It is a discovery/filtering aid, not a
    claim about ownership or programming. Curated metadata can override it
    with ``venue_type`` when a venue deserves a more specific label.
    """
    haystack = f"{chain} {name} {json.dumps(info or {})}".lower()
    if any(token in haystack for token in ("drive-in", "drive in", "drivein")):
        return "drive_in"
    if any(token in haystack for token in ("art house", "arthouse", "repertory", "filmforum",
                                           "metrograph", "coolidge", "roxie", "ifc center")):
        return "art_house"
    if any(token in haystack for token in ("drafthouse", "alamo", "dine-in", "dine in")):
        return "dine_in"
    if chain == "independent":
        return "independent"
    if chain in {"amc", "regal", "cinemark", "c360", "alamo"}:
        return "multiplex"
    return "cinema"


def _city_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _matches_city(venue: Venue, city: str) -> bool:
    needle = _city_key(city)
    if not needle:
        return True
    return any(
        needle in _city_key(value)
        for value in (venue.city, venue.market, venue.name)
    )


def _independent_seed() -> dict[str, dict]:
    path = pathlib.Path(__file__).resolve().parents[1] / "data" / "independent_venues.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        item["venue_id"]: {
            **item,
            "chain": "independent",
            "venue_type": _venue_type("independent", item.get("name", ""), item),
            "source": "independent-registry",
            "source_url": item.get("url"),
        }
        for item in payload.get("venues", [])
        if item.get("venue_id")
    }


class VenueDirectory:
    def __init__(self, venues: dict[str, dict] | None = None) -> None:
        if venues is None:
            # Independent venues are routing/configuration entries, not
            # hardware claims. National chains are discovered from their own
            # official directories and hydrated into the store by SearchService.
            source = _independent_seed()
        else:
            source = venues
        self._venues = {vid: _to_venue(vid, info) for vid, info in source.items()}

    def get(self, venue_id: str) -> Venue | None:
        return self._venues.get(venue_id)

    def register(self, venues: list[Venue]) -> None:
        """Merge provider-discovered venues in.

        Provider discovery supplies the freshest routing, geography, and
        provenance. A directory record never implies room hardware.
        """
        for venue in venues:
            current = self._venues.get(venue.venue_id)
            if current is None:
                self._venues[venue.venue_id] = (
                    replace(
                        venue,
                        venue_type=_venue_type(venue.chain, venue.name, venue.to_dict()),
                    )
                    if venue.venue_type == "cinema" else venue
                )
                continue
            # Discovery has fresher coordinates and routing identifiers; the
            # curated directory has better type/format context. Merge both.
            source = venue.source
            source_url = venue.source_url
            if source in {None, "", "directory"} and current.source:
                source = current.source
                source_url = source_url or current.source_url
            self._venues[venue.venue_id] = Venue(
                venue_id=current.venue_id,
                name=venue.name if venue.name != venue.venue_id else current.name,
                chain=current.chain if current.chain != "unknown" else venue.chain,
                tz=venue.tz or current.tz,
                point=venue.point or current.point,
                market=venue.market or current.market,
                city=venue.city or current.city,
                state=venue.state or current.state,
                ticketing_platform=current.ticketing_platform or venue.ticketing_platform,
                url=venue.url or current.url,
                venue_type=(
                    current.venue_type
                    if current.venue_type != "cinema"
                    else venue.venue_type
                ),
                markup=current.markup or venue.markup,
                notes=current.notes or venue.notes,
                source=source or current.source,
                source_url=source_url or current.source_url,
                observed_at=venue.observed_at or current.observed_at,
            )

    def all(self) -> list[Venue]:
        return list(self._venues.values())

    def describe(self, venue_id: str, *, origin: GeoPoint | None = None) -> dict | None:
        venue = self.get(venue_id)
        return venue.to_dict(origin=origin) if venue else None

    def filter(
        self,
        *,
        chain: str | None = None,
        venue_type: str | None = None,
        query: str | None = None,
        city: str | None = None,
        origin: GeoPoint | None = None,
        radius_km: float | None = None,
        include_unknown: bool = True,
        sort: str = "distance",
        limit: int = 200,
    ) -> list[Venue]:
        needle = (query or "").strip().lower()
        rows = [
            venue for venue in self._venues.values()
            if (not chain or venue.chain == chain)
            and (not venue_type or venue.venue_type == venue_type)
            and (not city or _matches_city(venue, city))
            and (
                not needle
                or needle in venue.name.lower()
                or needle in venue.venue_id.lower()
                or needle in (venue.market or "").lower()
            )
            and (
                origin is None
                or radius_km is None
                or (
                    venue.distance_km(origin) is not None
                    and venue.distance_km(origin) <= radius_km
                )
                or (include_unknown and venue.distance_km(origin) is None)
            )
        ]
        if sort == "name":
            rows.sort(key=lambda venue: venue.name.lower())
        elif sort == "type":
            rows.sort(key=lambda venue: (venue.venue_type, venue.name.lower()))
        elif sort == "chain":
            rows.sort(key=lambda venue: (venue.chain, venue.name.lower()))
        else:
            rows.sort(
                key=lambda venue: (
                    (
                        venue.distance_km(origin)
                        if venue.distance_km(origin) is not None
                        else float("inf")
                    ),
                    venue.name.lower(),
                )
            )
        return rows[: max(1, min(limit, 1000))]

    def types(self) -> list[str]:
        return sorted({venue.venue_type for venue in self._venues.values()})

    def matching(
        self,
        location: LocationSpec,
        *,
        chain: str | None = None,
        include_unknown: bool = False,
    ) -> list[Venue]:
        """Venues the spec admits, nearest first.

        Explicitly allowed venues are always included and sorted first even
        when they are outside the radius - naming a venue means you want it,
        which is exactly the drive-two-hours-for-70mm case.

        ``include_unknown`` is used only by an explicit exhaustive origin
        search. It keeps coordinate-less source records in the candidate set
        so missing geography does not become a false negative; provider stats
        make that uncertainty visible.
        """
        out = [
            v for v in self._venues.values()
            if (chain is None or v.chain == chain)
            and (not location.chains or v.chain in location.chains)
            and (not location.venue_types or v.venue_type in location.venue_types)
            and (not location.city or _matches_city(v, location.city))
            and (
                location.admits(v.venue_id, v.point)
                or (
                    location.city is not None
                    and _matches_city(v, location.city)
                    and v.point is None
                )
                or (include_unknown and v.point is None and location.origin is not None)
            )
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
