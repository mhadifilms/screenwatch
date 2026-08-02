"""Always-on monitors.

The behaviour that matters: *"some 70mm tickets already dropped, but tell me
when NEW ones appear for Dune 3 in the Bay Area."* A watch therefore seeds a
seen-set at creation time with everything currently on sale, and only fires
on screenings it has never reported. Without that, every new watch immediately
pages you about tickets that have been available for a week.

Delivery is MCP notification plus a poll tool, plus an optional local webhook.
Hits are persisted before delivery is attempted, so a failed webhook or a
disconnected client loses nothing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..ranking.candidate import Option
from ..ranking.spec import SearchSpec
from .search import SearchService
from .store import DEFAULT_USER, Store
from .serde import spec_from_json, spec_to_json


@dataclass
class WatchHit:
    watch_id: str
    option: Option

    def payload(self) -> dict:
        o, s = self.option, self.option.screening
        return {
            "watch_id": self.watch_id,
            "screening_id": s.screening_id,
            "option_id": o.option_id,
            "title": s.work.title,
            "venue": s.venue_name,
            "starts_at_local": s.starts_at_local.isoformat(),
            "presentation": s.presentation.describe(),
            "availability": s.availability.value,
            "score": o.score,
            "seats": o.seats.describe() if o.seats else None,
            "reasons": list(o.reasons),
            "tradeoffs": list(o.tradeoffs),
            "booking_link": s.deeplink,
        }


class WatchService:
    def __init__(self, search: SearchService, store: Store | None = None) -> None:
        self.search = search
        self.store = store or search.store

    # ------------------------------------------------------------------
    def create(
        self,
        spec: SearchSpec,
        label: str,
        *,
        user_id: str = DEFAULT_USER,
        cadence_s: int = 300,
        webhook: str | None = None,
        seed: bool = True,
        today: date | None = None,
    ) -> str:
        """Create a watch, seeding what already exists so it stays quiet.

        `seed=False` is for "tell me about everything you can find, now" -
        useful when the watch is created before any tickets exist at all.
        """
        watch_id = f"w_{uuid.uuid4().hex[:12]}"
        self.store.create_watch(
            watch_id, label, spec_to_json(spec),
            user_id=user_id, cadence_s=cadence_s, webhook=webhook,
        )

        if seed:
            existing = self.search.search(spec, today=today)
            self.store.seed_seen(
                watch_id, [o.screening.screening_id for o in existing.options]
            )
        return watch_id

    def cancel(self, watch_id: str) -> bool:
        return self.store.cancel_watch(watch_id)

    def list(self, user_id: str = DEFAULT_USER) -> list[dict]:
        return self.store.list_watches(user_id)

    # ------------------------------------------------------------------
    def run(self, watch_id: str, *, today: date | None = None) -> list[WatchHit]:
        """Poll one watch. Returns only genuinely new screenings."""
        row = self.store.get_watch(watch_id)
        if row is None or not row["active"]:
            return []

        spec = spec_from_json(row["spec"])
        result = self.search.search(spec, today=today)

        by_id = {o.screening.screening_id: o for o in result.options}
        fresh = self.store.unseen(watch_id, list(by_id))

        hits = [WatchHit(watch_id, by_id[sid]) for sid in fresh]
        for hit in hits:
            self.store.record_hit(watch_id, hit.option.screening.screening_id, hit.payload())

        self.store.mark_seen(watch_id, list(by_id))
        self.store.touch_watch(watch_id, hit=bool(hits))

        if hits and row["webhook"]:
            self._deliver_webhook(row["webhook"], hits)
        return hits

    def run_due(self, *, user_id: str = DEFAULT_USER, today: date | None = None) -> list[WatchHit]:
        """Poll every watch whose cadence has elapsed."""
        now = datetime.now(timezone.utc)
        out: list[WatchHit] = []
        for row in self.store.list_watches(user_id):
            last = row["last_run"]
            if last and (now - datetime.fromisoformat(last)).total_seconds() < row["cadence_s"]:
                continue
            out.extend(self.run(row["watch_id"], today=today))
        return out

    # ------------------------------------------------------------------
    def pending(self, user_id: str = DEFAULT_USER) -> list[dict]:
        return self.store.pending_hits(user_id)

    def acknowledge(self, hit_ids: list[int]) -> None:
        self.store.mark_delivered(hit_ids)

    @staticmethod
    def _deliver_webhook(url: str, hits: list[WatchHit]) -> None:
        """Best effort. The hit is already durable, so a failure here is
        recoverable by polling - never let it break the run loop."""
        body = json.dumps({"hits": [h.payload() for h in hits]}).encode()
        request = urllib.request.Request(
            url, data=body, headers={"content-type": "application/json"}
        )
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except (urllib.error.URLError, OSError):
            pass
