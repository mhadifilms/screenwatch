from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

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


def _client(*, session_ttl_s=30 * 60, clock=None):
    from fastapi.testclient import TestClient

    store = Store.memory()
    service = SearchService(
        [Provider()],
        store=store,
        directory=VenueDirectory(),
        transport=object(),
    )
    app = create_app(
        service,
        WatchService(service, store),
        session_ttl_s=session_ttl_s,
        clock=clock,
    )
    return TestClient(app), store


def test_local_app_and_data_endpoints_are_available():
    client, store = _client()
    assert client.get("/").status_code == 200
    assert "theater intelligence" in client.get("/").text
    openapi = client.get("/openapi.json").json()
    assert openapi["info"]["version"] == "1.0.0"
    assert openapi["paths"]["/v1/search"]["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchSpecInput"
    }

    overview = client.get("/v1/analytics/overview").json()
    assert overview["directory"]["venues"] >= 1
    assert overview["providers"][0]["seat_data"] == "exact"
    assert overview["hardware"]["permanent_claims"] == 0
    assert overview["hardware"]["status"] == "observations-only"

    searched = client.post(
        "/v1/search",
        json={
            "work": {"query": WORK.title},
            "date_window": {"start": "2026-08-02", "end": "2026-08-03"},
        },
    )
    assert searched.status_code == 200
    assert searched.json()["coverage"] == "nearby"
    assert client.get("/v1/analytics/inventory?group_by=chain").json()["groups"]

    venues = client.get("/v1/venues?sort=name").json()
    assert venues["venues"]
    assert {venue["type"] for venue in venues["venues"]}
    scoped = client.get("/v1/venues?chain=amc&type=multiplex").json()
    assert scoped["venues"]
    assert {venue["chain"] for venue in scoped["venues"]} == {"amc"}
    refreshed = client.post("/v1/venues/refresh", json={"chains": ["amc"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["provider_stats"][0]["status"] == "unsupported"
    venue_detail = client.get("/v1/venues/amc-metreon-16").json()
    assert venue_detail["hardware"]["status"] == "observations-only"
    assert venue_detail["hardware"]["usable_for_inference"] is False
    assert venue_detail["capabilities"][0]["label"]
    assert venue_detail["evidence"]["presentations"]
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
    provider_health = client.get("/v1/analytics/providers").json()["providers"]
    assert provider_health[0]["chain"] == "amc"
    assert provider_health[0]["health"] == "healthy"
    assert body["options"][0]["starts_at_local_offset"] == "+00:00"
    assert body["options"][0]["source_listings"][0]["source"] == "amc"
    venue_inventory = client.get(
        "/v1/venues?chain=amc&q=Metreon"
    ).json()["venues"][0]["inventory"]
    assert venue_inventory["seat_screenings"] == 1
    assert venue_inventory["seats_capacity"] == 12
    analytics = client.get("/v1/analytics/inventory?group_by=chain").json()
    assert analytics["groups"][0]["group_key"] == "amc"
    assert analytics["groups"][0]["seat_coverage"] == 1.0
    option_id = body["options"][0]["option_id"]

    session = client.get(f"/v1/search/{body['search_id']}").json()
    assert session["options"][0]["option_id"] == option_id
    seatmap = client.get(
        f"/v1/search/{body['search_id']}/seatmap/{option_id}"
    )
    assert seatmap.status_code == 200
    assert seatmap.json()["seat_data"] == "grid"
    runway = client.post(
        f"/v1/search/{body['search_id']}/booking-runway/{option_id}",
        json={"party_size": 2, "transaction_limit": 1, "parallel_checkouts": 2},
    )
    assert runway.status_code == 200
    assert runway.json()["split"] == [1, 1]
    assert runway.json()["exact_seat_assignment"] is True
    assert {lane["profile"] for lane in runway.json()["lanes"]} == {
        "Checkout lane A", "Checkout lane B"
    }
    mismatch = client.post(
        f"/v1/search/{body['search_id']}/booking-runway/{option_id}",
        json={"party_size": 1, "transaction_limit": 1, "parallel_checkouts": 1},
    )
    assert mismatch.status_code == 422
    detail = client.get("/v1/venues/amc-metreon-16").json()
    assert detail["observed_rooms"][0]["capacity_max"] == 12
    assert detail["observed_rooms"][0]["rows"] == 2
    assert "unqueried rooms" in detail["room_caveat"]
    assert store.recent_search_runs()[0]["run_id"] == body["search_id"]
    assert client.get(
        f"/v1/search/{body['search_id']}", headers={"x-user-id": "another-user"}
    ).status_code == 404
    assert client.get(
        f"/v1/search/{body['search_id']}", headers={"x-user-id": "local"}
    ).status_code == 200
    health = client.get("/v1/health").json()
    assert health["search_sessions"]["live"] == 1
    assert health["search_sessions"]["ttl_s"] == 1800
    assert health["seating_optimizer_cache"]["maxsize"] == 128
    store.close()


def test_expired_search_session_retains_audit_record_but_not_heavy_seat_state():
    now = [100.0]
    client, store = _client(session_ttl_s=10, clock=lambda: now[0])
    response = client.post(
        "/v1/search",
        json={
            "work": {"query": WORK.title},
            "party_size": 2,
            "date_window": {"start": "2026-08-02", "end": "2026-08-03"},
        },
    ).json()
    search_id = response["search_id"]
    option_id = response["options"][0]["option_id"]

    now[0] += 11
    persisted = client.get(f"/v1/search/{search_id}").json()
    assert persisted["persisted"]["run_id"] == search_id
    assert persisted["options"] == []
    expired_map = client.get(f"/v1/search/{search_id}/seatmap/{option_id}")
    assert expired_map.status_code == 410
    assert client.get("/v1/health").json()["search_sessions"]["live"] == 0
    store.close()


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({"work": {"query": ""}}, "work needs"),
        (
            {"work": {"query": "Dune"}, "location": {"origin": {"lat": 95, "lon": 0}}},
            "less than or equal to 90",
        ),
        (
            {
                "work": {"query": "Dune"},
                "party_size": 2,
                "seating": {"wheelchair_spaces": 2, "companion_seats": 1},
            },
            "cannot exceed party_size",
        ),
        (
            {
                "work": {"query": "Dune"},
                "party_size": 2,
                "seating": {"relationships": [{"a": 0, "b": 2}]},
            },
            "exceeds party_size",
        ),
        (
            {"work": {"query": "Dune"}, "weights": {"seat_quality": -0.1}},
            "finite and non-negative",
        ),
    ],
)
def test_search_contract_rejects_impossible_or_unsafe_inputs(payload, fragment):
    client, store = _client()
    response = client.post("/v1/search", json=payload)

    assert response.status_code == 422
    assert fragment in response.text
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


def test_release_radar_persists_catalog_signals_until_the_source_changes():
    class SitemapTransport:
        def __init__(self):
            self.raw = (
                '<url><loc>https://www.amctheatres.com/movies/the-odyssey-76238</loc>'
                '<lastmod>2026-08-01T00:00:00Z</lastmod></url>'
            )

        def get(self, _url, **_kwargs):
            return SimpleNamespace(text=self.raw)

    store = Store.memory()
    transport = SitemapTransport()
    service = SearchService(
        [Provider()], store=store, directory=VenueDirectory(), transport=transport
    )
    watches = WatchService(service, store)
    from fastapi.testclient import TestClient

    client = TestClient(create_app(service, watches))
    signals_response = client.get("/v1/releases/signals?query=The%20Odyssey")
    assert signals_response.status_code == 200
    assert signals_response.json()["signals"][0]["movie_id"] == "76238"
    watch_id = watches.create(
        SearchSpec(
            work=WorkRef(query="The Odyssey"),
            release_radar=True,
            date_window=DateWindow(datetime(2026, 8, 2).date(), datetime(2026, 8, 3).date()),
        ),
        "catalog radar",
        today=datetime(2026, 8, 2).date(),
    )
    assert watches.run(watch_id, today=datetime(2026, 8, 2).date()) == []

    transport.raw = transport.raw.replace("2026-08-01", "2026-08-02")
    [hit] = watches.run(watch_id, today=datetime(2026, 8, 2).date())
    assert hit.alert_type == "release_signal_updated"
    assert hit.payload()["release_signal"]["movie_id"] == "76238"
    assert watches.run(watch_id, today=datetime(2026, 8, 2).date()) == []
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
    history = client.get(f"/v1/watches/{watch_id}/history").json()
    assert history["hits"][0]["hit_id"] == hit_id
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

    await server.call_tool("find_screenings", {"spec": {
        "work": {"query": WORK.title},
        "date_window": {"start": "2026-08-02", "end": "2026-08-02"},
    }})
    venues_result = await server.call_tool("list_venues", {"query": "Metreon"})
    venues = json.loads(venues_result.content[0].text)
    assert venues["venues"][0]["id"] == "amc-metreon-16"

    venue_result = await server.call_tool("get_venue", {"venue_id": "amc-metreon-16"})
    venue = json.loads(venue_result.content[0].text)
    assert venue["seat_surface"] == "exact"

    analytics_result = await server.call_tool("get_inventory_analytics", {"group_by": "chain"})
    analytics = json.loads(analytics_result.content[0].text)
    assert analytics["groups"]

    history_result = await server.call_tool("get_watch_history", {"watch_id": "missing"})
    assert json.loads(history_result.content[0].text)["error"] == "unknown watch_id"

    refresh_result = await server.call_tool("refresh_venues", {"location": {}})
    refresh = json.loads(refresh_result.content[0].text)
    assert refresh["provider_stats"][0]["status"] == "unsupported"
    store.close()
