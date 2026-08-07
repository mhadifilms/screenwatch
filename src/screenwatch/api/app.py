"""Local-first HTTP API and browser app.

The API is deliberately a thin transport over the same services used by MCP.
Search results get a short-lived ``search_id`` so seat maps can be fetched
without relying on a single global "last result". The legacy routes remain in
place for small scripts and existing MCP clients.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..identity.normalize import analyze
from ..identity.work import WorkRef
from ..mcp.schemas import LocationInput, SearchSpecInput
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
from ..service.store import DEFAULT_USER
from ..service.watch import WatchService

UI_DIR = Path(__file__).resolve().parents[1] / "ui"
MAX_SEARCH_SESSIONS = 32


class WatchCreate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    spec: SearchSpecInput
    cadence_s: int = Field(300, ge=30, le=31 * 24 * 60 * 60)
    webhook: str | None = None
    seed: bool = True


class WatchRun(BaseModel):
    today: str | None = Field(None, description="Optional YYYY-MM-DD anchor for testing/replay")


def _result_payload(result: SearchResult, *, limit: int | None = None) -> dict:
    options = result.options if limit is None else result.options[:limit]
    return {
        "search_id": result.search_id,
        "narration": result.narrate(),
        "comparison": result.comparison(),
        "considered": result.considered,
        "seatmaps_fetched": result.seatmaps_fetched,
        "provider_errors": list(result.provider_errors),
        "clipped": list(result.clipped),
        "complete": result.complete,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_ms": result.duration_ms,
        "provider_stats": list(result.provider_stats),
        "unresolved_titles": list(result.unresolved_titles),
        "options": [option_to_dict(option) for option in options],
    }


def create_app(search: SearchService, watches: WatchService) -> FastAPI:
    app = FastAPI(
        title="Screenwatch",
        version="0.2.0",
        description=(
            "US theater and release intelligence: ranked showtimes, seat-aware "
            "options, durable watches, venue coverage, and a hard stop at booking."
        ),
    )
    observatory = Observatory(search, store=search.store, directory=search.directory)
    sessions: OrderedDict[str, SearchResult] = OrderedDict()
    session_users: dict[str, str] = {}

    def user(x_user_id: str = Header(default=DEFAULT_USER)) -> str:
        return x_user_id

    def remember(result: SearchResult, user_id: str = DEFAULT_USER) -> None:
        sessions[result.search_id] = result
        session_users[result.search_id] = user_id
        sessions.move_to_end(result.search_id)
        while len(sessions) > MAX_SEARCH_SESSIONS:
            expired, _ = sessions.popitem(last=False)
            session_users.pop(expired, None)

    def latest_session_id(user_id: str) -> str | None:
        return next(
            (
                candidate_id
                for candidate_id in reversed(sessions)
                if session_users.get(candidate_id) == user_id
            ),
            None,
        )

    def find_option(
        option_id: str,
        search_id: str | None = None,
        user_id: str = DEFAULT_USER,
    ):
        result = sessions.get(search_id) if search_id else None
        if result is not None and session_users.get(search_id) != user_id:
            result = None
            raise HTTPException(404, "unknown search_id")
        if result is None and search_id:
            raise HTTPException(410, "search session expired; run the search again")
        if result is None and sessions:
            session_id = latest_session_id(user_id)
            result = sessions.get(session_id) if session_id else None
        if result is None:
            raise HTTPException(409, "no search has been run in this process")
        option = next((item for item in result.options if item.option_id == option_id), None)
        if option is None:
            raise HTTPException(404, "unknown option_id")
        return option

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    @app.get("/assets/app.css", include_in_schema=False)
    def app_css() -> FileResponse:
        return FileResponse(UI_DIR / "app.css", media_type="text/css")

    @app.get("/assets/app.js", include_in_schema=False)
    def app_js() -> FileResponse:
        return FileResponse(UI_DIR / "app.js", media_type="text/javascript")

    @app.get("/assets/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(UI_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/v1/health")
    def health(uid: str = Depends(user)) -> dict:
        return {
            "ok": True,
            "service": "screenwatch",
            "providers": [p.chain for p in search.providers],
            "active_watches": len(watches.list(uid)),
        }

    @app.get("/v1/meta")
    def meta(uid: str = Depends(user)) -> dict:
        return observatory.overview(user_id=uid)

    @app.get("/v1/analytics/overview")
    def overview(uid: str = Depends(user)) -> dict:
        return observatory.overview(user_id=uid)

    @app.get("/v1/analytics/searches")
    def recent_searches(
        limit: int = Query(20, ge=1, le=100), uid: str = Depends(user)
    ) -> dict:
        return {"searches": observatory.recent_searches(limit=limit, user_id=uid)}

    @app.get("/v1/analytics/providers")
    def provider_health(
        limit: int = Query(100, ge=1, le=1000), uid: str = Depends(user)
    ) -> dict:
        return {"providers": search.store.provider_health(user_id=uid, limit=limit)}

    @app.get("/v1/venues")
    def venues(
        chain: str | None = None,
        type: str | None = None,
        q: str | None = None,
        sort: str = Query("distance", pattern="^(distance|name|type|chain)$"),
        limit: int = Query(200, ge=1, le=1000),
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict:
        origin = GeoPoint(lat, lon) if lat is not None and lon is not None else None
        rows = observatory.list_venues(
            chain=chain,
            venue_type=type,
            query=q,
            origin=origin,
            sort=sort,
            limit=limit,
        )
        return {"venues": rows, "total": len(rows), "types": search.directory.types()}

    @app.post("/v1/venues/refresh")
    def refresh_venues(location: LocationInput) -> dict:
        """Discover source venue metadata without fetching showtimes."""
        spec = SearchSpec(
            work=WorkRef(query="venue refresh"),
            location=location_from_dict(location.model_dump()),
        )
        return search.discover_venues(spec)

    @app.get("/v1/venues/{venue_id}")
    def venue_detail(venue_id: str, lat: float | None = None, lon: float | None = None) -> dict:
        origin = GeoPoint(lat, lon) if lat is not None and lon is not None else None
        record = observatory.get_venue(venue_id, origin=origin)
        if record is None:
            raise HTTPException(404, "unknown venue_id")
        return record

    @app.get("/v1/resolve")
    def resolve(query: str) -> dict:
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
            "candidates": [vars(candidate) for candidate in resolution.candidates],
        }

    @app.post("/v1/search")
    def do_search(spec: SearchSpecInput, uid: str = Depends(user)) -> dict:
        try:
            result = search.search(spec_from_dict(spec.to_dict()), user_id=uid)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        remember(result, uid)
        return _result_payload(result)

    @app.get("/v1/search/{search_id}")
    def get_search(search_id: str, uid: str = Depends(user)) -> dict:
        result = sessions.get(search_id)
        if result is not None and session_users.get(search_id) != uid:
            raise HTTPException(404, "unknown search_id")
        if result is None:
            persisted = search.store.get_search_run(search_id, user_id=uid)
            if persisted is None:
                raise HTTPException(404, "unknown search_id")
            return {"search_id": search_id, "persisted": persisted, "options": []}
        return _result_payload(result)

    @app.get("/v1/search/{search_id}/seatmap/{option_id}.svg")
    def search_seatmap_svg(
        search_id: str, option_id: str, uid: str = Depends(user)
    ) -> Response:
        option = find_option(option_id, search_id, uid)
        if option.auditorium is None:
            raise HTTPException(404, f"no seat map ({option.seat_data})")
        picked = {seat.id for seat in option.seats.seats} if option.seats else set()
        return Response(to_svg(option.auditorium, picked), media_type="image/svg+xml")

    @app.get("/v1/search/{search_id}/seatmap/{option_id}")
    def search_seatmap(
        search_id: str, option_id: str, uid: str = Depends(user)
    ) -> dict:
        option = find_option(option_id, search_id, uid)
        if option.auditorium is None:
            raise HTTPException(404, f"no seat map ({option.seat_data})")
        picked = {seat.id for seat in option.seats.seats} if option.seats else set()
        return {
            "search_id": search_id,
            "option_id": option_id,
            "grid": to_unicode_grid(option.auditorium, picked),
            "seat_data": option.seat_data,
        }

    # Legacy seat-map routes. They now accept an optional search_id query
    # parameter and remain useful for simple scripts.
    @app.get("/v1/seatmap/{option_id}.svg")
    def seatmap_svg(
        option_id: str,
        search_id: str | None = Query(None),
        uid: str = Depends(user),
    ) -> Response:
        option = find_option(option_id, search_id, uid)
        if option.auditorium is None:
            raise HTTPException(404, f"no seat map ({option.seat_data})")
        picked = {seat.id for seat in option.seats.seats} if option.seats else set()
        return Response(to_svg(option.auditorium, picked), media_type="image/svg+xml")

    @app.get("/v1/seatmap/{option_id}")
    def seatmap(
        option_id: str,
        search_id: str | None = Query(None),
        uid: str = Depends(user),
    ) -> dict:
        option = find_option(option_id, search_id, uid)
        if option.auditorium is None:
            raise HTTPException(404, f"no seat map ({option.seat_data})")
        picked = {seat.id for seat in option.seats.seats} if option.seats else set()
        return {
            "search_id": search_id or latest_session_id(uid),
            "option_id": option_id,
            "grid": to_unicode_grid(option.auditorium, picked),
            "seat_data": option.seat_data,
        }

    @app.post("/v1/watches")
    def create_watch(body: WatchCreate, uid: str = Depends(user)) -> dict:
        try:
            watch_id = watches.create(
                spec_from_dict(body.spec.to_dict()), body.label, user_id=uid,
                cadence_s=body.cadence_s, webhook=body.webhook, seed=body.seed,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"watch_id": watch_id, "seeded": body.seed}

    @app.get("/v1/watches")
    def list_watches(uid: str = Depends(user)) -> list[dict]:
        return [
            {**watch, "spec": spec_to_dict(spec_from_json(watch["spec"]))}
            for watch in watches.list(uid)
        ]

    @app.get("/v1/watches/{watch_id}")
    def get_watch(watch_id: str, uid: str = Depends(user)) -> dict:
        row = search.store.get_watch(watch_id)
        if row is None or row["user_id"] != uid:
            raise HTTPException(404, "unknown watch_id")
        return {**row, "spec": spec_to_dict(spec_from_json(row["spec"]))}

    @app.post("/v1/watches/{watch_id}/run")
    def run_watch(watch_id: str, body: WatchRun | None = None, uid: str = Depends(user)) -> dict:
        row = search.store.get_watch(watch_id)
        if row is None or row["user_id"] != uid:
            raise HTTPException(404, "unknown watch_id")
        today = None
        if body and body.today:
            from datetime import date

            try:
                today = date.fromisoformat(body.today)
            except ValueError as exc:
                raise HTTPException(422, "today must be YYYY-MM-DD") from exc
        hits = watches.run(watch_id, today=today)
        return {
            "watch_id": watch_id,
            "new_hits": len(hits),
            "hits": [hit.payload() for hit in hits],
        }

    @app.delete("/v1/watches/{watch_id}")
    def cancel_watch(watch_id: str, uid: str = Depends(user)) -> dict:
        if not watches.cancel(watch_id, user_id=uid):
            raise HTTPException(404, "unknown watch_id")
        return {"cancelled": True}

    @app.get("/v1/notifications")
    def notifications(uid: str = Depends(user)) -> dict:
        pending = watches.pending(uid)
        return {"count": len(pending), "hits": [hit["payload"] for hit in pending]}

    @app.post("/v1/watches/acknowledge")
    def acknowledge_hits(body: dict, uid: str = Depends(user)) -> dict:
        hit_ids = body.get("hit_ids") or []
        if not all(isinstance(hit_id, int) for hit_id in hit_ids):
            raise HTTPException(422, "hit_ids must be integers")
        watches.acknowledge(hit_ids, user_id=uid)
        return {"acknowledged": hit_ids}

    @app.post("/v1/watches/poll")
    def poll(uid: str = Depends(user), acknowledge: bool = False) -> dict:
        fresh = watches.run_due(user_id=uid)
        pending = watches.pending(uid)
        if acknowledge:
            watches.acknowledge([hit["hit_id"] for hit in pending], user_id=uid)
        return {"new_hits": len(fresh), "hits": [hit["payload"] for hit in pending]}

    @app.get("/v1/booking-link/{option_id}")
    def booking_link(
        option_id: str,
        search_id: str | None = Query(None),
        uid: str = Depends(user),
    ) -> dict:
        find_option(option_id, search_id, uid)
        link = search.booking_link(option_id)
        if not link:
            raise HTTPException(404, "no booking link for that option")
        return {
            "booking_link": link,
            "note": "screenwatch stops here; complete the purchase yourself",
        }

    @app.get("/v1/seat-history/{screening_id}")
    def seat_history(screening_id: str, limit: int = Query(20, ge=1, le=200)) -> dict:
        return {
            "screening_id": screening_id,
            "snapshots": search.store.seat_history(screening_id, limit),
        }

    return app


def app_for_db(db: str = "screenwatch.db") -> FastAPI:
    from ..service.defaults import default_service

    search, watches, _store = default_service(db)
    return create_app(search, watches)


def default_app() -> FastAPI:
    return app_for_db()
