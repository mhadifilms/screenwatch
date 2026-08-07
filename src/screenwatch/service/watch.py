"""Always-on monitors.

The behaviour that matters: *"some 70mm tickets already dropped, but tell me
when NEW ones appear for Dune 3 in the Bay Area."* A watch therefore seeds a
seen-set at creation time with everything currently on sale, then compares
later observations for positive changes too. Without that, every new watch
immediately pages you about tickets that have been available for a week, while
a screening that gains seats stays silent.

Delivery is a durable poll queue plus an optional local webhook. Hits are
persisted before delivery is attempted, so a failed webhook or a disconnected
client loses nothing.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from ..models import Availability
from ..ranking.candidate import Option
from ..ranking.spec import DateWindow, SearchSpec
from ..seating.quality import QualityModel
from .search import SearchService
from .serde import spec_from_json, spec_to_json
from .store import DEFAULT_USER, Store

_NEARLY_SOLD_OUT_RATIO = 0.10
_NEARLY_SOLD_OUT_COUNT = 8

_ALERT_PRIORITY = {
    "tickets_returned": 100,
    "middle_seats_available": 95,
    "party_fits": 90,
    "seats_together": 85,
    "more_seats": 75,
    "seat_map_available": 70,
    "better_option": 60,
    "new_screening": 50,
}


def _counts_for(option: Option) -> tuple[int | None, int | None]:
    auditorium = option.auditorium
    if auditorium is not None:
        return auditorium.available, auditorium.capacity
    return option.screening.seats_available, option.screening.seats_capacity


def _availability_status(
    option: Option, available: int | None, capacity: int | None
) -> str:
    screening_status = option.screening.availability
    if screening_status is Availability.SOLD_OUT:
        return "sold_out"
    if available is not None and capacity and available <= 0:
        return "sold_out"
    if screening_status is Availability.ALMOST_FULL:
        return "nearly_sold_out"
    if (
        available is not None
        and capacity
        and (
            available <= _NEARLY_SOLD_OUT_COUNT
            or available / capacity <= _NEARLY_SOLD_OUT_RATIO
        )
    ):
        return "nearly_sold_out"
    if screening_status is Availability.SELLABLE or available is not None:
        return "available"
    return "unknown"


def _seat_position(option: Option) -> str:
    """Summarize whether the best current group is in the middle area.

    This is deliberately a state label, not a second ranking algorithm. The
    same normalized geometry that selected `option.seats` defines the middle
    band, so a watch can say why a change matters without disagreeing with
    the recommendation shown to the user.
    """
    group = option.seats
    auditorium = option.auditorium
    if group is None or auditorium is None:
        if option.seat_data == "no_seating":
            return "no_middle_seats"
        return "unknown"

    model = QualityModel().for_auditorium(auditorium)
    if model.middle_band is None:
        return "unknown"
    low, high = model.middle_band
    middle = [
        seat for seat in group.seats
        if low <= seat.y <= high and abs(seat.x) <= 0.6
    ]
    if len(middle) == len(group.seats):
        return "middle_area"
    if middle:
        return "near_middle"
    return "no_middle_seats"


def _state_for(option: Option) -> dict:
    """Turn a ranked option into the durable state a watch can compare."""
    screening = option.screening
    group = option.seats
    available, capacity = _counts_for(option)
    return {
        "screening_id": screening.screening_id,
        "canonical_screening_id": screening.canonical_screening_id,
        "option_id": option.option_id,
        "availability": screening.availability.value,
        "availability_status": _availability_status(option, available, capacity),
        "seat_data": option.seat_data,
        "available": available,
        "capacity": capacity,
        "complete": group.complete if group is not None else None,
        "together": (
            group.cohesion.is_together if group is not None else None
        ),
        "cohesion": group.cohesion.value if group is not None else None,
        "seats": group.labels if group is not None else None,
        "seat_position": _seat_position(option),
        "seat_quality": option.components.get("seat_quality"),
        "score": round(option.score, 4),
    }


def _changes(previous: dict, current: dict) -> tuple[str, ...]:
    """Return only positive changes worth interrupting someone for."""
    changes: list[str] = []
    old_availability = previous.get("availability_status", previous.get("availability"))
    new_availability = current.get("availability_status", current.get("availability"))
    if (
        old_availability in {"sold_out", "unknown"}
        and new_availability in {
            "available", "nearly_sold_out", "sellable", "almost_full",
        }
    ):
        changes.append("tickets_returned")

    old_seat_data = previous.get("seat_data")
    new_seat_data = current.get("seat_data")
    if old_seat_data in {"unavailable", "not_fetched"} and new_seat_data in {
        "grid", "estimated", "count_only"
    }:
        changes.append("seat_map_available")

    old_available = previous.get("available")
    new_available = current.get("available")
    if (
        isinstance(old_available, int)
        and isinstance(new_available, int)
        and new_available > old_available
    ):
        changes.append("more_seats")

    if current.get("complete") and not previous.get("complete"):
        changes.append("party_fits")
    if current.get("together") and not previous.get("together"):
        changes.append("seats_together")

    old_position = previous.get("seat_position")
    new_position = current.get("seat_position")
    if (
        new_position == "middle_area"
        and old_position in {"no_middle_seats", "near_middle", "unknown", None}
    ):
        changes.append("middle_seats_available")

    old_score = previous.get("score")
    new_score = current.get("score")
    if (
        isinstance(old_score, (int, float))
        and isinstance(new_score, (int, float))
        and new_score >= old_score + 0.05
    ):
        changes.append("better_option")
    return tuple(changes)


def _delta(previous: dict | None, current: dict) -> dict | None:
    if previous is None:
        return None
    delta: dict = {}
    old_available = previous.get("available")
    new_available = current.get("available")
    if isinstance(old_available, int) and isinstance(new_available, int):
        delta["available"] = new_available - old_available
    old_score = previous.get("score")
    new_score = current.get("score")
    if isinstance(old_score, (int, float)) and isinstance(new_score, (int, float)):
        delta["score"] = round(new_score - old_score, 4)
    for field in (
        "availability", "availability_status", "seat_data", "seat_position",
        "seat_quality", "together", "complete",
    ):
        if previous.get(field) != current.get(field):
            delta[field] = {
                "from": previous.get(field),
                "to": current.get(field),
            }
    return delta


def _best_options(options: list[Option]) -> dict[str, Option]:
    """Compare one best current option per showing, not arbitrary duplicates."""
    best: dict[str, Option] = {}
    for option in options:
        canonical_id = option.screening.canonical_screening_id
        if canonical_id not in best or option.score > best[canonical_id].score:
            best[canonical_id] = option
    return best


@dataclass
class WatchHit:
    watch_id: str
    option: Option
    alert_type: str = "new_screening"
    changes: tuple[str, ...] = ()
    previous: dict | None = None
    observed_at: str | None = None
    hit_id: int | None = None

    def payload(self) -> dict:
        o, s = self.option, self.option.screening
        current = _state_for(o)
        alert_type = self.alert_type
        return {
            "watch_id": self.watch_id,
            "screening_id": s.screening_id,
            "canonical_screening_id": s.canonical_screening_id,
            "option_id": o.option_id,
            "alert_type": alert_type,
            "priority": _ALERT_PRIORITY.get(alert_type, 0),
            "changes": list(self.changes),
            "delta": _delta(self.previous, current),
            "observed_at": self.observed_at,
            "title": s.work.title,
            "venue": s.venue_name,
            "starts_at_local": s.starts_at_local.isoformat(),
            "presentation": s.presentation.describe(),
            "availability": s.availability.value,
            "inventory_status": current["availability_status"],
            "seat_position": current["seat_position"],
            "score": o.score,
            "seats": o.seats.describe() if o.seats else None,
            "reasons": list(o.reasons),
            "tradeoffs": list(o.tradeoffs),
            "booking_link": s.deeplink,
            "previous": self.previous,
            "current": current,
        }


class WatchService:
    def __init__(
        self,
        search: SearchService,
        store: Store | None = None,
        *,
        webhook_sender=None,
        clock=None,
    ) -> None:
        self.search = search
        self.store = store or search.store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._webhook_sender = webhook_sender or self._post_webhook

    def _search(self, spec: SearchSpec, *, today: date | None, user_id: str):
        """Call old injected search doubles without losing user scoping.

        SearchService grew ``user_id`` after the watch protocol was already
        useful to callers. A small compatibility shim keeps third-party test
        doubles and integrations working while the production service records
        the correct owner on every search run.
        """
        try:
            return self.search.search(spec, today=today, user_id=user_id)
        except TypeError as exc:
            if "user_id" not in str(exc):
                raise
            return self.search.search(spec, today=today)

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

        A watch also keeps sold-out screenings in its baseline. That lets a
        sold-out show become a precise "tickets_returned" alert instead of
        looking like a brand-new screening. A format preference on a watch is
        a filter, not a ranking: an alert
        for a format the user did not ask for is a wrong answer rather than a
        partial one, and a few of those teach them to ignore the rest. So the
        spec is stored with `strict_presentations` on, and the seed uses the
        same spec - otherwise the seen-set would be seeded with screenings the
        watch will never report and the first genuine hit would be missed.
        """
        spec = replace(
            spec,
            include_sold_out=True,
            strict_presentations=(
                True if spec.presentations is not None else spec.strict_presentations
            ),
        )
        # ``today`` is primarily a deterministic replay/test anchor, but it
        # also needs to survive from creation into the first poll. Otherwise
        # an unseeded watch created against a supplied date can immediately
        # move outside its default relative window before it ever fires.
        if today is not None and spec.date_window is None:
            spec = replace(spec, date_window=DateWindow.next_days(today, 7))
        watch_id = f"w_{uuid.uuid4().hex[:12]}"
        self.store.create_watch(
            watch_id, label, spec_to_json(spec),
            user_id=user_id, cadence_s=cadence_s, webhook=webhook,
        )

        if seed:
            existing = self._search(spec, today=today, user_id=user_id)
            states = {
                sid: _state_for(option)
                for sid, option in _best_options(existing.options).items()
            }
            self.store.observe_watch(watch_id, states)
        return watch_id

    def cancel(self, watch_id: str, *, user_id: str = DEFAULT_USER) -> bool:
        return self.store.cancel_watch(watch_id, user_id=user_id)

    def list(self, user_id: str = DEFAULT_USER) -> list[dict]:
        return self.store.list_watches(user_id)

    # ------------------------------------------------------------------
    def run(self, watch_id: str, *, today: date | None = None) -> list[WatchHit]:
        """Poll one watch and emit new or materially improved options."""
        row = self.store.get_watch(watch_id)
        if row is None or not row["active"]:
            return []

        try:
            spec = spec_from_json(row["spec"])
            result = self._search(spec, today=today, user_id=row["user_id"])
        except Exception as exc:
            self.store.touch_watch(
                watch_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        by_id = _best_options(result.options)
        canonical_ids = list(by_id)
        seen = self.store.seen_canonical_ids(watch_id, canonical_ids)
        previous = self.store.watch_states_for(watch_id, canonical_ids)
        observed_at = self._clock().isoformat()
        warnings = tuple(
            getattr(result, field, ())
            for field in ("provider_errors", "clipped")
        )
        warning_text = "; ".join(item for group in warnings for item in group) or None
        hits: list[WatchHit] = []
        states: dict[str, dict] = {}

        for canonical_id, option in by_id.items():
            current = _state_for(option)
            states[canonical_id] = current
            old = previous.get(canonical_id)
            if old is None:
                # A database created before watch_states existed still has a
                # legacy seen-set. Treat it as a baseline, not as a new alert.
                if canonical_id in seen:
                    continue
                alert_type = "new_screening"
                changed = ()
            else:
                changed = _changes(old, current)
                if not changed:
                    continue
                alert_type = max(
                    changed,
                    key=lambda kind: _ALERT_PRIORITY.get(kind, 0),
                )

            hits.append(
                WatchHit(
                    watch_id,
                    option,
                    alert_type=alert_type,
                    changes=changed,
                    previous=old,
                    observed_at=observed_at,
                )
            )

        durable_hits: list[WatchHit] = []
        for hit in hits:
            canonical_id = hit.option.screening.canonical_screening_id
            current = states[canonical_id]
            event_key = (
                f"{hit.alert_type}:{canonical_id}"
                if hit.alert_type == "new_screening"
                else f"state:{canonical_id}:{json.dumps(current, sort_keys=True)}"
            )
            hit_id, inserted = self.store.record_hit_status(
                watch_id,
                canonical_id,
                hit.payload(),
                event_key=event_key,
            )
            if inserted:
                hit.hit_id = hit_id
                self.store.queue_delivery(hit_id)
                durable_hits.append(hit)

        self.store.observe_watch(watch_id, states)
        self.store.touch_watch(
            watch_id,
            hit=bool(durable_hits),
            success=True,
            warning=warning_text,
        )
        self._deliver_webhook(row)
        return durable_hits

    def run_due(self, *, user_id: str = DEFAULT_USER, today: date | None = None) -> list[WatchHit]:
        """Poll every watch whose cadence has elapsed."""
        now = self._clock()
        out: list[WatchHit] = []
        for row in self.store.list_watches(user_id):
            last = row["last_run"]
            if last and (now - datetime.fromisoformat(last)).total_seconds() < row["cadence_s"]:
                continue
            with contextlib.suppress(Exception):
                out.extend(self.run(row["watch_id"], today=today))
        with contextlib.suppress(Exception):
            self.deliver_due(user_id=user_id)
        return out

    # ------------------------------------------------------------------
    def pending(self, user_id: str = DEFAULT_USER) -> list[dict]:
        rows = self.store.pending_hits(user_id)
        return [
            {**row, "payload": {**row["payload"], "hit_id": row["hit_id"]}}
            for row in rows
        ]

    def acknowledge(self, hit_ids: list[int], *, user_id: str = DEFAULT_USER) -> None:
        self.store.mark_delivered(hit_ids, user_id=user_id)

    def deliver_due(self, *, user_id: str = DEFAULT_USER) -> None:
        """Retry webhook deliveries independently of search cadence."""
        for row in self.store.list_watches(user_id):
            self._deliver_webhook(row)

    def _deliver_webhook(self, row: dict) -> None:
        """Deliver due alerts, retaining them for retry on failure."""
        if not row["webhook"]:
            return
        self.store.backfill_deliveries(row["watch_id"])
        pending = self.store.pending_webhook_hits(
            row["watch_id"], now=self._clock().isoformat()
        )
        if not pending:
            return
        hits = [
            {
                **entry["payload"],
                "hit_id": entry["hit_id"],
                "delivery_attempt": entry["attempts"] + 1,
            }
            for entry in pending
        ]
        body = {
            "event": "screenwatch.alerts.v1",
            "watch_id": row["watch_id"],
            "watch_label": row["label"],
            "sent_at": self._clock().isoformat(),
            "hits": hits,
        }
        hit_ids = [entry["hit_id"] for entry in pending]
        request = urllib.request.Request(
            row["webhook"],
            data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "user-agent": "screenwatch-alerts/1",
                "x-screenwatch-event": "screenwatch.alerts.v1",
                "x-screenwatch-hit-ids": ",".join(str(hit_id) for hit_id in hit_ids),
                "x-screenwatch-idempotency-key": (
                    f"{row['watch_id']}:{','.join(str(hit_id) for hit_id in hit_ids)}"
                ),
            },
        )
        try:
            result = self._webhook_sender(row["webhook"], body, request)
            if result is False:
                raise RuntimeError("webhook sender returned false")
        except Exception as exc:                              # noqa: BLE001
            self.store.record_delivery_result(
                hit_ids,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                now=self._clock(),
            )
        else:
            self.store.record_delivery_result(
                hit_ids, success=True, now=self._clock()
            )

    @staticmethod
    def _post_webhook(url: str, body: dict, request: urllib.request.Request):
        """Default sender. The request is injectable so retries are testable."""
        del url, body
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    request.full_url, response.status, "webhook rejected", response.headers, None
                )
