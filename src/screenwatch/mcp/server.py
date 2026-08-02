"""MCP server over stdio.

Thin by design: every tool is a call into `service/`, so the MCP surface and
the HTTP API cannot drift apart.

`get_booking_link` is the last step the system takes. It returns a URL and
stops - no cart, no checkout, no stored payment.
"""

from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer

from ..identity.normalize import analyze
from ..seating.render import to_svg, to_unicode_grid
from ..service.search import SearchResult, SearchService
from ..service.serde import option_to_dict, spec_from_dict, spec_from_json, spec_to_dict
from ..service.watch import WatchService
from .schemas import SearchSpecInput


def build_server(search: SearchService, watches: WatchService) -> MCPServer:
    server = MCPServer(
        name="screenwatch",
        version="0.1.0",
        instructions=(
            "Ranked cinema showtime search across chains and independents. "
            "Call resolve_title first if a title is ambiguous or a re-release. "
            "find_screenings returns bookable options - a showing plus the "
            "actual seats you would get - each with reasons and tradeoffs. "
            "It stops at a booking link; it never purchases."
        ),
    )
    last: dict[str, SearchResult | None] = {"result": None}

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
            "narration": result.narrate(),
            "comparison": result.comparison(),
            "considered": result.considered,
            "seatmaps_fetched": result.seatmaps_fetched,
            "provider_errors": list(result.provider_errors),
            "unresolved_titles": list(result.unresolved_titles),
            "options": [option_to_dict(o) for o in result.options[:20]],
        }

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

    @server.tool(description="List active monitors and what each is watching for.")
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
                "spec": spec_to_dict(spec_from_json(w["spec"])),
            }
            for w in watches.list()
        ]}

    @server.tool(description="Stop a monitor.")
    def cancel_watch(watch_id: str) -> dict:
        return {"cancelled": watches.cancel(watch_id)}

    @server.tool(
        description="Run every monitor whose cadence has elapsed and return any "
                    "undelivered hits."
    )
    def poll_watches(acknowledge: bool = True) -> dict:
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
