"""The scheduler: cadence tiers, due-selection, and not dying on one bad watch.

Entirely clock-injected, so the tests are instant and deterministic — a
scheduler tested with real sleeps is a scheduler nobody runs in CI.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from screenwatch.identity.work import WorkRef
from screenwatch.ranking.spec import DateWindow, SearchSpec
from screenwatch.service.scheduler import (
    COLD_S,
    HOT_S,
    WARM_S,
    Scheduler,
    cadence_for,
    describe_hits,
    jittered,
)
from screenwatch.service.serde import spec_to_json
from screenwatch.service.store import Store
from screenwatch.service.watch import WatchService

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def spec_json(start: date | None, end: date | None) -> str:
    window = DateWindow(start, end) if start and end else None
    return spec_to_json(SearchSpec(work=WorkRef(query="dune"), date_window=window))


def row(**kw) -> dict:
    base = {
        "watch_id": "w1", "spec": spec_json(None, None),
        "last_run": None, "last_hit": None, "cadence_s": 300, "active": 1,
    }
    base.update(kw)
    return base


class TestCadence:
    def test_target_date_today_is_hot(self):
        tier = cadence_for(row(spec=spec_json(TODAY, TODAY)), now=NOW, today=TODAY)
        assert (tier.name, tier.interval_s) == ("hot", HOT_S)

    def test_a_recent_hit_makes_it_hot_regardless_of_date(self):
        """Something just appeared; more usually follows, and that is exactly
        when seats move."""
        far = TODAY + timedelta(days=30)
        tier = cadence_for(
            row(spec=spec_json(far, far), last_hit=(NOW - timedelta(minutes=5)).isoformat()),
            now=NOW, today=TODAY,
        )
        assert tier.name == "hot"

    def test_an_old_hit_does_not_keep_it_hot(self):
        far = TODAY + timedelta(days=30)
        tier = cadence_for(
            row(spec=spec_json(far, far), last_hit=(NOW - timedelta(days=2)).isoformat()),
            now=NOW, today=TODAY,
        )
        assert tier.name == "cold"

    def test_imminent_target_is_warm(self):
        soon = TODAY + timedelta(days=2)
        assert cadence_for(row(spec=spec_json(soon, soon)), now=NOW, today=TODAY).name == "warm"

    def test_distant_target_is_cold(self):
        far = TODAY + timedelta(days=40)
        tier = cadence_for(row(spec=spec_json(far, far)), now=NOW, today=TODAY)
        assert (tier.name, tier.interval_s) == ("cold", COLD_S)

    def test_a_passed_window_retires_the_watch(self):
        """Polling forever for a date that has gone is pure waste."""
        past = TODAY - timedelta(days=1)
        tier = cadence_for(row(spec=spec_json(past, past)), now=NOW, today=TODAY)
        assert tier.interval_s == 0

    def test_open_ended_watch_is_warm(self):
        assert cadence_for(row(), now=NOW, today=TODAY).name == "warm"

    def test_unparseable_spec_does_not_crash_the_tiering(self):
        assert cadence_for(row(spec="{ not json"), now=NOW, today=TODAY).name == "warm"


class TestJitter:
    def test_stays_in_band(self):
        rng = random.Random(0)
        for _ in range(200):
            assert 0.7 * 100 <= jittered(100, rng=rng) <= 1.3 * 100

    def test_never_returns_zero(self):
        assert jittered(0.01) >= 1.0


class FakeWatchService:
    def __init__(self, store, hits_for=None, raises=()):
        self.store = store
        self.hits_for = hits_for or {}
        self.raises = set(raises)
        self.ran: list[str] = []

    def run(self, watch_id, **kw):
        self.ran.append(watch_id)
        if watch_id in self.raises:
            raise RuntimeError("provider exploded")
        return self.hits_for.get(watch_id, [])


@pytest.fixture
def store():
    s = Store.memory()
    yield s
    s.close()


class TestDueSelection:
    def scheduler(self, store, **kw):
        return Scheduler(FakeWatchService(store), store=store,
                         clock=lambda: NOW, sleeper=lambda _s: None, **kw)

    def test_a_never_run_watch_is_immediately_due(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        assert [r["watch_id"] for r in self.scheduler(store).due(NOW)] == ["w1"]

    def test_a_just_run_watch_is_not_due(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        # Stamped against the injected clock, not the wall clock - touch_watch
        # writes real now, which relative to the simulated NOW looks stale.
        store._conn.execute(
            "UPDATE watches SET last_run=? WHERE watch_id=?", (NOW.isoformat(), "w1")
        )
        assert self.scheduler(store).due(NOW) == []

    def test_a_stale_watch_is_due_again(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        store._conn.execute(
            "UPDATE watches SET last_run=? WHERE watch_id=?",
            ((NOW - timedelta(seconds=WARM_S + 10)).isoformat(), "w1"),
        )
        assert [r["watch_id"] for r in self.scheduler(store).due(NOW)] == ["w1"]

    def test_a_retired_watch_is_never_due(self, store):
        past = TODAY - timedelta(days=5)
        store.create_watch("w1", "x", spec_json(past, past))
        assert self.scheduler(store).due(NOW) == []

    def test_cancelled_watches_are_excluded(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        store.cancel_watch("w1")
        assert self.scheduler(store).due(NOW) == []


class TestTick:
    def test_runs_every_due_watch(self, store):
        for i in range(3):
            store.create_watch(f"w{i}", "x", spec_json(None, None))
        watches = FakeWatchService(store)
        sched = Scheduler(watches, store=store, clock=lambda: NOW, sleeper=lambda _s: None)
        sched.tick()
        assert sorted(watches.ran) == ["w0", "w1", "w2"]

    def test_one_exploding_watch_does_not_stop_the_others(self, store):
        """A provider outage in one watch must not silently stop every other
        monitor the user is relying on."""
        for i in range(3):
            store.create_watch(f"w{i}", "x", spec_json(None, None))
        watches = FakeWatchService(store, raises={"w1"})
        sched = Scheduler(watches, store=store, clock=lambda: NOW, sleeper=lambda _s: None)
        sched.tick()
        assert sorted(watches.ran) == ["w0", "w1", "w2"]

    def test_a_failing_watch_still_gets_stamped_so_it_backs_off(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        watches = FakeWatchService(store, raises={"w1"})
        Scheduler(watches, store=store, clock=lambda: NOW,
                  sleeper=lambda _s: None).tick()
        assert store.get_watch("w1")["last_run"] is not None

    def test_hits_are_handed_to_the_callback(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        marker = object()
        watches = FakeWatchService(store, hits_for={"w1": [marker]})
        seen = []
        sched = Scheduler(watches, store=store, clock=lambda: NOW,
                          sleeper=lambda _s: None, on_hits=seen.append)
        assert sched.tick() == [marker]
        assert seen == [[marker]]

    def test_no_hits_means_no_callback(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        seen = []
        Scheduler(FakeWatchService(store), store=store, clock=lambda: NOW,
                  sleeper=lambda _s: None, on_hits=seen.append).tick()
        assert seen == []


class TestRunForever:
    def test_stop_ends_the_loop(self, store):
        store.create_watch("w1", "x", spec_json(None, None))
        watches = FakeWatchService(store)
        sched = Scheduler(watches, store=store, clock=lambda: NOW, tick_s=0.001)

        ticks = {"n": 0}
        original = sched.tick

        def counting():
            ticks["n"] += 1
            if ticks["n"] >= 3:
                sched.stop()
            return original()

        sched.tick = counting
        sched.run_forever()
        assert ticks["n"] == 3


class TestReporting:
    def test_describe_hits_is_readable(self):
        class Hit:
            @staticmethod
            def payload():
                return {"title": "Dune: Part Three", "presentation": "IMAX / 70mm",
                        "venue": "AMC Metreon 16", "starts_at_local": "2026-08-03T19:00",
                        "booking_link": "https://x/1"}

        text = describe_hits([Hit()])
        assert "Dune: Part Three" in text and "IMAX / 70mm" in text and "https://x/1" in text

    def test_no_hits_reads_plainly(self):
        assert describe_hits([]) == "no new screenings"
