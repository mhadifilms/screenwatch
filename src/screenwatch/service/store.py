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
from datetime import datetime, timezone

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
    watch_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    label      TEXT NOT NULL,
    spec       TEXT NOT NULL,
    cadence_s  INTEGER NOT NULL DEFAULT 300,
    active     INTEGER NOT NULL DEFAULT 1,
    webhook    TEXT,
    created_at TEXT NOT NULL,
    last_run   TEXT,
    last_hit   TEXT
);
CREATE INDEX IF NOT EXISTS idx_watches_user ON watches(user_id, active);

-- The seen-set. This is what makes "tell me about NEW 70mm tickets" work
-- when some tickets already dropped: a screening only fires once, ever.
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
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hits_undelivered ON watch_hits(watch_id, delivered);

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
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | pathlib.Path = "screenwatch.db") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @classmethod
    def memory(cls) -> "Store":
        return cls(":memory:")

    def close(self) -> None:
        self._conn.close()

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

    def cancel_watch(self, watch_id: str) -> bool:
        with self.tx() as c:
            return c.execute(
                "UPDATE watches SET active=0 WHERE watch_id=?", (watch_id,)
            ).rowcount > 0

    def touch_watch(self, watch_id: str, *, hit: bool = False) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE watches SET last_run=?" + (", last_hit=?" if hit else "")
                + " WHERE watch_id=?",
                (_now(), _now(), watch_id) if hit else (_now(), watch_id),
            )

    # ----------------------------------------------------------- seen-set
    def unseen(self, watch_id: str, screening_ids: list[str]) -> list[str]:
        """Which of these has this watch never reported before.

        The whole point of a watch is that it fires on *new* screenings. If
        some 70mm tickets already dropped before the watch was created, those
        are not news; the ones that appear afterwards are.
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

    def seed_seen(self, watch_id: str, screening_ids: list[str]) -> None:
        """Record what already existed when a watch was created.

        Without this, a new watch immediately 'finds' every current showing
        and pages you about tickets that have been on sale for a week.
        """
        self.mark_seen(watch_id, screening_ids)

    # --------------------------------------------------------------- hits
    def record_hit(self, watch_id: str, screening_id: str, payload: dict) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO watch_hits (watch_id, screening_id, payload, created_at) "
                "VALUES (?,?,?,?)",
                (watch_id, screening_id, json.dumps(payload), _now()),
            )
            return cur.lastrowid

    def pending_hits(self, user_id: str = DEFAULT_USER, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT h.* FROM watch_hits h JOIN watches w USING (watch_id) "
            "WHERE w.user_id=? AND h.delivered=0 ORDER BY h.created_at LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def mark_delivered(self, hit_ids: list[int]) -> None:
        if not hit_ids:
            return
        with self.tx() as c:
            c.executemany(
                "UPDATE watch_hits SET delivered=1 WHERE hit_id=?",
                [(h,) for h in hit_ids],
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
