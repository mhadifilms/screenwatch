"""The loop that makes monitors actually always-on.

`WatchService.run_due()` existed but nothing called it, which meant every
watch was really a manual poll. This is the missing piece.

Cadence is tiered rather than fixed, because the cost of a miss is not
constant. A watch whose film has no showings yet can be checked lazily; one
whose target date is tonight, or that just saw its first screening appear, is
in the window where tickets actually move. The tiers mirror the ones designed
for the read layer:

    cold      nothing found yet, target far off      30 min
    warm      screenings exist for the target        5 min
    hot       a hit in the last hour, or target      45 s (jittered)
              date is today
    retired   target date has passed                 no longer polled

Jitter is applied to every sleep. Polling on exact round intervals is both a
recognisable signature and a good way for several watches to stampede the
same origin at the same instant.
"""

from __future__ import annotations

import random
import signal
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .serde import spec_from_json
from .store import DEFAULT_USER, Store
from .watch import WatchHit, WatchService

COLD_S = 1800
WARM_S = 300
HOT_S = 45
JITTER = 0.25


@dataclass
class Tier:
    name: str
    interval_s: int


def cadence_for(row: dict, *, now: datetime, today: date) -> Tier:
    """How urgently this watch should be polled.

    Reads only what the store already records, so it costs nothing and stays
    correct across restarts.
    """
    last_hit = row.get("last_hit")
    if last_hit:
        age = (now - datetime.fromisoformat(last_hit)).total_seconds()
        if age < 3600:
            # Something just appeared. More usually follows, and this is
            # exactly the window where seats move.
            return Tier("hot", HOT_S)

    try:
        spec = spec_from_json(row["spec"])
    except Exception:                                          # noqa: BLE001
        return Tier("warm", WARM_S)

    window = spec.date_window
    if window is not None:
        if window.end < today:
            return Tier("retired", 0)
        if window.start <= today <= window.end:
            return Tier("hot", HOT_S)
        if (window.start - today).days <= 2:
            return Tier("warm", WARM_S)
        return Tier("cold", COLD_S)

    return Tier("warm", WARM_S)


def jittered(seconds: float, *, rng: random.Random | None = None) -> float:
    rng = rng or random
    return max(1.0, seconds * (1 + rng.uniform(-JITTER, JITTER)))


class Scheduler:
    """Polls watches forever, honouring per-watch cadence.

    Single-threaded on purpose. Providers keep warm sessions and pace their
    own requests; running watches concurrently would defeat both and turn a
    polite poller into a burst of parallel traffic.
    """

    def __init__(
        self,
        watches: WatchService,
        *,
        store: Store | None = None,
        user_id: str = DEFAULT_USER,
        tick_s: float = 15.0,
        on_hits=None,
        clock=None,
        sleeper=None,
    ) -> None:
        self.watches = watches
        self.store = store or watches.store
        self.user_id = user_id
        self.tick_s = tick_s
        self.on_hits = on_hits
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or time.sleep
        self._stop = threading.Event()
        self.polls = 0

    # ------------------------------------------------------------------
    def due(self, now: datetime) -> list[dict]:
        today = now.date()
        out = []
        for row in self.store.list_watches(self.user_id):
            tier = cadence_for(row, now=now, today=today)
            if tier.interval_s <= 0:
                continue                       # retired: window has passed
            last = row.get("last_run")
            if last is None:
                out.append(row)
                continue
            elapsed = (now - datetime.fromisoformat(last)).total_seconds()
            if elapsed >= tier.interval_s:
                out.append(row)
        return out

    def tick(self) -> list[WatchHit]:
        """One pass. Returns whatever fired, for the caller to deliver."""
        now = self._clock()
        hits: list[WatchHit] = []
        for row in self.due(now):
            self.polls += 1
            try:
                hits.extend(self.watches.run(row["watch_id"]))
            except Exception:                                  # noqa: BLE001
                # A watch that throws must not stop the loop; the run is
                # recorded either way so it backs off rather than hot-spinning.
                self.store.touch_watch(row["watch_id"])
        if hits and self.on_hits:
            self.on_hits(hits)
        return hits

    def run_forever(self) -> None:
        self._install_signal_handlers()
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(jittered(self.tick_s))

    def stop(self, *_args) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass          # not on the main thread; caller drives stop()


def describe_hits(hits: list[WatchHit]) -> str:
    if not hits:
        return "no new screenings"
    lines = [f"{len(hits)} new screening(s):"]
    for hit in hits:
        p = hit.payload()
        lines.append(
            f"  {p['title']} — {p['presentation']} — {p['venue']} "
            f"{p['starts_at_local']}  {p['booking_link'] or ''}"
        )
    return "\n".join(lines)


def main() -> None:
    """`python -m screenwatch.service.scheduler`"""
    from .defaults import default_service

    _search, watches, store = default_service()
    scheduler = Scheduler(watches, on_hits=lambda h: print(describe_hits(h), flush=True))
    active = len(store.list_watches())
    print(f"screenwatch scheduler: {active} active watch(es); ctrl-c to stop", flush=True)
    scheduler.run_forever()
    print("stopped", flush=True)


if __name__ == "__main__":
    main()
