"""Localhost HTTP API.

Mirrors the MCP tools one-for-one over the same service objects, so the two
transports cannot diverge in behaviour. `user_id` is threaded through as a
header with a local default - single-user today, no migration needed later.
"""

from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from ..identity.normalize import analyze
from ..seating.render import to_svg, to_unicode_grid
from ..service.search import SearchService
from ..service.serde import option_to_dict, spec_from_dict, spec_to_dict
from ..service.store import DEFAULT_USER
from ..service.watch import WatchService


class WatchCreate(BaseModel):
    label: str
    spec: dict
    cadence_s: int = 300
    webhook: str | None = None
    seed: bool = True


def create_app(search: SearchService, watches: WatchService) -> FastAPI:
    app = FastAPI(
        title="screenwatch",
        version="0.1.0",
        description="Ranked cinema showtime search. Stops at the booking link.",
    )
    cache: dict[str, object] = {}

    def user(x_user_id: str = Header(default=DEFAULT_USER)) -> str:
        return x_user_id

    @app.get("/v1/health")
    def health() -> dict:
        return {"ok": True, "providers": [p.chain for p in search.providers]}

    @app.get("/v1/resolve")
    def resolve(query: str) -> dict:
        analysis = analyze(query)
        resolution = search.resolver.resolve("query", query, query)
        return {
            "clean_title": analysis.clean,
            "product_kind": analysis.kind.value,
            "attributes": sorted(a.value for a in analysis.attrs),
            "work": resolution.work.__dict__ if resolution.work else None,
            "confidence": resolution.link.confidence,
            "needs_review": resolution.link.needs_review,
        }

    @app.post("/v1/search")
    def do_search(spec: dict, uid: str = Depends(user)) -> dict:
        result = search.search(spec_from_dict(spec))
        cache["last"] = result
        return {
            "narration": result.narrate(),
            "comparison": result.comparison(),
            "considered": result.considered,
            "seatmaps_fetched": result.seatmaps_fetched,
            "provider_errors": list(result.provider_errors),
            "options": [option_to_dict(o) for o in result.options],
        }

    def _option(option_id: str):
        result = cache.get("last")
        if result is None:
            raise HTTPException(409, "no search has been run in this process")
        option = next((o for o in result.options if o.option_id == option_id), None)
        if option is None:
            raise HTTPException(404, "unknown option_id")
        return option

    # Declared before the JSON route on purpose. Starlette matches in
    # declaration order and `{option_id}` happily swallows a trailing ".svg",
    # so with the JSON route first this one was unreachable: every request for
    # an SVG got JSON back with an option_id nobody had, i.e. a 404.
    @app.get("/v1/seatmap/{option_id}.svg")
    def seatmap_svg(option_id: str) -> Response:
        option = _option(option_id)
        if option.auditorium is None:
            raise HTTPException(404, f"no seat map ({option.seat_data})")
        picked = {s.id for s in option.seats.seats} if option.seats else set()
        return Response(to_svg(option.auditorium, picked), media_type="image/svg+xml")

    @app.get("/v1/seatmap/{option_id}")
    def seatmap(option_id: str) -> dict:
        option = _option(option_id)
        if option.auditorium is None:
            raise HTTPException(404, f"no seat map ({option.seat_data})")
        picked = {s.id for s in option.seats.seats} if option.seats else set()
        return {"grid": to_unicode_grid(option.auditorium, picked),
                "seat_data": option.seat_data}

    @app.post("/v1/watches")
    def create_watch(body: WatchCreate, uid: str = Depends(user)) -> dict:
        watch_id = watches.create(
            spec_from_dict(body.spec), body.label, user_id=uid,
            cadence_s=body.cadence_s, webhook=body.webhook, seed=body.seed,
        )
        return {"watch_id": watch_id, "seeded": body.seed}

    @app.get("/v1/watches")
    def list_watches(uid: str = Depends(user)) -> list[dict]:
        return [
            {**w, "spec": spec_to_dict(spec_from_dict(json.loads(w["spec"])))}
            for w in watches.list(uid)
        ]

    @app.delete("/v1/watches/{watch_id}")
    def cancel_watch(watch_id: str) -> dict:
        if not watches.cancel(watch_id):
            raise HTTPException(404, "unknown watch_id")
        return {"cancelled": True}

    @app.post("/v1/watches/poll")
    def poll(uid: str = Depends(user), acknowledge: bool = True) -> dict:
        hits = watches.run_due(user_id=uid)
        pending = watches.pending(uid)
        if acknowledge:
            watches.acknowledge([h["hit_id"] for h in pending])
        return {"new_hits": len(hits), "hits": [h["payload"] for h in pending]}

    @app.get("/v1/booking-link/{option_id}")
    def booking_link(option_id: str) -> dict:
        link = search.booking_link(option_id)
        if not link:
            raise HTTPException(404, "no booking link for that option")
        return {"booking_link": link,
                "note": "screenwatch stops here; complete the purchase yourself"}

    return app


def default_app() -> FastAPI:
    from ..service.defaults import default_service

    search, watches, _store = default_service()
    return create_app(search, watches)
