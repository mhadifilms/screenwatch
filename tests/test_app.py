from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from screenwatch.api.app import create_app
from screenwatch.identity.work import Work, WorkRef
from screenwatch.models import Availability, Brand, Presentation, Projection
from screenwatch.ranking.candidate import Screening
from screenwatch.ranking.spec import DateWindow, SearchSpec
from screenwatch.seating.render import build_auditorium
from screenwatch.service.search import SearchService
from screenwatch.service.serde import spec_to_json
from screenwatch.service.store import Store
from screenwatch.service.venues import VenueDirectory
from screenwatch.service.watch import WatchService

WORK = Work(work_id="tmdb:app", title="Dune: Part Three", year=2026)


def _screening() -> Screening:
    when = datetime(2026, 8, 2, 20, tzinfo=UTC)
    return Screening(
        screening_id="amc:app-1",
        work=WORK,
        venue_id="amc-metreon-16",
        venue_name="AMC Metreon 16",
        chain="amc",
        starts_at_utc=when,
        starts_at_local=when.replace(tzinfo=None),
        presentation=Presentation(Projection.DIGITAL_LASER, Brand.IMAX, "1.43"),
        availability=Availability.SELLABLE,
        deeplink="https://example.test/app-1",
    )


class Provider:
    chain = "amc"

    def screenings(self, spec, venues, transport):
        return [_screening()]

    def fetch_seats(self, option, transport):
        return build_auditorium("amc-metreon-16", "app-1", ["......", "......"])


def _client():
    from fastapi.testclient import TestClient

    store = Store.memory()
    service = SearchService(
        [Provider()],
        store=store,
        directory=VenueDirectory(),
        transport=object(),
    )
    return TestClient(create_app(service, WatchService(service, store))), store


def test_local_app_and_data_endpoints_are_available():
    client, store = _client()
    assert client.get("/").status_code == 200
    assert "theater intelligence" in client.get("/").text
    openapi = client.get("/openapi.json").json()
    assert openapi["paths"]["/v1/search"]["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchSpecInput"
    }

    overview = client.get("/v1/analytics/overview").json()
    assert overview["directory"]["venues"] >= 1
    assert overview["providers"][0]["seat_data"] == "exact"

    venues = client.get("/v1/venues?sort=name").json()
    assert venues["venues"]
    assert {venue["type"] for venue in venues["venues"]}
    scoped = client.get("/v1/venues?chain=amc&type=multiplex").json()
    assert scoped["venues"]
    assert {venue["chain"] for venue in scoped["venues"]} == {"amc"}
    refreshed = client.post("/v1/venues/refresh", json={"chains": ["amc"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["provider_stats"][0]["status"] == "unsupported"
    assert client.get("/v1/venues/amc-metreon-16").status_code == 200
    store.close()


def test_search_id_persists_and_scopes_seat_map():
    client, store = _client()
    response = client.post(
        "/v1/search",
        json={
            "work": {"query": WORK.title},
            "party_size": 2,
            "date_window": {"start": "2026-08-02", "end": "2026-08-03"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["search_id"].startswith("search_")
    assert body["provider_stats"][0]["screenings"] == 1
    assert body["complete"] is True
    option_id = body["options"][0]["option_id"]

    session = client.get(f"/v1/search/{body['search_id']}").json()
    assert session["options"][0]["option_id"] == option_id
    seatmap = client.get(
        f"/v1/search/{body['search_id']}/seatmap/{option_id}"
    )
    assert seatmap.status_code == 200
    assert seatmap.json()["seat_data"] == "grid"
    assert store.recent_search_runs()[0]["run_id"] == body["search_id"]
    store.close()


def test_watch_replay_anchor_is_preserved_for_a_manual_run():
    store = Store.memory()
    service = SearchService(
        [Provider()],
        store=store,
        directory=VenueDirectory(),
        transport=object(),
    )
    watches = WatchService(service, store)
    watch_id = watches.create(
        SearchSpec(work=WorkRef(query=WORK.title)),
        "replay",
        seed=False,
        today=datetime(2026, 8, 2, tzinfo=UTC).date(),
    )
    row = store.get_watch(watch_id)
    assert "date_window" in row["spec"]
    assert watches.run(watch_id)  # the run uses the persisted replay window
    store.close()


def test_local_notification_poll_can_acknowledge_displayed_hits():
    client, store = _client()
    watch_id = "w-notification"
    store.create_watch(
        "w-notification",
        "notification test",
        spec_to_json(SearchSpec(
            work=WorkRef(query=WORK.title),
            date_window=DateWindow(datetime(2026, 8, 2).date(), datetime(2026, 8, 2).date()),
        )),
    )
    hit_id, _ = store.record_hit_status(
        watch_id, "canonical:test", {"title": WORK.title}, event_key="test-alert"
    )
    response = client.post("/v1/watches/poll?acknowledge=true")
    assert response.status_code == 200
    assert response.json()["hits"][0]["hit_id"] == hit_id
    assert client.get("/v1/notifications").json()["count"] == 0
    store.close()


@pytest.mark.asyncio
async def test_mcp_exposes_data_and_venue_intelligence():
    from screenwatch.mcp.server import build_server

    store = Store.memory()
    service = SearchService(
        [Provider()],
        store=store,
        directory=VenueDirectory(),
        transport=object(),
    )
    server = build_server(service, WatchService(service, store))

    overview_result = await server.call_tool("get_data_overview", {})
    overview = json.loads(overview_result.content[0].text)
    assert overview["providers"][0]["seat_data"] == "exact"

    venues_result = await server.call_tool("list_venues", {"query": "Metreon"})
    venues = json.loads(venues_result.content[0].text)
    assert venues["venues"][0]["id"] == "amc-metreon-16"

    venue_result = await server.call_tool("get_venue", {"venue_id": "amc-metreon-16"})
    venue = json.loads(venue_result.content[0].text)
    assert venue["seat_surface"] == "exact"

    refresh_result = await server.call_tool("refresh_venues", {"location": {}})
    refresh = json.loads(refresh_result.content[0].text)
    assert refresh["provider_stats"][0]["status"] == "unsupported"
    store.close()
