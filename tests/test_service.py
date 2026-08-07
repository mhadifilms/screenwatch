"""Service-layer tests: persistence, spec round-tripping, watches, transports.

The two that matter most are the spec round trip (a watch replays a stored
spec days later, and losing a field there silently changes what it monitors)
and the seen-set (a watch must not page you about tickets that were already
on sale when you created it).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from screenwatch.identity.normalize import ProductKind
from screenwatch.identity.work import Method, TitleLink, Work, WorkRef
from screenwatch.models import (
    Attribute,
    Availability,
    Brand,
    Preference,
    Presentation,
    PresentationSpec,
    Projection,
)
from screenwatch.ranking.candidate import Screening
from screenwatch.ranking.spec import (
    Budget,
    DateWindow,
    GeoPoint,
    LocationSpec,
    Membership,
    SearchSpec,
    SeatingPrefs,
    TimeWindow,
    Weights,
)
from screenwatch.seating.model import SeatDataUnavailable
from screenwatch.seating.render import build_auditorium
from screenwatch.service.search import SearchService
from screenwatch.service.serde import (
    option_to_dict,
    spec_from_json,
    spec_to_dict,
    spec_to_json,
)
from screenwatch.service.store import Store
from screenwatch.service.venues import Venue, VenueDirectory
from screenwatch.service.watch import WatchService

WORK = Work(work_id="tmdb:1", title="Dune: Part Three", year=2026)


def screening(sid: str, hour: int = 20, *, venue="amc-metreon-16", day=2) -> Screening:
    when = datetime(2026, 8, day, hour, tzinfo=UTC)
    return Screening(
        screening_id=sid, work=WORK, venue_id=venue, venue_name=venue, chain="amc",
        starts_at_utc=when, starts_at_local=when.replace(tzinfo=None),
        presentation=Presentation(Projection.FILM_70MM_15PERF, Brand.IMAX, "1.43"),
        availability=Availability.SELLABLE,
        deeplink=f"https://example.test/{sid}",
    )


class FakeProvider:
    chain = "amc"

    def __init__(self, screenings, auditorium=None):
        self._screenings = list(screenings)
        self._auditorium = auditorium

    def screenings(self, spec, venues, transport):
        return list(self._screenings)

    def fetch_seats(self, option, transport):
        if self._auditorium is None:
            raise SeatDataUnavailable("no seats here")
        return self._auditorium


@pytest.fixture
def store():
    s = Store.memory()
    yield s
    s.close()


# --------------------------------------------------------------------------
class TestSpecRoundTrip:
    @pytest.fixture
    def rich_spec(self):
        return SearchSpec(
            work=WorkRef(query="dune part three"),
            party_size=4,
            location=LocationSpec(
                origin=GeoPoint(37.78, -122.40), radius_km=65,
                allow=frozenset({"amc-metreon-16"}), deny=frozenset({"amc-empire-25"}),
                chains=frozenset({"amc", "independent"}),
                venue_types=frozenset({"multiplex", "art_house"}),
            ),
            date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 9)),
            time_windows=(TimeWindow(start=time(18), end=time(2)),),
            presentations=Preference([
                PresentationSpec(projection=Projection.FILM_70MM_15PERF,
                                 brand=Brand.IMAX, label="IMAX 70mm"),
                PresentationSpec(requires=frozenset({Attribute.OPEN_CAPTION}),
                                 excludes=frozenset({Attribute.THREE_D}), label="OC 2D"),
            ]),
            memberships=frozenset({Membership.AMC_ALIST}),
            seating=SeatingPrefs(allow_split=False, avoid_front_rows=4,
                                 ideal_depth=0.66, wheelchair_spaces=1),
            budget=Budget(max_total_usd=120, max_per_ticket_usd=32),
            weights=Weights(format_fit=2.0, group_cohesion=0.5),
            include_sold_out=True,
            max_seatmap_fetches=4,
        )

    def test_survives_json(self, rich_spec):
        """A watch stores this and replays it days later. Any field lost here
        silently changes what the watch monitors."""
        assert spec_to_dict(spec_from_json(spec_to_json(rich_spec))) == spec_to_dict(rich_spec)

    def test_preserves_the_wrapping_time_window(self, rich_spec):
        back = spec_from_json(spec_to_json(rich_spec))
        assert back.time_windows[0].wraps
        assert back.admits_time(datetime(2026, 8, 3, 0, 45))

    def test_preserves_format_ranking_order(self, rich_spec):
        back = spec_from_json(spec_to_json(rich_spec))
        imax = Presentation(Projection.FILM_70MM_15PERF, Brand.IMAX, "1.43")
        assert back.presentations.rank(imax) == 0

    def test_preserves_weights_and_memberships(self, rich_spec):
        back = spec_from_json(spec_to_json(rich_spec))
        assert back.weights.format_fit == 2.0
        assert back.covered_by_membership("amc")

    def test_minimal_spec_round_trips(self):
        spec = SearchSpec(work=WorkRef(query="x"))
        assert spec_to_dict(spec_from_json(spec_to_json(spec))) == spec_to_dict(spec)

    def test_open_ticket_watch_uses_rolling_31_day_horizon(self):
        spec = SearchSpec(work=WorkRef(query="dune"), include_sold_out=True)
        assert spec.window(date(2026, 8, 2)) == DateWindow(
            date(2026, 8, 2), date(2026, 9, 2)
        )


# --------------------------------------------------------------------------
class TestStore:
    def test_directory_venues_survive_restart(self, tmp_path):
        path = tmp_path / "directory.db"
        first = Store(path)
        first.put_directory_venues([
            Venue(
                venue_id="regal-test", name="Regal Test", chain="regal",
                city="Testville", state="CA", venue_type="multiplex",
            )
        ])
        first.close()

        second = Store(path)
        rows = second.directory_venues()
        assert rows[0]["venue_id"] == "regal-test"
        assert rows[0]["city"] == "Testville"
        service = SearchService([], store=second, directory=VenueDirectory())
        assert service.directory.get("regal-test").city == "Testville"
        second.close()

    def test_work_and_link_persist(self, store):
        store.put_work(WORK)
        store.put_link(TitleLink("amc", "1", "Dune: Part Three", "tmdb:1",
                                 Method.TMDB_EXACT, 0.95, kind=ProductKind.FEATURE))
        assert store.get_work("tmdb:1").title == "Dune: Part Three"
        assert store.get_link("amc", "1")["confidence"] == 0.95

    def test_links_needing_review_are_queryable(self, store):
        store.put_link(TitleLink("amc", "1", "Mystery", None, Method.UNRESOLVED, 0.3))
        store.put_link(TitleLink("amc", "2", "Certain", "tmdb:9", Method.TMDB_EXACT, 0.95))
        assert [r["source_movie_id"] for r in store.links_needing_review()] == ["1"]

    def test_upsert_screening_is_idempotent(self, store):
        for _ in range(3):
            store.upsert_screening("amc:1", work_id="tmdb:1", venue_id="v", chain="amc",
                                   starts_at_utc=datetime.now(UTC),
                                   presentation="IMAX", availability="sellable",
                                   deeplink="https://x")
        rows = store._conn.execute("SELECT COUNT(*) c FROM screenings").fetchone()
        assert rows["c"] == 1

    def test_prefs_round_trip(self, store):
        store.set_pref("home", {"lat": 1.0, "lon": 2.0})
        assert store.get_pref("home")["lat"] == 1.0
        assert store.get_pref("missing", "fallback") == "fallback"

    def test_alert_event_key_is_idempotent(self, store):
        store.create_watch("w1", "x", "{}")
        first = store.record_hit_status(
            "w1", "amc:1", {"alert_type": "new_screening"},
            event_key="new_screening:amc:1",
        )
        second = store.record_hit_status(
            "w1", "amc:1", {"alert_type": "new_screening"},
            event_key="new_screening:amc:1",
        )

        assert first[0] == second[0]
        assert second[1] is False
        assert store._conn.execute("SELECT COUNT(*) FROM watch_hits").fetchone()[0] == 1

    def test_existing_watch_database_migrates_alert_columns(self, tmp_path):
        import sqlite3

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE watches (
                watch_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                label TEXT NOT NULL, spec TEXT NOT NULL,
                cadence_s INTEGER NOT NULL DEFAULT 300,
                active INTEGER NOT NULL DEFAULT 1, webhook TEXT,
                created_at TEXT NOT NULL, last_run TEXT, last_hit TEXT
            );
            CREATE TABLE watch_hits (
                hit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id TEXT NOT NULL, screening_id TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.close()

        store = Store(path)
        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(watches)")
        }
        hit_columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(watch_hits)")
        }
        assert {"last_success", "last_error", "last_warning", "error_count"} <= columns
        assert "event_key" in hit_columns
        store.close()


# --------------------------------------------------------------------------
class TestSeenSet:
    def test_seeding_suppresses_what_is_already_on_sale(self, store):
        store.create_watch("w1", "dune 70mm", "{}")
        store.seed_seen("w1", ["amc:1", "amc:2"])
        assert store.unseen("w1", ["amc:1", "amc:2"]) == []

    def test_only_genuinely_new_screenings_come_back(self, store):
        store.create_watch("w1", "dune 70mm", "{}")
        store.seed_seen("w1", ["amc:1"])
        assert store.unseen("w1", ["amc:1", "amc:2", "amc:3"]) == ["amc:2", "amc:3"]

    def test_a_screening_only_fires_once_ever(self, store):
        store.create_watch("w1", "x", "{}")
        fresh = store.unseen("w1", ["amc:1"])
        store.mark_seen("w1", fresh)
        assert store.unseen("w1", ["amc:1"]) == []

    def test_empty_input_is_handled(self, store):
        store.create_watch("w1", "x", "{}")
        assert store.unseen("w1", []) == []


# --------------------------------------------------------------------------
class TestSearchService:
    def build(self, screenings, auditorium=None, store=None):
        provider = FakeProvider(screenings, auditorium)
        return SearchService(
            [provider],
            store=store or Store.memory(),
            directory=VenueDirectory(),
            transport=object(),
        )

    def test_end_to_end_search_ranks_and_explains(self):
        service = self.build([screening("amc:1", 20), screening("amc:2", 23)])
        spec = SearchSpec(work=WorkRef(query="dune"),
                          date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)))
        result = service.search(spec, today=date(2026, 8, 2))
        assert result.options and result.best.reasons is not None
        assert "1." in result.narrate()

    def test_query_filters_to_the_requested_film(self):
        other = screening("amc:9")
        object.__setattr__(other, "work", Work(work_id="tmdb:2", title="Wicked"))
        service = self.build([screening("amc:1"), other])
        result = service.search(
            SearchSpec(work=WorkRef(query="dune"),
                       date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2))),
            today=date(2026, 8, 2),
        )
        assert [o.screening.screening_id for o in result.options] == ["amc:1"]

    def test_provider_failure_is_surfaced_not_swallowed(self):
        class Broken:
            chain = "amc"
            def screenings(self, spec, venues, transport):
                raise ConnectionError("chain down")
            def fetch_seats(self, option, transport):
                raise SeatDataUnavailable("n/a")

        service = SearchService([Broken()], store=Store.memory(),
                                directory=VenueDirectory(), transport=object())
        result = service.search(SearchSpec(work=WorkRef(query="dune")),
                                today=date(2026, 8, 2))
        assert result.options == []
        assert result.provider_errors and "chain down" in result.provider_errors[0]

    def test_venue_refresh_persists_discovery_and_respects_chain_scope(self):
        class Discovering:
            chain = "regal"

            def __init__(self):
                self.calls = 0

            def discover(self, spec):
                self.calls += 1
                return [Venue(
                    venue_id="regal-discovered",
                    name="Regal Discovered",
                    chain=self.chain,
                )]

        store = Store.memory()
        provider = Discovering()
        service = SearchService([provider], store=store,
                                directory=VenueDirectory(), transport=object())
        out_of_scope = service.discover_venues(
            SearchSpec(work=WorkRef(query="venue refresh"),
                       location=LocationSpec(chains=frozenset({"amc"})))
        )
        assert provider.calls == 0
        assert out_of_scope["provider_stats"][0]["status"] == "not_in_scope"

        in_scope = service.discover_venues(
            SearchSpec(work=WorkRef(query="venue refresh"),
                       location=LocationSpec(chains=frozenset({"regal"})))
        )
        assert provider.calls == 1
        assert in_scope["discovered"] == 1
        assert store.directory_venues()[0]["venue_id"] == "regal-discovered"
        store.close()

    def test_missing_seat_data_degrades_rather_than_failing(self):
        service = self.build([screening("amc:1")])
        result = service.search(
            SearchSpec(work=WorkRef(query="dune"),
                       date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2))),
            today=date(2026, 8, 2),
        )
        assert result.best.seat_data == "unavailable"
        assert result.seatmaps_fetched == 0
        assert result.best.score > 0

    def test_seat_data_is_used_when_available(self):
        room = build_auditorium("amc-metreon-16", "1", ["××××", "×..×", "...."])
        service = self.build([screening("amc:1")], auditorium=room)
        result = service.search(
            SearchSpec(work=WorkRef(query="dune"), party_size=2,
                       date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2))),
            today=date(2026, 8, 2),
        )
        assert result.best.seat_data == "grid"
        assert result.best.can_seat_party is True
        assert result.seatmaps_fetched == 1

    def test_booking_link_is_the_last_step(self):
        store = Store.memory()
        service = self.build([screening("amc:1")], store=store)
        result = service.search(
            SearchSpec(work=WorkRef(query="dune"),
                       date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2))),
            today=date(2026, 8, 2),
        )
        assert service.booking_link(result.best.option_id) == "https://example.test/amc:1"

    def test_option_wire_shape_is_complete(self):
        service = self.build([screening("amc:1")])
        result = service.search(
            SearchSpec(work=WorkRef(query="dune"),
                       date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2))),
            today=date(2026, 8, 2),
        )
        payload = option_to_dict(result.best)
        for key in ("option_id", "score", "title", "venue", "presentation",
                    "components", "reasons", "tradeoffs", "booking_link"):
            assert key in payload


# --------------------------------------------------------------------------
class TestWatches:
    def build(self, screenings):
        store = Store.memory()
        service = SearchService([FakeProvider(screenings)], store=store,
                                directory=VenueDirectory(), transport=object())
        return service, WatchService(service, store), store

    def spec(self):
        return SearchSpec(work=WorkRef(query="dune"),
                          date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 3)))

    def test_a_seeded_watch_is_quiet_about_existing_tickets(self):
        """The Dune-3 case: tickets already dropped, only tell me about new
        ones."""
        service, watches, _ = self.build([screening("amc:1")])
        wid = watches.create(self.spec(), "dune 70mm", today=date(2026, 8, 2))
        assert watches.run(wid, today=date(2026, 8, 2)) == []

    def test_it_fires_when_a_new_screening_appears(self):
        service, watches, _ = self.build([screening("amc:1")])
        wid = watches.create(self.spec(), "dune 70mm", today=date(2026, 8, 2))

        service.providers[0]._screenings.append(screening("amc:2", 22))
        hits = watches.run(wid, today=date(2026, 8, 2))
        assert [h.option.screening.screening_id for h in hits] == ["amc:2"]

    def test_it_does_not_fire_twice_for_the_same_screening(self):
        service, watches, _ = self.build([screening("amc:1")])
        wid = watches.create(self.spec(), "x", today=date(2026, 8, 2))
        service.providers[0]._screenings.append(screening("amc:2", 22))
        watches.run(wid, today=date(2026, 8, 2))
        assert watches.run(wid, today=date(2026, 8, 2)) == []

    def test_unseeded_watch_reports_everything_immediately(self):
        service, watches, _ = self.build([screening("amc:1")])
        wid = watches.create(self.spec(), "x", seed=False, today=date(2026, 8, 2))
        assert len(watches.run(wid, today=date(2026, 8, 2))) == 1

    def test_hits_persist_before_delivery(self):
        service, watches, store = self.build([screening("amc:1")])
        wid = watches.create(self.spec(), "x", seed=False, today=date(2026, 8, 2))
        watches.run(wid, today=date(2026, 8, 2))
        pending = watches.pending()
        assert pending and pending[0]["payload"]["booking_link"]
        watches.acknowledge([h["hit_id"] for h in pending])
        assert watches.pending() == []

    def test_cancelled_watch_stops_running(self):
        service, watches, _ = self.build([screening("amc:1")])
        wid = watches.create(self.spec(), "x", seed=False, today=date(2026, 8, 2))
        assert watches.cancel(wid)
        assert watches.run(wid, today=date(2026, 8, 2)) == []

    def test_listed_watches_expose_their_spec(self):
        service, watches, _ = self.build([screening("amc:1")])
        watches.create(self.spec(), "dune 70mm", today=date(2026, 8, 2))
        [row] = watches.list()
        assert row["label"] == "dune 70mm"
        assert spec_from_json(row["spec"]).work.query == "dune"

    def test_alert_payload_explains_a_new_drop(self):
        _service, watches, _ = self.build([screening("amc:1")])
        watch_id = watches.create(
            self.spec(), "x", seed=False, today=date(2026, 8, 2)
        )

        [hit] = watches.run(watch_id, today=date(2026, 8, 2))
        payload = hit.payload()

        assert payload["alert_type"] == "new_screening"
        assert payload["priority"] > 0
        assert payload["current"]["screening_id"] == "amc:1"
        assert payload["delta"] is None
        assert payload["booking_link"]
        assert payload["observed_at"]

    def test_existing_screening_alerts_when_a_party_can_now_fit(self):
        service, watches, _ = self.build([screening("amc:1")])
        watch_id = watches.create(self.spec(), "x", today=date(2026, 8, 2))

        # The first search had no seat surface. A later poll exposes two free
        # seats for the same screening; that is news even though its id did
        # not change.
        service.providers[0]._auditorium = build_auditorium(
            "amc-metreon-16", "1", [".."]
        )
        [hit] = watches.run(watch_id, today=date(2026, 8, 2))

        assert hit.alert_type == "party_fits"
        assert "seat_map_available" in hit.changes
        assert hit.previous["seat_data"] == "unavailable"
        payload = hit.payload()
        assert payload["current"]["seat_data"] == "grid"
        assert payload["delta"]["seat_data"] == {
            "from": "unavailable", "to": "grid"
        }

    def test_sold_out_showing_alerts_when_tickets_return(self):
        sold = replace(screening("amc:1"), availability=Availability.SOLD_OUT)
        service, watches, store = self.build([sold])
        watch_id = watches.create(self.spec(), "x", today=date(2026, 8, 2))
        assert spec_from_json(store.get_watch(watch_id)["spec"]).include_sold_out

        service.providers[0]._screenings = [screening("amc:1")]
        [hit] = watches.run(watch_id, today=date(2026, 8, 2))

        assert hit.alert_type == "tickets_returned"
        assert hit.payload()["delta"]["availability"] == {
            "from": "sold_out", "to": "sellable"
        }

    def test_tickets_return_again_after_a_second_sellout_cycle(self):
        sold = replace(screening("amc:1"), availability=Availability.SOLD_OUT)
        service, watches, _ = self.build([sold])
        watch_id = watches.create(self.spec(), "x", today=date(2026, 8, 2))

        service.providers[0]._screenings = [screening("amc:1")]
        [first] = watches.run(watch_id, today=date(2026, 8, 2))
        assert first.alert_type == "tickets_returned"

        service.providers[0]._screenings = [sold]
        assert watches.run(watch_id, today=date(2026, 8, 2)) == []

        service.providers[0]._screenings = [screening("amc:1")]
        [second] = watches.run(watch_id, today=date(2026, 8, 2))
        assert second.alert_type == "tickets_returned"

    def test_state_calls_out_nearly_sold_out_inventory(self):
        nearly = replace(
            screening("amc:1"), seats_available=5, seats_capacity=100
        )
        service, watches, _ = self.build([nearly])
        watch_id = watches.create(
            self.spec(), "x", seed=False, today=date(2026, 8, 2)
        )

        [hit] = watches.run(watch_id, today=date(2026, 8, 2))
        payload = hit.payload()
        assert payload["inventory_status"] == "nearly_sold_out"
        assert payload["current"]["availability_status"] == "nearly_sold_out"
        assert payload["current"]["seat_position"] == "unknown"

    def test_middle_seat_change_is_a_first_class_alert(self):
        initial = build_auditorium("amc-metreon-16", "1", ["....", "××××", "...."])
        service, watches, _ = self.build([screening("amc:1")])
        service.providers[0]._auditorium = initial
        watch_id = watches.create(self.spec(), "x", today=date(2026, 8, 2))

        service.providers[0]._auditorium = build_auditorium(
            "amc-metreon-16", "1", ["××××", "....", "××××"]
        )
        [hit] = watches.run(watch_id, today=date(2026, 8, 2))

        assert hit.alert_type == "middle_seats_available"
        assert "middle_seats_available" in hit.changes
        payload = hit.payload()
        assert payload["previous"]["seat_position"] == "no_middle_seats"
        assert payload["current"]["seat_position"] == "middle_area"
        assert payload["seat_position"] == "middle_area"

    def test_webhook_success_does_not_consume_poll_alerts(self):
        calls = []

        def sender(url, body, request):
            calls.append((url, body, request.headers))

        service, _old_watches, store = self.build([screening("amc:1")])
        watches = WatchService(
            service, store, webhook_sender=sender,
        )
        watch_id = watches.create(
            self.spec(), "x", seed=False,
            webhook="https://alerts.example.test/hook",
            today=date(2026, 8, 2),
        )

        watches.run(watch_id, today=date(2026, 8, 2))

        assert len(calls) == 1
        assert calls[0][1]["event"] == "screenwatch.alerts.v1"
        assert calls[0][1]["hits"][0]["hit_id"]
        assert calls[0][1]["hits"][0]["delivery_attempt"] == 1
        assert any(
            key.lower() == "x-screenwatch-idempotency-key"
            for key in calls[0][2]
        )
        assert watches.pending()
        assert watches.pending()[0]["payload"]["hit_id"] == watches.pending()[0]["hit_id"]
        assert store.pending_webhook_hits(watch_id) == []

        watches.acknowledge([watches.pending()[0]["hit_id"]])
        assert watches.pending() == []

    def test_failed_webhook_is_retried_with_durable_backoff(self):
        clock = [datetime(2026, 8, 2, 12, 0, tzinfo=UTC)]
        attempts = []

        def sender(_url, _body, _request):
            attempts.append(clock[0])
            if len(attempts) == 1:
                raise RuntimeError("receiver offline")

        service, _old_watches, store = self.build([screening("amc:1")])
        watches = WatchService(
            service, store, webhook_sender=sender, clock=lambda: clock[0]
        )
        watch_id = watches.create(
            self.spec(), "x", seed=False,
            webhook="https://alerts.example.test/hook",
            today=date(2026, 8, 2),
        )

        watches.run(watch_id, today=date(2026, 8, 2))
        assert len(attempts) == 1
        assert store.pending_webhook_hits(watch_id, now=clock[0].isoformat()) == []
        delivery = store._conn.execute(
            "SELECT attempts, delivered_at, next_attempt_at, last_error "
            "FROM watch_deliveries"
        ).fetchone()
        assert delivery["attempts"] == 1
        assert delivery["delivered_at"] is None
        assert "receiver offline" in delivery["last_error"]

        clock[0] += timedelta(seconds=61)
        watches.deliver_due()
        assert len(attempts) == 2
        assert store.pending_webhook_hits(watch_id, now=clock[0].isoformat()) == []

    def test_watch_health_records_failures_and_recovers(self):
        class FlakySearch:
            def __init__(self, store):
                self.store = store
                self.fail = True
                self.warn = False

            def search(self, spec, *, today=None):
                if self.fail:
                    raise RuntimeError("upstream unavailable")
                return type(
                    "Result",
                    (),
                    {
                        "options": [],
                        "provider_errors": ("regal: partial outage",) if self.warn else (),
                        "clipped": (),
                    },
                )()

        store = Store.memory()
        service = FlakySearch(store)
        watches = WatchService(service, store)
        watch_id = watches.create(
            self.spec(), "x", seed=False, today=date(2026, 8, 2)
        )

        with pytest.raises(RuntimeError, match="upstream unavailable"):
            watches.run(watch_id, today=date(2026, 8, 2))
        assert store.get_watch(watch_id)["error_count"] == 1
        assert "upstream unavailable" in store.get_watch(watch_id)["last_error"]

        service.fail = False
        watches.run(watch_id, today=date(2026, 8, 2))
        health = store.get_watch(watch_id)
        assert health["error_count"] == 0
        assert health["last_error"] is None
        assert health["last_success"]

        service.warn = True
        watches.run(watch_id, today=date(2026, 8, 2))
        assert store.get_watch(watch_id)["last_warning"] == "regal: partial outage"


# --------------------------------------------------------------------------
class TestVenueDirectory:
    def test_radius_filters(self):
        d = VenueDirectory()
        sf = LocationSpec(origin=GeoPoint(37.78, -122.40), radius_km=30)
        ids = {v.venue_id for v in d.matching(sf)}
        assert "amc-metreon-16" in ids
        assert "amc-lincoln-square-13" not in ids

    def test_explicit_allow_beats_the_radius(self):
        """Naming a venue means you want it even if it is a flight away -
        the 70mm pilgrimage case."""
        d = VenueDirectory()
        spec = LocationSpec(origin=GeoPoint(37.78, -122.40), radius_km=5,
                            allow=frozenset({"amc-lincoln-square-13"}))
        assert d.matching(spec)[0].venue_id == "amc-lincoln-square-13"

    def test_deny_wins_over_allow(self):
        d = VenueDirectory()
        spec = LocationSpec(allow=frozenset({"amc-metreon-16"}),
                            deny=frozenset({"amc-metreon-16"}))
        assert "amc-metreon-16" not in {v.venue_id for v in d.matching(spec)}

    def test_chain_filter(self):
        d = VenueDirectory()
        chains = {v.chain for v in d.matching(LocationSpec(), chain="independent")}
        assert chains == {"independent"}

    def test_search_location_filters_by_chain_and_type(self):
        d = VenueDirectory()
        spec = LocationSpec(
            chains=frozenset({"independent"}),
            venue_types=frozenset({"independent"}),
        )
        rows = d.matching(spec)
        assert rows
        assert {venue.chain for venue in rows} == {"independent"}
        assert {venue.venue_type for venue in rows} == {"independent"}

    def test_discovered_chain_gets_a_directory_type(self):
        d = VenueDirectory()
        d.register([Venue(venue_id="regal-new", name="Regal New", chain="regal")])
        assert d.get("regal-new").venue_type == "multiplex"

    def test_city_filter_admits_coordinate_less_city_records(self):
        d = VenueDirectory({
            "c360-cambridge": {
                "name": "Apple Cinemas Cambridge",
                "chain": "c360",
                "city": "Cambridge",
                "venue_type": "multiplex",
            },
            "c360-white-plains": {
                "name": "Apple Cinemas White Plains",
                "chain": "c360",
                "city": "White Plains",
                "venue_type": "multiplex",
            },
        })
        rows = d.matching(LocationSpec(city="Cambridge"))
        assert [venue.venue_id for venue in rows] == ["c360-cambridge"]


# --------------------------------------------------------------------------
class TestTransportsAgree:
    def test_api_exposes_every_route(self):
        from screenwatch.api.app import create_app

        store = Store.memory()
        service = SearchService([FakeProvider([screening("amc:1")])], store=store,
                                directory=VenueDirectory(), transport=object())
        app = create_app(service, WatchService(service, store))
        routes = {r.path for r in app.routes}
        for path in ("/v1/search", "/v1/watches", "/v1/resolve",
                     "/v1/watches/acknowledge",
                     "/v1/seatmap/{option_id}", "/v1/booking-link/{option_id}",
                     "/v1/seatmap/{option_id}.svg"):
            assert path in routes

    def test_the_svg_route_is_actually_reachable(self):
        """Route registration is not route matching.

        `{option_id}` swallows a trailing ".svg", so with the JSON route
        declared first every SVG request was answered by the JSON handler
        looking for an option whose id ended in ".svg" - a 404 that looked
        like a missing option rather than a shadowed route.
        """
        from fastapi.testclient import TestClient

        from screenwatch.api.app import create_app

        store = Store.memory()
        room = build_auditorium("amc-metreon-16", "1", ["××....", "......"])
        service = SearchService([FakeProvider([screening("amc:1")], room)],
                                store=store, directory=VenueDirectory(),
                                transport=object())
        client = TestClient(create_app(service, WatchService(service, store)))
        searched = client.post(
            "/v1/search",
            json={"work": {"query": WORK.title}, "party_size": 2,
                  "date_window": {"start": "2026-08-02", "end": "2026-08-03"}},
        )
        assert searched.status_code == 200
        option_id = searched.json()["options"][0]["option_id"]

        svg = client.get(f"/v1/seatmap/{option_id}.svg")
        assert svg.status_code == 200
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert svg.text.startswith("<svg")

        grid = client.get(f"/v1/seatmap/{option_id}")
        assert grid.status_code == 200
        assert "grid" in grid.json()

    def test_api_poll_is_safe_until_alerts_are_acknowledged(self):
        from fastapi.testclient import TestClient

        from screenwatch.api.app import create_app

        store = Store.memory()
        service = SearchService(
            [FakeProvider([screening("amc:1")])],
            store=store,
            directory=VenueDirectory(),
            transport=object(),
        )
        watches = WatchService(service, store)
        watch_id = watches.create(
            SearchSpec(work=WorkRef(query=WORK.title)),
            "x",
            seed=False,
            today=date(2026, 8, 2),
        )
        client = TestClient(create_app(service, watches))

        first = client.post("/v1/watches/poll")
        hit_id = first.json()["hits"][0]["hit_id"]
        assert first.status_code == 200
        assert client.post("/v1/watches/poll").json()["hits"][0]["hit_id"] == hit_id

        acknowledged = client.post(
            "/v1/watches/acknowledge", json={"hit_ids": [hit_id]}
        )
        assert acknowledged.json() == {"acknowledged": [hit_id]}
        assert client.post("/v1/watches/poll").json()["hits"] == []
        assert store.get_watch(watch_id)["last_success"]


class TestMcpTools:
    """Exercised through the real MCP dispatch path, not by calling the
    Python functions directly - schema generation and argument coercion are
    exactly where a hand-written tool surface breaks."""

    ROOM = ["××××××××", "××....××", "........"]

    @pytest.fixture
    def server(self):
        from screenwatch.mcp.server import build_server

        store = Store.memory()
        service = SearchService(
            [FakeProvider([screening("amc:1", 20), screening("amc:2", 23)],
                          build_auditorium("amc-metreon-16", "1", self.ROOM))],
            store=store, directory=VenueDirectory(), transport=object(),
        )
        return build_server(service, WatchService(service, store))

    @staticmethod
    async def call(server, name, args):
        result = await server.call_tool(name, args)
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @pytest.mark.asyncio
    async def test_all_tools_are_registered(self, server):
        names = {t.name for t in await server.list_tools()}
        assert names == {
            "resolve_title", "find_screenings", "get_seatmap", "explain_ranking",
            "create_watch", "list_watches", "cancel_watch", "poll_watches",
            "acknowledge_hits", "get_booking_link", "get_data_overview",
            "list_venues", "get_venue", "refresh_venues", "get_inventory_analytics",
            "find_release_signals", "get_watch_history",
        }

    @pytest.mark.asyncio
    async def test_spec_schema_covers_every_documented_param(self, server):
        """Location, party size, format, dates, times, memberships, seating -
        all of it has to be reachable from the tool schema or an MCP client
        cannot express the search."""
        tool = next(t for t in await server.list_tools() if t.name == "find_screenings")
        schema = tool.input_schema
        props = (schema.get("$defs", {}).get("SearchSpecInput", {}).get("properties")
                 or schema["properties"])
        assert {"work", "party_size", "location", "date_window", "time_windows",
                "presentations", "memberships", "seating", "budget",
                "weights"} <= set(props)

    @pytest.mark.asyncio
    async def test_find_screenings_returns_ranked_options_with_rationale(self, server):
        out = await self.call(server, "find_screenings", {"spec": {
            "work": {"query": "dune"}, "party_size": 2,
            "date_window": {"start": "2026-08-02", "end": "2026-08-02"},
            "time_windows": [{"start": "18:00", "end": "02:00"}],
        }})
        assert out["options"]
        top = out["options"][0]
        assert top["seats"]["together"] is True
        assert top["reasons"] and "narration" in out

    @pytest.mark.asyncio
    async def test_seatmap_tool_renders_the_recommended_seats(self, server):
        out = await self.call(server, "find_screenings", {"spec": {
            "work": {"query": "dune"}, "party_size": 2,
            "date_window": {"start": "2026-08-02", "end": "2026-08-02"}}})
        grid = await self.call(server, "get_seatmap",
                               {"option_id": out["options"][0]["option_id"]})
        assert "SCREEN" in grid and "▮" in grid

    @pytest.mark.asyncio
    async def test_seatmap_svg_variant(self, server):
        out = await self.call(server, "find_screenings", {"spec": {
            "work": {"query": "dune"},
            "date_window": {"start": "2026-08-02", "end": "2026-08-02"}}})
        svg = await self.call(server, "get_seatmap",
                              {"option_id": out["options"][0]["option_id"],
                               "format": "svg"})
        assert svg.startswith("<svg")

    @pytest.mark.asyncio
    async def test_tools_that_need_a_prior_search_say_so(self, server):
        assert "find_screenings first" in await self.call(
            server, "get_seatmap", {"option_id": "nope"}
        )

    @pytest.mark.asyncio
    async def test_resolve_title_reports_variant_and_bookability(self, server):
        out = await self.call(server, "resolve_title",
                              {"query": "The Odyssey Private Theatre Rental"})
        assert out["clean_title"] == "The Odyssey"
        assert out["bookable"] is False and out["product_kind"] == "rental"

    @pytest.mark.asyncio
    async def test_watch_lifecycle_through_mcp(self, server):
        spec = {"work": {"query": "dune"},
                "date_window": {"start": "2026-08-02", "end": "2026-08-03"}}
        created = await self.call(server, "create_watch",
                                  {"label": "dune 70mm", "spec": spec})
        assert created["seeded"] is True

        listed = await self.call(server, "list_watches", {})
        assert listed["watches"][0]["label"] == "dune 70mm"

        cancelled = await self.call(server, "cancel_watch",
                                    {"watch_id": created["watch_id"]})
        assert cancelled["cancelled"] is True

    @pytest.mark.asyncio
    async def test_booking_link_is_terminal(self, server):
        out = await self.call(server, "find_screenings", {"spec": {
            "work": {"query": "dune"},
            "date_window": {"start": "2026-08-02", "end": "2026-08-02"}}})
        link = await self.call(server, "get_booking_link",
                               {"option_id": out["options"][0]["option_id"]})
        assert link["booking_link"].startswith("https://")
        assert "yourself" in link["note"]


class TestSeatFetchIsNeverFatal:
    """A seat fetch is enrichment, not a precondition.

    A Cloudflare challenge on one chain's seat map once returned zero results
    for every chain, because the provider-specific exception propagated out of
    the search instead of degrading that single option.
    """

    def service(self, provider):
        return SearchService([provider], store=Store.memory(),
                             directory=VenueDirectory(), transport=object())

    def spec(self):
        return SearchSpec(work=WorkRef(query="dune"),
                          date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)),
                          max_seatmap_fetches=2)

    def test_an_arbitrary_provider_exception_degrades_one_option(self):
        class Exploding(FakeProvider):
            def fetch_seats(self, option, transport):
                raise RuntimeError("cloudflare challenge persisted")

        result = self.service(
            Exploding([screening("amc:1"), screening("amc:2", 22)])
        ).search(self.spec(), today=date(2026, 8, 2))

        assert len(result.options) == 2, "the search must still return results"
        assert all(o.seat_data == "unavailable" for o in result.options)

    def test_the_reason_is_preserved_for_diagnosis(self):
        class Exploding(FakeProvider):
            def fetch_seats(self, option, transport):
                raise RuntimeError("cloudflare challenge persisted")

        service = self.service(Exploding([screening("amc:1")]))
        try:
            service.search(self.spec(), today=date(2026, 8, 2))
        except Exception as exc:  # pragma: no cover - must not happen
            pytest.fail(f"search raised {exc}")

    def test_a_working_provider_still_gets_its_grid(self):
        room = build_auditorium("v", "1", ["....", "...."])
        result = self.service(
            FakeProvider([screening("amc:1")], room)
        ).search(self.spec(), today=date(2026, 8, 2))
        assert result.options[0].seat_data == "grid"


class TestScopeIsReported:
    """A cap that nobody hears about is indistinguishable from a full search.

    Providers cap venues and days for good reasons, but a search that read
    three of eleven nearby theatres and found nothing used to report exactly
    what a search that read all eleven and found nothing reported.
    """

    def test_clipping_venues_is_recorded(self):
        from screenwatch.providers.scope import ScopeReporting
        from screenwatch.service.venues import Venue

        class Capped(ScopeReporting):
            chain = "amc"
            max_venues = 2

        provider = Capped()
        provider._reset_scope()
        venues = [Venue(venue_id=f"v{i}", name=f"Cinema {i}", chain="amc")
                  for i in range(5)]
        assert len(provider._clip_venues(venues)) == 2
        assert provider.clipped
        assert "read 2 of 5 matching venues" in provider.clipped[0]
        assert "Cinema 2" in provider.clipped[0]

    def test_clipping_days_names_the_last_day_actually_checked(self):
        from screenwatch.providers.scope import ScopeReporting

        class Capped(ScopeReporting):
            chain = "cinemark"
            max_days = 2

        provider = Capped()
        provider._reset_scope()
        days = [date(2026, 8, d) for d in range(2, 9)]
        assert provider._clip_days(days) == days[:2]
        assert "nothing later than 2026-08-03 was checked" in provider.clipped[0]

    def test_scope_notes_do_not_survive_into_the_next_search(self):
        from screenwatch.providers.scope import ScopeReporting
        from screenwatch.service.venues import Venue

        class Capped(ScopeReporting):
            chain = "amc"
            max_venues = 1

        provider = Capped()
        provider._reset_scope()
        provider._clip_venues([Venue(venue_id=f"v{i}", name=str(i), chain="amc")
                               for i in range(3)])
        assert provider.clipped
        provider._reset_scope()
        assert provider.clipped == ()

    def test_an_uncapped_provider_reports_nothing(self):
        from screenwatch.providers.scope import ScopeReporting
        from screenwatch.service.venues import Venue

        class Uncapped(ScopeReporting):
            chain = "independent"
            max_venues = None

        provider = Uncapped()
        provider._reset_scope()
        venues = [Venue(venue_id=f"v{i}", name=str(i), chain="independent")
                  for i in range(9)]
        assert len(provider._clip_venues(venues)) == 9
        assert provider.clipped == ()

    def test_the_search_result_carries_it_up(self):
        class Clipping(FakeProvider):
            clipped = ("amc: read 2 of 5 matching venues",)

        store = Store.memory()
        service = SearchService([Clipping([screening("amc:1")])], store=store,
                                directory=VenueDirectory(), transport=object())
        result = service.search(
            SearchSpec(work=WorkRef(query=WORK.title)), today=date(2026, 8, 2)
        )
        assert result.clipped == ("amc: read 2 of 5 matching venues",)
        assert not result.complete

    def test_a_full_search_is_marked_complete(self):
        store = Store.memory()
        service = SearchService([FakeProvider([screening("amc:1")])], store=store,
                                directory=VenueDirectory(), transport=object())
        result = service.search(
            SearchSpec(work=WorkRef(query=WORK.title)), today=date(2026, 8, 2)
        )
        assert result.clipped == ()
        assert result.complete


class TestSerdeCoversEverySpecField:
    """The round-trip is how a watch survives being stored.

    A watch keeps its SearchSpec as JSON in SQLite, so a field the
    serialiser does not know about is a field the watch silently loses on
    its very first poll. Enumerating the dataclass rather than a hand-written
    list means a new field fails here instead of in production.
    """

    def test_no_spec_field_is_dropped_by_the_serialiser(self):
        from dataclasses import fields

        from screenwatch.service.serde import spec_to_dict

        spec = SearchSpec(work=WorkRef(query="x"))
        serialised = spec_to_dict(spec)
        missing = [
            f.name for f in fields(SearchSpec)
            if f.name not in serialised and f.name != "weights"
        ]
        assert missing == [], f"spec fields never serialised: {missing}"

    def test_strict_presentations_survives_a_round_trip(self):
        from screenwatch.models import Brand, Preference, PresentationSpec
        from screenwatch.service.serde import spec_from_dict, spec_to_dict

        spec = SearchSpec(
            work=WorkRef(query="dune"),
            presentations=Preference([PresentationSpec(brand=Brand.IMAX)]),
            strict_presentations=True,
        )
        assert spec_from_dict(spec_to_dict(spec)).strict_presentations is True


class TestWatchesFilterByFormat:
    """"Tell me about new 70mm IMAX Dune tickets" is a request about 70mm
    IMAX. A standard digital showing is not a partial answer to it."""

    def spec(self, **kw):
        from screenwatch.models import Brand, Preference, PresentationSpec

        return SearchSpec(
            work=WorkRef(query=WORK.title),
            presentations=Preference([PresentationSpec(brand=Brand.IMAX)]),
            **kw,
        )

    def digital(self, sid):
        s = screening(sid)
        return replace(s, presentation=Presentation(Projection.DIGITAL))

    def test_a_search_still_ranks_rather_than_filters(self):
        store = Store.memory()
        service = SearchService([FakeProvider([self.digital("amc:1")])],
                                store=store, directory=VenueDirectory(),
                                transport=object())
        result = service.search(self.spec(), today=date(2026, 8, 2))
        # Kept: with everything else sold out, a lesser format beats not going.
        assert len(result.options) == 1

    def test_a_strict_spec_drops_the_wrong_format(self):
        store = Store.memory()
        service = SearchService([FakeProvider([self.digital("amc:1")])],
                                store=store, directory=VenueDirectory(),
                                transport=object())
        result = service.search(
            self.spec(strict_presentations=True), today=date(2026, 8, 2)
        )
        assert result.options == []

    def test_creating_a_watch_makes_its_format_preference_strict(self):
        store = Store.memory()
        service = SearchService([FakeProvider([self.digital("amc:1")])],
                                store=store, directory=VenueDirectory(),
                                transport=object())
        watches = WatchService(service, store)
        watch_id = watches.create(self.spec(), "dune imax", today=date(2026, 8, 2))

        stored = spec_from_json(store.get_watch(watch_id)["spec"])
        assert stored.strict_presentations is True

    def test_a_watch_does_not_fire_on_a_format_nobody_asked_for(self):
        store = Store.memory()
        provider = FakeProvider([])
        service = SearchService([provider], store=store,
                                directory=VenueDirectory(), transport=object())
        watches = WatchService(service, store)
        watch_id = watches.create(self.spec(), "dune imax", today=date(2026, 8, 2))

        # A digital showing appears. It is new, but it is not what was asked for.
        provider._screenings = [self.digital("amc:99")]
        assert watches.run(watch_id, today=date(2026, 8, 2)) == []

        # The IMAX one is a hit.
        provider._screenings = [screening("amc:100")]
        hits = watches.run(watch_id, today=date(2026, 8, 2))
        assert [h.option.screening.screening_id for h in hits] == ["amc:100"]


class TestOneTitlePerFilm:
    """Which chain was polled first is not a property a user should observe."""

    def screenings_with(self, *titles):
        from screenwatch.identity.work import Work

        return [
            replace(screening(f"amc:{i}"),
                    work=Work(work_id="local:spider man", title=t))
            for i, t in enumerate(titles)
        ]

    def test_the_richest_title_wins_regardless_of_order(self):
        rich, poor = "Spider-Man: Brand New Day", "Spider Man Brand New Day"
        for order in ((poor, rich), (rich, poor)):
            unified = SearchService.unify_titles(self.screenings_with(*order))
            assert {s.work.title for s in unified} == {rich}

    def test_distinct_works_are_left_alone(self):
        from screenwatch.identity.work import Work

        rows = [
            replace(screening("amc:1"), work=Work(work_id="w1", title="Vertigo")),
            replace(screening("amc:2"), work=Work(work_id="w2", title="La Notte")),
        ]
        assert {s.work.title for s in SearchService.unify_titles(rows)} == {
            "Vertigo", "La Notte"
        }

    def test_an_empty_set_is_fine(self):
        assert SearchService.unify_titles([]) == []
