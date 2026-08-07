"""MCP server over stdio.

Thin by design: every tool is a call into `service/`, so the MCP surface and
the HTTP API cannot drift apart.

`get_booking_link` is the last step the system takes. It returns a URL and
stops - no cart, no checkout, no stored payment.
"""

from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer

from .. import __version__
from ..identity.normalize import analyze
from ..identity.work import WorkRef
from ..ranking.spec import GeoPoint, SearchSpec
from ..seating.render import to_svg, to_unicode_grid
from ..service.observatory import Observatory
from ..service.search import SearchResult, SearchService
from ..service.serde import (
    location_from_dict,
    option_to_dict,
    spec_from_dict,
    spec_from_json,
    spec_to_dict,
)
from ..service.watch import WatchService
from .schemas import LocationInput, OriginInput, SearchSpecInput


def build_server(search: SearchService, watches: WatchService) -> MCPServer:
    server = MCPServer(
        name="screenwatch",
        version=__version__,
        instructions=(
            "US theater and release intelligence across chains and independents. "
            "Use list_venues and get_data_overview to understand coverage before "
            "searching; venue and room claims are source-linked observations with "
            "explicit scope and freshness, not an inferred national hardware census. "
            "Call resolve_title first if a title is ambiguous or a re-release. "
            "find_screenings returns bookable options - a showing plus the "
            "actual seats you would get - each with reasons and tradeoffs. "
            "It stops at a booking link; it never purchases."
        ),
    )
    last: dict[str, SearchResult | None] = {"result": None}
    observatory = Observatory(search, store=search.store, directory=search.directory)

    def _find(option_id: str):
        result = last["result"]
        if result is None:
            return None, "Run find_screenings first."
        option = next((o for o in result.options if o.option_id == option_id), None)
        if option is None:
            return None, f"Unknown option_id {option_id!r}."
        return option, None

    @server.tool(
        description="Resolve free text to a canonical film. Collapses a chain's "
                    "product variants (sensory-friendly, open-caption, private "
                    "rental) onto one film and reports the variant separately."
    )
    def resolve_title(query: str) -> dict:
        analysis = analyze(query)
        resolution = search.resolver.resolve("query", query, query)
        return {
            "clean_title": analysis.clean,
            "match_key": analysis.match_key,
            "product_kind": analysis.kind.value,
            "bookable": analysis.is_bookable,
            "attributes": sorted(a.value for a in analysis.attrs),
            "presentation_in_title": analysis.presentation.describe(),
            "work": vars(resolution.work) if resolution.work else None,
            "confidence": resolution.link.confidence,
            "needs_review": resolution.link.needs_review,
            "candidates": [vars(c) for c in resolution.candidates],
        }

    @server.tool(
        description="Rank bookable options for a film. Scores format, showtime, "
                    "distance, pass coverage and availability; then for the top "
                    "few fetches real seat maps and scores seat quality and "
                    "whether your party can sit together. Every option carries "
                    "reasons and tradeoffs explaining its position."
    )
    def find_screenings(spec: SearchSpecInput) -> dict:
        result = search.search(spec_from_dict(spec.to_dict()))
        last["result"] = result
        return {
            "search_id": result.search_id,
            "narration": result.narrate(),
            "comparison": result.comparison(),
            "considered": result.considered,
            "seatmaps_fetched": result.seatmaps_fetched,
            "complete": result.complete,
            "coverage": result.coverage,
            "duration_ms": result.duration_ms,
            "provider_stats": list(result.provider_stats),
            "provider_errors": list(result.provider_errors),
            # What fast-path caps or source failures left unread, so a model
            # can say "nothing in what I checked" rather than "nothing".
            "clipped": list(result.clipped),
            "unresolved_titles": list(result.unresolved_titles),
            "options": [option_to_dict(o) for o in result.options[:20]],
        }

    @server.tool(
        description="Read the local evidence overview: indexed screenings, works, "
                    "venues, provider seat surfaces, coverage types, active watches, "
                    "and alert backlog. This is read-only and does not trigger a poll."
    )
    def get_data_overview() -> dict:
        return observatory.overview()

    @server.tool(
        description="Query the local evidence store as a data cube. Group indexed "
        "screenings by chain, venue, venue type, city, format, or availability; "
        "include latest seat coverage and open/capacity totals. This is read-only."
    )
    def get_inventory_analytics(
        group_by: Literal[
            "chain", "venue", "venue_type", "city", "format", "availability"
        ] = "chain",
        limit: int = 100,
    ) -> dict:
        return observatory.inventory_analytics(group_by=group_by, limit=limit)

    @server.tool(
        description="Check the low-cost AMC movie catalog signal for a title. "
        "This can reveal that a release entry exists before showtimes are listed, "
        "but it never claims tickets are on sale; use a strict screening watch for "
        "the actual drop."
    )
    def find_release_signals(query: str) -> dict:
        spec = SearchSpec(work=WorkRef(query=query.strip()), release_radar=True)
        try:
            signals = search.release_signals(spec)
        except Exception as exc:                       # noqa: BLE001
            return {
                "query": query,
                "source": "amc:sitemap",
                "signals": [],
                "complete": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "query": query,
            "source": "amc:sitemap",
            "signals": signals,
            "complete": True,
            "ticket_sale_proven": False,
        }

    @server.tool(
        description="Refresh the local venue graph from the configured US sources. "
        "This discovers venue metadata and coordinates, persists it locally, and "
        "does not fetch film showtimes or seat maps."
    )
    def refresh_venues(location: LocationInput) -> dict:
        spec = SearchSpec(
            work=WorkRef(query="venue refresh"),
            location=location_from_dict(location.model_dump()),
        )
        return search.discover_venues(spec)

    @server.tool(
        description="List known US venues with type, chain, coordinates, seat-data "
                    "surface, current local inventory, and source-backed observed "
                    "capabilities with provenance. Observed capabilities are "
                    "not a permanent room inventory. "
                    "Use this to find art houses, multiplexes, or exact-seat providers."
    )
    def list_venues(
        chain: str | None = None,
        venue_type: str | None = None,
        query: str | None = None,
        city: str | None = None,
        origin: OriginInput | None = None,
        radius_km: float | None = None,
        include_unknown: bool = True,
        sort: Literal["distance", "name", "type", "chain"] = "distance",
        limit: int = 100,
    ) -> dict:
        return {
            "venues": observatory.list_venues(
                chain=chain,
                venue_type=venue_type,
                query=query,
                city=city,
                origin=GeoPoint(origin.lat, origin.lon) if origin else None,
                radius_km=radius_km,
                include_unknown=include_unknown,
                sort=sort,
                limit=limit,
            ),
            "types": search.directory.types(),
        }

    @server.tool(
        description="Get a single venue's detailed record, including source-backed "
                    "observed presentations and room profiles, ticketing platform, "
                    "inventory, and seat-data limits."
    )
    def get_venue(venue_id: str) -> dict:
        record = observatory.get_venue(venue_id)
        return record or {"error": f"Unknown venue_id {venue_id!r}."}

    @server.tool(
        description="Return source-linked venue evidence for one venue. It groups "
                    "observed screening presentations and room/seat observations "
                    "by source, URL, scope, and freshness. It never turns a "
                    "single showing into a permanent hardware claim."
    )
    def get_venue_evidence(venue_id: str) -> dict:
        if search.directory.get(venue_id) is None:
            return {"venue_id": venue_id, "error": "unknown venue_id"}
        return search.store.venue_evidence(venue_id)

    @server.tool(
        description="Seat map for one option, normalized so a 500-seat IMAX and "
                    "a 40-seat microcinema render comparably. Your recommended "
                    "seats are highlighted."
    )
    def get_seatmap(option_id: str, format: Literal["unicode", "svg"] = "unicode") -> str:
        option, error = _find(option_id)
        if error:
            return error
        if option.auditorium is None:
            return (
                f"No seat map for this option (seat_data={option.seat_data}). "
                "Ranking fell back to availability only."
            )
        picked = {s.id for s in option.seats.seats} if option.seats else set()
        return (
            to_svg(option.auditorium, picked) if format == "svg"
            else to_unicode_grid(option.auditorium, picked)
        )

    @server.tool(
        description="Why the top option beat the runner-up, component by component."
    )
    def explain_ranking() -> dict:
        result = last["result"]
        if result is None:
            return {"error": "Run find_screenings first."}
        top = result.options[:2]
        return {
            "comparison": result.comparison(),
            "narration": result.narrate(limit=5),
            "components": {o.option_id: o.components for o in top},
        }

    @server.tool(
        description="Monitor for NEW screenings matching a spec. By default it "
                    "records what is already on sale and stays quiet about it, "
                    "firing only on genuinely new drops - e.g. new 70mm IMAX for "
                    "a film that is already playing. Set seed=false to be told "
                    "about everything currently available too."
    )
    def create_watch(
        label: str,
        spec: SearchSpecInput,
        cadence_s: int = 300,
        webhook: str | None = None,
        seed: bool = True,
    ) -> dict:
        watch_id = watches.create(
            spec_from_dict(spec.to_dict()), label,
            cadence_s=cadence_s, webhook=webhook, seed=seed,
        )
        return {
            "watch_id": watch_id,
            "seeded": seed,
            "note": ("Seeded: only new screenings will be reported."
                     if seed else "Unseeded: current screenings will be reported too."),
        }

    @server.tool(description="List active monitors, health, and what each is watching for.")
    def list_watches() -> dict:
        # An object rather than a bare list: the SDK wraps non-object returns
        # in a synthetic envelope, so every tool here returns a dict to keep
        # the client-visible shape predictable.
        return {"watches": [
            {
                "watch_id": w["watch_id"],
                "label": w["label"],
                "cadence_s": w["cadence_s"],
                "last_run": w["last_run"],
                "last_hit": w["last_hit"],
                "last_success": w["last_success"],
                "last_error": w["last_error"],
                "last_warning": w["last_warning"],
                "error_count": w["error_count"],
                "webhook": bool(w["webhook"]),
                "spec": spec_to_dict(spec_from_json(w["spec"])),
            }
            for w in watches.list()
        ]}

    @server.tool(
        description="Read the durable alert history for one monitor, including "
        "the change type, previous/current state, source-backed booking link, "
        "and whether the local alert queue has acknowledged it."
    )
    def get_watch_history(watch_id: str, limit: int = 100) -> dict:
        row = search.store.get_watch(watch_id)
        if row is None:
            return {"watch_id": watch_id, "hits": [], "error": "unknown watch_id"}
        return {
            "watch_id": watch_id,
            "hits": search.store.hit_history(watch_id, limit=limit),
        }

    @server.tool(description="Stop a monitor.")
    def cancel_watch(watch_id: str) -> dict:
        return {"cancelled": watches.cancel(watch_id)}

    @server.tool(
        description="Acknowledge alert ids after the client has actually handled them."
    )
    def acknowledge_hits(hit_ids: list[int]) -> dict:
        watches.acknowledge(hit_ids)
        return {"acknowledged": hit_ids}

    @server.tool(
        description="Run every monitor whose cadence has elapsed and return any "
                    "unacknowledged hits. Polling is non-destructive by default; "
                    "acknowledge them explicitly after handling."
    )
    def poll_watches(acknowledge: bool = False) -> dict:
        fresh = watches.run_due()
        pending = watches.pending()
        if acknowledge:
            watches.acknowledge([h["hit_id"] for h in pending])
        return {"new_hits": len(fresh), "hits": [h["payload"] for h in pending]}

    @server.tool(
        description="Booking URL for an option. This is the final step - "
                    "screenwatch never purchases, holds, or stores payment."
    )
    def get_booking_link(option_id: str) -> dict:
        option, error = _find(option_id)
        link = option.screening.deeplink if option else search.booking_link(option_id)
        if not link:
            return {"error": error or "No booking link for that option."}
        return {
            "booking_link": link,
            "note": "Open this to complete the booking yourself.",
        }

    return server


def build_default() -> tuple[MCPServer, SearchService, WatchService]:
    from ..service.defaults import default_service

    search, watches, _store = default_service()
    return build_server(search, watches), search, watches


def main() -> None:
    server, _, _ = build_default()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
