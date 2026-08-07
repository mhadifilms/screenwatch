"""Local, read-only intelligence about the evidence Screenwatch has.

Search is about answering one question well. The observatory is about making
the system understandable: which venue types are represented, what seat
surface each provider exposes, how fresh the local inventory is, and where a
result was clipped or degraded. It never performs network discovery on its
own; callers can opt into that through the normal provider search path.
"""

from __future__ import annotations

from collections import Counter

from ..presentation import venue_capabilities
from ..ranking.spec import GeoPoint
from .search import SearchService
from .store import DEFAULT_USER, Store
from .venues import Venue, VenueDirectory

_SEAT_SURFACES = {
    "amc": ("exact", "per-seat grid"),
    "cinemark": ("exact", "per-seat grid"),
    "regal": ("exact", "per-seat grid"),
    "c360": ("estimated", "exact count + auditorium shape"),
    "alamo": ("availability", "sold-out state only"),
    "independent": ("availability", "listing state only"),
}


class Observatory:
    """Build source-backed summaries for the API, MCP, and local UI."""

    def __init__(
        self,
        search: SearchService,
        *,
        store: Store | None = None,
        directory: VenueDirectory | None = None,
    ) -> None:
        self.search = search
        self.store = store or search.store
        self.directory = directory or search.directory

    def overview(self, *, user_id: str = DEFAULT_USER) -> dict:
        inventory = self.store.inventory_overview(user_id=user_id)
        venues = self.directory.all()
        chains = Counter(venue.chain for venue in venues)
        types = Counter(venue.venue_type for venue in venues)
        providers = []
        for provider in self.search.providers:
            level, detail = _SEAT_SURFACES.get(
                provider.chain, ("unknown", "provider-specific")
            )
            providers.append({
                "chain": provider.chain,
                "seat_data": level,
                "seat_detail": detail,
                "max_venues": getattr(provider, "max_venues", None),
                "max_days": getattr(provider, "max_days", None),
            })
        return {
            "inventory": inventory,
            "directory": {
                "venues": len(venues),
                "with_coordinates": sum(venue.has_coordinates for venue in venues),
                "by_chain": [
                    {"chain": key, "venues": value}
                    for key, value in sorted(chains.items())
                ],
                "by_type": [
                    {"type": key, "venues": value}
                    for key, value in sorted(types.items())
                ],
                "types": self.directory.types(),
            },
            "providers": providers,
            "provider_health": self.store.provider_health(user_id=user_id),
            "principles": [
                "A confirmed seat grid outranks an estimate.",
                "Unknown coverage is reported instead of being presented as empty.",
                "A booking link is the hard stop; Screenwatch never creates a hold.",
            ],
        }

    def list_venues(
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
    ) -> list[dict]:
        rows = []
        for venue in self.directory.filter(
            chain=chain,
            venue_type=venue_type,
            query=query,
            city=city,
            origin=origin,
            radius_km=radius_km,
            include_unknown=include_unknown,
            sort=sort,
            limit=limit,
        ):
            rows.append(self._venue_record(venue, origin=origin))
        return rows

    def get_venue(self, venue_id: str, *, origin: GeoPoint | None = None) -> dict | None:
        venue = self.directory.get(venue_id)
        return self._venue_record(venue, origin=origin) if venue else None

    def recent_searches(self, *, limit: int = 20, user_id: str = "local") -> list[dict]:
        return self.store.recent_search_runs(limit=limit, user_id=user_id)

    def inventory_analytics(self, *, group_by: str = "chain", limit: int = 100) -> dict:
        """Return grouped, source-backed inventory metrics for dashboards.

        The store owns the SQL and latest-snapshot semantics; this wrapper
        gives every transport one stable response shape and a concise caveat
        about what the numbers mean.
        """
        return {
            "group_by": group_by,
            "groups": self.store.inventory_analytics(group_by=group_by, limit=limit),
            "source": "local evidence store",
            "caveat": (
                "Counts describe screenings observed by configured providers. "
                "Seat totals use the latest snapshot per screening; missing seat "
                "surfaces are not inferred to be empty."
            ),
        }

    def _venue_record(self, venue: Venue, *, origin: GeoPoint | None = None) -> dict:
        record = venue.to_dict(origin=origin)
        record["inventory"] = self.store.inventory_by_venue(venue.venue_id)
        record["seat_surface"] = _SEAT_SURFACES.get(
            venue.chain, ("unknown", "provider-specific")
        )[0]
        record["seat_detail"] = _SEAT_SURFACES.get(
            venue.chain, ("unknown", "provider-specific")
        )[1]
        record["capabilities"] = [
            {
                "projection": capability.projection.value,
                "brand": capability.brand.value,
                "aspect": capability.aspect,
                "label": capability.describe(),
            }
            for capability in venue_capabilities(venue.venue_id)
        ]
        return record
