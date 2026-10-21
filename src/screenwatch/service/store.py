"""SQLite persistence.

Single-user locally, but every user-scoped table carries `user_id` from the
start. Adding it later would mean a migration across watches, seen-sets and
preferences at exactly the point the system has real data in it.

WAL mode, one writer, no ORM. The access patterns are small and known.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from ..identity.work import TitleLink, Work

DEFAULT_USER = "local"

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    work_id        TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    year           INTEGER,
    tmdb_id        INTEGER,
    runtime_min    INTEGER,
    original_title TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS title_links (
    source          TEXT NOT NULL,
    source_movie_id TEXT NOT NULL,
    raw_title       TEXT NOT NULL,
    work_id         TEXT,
    method          TEXT NOT NULL,
    confidence      REAL NOT NULL,
    kind            TEXT NOT NULL,
    attrs           TEXT NOT NULL DEFAULT '[]',
    linked_at       TEXT NOT NULL,
    PRIMARY KEY (source, source_movie_id)
);
CREATE INDEX IF NOT EXISTS idx_links_work ON title_links(work_id);

CREATE TABLE IF NOT EXISTS screenings (
    screening_id  TEXT PRIMARY KEY,
    work_id       TEXT,
    venue_id      TEXT NOT NULL,
    chain         TEXT NOT NULL,
    starts_at_utc TEXT NOT NULL,
    presentation  TEXT NOT NULL,
    availability  TEXT NOT NULL,
    deeplink      TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screenings_work ON screenings(work_id, starts_at_utc);

CREATE TABLE IF NOT EXISTS seat_snapshots (
    screening_id TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    available    INTEGER NOT NULL,
    capacity     INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (screening_id, captured_at)
);

CREATE TABLE IF NOT EXISTS watches (
    watch_id       TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    label          TEXT NOT NULL,
    spec           TEXT NOT NULL,
    cadence_s      INTEGER NOT NULL DEFAULT 300,
    active         INTEGER NOT NULL DEFAULT 1,
    webhook        TEXT,
    created_at     TEXT NOT NULL,
    last_run       TEXT,
    last_hit       TEXT,
    last_success   TEXT,
    last_error     TEXT,
    last_warning   TEXT,
    error_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_watches_user ON watches(user_id, active);

-- The seen-set. This is what makes "tell me about NEW 70mm tickets" work
-- when some tickets already dropped. Positive changes are tracked separately
-- in watch_states, so an existing screening can alert again when it improves.
CREATE TABLE IF NOT EXISTS watch_seen (
    watch_id     TEXT NOT NULL,
    screening_id TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    PRIMARY KEY (watch_id, screening_id)
);

CREATE TABLE IF NOT EXISTS watch_hits (
    hit_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id     TEXT NOT NULL,
    screening_id TEXT NOT NULL,
    event_key    TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hits_undelivered ON watch_hits(watch_id, delivered);

-- Latest observation per watch and screening. Unlike `watch_seen`, this keeps
-- enough state to alert when an existing showing improves (seats return,
-- sold-out flips to sellable, or a contiguous group appears).
CREATE TABLE IF NOT EXISTS watch_states (
    watch_id     TEXT NOT NULL,
    screening_id TEXT NOT NULL,
    state        TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    PRIMARY KEY (watch_id, screening_id)
);

-- Delivery is channel-specific. A webhook succeeding must not make a poll
-- client lose the same alert, and a failed webhook needs durable retry state.
CREATE TABLE IF NOT EXISTS watch_deliveries (
    hit_id          INTEGER NOT NULL,
    channel         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_attempt_at TEXT,
    delivered_at    TEXT,
    last_error      TEXT,
    PRIMARY KEY (hit_id, channel)
);

-- Coordinates learned by visiting a venue page. Some chains (Cinemark) put
-- geography only on the theatre page itself, so discovering it costs a fetch;
-- persisting it means that cost is paid once ever, not once per process.
CREATE TABLE IF NOT EXISTS venue_geo (
    venue_id   TEXT PRIMARY KEY,
    chain      TEXT NOT NULL,
    name       TEXT,
    lat        REAL,
    lon        REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | pathlib.Path = "screenwatch.db") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    @classmethod
    def memory(cls) -> Store:
        return cls(":memory:")

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        """Add alert columns to databases created before the alert overhaul."""
        columns = {
            "last_success": "TEXT",
            "last_error": "TEXT",
            "last_warning": "TEXT",
            "error_count": "INTEGER NOT NULL DEFAULT 0",
        }
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(watches)")
        }
        for name, definition in columns.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE watches ADD COLUMN {name} {definition}"
                )

        hit_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(watch_hits)")
        }
        if "event_key" not in hit_columns:
            self._conn.execute(
                "ALTER TABLE watch_hits ADD COLUMN event_key TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hits_event "
            "ON watch_hits(watch_id, event_key) WHERE event_key <> ''"
        )

    @contextmanager
    def tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ----------------------------------------------------------- identity
    def put_work(self, work: Work) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO works
                   (work_id, title, year, tmdb_id, runtime_min, original_title, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(work_id) DO UPDATE SET
                     title=excluded.title, year=excluded.year, tmdb_id=excluded.tmdb_id,
                     runtime_min=excluded.runtime_min, updated_at=excluded.updated_at""",
                (work.work_id, work.title, work.year, work.tmdb_id,
                 work.runtime_min, work.original_title, _now()),
            )

    def get_work(self, work_id: str) -> Work | None:
        row = self._conn.execute(
            "SELECT * FROM works WHERE work_id=?", (work_id,)
        ).fetchone()
        if not row:
            return None
        return Work(
            work_id=row["work_id"], title=row["title"], year=row["year"],
            tmdb_id=row["tmdb_id"], runtime_min=row["runtime_min"],
            original_title=row["original_title"],
        )

    def put_link(self, link: TitleLink) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO title_links
                   (source, source_movie_id, raw_title, work_id, method, confidence,
                    kind, attrs, linked_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, source_movie_id) DO UPDATE SET
                     work_id=excluded.work_id, method=excluded.method,
                     confidence=excluded.confidence, raw_title=excluded.raw_title,
                     linked_at=excluded.linked_at""",
                (link.source, link.source_movie_id, link.raw_title, link.work_id,
                 link.method.value, link.confidence, link.kind.value,
                 json.dumps(sorted(a.value for a in link.attrs)), _now()),
            )

    def get_link(self, source: str, source_movie_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM title_links WHERE source=? AND source_movie_id=?",
            (source, str(source_movie_id)),
        ).fetchone()
        return dict(row) if row else None

    def links_needing_review(self, threshold: float = 0.75) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM title_links WHERE work_id IS NULL OR confidence < ? "
            "ORDER BY confidence",
            (threshold,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- screenings
    def upsert_screening(self, screening_id: str, *, work_id: str | None,
                         venue_id: str, chain: str, starts_at_utc: datetime,
                         presentation: str, availability: str,
                         deeplink: str | None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO screenings
                   (screening_id, work_id, venue_id, chain, starts_at_utc,
                    presentation, availability, deeplink, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(screening_id) DO UPDATE SET
                     availability=excluded.availability, last_seen=excluded.last_seen""",
                (screening_id, work_id, venue_id, chain, starts_at_utc.isoformat(),
                 presentation, availability, deeplink, _now(), _now()),
            )

    # -------------------------------------------------------------- seats
    def put_seat_snapshot(self, screening_id: str, available: int, capacity: int,
                          payload: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO seat_snapshots VALUES (?,?,?,?,?)",
                (screening_id, _now(), available, capacity, json.dumps(payload)),
            )

    def seat_history(self, screening_id: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT captured_at, available, capacity FROM seat_snapshots "
            "WHERE screening_id=? ORDER BY captured_at DESC LIMIT ?",
            (screening_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ watches
    def create_watch(self, watch_id: str, label: str, spec_json: str, *,
                     user_id: str = DEFAULT_USER, cadence_s: int = 300,
                     webhook: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO watches (watch_id, user_id, label, spec, cadence_s, "
                "active, webhook, created_at) VALUES (?,?,?,?,?,1,?,?)",
                (watch_id, user_id, label, spec_json, cadence_s, webhook, _now()),
            )

    def list_watches(self, user_id: str = DEFAULT_USER, *, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM watches WHERE user_id=?"
        if active_only:
            sql += " AND active=1"
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY created_at", (user_id,))]

    def get_watch(self, watch_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM watches WHERE watch_id=?", (watch_id,)
        ).fetchone()
        return dict(row) if row else None

    def cancel_watch(self, watch_id: str, *, user_id: str = DEFAULT_USER) -> bool:
        with self.tx() as c:
            return c.execute(
                "UPDATE watches SET active=0 WHERE watch_id=? AND user_id=?",
                (watch_id, user_id),
            ).rowcount > 0

    def touch_watch(
        self,
        watch_id: str,
        *,
        hit: bool = False,
        success: bool = False,
        error: str | None = None,
        warning: str | None = None,
    ) -> None:
        now = _now()
        fields = ["last_run=?"]
        values: list[object] = [now]
        if hit:
            fields.append("last_hit=?")
            values.append(now)
        if success:
            fields.extend([
                "last_success=?",
                "last_error=NULL",
                "last_warning=?",
                "error_count=0",
            ])
            values.append(now)
            values.append(warning)
        elif error:
            fields.extend(["last_error=?", "error_count=error_count+1"])
            values.append(error)
        values.append(watch_id)
        with self.tx() as c:
            c.execute(
                f"UPDATE watches SET {', '.join(fields)} WHERE watch_id=?",
                values,
            )

    # ----------------------------------------------------------- seen-set
    def unseen(self, watch_id: str, screening_ids: list[str]) -> list[str]:
        """Which of these has this watch never reported before.

        This is the baseline for *new* screening alerts. Positive changes to
        an existing screening are compared through `watch_states` instead.
        """
        if not screening_ids:
            return []
        marks = ",".join("?" * len(screening_ids))
        seen = {
            r[0] for r in self._conn.execute(
                f"SELECT screening_id FROM watch_seen WHERE watch_id=? "
                f"AND screening_id IN ({marks})",
                (watch_id, *screening_ids),
            )
        }
        return [s for s in screening_ids if s not in seen]

    def mark_seen(self, watch_id: str, screening_ids: list[str]) -> None:
        if not screening_ids:
            return
        with self.tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO watch_seen VALUES (?,?,?)",
                [(watch_id, s, _now()) for s in screening_ids],
            )

    def seen_ids(self, watch_id: str, screening_ids: list[str]) -> set[str]:
        """Return the subset already observed by this watch."""
        if not screening_ids:
            return set()
        marks = ",".join("?" * len(screening_ids))
        return {
            row[0]
            for row in self._conn.execute(
                f"SELECT screening_id FROM watch_seen WHERE watch_id=? "
                f"AND screening_id IN ({marks})",
                (watch_id, *screening_ids),
            )
        }

    def watch_states(self, watch_id: str) -> dict[str, dict]:
        rows = self._conn.execute(
            "SELECT screening_id, state FROM watch_states WHERE watch_id=?",
            (watch_id,),
        ).fetchall()
        return {row["screening_id"]: json.loads(row["state"]) for row in rows}

    def observe_watch(self, watch_id: str, states: dict[str, dict]) -> None:
        """Persist the latest state while advancing the legacy seen-set."""
        if not states:
            return
        now = _now()
        with self.tx() as c:
            c.executemany(
                """INSERT INTO watch_states (watch_id, screening_id, state, observed_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(watch_id, screening_id) DO UPDATE SET
                     state=excluded.state, observed_at=excluded.observed_at""",
                [
                    (watch_id, screening_id, json.dumps(state, sort_keys=True), now)
                    for screening_id, state in states.items()
                ],
            )
            c.executemany(
                "INSERT OR IGNORE INTO watch_seen VALUES (?,?,?)",
                [(watch_id, screening_id, now) for screening_id in states],
            )

    def seed_seen(self, watch_id: str, screening_ids: list[str]) -> None:
        """Record what already existed when a watch was created.

        Without this, a new watch immediately 'finds' every current showing
        and pages you about tickets that have been on sale for a week.
        """
        self.mark_seen(watch_id, screening_ids)

    # --------------------------------------------------------------- hits
    def record_hit(
        self,
        watch_id: str,
        screening_id: str,
        payload: dict,
        *,
        event_key: str = "",
    ) -> int:
        """Persist a hit and return its id (legacy-compatible API)."""
        hit_id, _inserted = self.record_hit_status(
            watch_id, screening_id, payload, event_key=event_key
        )
        return hit_id

    def record_hit_status(
        self,
        watch_id: str,
        screening_id: str,
        payload: dict,
        *,
        event_key: str = "",
    ) -> tuple[int, bool]:
        with self.tx() as c:
            if event_key:
                c.execute(
                    "INSERT OR IGNORE INTO watch_hits "
                    "(watch_id, screening_id, event_key, payload, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (watch_id, screening_id, event_key, json.dumps(payload), _now()),
                )
                row = c.execute(
                    "SELECT hit_id FROM watch_hits WHERE watch_id=? AND event_key=?",
                    (watch_id, event_key),
                ).fetchone()
                inserted = c.execute("SELECT changes()").fetchone()[0] == 1
            else:
                cur = c.execute(
                    "INSERT INTO watch_hits "
                    "(watch_id, screening_id, payload, created_at) VALUES (?,?,?,?)",
                    (watch_id, screening_id, json.dumps(payload), _now()),
                )
                row = (cur.lastrowid,)
                inserted = True
            return row[0], inserted

    def queue_delivery(self, hit_id: int, channel: str = "webhook") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO watch_deliveries (hit_id, channel) VALUES (?,?)",
                (hit_id, channel),
            )

    def backfill_deliveries(self, watch_id: str, channel: str = "webhook") -> None:
        """Make pre-overhaul hits eligible for the new channel ledger."""
        with self.tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO watch_deliveries (hit_id, channel)
                   SELECT hit_id, ? FROM watch_hits WHERE watch_id=?""",
                (channel, watch_id),
            )

    def pending_webhook_hits(
        self,
        watch_id: str,
        *,
        now: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        now = now or _now()
        rows = self._conn.execute(
            """SELECT h.hit_id, h.screening_id, h.payload, h.created_at,
                      d.attempts, d.last_error, w.webhook, w.label
                 FROM watch_hits h
                 JOIN watch_deliveries d ON d.hit_id=h.hit_id
                 JOIN watches w ON w.watch_id=h.watch_id
                WHERE h.watch_id=? AND d.channel='webhook'
                  AND d.delivered_at IS NULL
                  AND (d.next_attempt_at IS NULL OR d.next_attempt_at<=?)
                ORDER BY h.created_at, h.hit_id
                LIMIT ?""",
            (watch_id, now, limit),
        ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

    def record_delivery_result(
        self,
        hit_ids: list[int],
        *,
        channel: str = "webhook",
        success: bool,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not hit_ids:
            return
        now = now or datetime.now(UTC)
        with self.tx() as c:
            for hit_id in hit_ids:
                row = c.execute(
                    "SELECT attempts FROM watch_deliveries "
                    "WHERE hit_id=? AND channel=?",
                    (hit_id, channel),
                ).fetchone()
                if row is None:
                    continue
                attempts = row[0] + 1
                if success:
                    c.execute(
                        """UPDATE watch_deliveries
                              SET attempts=?, last_attempt_at=?, delivered_at=?,
                                  next_attempt_at=NULL, last_error=NULL
                            WHERE hit_id=? AND channel=?""",
                        (attempts, now.isoformat(), now.isoformat(), hit_id, channel),
                    )
                else:
                    delay = min(3600, 60 * (2 ** min(attempts - 1, 5)))
                    retry_at = now + timedelta(seconds=delay)
                    c.execute(
                        """UPDATE watch_deliveries
                              SET attempts=?, last_attempt_at=?,
                                  next_attempt_at=?, last_error=?
                            WHERE hit_id=? AND channel=?""",
                        (attempts, now.isoformat(), retry_at.isoformat(), error,
                         hit_id, channel),
                    )

    def pending_hits(self, user_id: str = DEFAULT_USER, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT h.* FROM watch_hits h JOIN watches w USING (watch_id) "
            "WHERE w.user_id=? AND h.delivered=0 ORDER BY h.created_at LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def mark_delivered(
        self, hit_ids: list[int], *, user_id: str = DEFAULT_USER
    ) -> None:
        if not hit_ids:
            return
        with self.tx() as c:
            c.executemany(
                """UPDATE watch_hits SET delivered=1
                     WHERE hit_id=? AND EXISTS (
                       SELECT 1 FROM watches
                        WHERE watches.watch_id=watch_hits.watch_id
                          AND watches.user_id=?
                     )""",
                [(h, user_id) for h in hit_ids],
            )

    # ---------------------------------------------------------- venue geo
    def put_venue_geo(self, venue_id: str, chain: str, name: str | None,
                      lat: float | None, lon: float | None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO venue_geo VALUES (?,?,?,?,?,?)",
                (venue_id, chain, name, lat, lon, _now()),
            )

    def venue_geo(self, chain: str) -> dict[str, dict]:
        rows = self._conn.execute(
            "SELECT * FROM venue_geo WHERE chain=?", (chain,)
        ).fetchall()
        return {r["venue_id"]: dict(r) for r in rows}

    # -------------------------------------------------------------- prefs
    def set_pref(self, key: str, value, user_id: str = DEFAULT_USER) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO user_prefs VALUES (?,?,?)",
                (user_id, key, json.dumps(value)),
            )

    def get_pref(self, key: str, default=None, user_id: str = DEFAULT_USER):
        row = self._conn.execute(
            "SELECT value FROM user_prefs WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone()
        return json.loads(row["value"]) if row else default
