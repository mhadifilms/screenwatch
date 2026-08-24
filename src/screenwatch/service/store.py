"""SQLite persistence.

Single-user locally, but every user-scoped table carries `user_id` from the
start. Adding it later would mean a migration across watches, seen-sets and
preferences at exactly the point the system has real data in it.

WAL mode, one writer, no ORM. The access patterns are small and known.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ..identity.work import TitleLink, Work
from ..seating.capture import (
    SeatCapture,
    SeatProbe,
    availability_payload,
    layout_fingerprint,
    static_layout_payload,
)
from ..seating.model import Auditorium, Seat, SeatKind, seat_failure

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
    canonical_id  TEXT,
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

-- Provider ids are aliases, not identities.  A chain can rotate or recycle
-- its showtime handle; this table keeps the durable mapping to the
-- source-independent showing id used by watches and cross-provider joins.
CREATE TABLE IF NOT EXISTS screening_aliases (
    source_screening_id TEXT PRIMARY KEY,
    canonical_id        TEXT NOT NULL,
    source              TEXT NOT NULL,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screening_aliases_canonical
    ON screening_aliases(canonical_id);

CREATE TABLE IF NOT EXISTS seat_snapshots (
    screening_id TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    available    INTEGER NOT NULL,
    capacity     INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (screening_id, captured_at)
);

-- A probe is everything needed to reproduce one seat request after restart.
CREATE TABLE IF NOT EXISTS seat_probes (
    probe_id             TEXT PRIMARY KEY,
    source               TEXT NOT NULL,
    venue_id             TEXT NOT NULL,
    source_venue_id      TEXT,
    showtime_id          TEXT NOT NULL,
    booking_url          TEXT,
    starts_at_local      TEXT,
    title                TEXT,
    source_screen_id     TEXT,
    ticketing_platform   TEXT,
    metadata             TEXT NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'pending',
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_failure_code    TEXT,
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seat_probes_venue
    ON seat_probes(venue_id, status, starts_at_local);

CREATE TABLE IF NOT EXISTS auditoriums (
    auditorium_id       TEXT PRIMARY KEY,
    venue_id            TEXT NOT NULL,
    display_name        TEXT,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    verification_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auditoriums_venue ON auditoriums(venue_id);

CREATE TABLE IF NOT EXISTS auditorium_aliases (
    source           TEXT NOT NULL,
    source_venue_id  TEXT NOT NULL,
    source_screen_id TEXT NOT NULL,
    auditorium_id    TEXT NOT NULL,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    PRIMARY KEY (source, source_venue_id, source_screen_id)
);

CREATE TABLE IF NOT EXISTS layouts (
    layout_hash        TEXT PRIMARY KEY,
    source_layout_id   TEXT,
    seat_count         INTEGER NOT NULL,
    normalized_payload TEXT NOT NULL,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layout_seats (
    layout_hash      TEXT NOT NULL,
    row_label        TEXT NOT NULL,
    row_index        INTEGER NOT NULL,
    col_label        TEXT NOT NULL,
    col_index        INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    x                REAL NOT NULL,
    y                REAL NOT NULL,
    aisle_adjacent   INTEGER NOT NULL,
    module_id        TEXT,
    module_position  INTEGER,
    module_size      INTEGER,
    module_required  INTEGER NOT NULL,
    PRIMARY KEY (layout_hash, row_index, col_index, row_label, col_label)
);

CREATE TABLE IF NOT EXISTS auditorium_layouts (
    auditorium_id TEXT NOT NULL,
    layout_hash   TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_until   TEXT,
    PRIMARY KEY (auditorium_id, layout_hash, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_auditorium_layouts_current
    ON auditorium_layouts(auditorium_id, valid_until);

CREATE TABLE IF NOT EXISTS raw_captures (
    capture_id    TEXT PRIMARY KEY,
    probe_id      TEXT NOT NULL,
    source        TEXT NOT NULL,
    url           TEXT,
    status_code   INTEGER,
    content_type  TEXT NOT NULL,
    body_hash     TEXT NOT NULL,
    body          BLOB,
    local_path    TEXT,
    captured_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_captures_probe
    ON raw_captures(probe_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS seat_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    screening_id   TEXT NOT NULL,
    probe_id        TEXT NOT NULL,
    auditorium_id  TEXT NOT NULL,
    layout_hash     TEXT NOT NULL,
    raw_capture_id  TEXT,
    captured_at     TEXT NOT NULL,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seat_observations_screening
    ON seat_observations(screening_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS harvest_runs (
    run_id               TEXT PRIMARY KEY,
    venue_id             TEXT,
    city                 TEXT,
    date_start           TEXT NOT NULL,
    date_end             TEXT NOT NULL,
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    showtimes_discovered INTEGER NOT NULL DEFAULT 0,
    probes_attempted     INTEGER NOT NULL DEFAULT 0,
    maps_captured        INTEGER NOT NULL DEFAULT 0,
    screens_discovered   INTEGER NOT NULL DEFAULT 0,
    failures             INTEGER NOT NULL DEFAULT 0,
    failure_counts       TEXT NOT NULL DEFAULT '{}',
    completion_status    TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS harvest_failures (
    failure_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    probe_id      TEXT NOT NULL,
    provider      TEXT NOT NULL,
    stage         TEXT NOT NULL,
    code          TEXT NOT NULL,
    retryable     INTEGER NOT NULL,
    action        TEXT NOT NULL,
    message       TEXT NOT NULL,
    source_url    TEXT,
    status_code   INTEGER,
    raw_capture_id TEXT,
    context       TEXT NOT NULL DEFAULT '{}',
    captured_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harvest_failures_run
    ON harvest_failures(run_id, code);

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
    source_venue_id TEXT,
    source_slug TEXT,
    updated_at TEXT NOT NULL
);

-- The durable venue graph. Fast discovery may be bounded, while exhaustive
-- discovery can add every source record in the requested scope. Once a venue
-- has been observed it should remain queryable after the process restarts.
CREATE TABLE IF NOT EXISTS directory_venues (
    venue_id           TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    chain              TEXT NOT NULL,
    tz                 TEXT,
    lat                REAL,
    lon                REAL,
    market             TEXT,
    city               TEXT,
    state              TEXT,
    ticketing_platform TEXT,
    url                TEXT,
    venue_type         TEXT NOT NULL,
    markup             TEXT,
    notes              TEXT,
    source             TEXT NOT NULL,
    source_url         TEXT,
    observed_at        TEXT,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_directory_venues_chain
    ON directory_venues(chain, venue_type);

-- Every non-trivial venue claim is an observation with a scope. A screening
-- format token is evidence about that screening; a seat map is evidence about
-- the observed room at that moment. Neither is silently promoted to a
-- permanent hardware fact. Keeping this append-only ledger makes freshness,
-- source disagreement, and coverage measurable instead of implicit.
CREATE TABLE IF NOT EXISTS venue_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id       TEXT NOT NULL,
    kind           TEXT NOT NULL,
    subject_key    TEXT NOT NULL,
    payload        TEXT NOT NULL,
    source         TEXT NOT NULL,
    source_url     TEXT,
    observed_at    TEXT NOT NULL,
    evidence_scope TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_venue_observations_venue
    ON venue_observations(venue_id, kind, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_venue_observations_source
    ON venue_observations(source, observed_at DESC);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- Search runs are the product's audit trail. A result can be great and still
-- be incomplete because one provider was clipped or challenged; keeping the
-- run summary makes that visible after the HTTP response is gone.
CREATE TABLE IF NOT EXISTS search_runs (
    run_id          TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    spec            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    duration_ms     REAL NOT NULL,
    considered      INTEGER NOT NULL DEFAULT 0,
    result_count    INTEGER NOT NULL DEFAULT 0,
    seatmaps_fetched INTEGER NOT NULL DEFAULT 0,
    complete        INTEGER NOT NULL DEFAULT 0,
    provider_stats  TEXT NOT NULL DEFAULT '[]',
    errors          TEXT NOT NULL DEFAULT '[]',
    clipped         TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_search_runs_user ON search_runs(user_id, started_at DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


_INVENTORY_GROUPS = {
    "chain": "s.chain",
    "venue": "s.venue_id",
    "venue_type": "COALESCE(d.venue_type, 'unknown')",
    "city": "COALESCE(NULLIF(d.city, ''), 'unknown')",
    "format": "s.presentation",
    "availability": "s.availability",
}
_INVENTORY_LABELS = {
    **_INVENTORY_GROUPS,
    "venue": "COALESCE(NULLIF(d.name, ''), s.venue_id)",
}


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
        """Add columns introduced after the first local database release."""
        screening_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(screenings)")
        }
        if "canonical_id" not in screening_columns:
            self._conn.execute(
                "ALTER TABLE screenings ADD COLUMN canonical_id TEXT"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_screenings_canonical "
            "ON screenings(canonical_id)"
        )

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

        directory_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(directory_venues)")
        }
        for name, definition in {
            "source_url": "TEXT",
            "observed_at": "TEXT",
        }.items():
            if name not in directory_columns:
                self._conn.execute(
                    f"ALTER TABLE directory_venues ADD COLUMN {name} {definition}"
                )
        # Older rows were still observed by the local process; using their
        # update timestamp is more truthful than leaving freshness blank.
        self._conn.execute(
            "UPDATE directory_venues SET observed_at=updated_at "
            "WHERE observed_at IS NULL"
        )

        venue_geo_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(venue_geo)")
        }
        for name in ("source_venue_id", "source_slug"):
            if name not in venue_geo_columns:
                self._conn.execute(f"ALTER TABLE venue_geo ADD COLUMN {name} TEXT")

        failure_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(harvest_failures)")
        }
        if "raw_capture_id" not in failure_columns:
            self._conn.execute(
                "ALTER TABLE harvest_failures ADD COLUMN raw_capture_id TEXT"
            )

    @contextmanager
    def tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _insert_venue_observation(
        c,
        *,
        venue_id: str,
        kind: str,
        subject_key: str,
        payload: dict,
        source: str,
        source_url: str | None,
        observed_at: str,
        evidence_scope: str,
        confidence: float = 1.0,
    ) -> None:
        c.execute(
            """INSERT INTO venue_observations
               (venue_id, kind, subject_key, payload, source, source_url,
                observed_at, evidence_scope, confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                venue_id,
                kind,
                subject_key,
                json.dumps(payload, sort_keys=True),
                source,
                source_url,
                observed_at,
                evidence_scope,
                max(0.0, min(1.0, confidence)),
            ),
        )

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
    @staticmethod
    def _register_alias(c, canonical_id: str, source_screening_id: str,
                         source: str, now: str) -> None:
        c.execute(
            """INSERT INTO screening_aliases
               (source_screening_id, canonical_id, source, first_seen, last_seen)
               VALUES (?,?,?,?,?)
               ON CONFLICT(source_screening_id) DO UPDATE SET
                 canonical_id=excluded.canonical_id,
                 source=excluded.source,
                 last_seen=excluded.last_seen""",
            (source_screening_id, canonical_id, source, now, now),
        )

    def register_screening_alias(self, canonical_id: str,
                                 source_screening_id: str,
                                 *, source: str) -> None:
        """Remember a provider handle even when its row is not ranked.

        Search registers every adapter result before collapsing equivalent
        showings, so a losing duplicate remains available as an alias for
        migration and auditing.
        """
        now = _now()
        with self.tx() as c:
            self._register_alias(c, canonical_id, source_screening_id, source, now)

    def register_screening_aliases(
        self, aliases: list[tuple[str, str, str]]
    ) -> None:
        """Register a batch of ``(canonical_id, source_id, source)`` aliases."""
        if not aliases:
            return
        now = _now()
        with self.tx() as c:
            for canonical_id, source_screening_id, source in aliases:
                self._register_alias(
                    c, canonical_id, source_screening_id, source, now
                )

    def aliases_for_screening(self, canonical_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT source_screening_id FROM screening_aliases "
            "WHERE canonical_id=? ORDER BY source_screening_id",
            (canonical_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def upsert_screening(self, screening_id: str, *, canonical_id: str | None = None,
                         work_id: str | None, venue_id: str, chain: str,
                         starts_at_utc: datetime, presentation: str,
                         availability: str, deeplink: str | None) -> None:
        canonical_id = canonical_id or screening_id
        now = _now()
        with self.tx() as c:
            c.execute(
                """INSERT INTO screenings
                   (screening_id, canonical_id, work_id, venue_id, chain, starts_at_utc,
                    presentation, availability, deeplink, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(screening_id) DO UPDATE SET
                     canonical_id=excluded.canonical_id,
                     work_id=excluded.work_id,
                     venue_id=excluded.venue_id,
                     chain=excluded.chain,
                     starts_at_utc=excluded.starts_at_utc,
                     presentation=excluded.presentation,
                     availability=excluded.availability,
                     deeplink=excluded.deeplink,
                     last_seen=excluded.last_seen""",
                (screening_id, canonical_id, work_id, venue_id, chain,
                 starts_at_utc.isoformat(), presentation, availability, deeplink,
                 now, now),
            )
            self._register_alias(c, canonical_id, screening_id, chain, now)

    # -------------------------------------------------------------- seats
    def put_seat_snapshot(
        self,
        screening_id: str,
        available: int,
        capacity: int,
        payload: dict,
        *,
        venue_id: str | None = None,
        source: str | None = None,
        source_url: str | None = None,
        evidence_scope: str = "screening-seat-map",
    ) -> None:
        """Persist a seat observation and its provenance as one transaction.

        Seat data is time-varying evidence, not a venue inventory census. The
        optional metadata is kept inside the snapshot payload for compatibility
        with the original table and copied into the append-only venue ledger so
        room profiles can show exactly what was observed and where.
        """
        captured_at = _now()
        source = source or "provider:unknown"
        payload = {
            **payload,
            "source": source,
            "source_url": source_url,
            "evidence_scope": evidence_scope,
            "observed_at": captured_at,
        }
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO seat_snapshots VALUES (?,?,?,?,?)",
                (screening_id, captured_at, available, capacity, json.dumps(payload)),
            )
            if venue_id:
                room_id = str(payload.get("screen_id") or screening_id)
                self._insert_venue_observation(
                    c,
                    venue_id=venue_id,
                    kind="room_observation",
                    subject_key=room_id,
                    payload={
                        **payload,
                        "screening_id": screening_id,
                        "available": available,
                        "capacity": capacity,
                    },
                    source=source,
                    source_url=source_url,
                    observed_at=captured_at,
                    evidence_scope=evidence_scope,
                    # Source observation confidence is independent from the
                    # geometry field: a count-only room is still a real
                    # observed count even when its geometry confidence is 0.
                    confidence=1.0,
                )

    def seat_history(self, screening_id: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT captured_at, available, capacity, payload FROM seat_snapshots "
            "WHERE screening_id=? ORDER BY captured_at DESC LIMIT ?",
            (screening_id, limit),
        ).fetchall()
        out = []
        for row in rows:
            item = {
                "captured_at": row["captured_at"],
                "available": row["available"],
                "capacity": row["capacity"],
            }
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            for key in ("source", "source_url", "evidence_scope", "screen_id"):
                if payload.get(key) is not None:
                    item[key] = payload[key]
            out.append(item)
        return out

    # ----------------------------------------------------- durable seat maps
    def put_seat_probe(self, probe: SeatProbe, *, status: str = "pending") -> str:
        now = _now()
        with self.tx() as c:
            c.execute(
                """INSERT INTO seat_probes
                   (probe_id, source, venue_id, source_venue_id, showtime_id,
                    booking_url, starts_at_local, title, source_screen_id,
                    ticketing_platform, metadata, status, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(probe_id) DO UPDATE SET
                     booking_url=COALESCE(excluded.booking_url, seat_probes.booking_url),
                     starts_at_local=COALESCE(excluded.starts_at_local,
                                              seat_probes.starts_at_local),
                     title=COALESCE(excluded.title, seat_probes.title),
                     source_screen_id=COALESCE(excluded.source_screen_id,
                                               seat_probes.source_screen_id),
                     ticketing_platform=COALESCE(excluded.ticketing_platform,
                                                 seat_probes.ticketing_platform),
                     metadata=excluded.metadata,
                     status=CASE WHEN seat_probes.status='captured'
                                 THEN seat_probes.status ELSE excluded.status END,
                     last_seen=excluded.last_seen""",
                (
                    probe.probe_id,
                    probe.source,
                    probe.venue_id,
                    probe.source_venue_id,
                    probe.showtime_id,
                    probe.booking_url,
                    probe.starts_at_local.isoformat() if probe.starts_at_local else None,
                    probe.title,
                    probe.source_screen_id,
                    probe.ticketing_platform,
                    json.dumps(probe.metadata, sort_keys=True, default=str),
                    status,
                    now,
                    now,
                ),
            )
        return probe.probe_id

    def get_seat_probe(self, probe_id: str) -> SeatProbe | None:
        row = self._conn.execute(
            "SELECT * FROM seat_probes WHERE probe_id=?", (probe_id,)
        ).fetchone()
        if row is None:
            return None
        return SeatProbe.from_dict({
            **dict(row),
            "metadata": json.loads(row["metadata"] or "{}"),
        })

    def mark_seat_probe_status(self, probe_id: str, status: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE seat_probes SET status=?, last_seen=? WHERE probe_id=?",
                (status, _now(), probe_id),
            )

    def seat_probes(
        self,
        *,
        venue_id: str | None = None,
        statuses: tuple[str, ...] = ("pending", "retry"),
        limit: int = 1000,
    ) -> list[SeatProbe]:
        clauses: list[str] = []
        params: list[object] = []
        if venue_id:
            clauses.append("venue_id=?")
            params.append(venue_id)
        if statuses:
            marks = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({marks})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 100_000)))
        rows = self._conn.execute(
            f"SELECT * FROM seat_probes {where} "
            "ORDER BY starts_at_local, probe_id LIMIT ?",
            params,
        ).fetchall()
        return [
            SeatProbe.from_dict({
                **dict(row),
                "metadata": json.loads(row["metadata"] or "{}"),
            })
            for row in rows
        ]

    def _raw_capture_path(
        self, body_hash: str, payload: bytes, content_type: str
    ) -> str | None:
        if self.path == ":memory:" or not payload:
            return None
        suffix = {
            "application/json": ".json",
            "text/html": ".html",
        }.get(content_type.split(";", 1)[0].lower(), ".bin")
        db_path = pathlib.Path(self.path).resolve()
        directory = db_path.parent / f"{db_path.stem}.captures"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{body_hash}{suffix}"
        if not destination.exists():
            destination.write_bytes(payload)
        return str(destination)

    @staticmethod
    def _auditorium_id(c, capture: SeatCapture, captured_at: str) -> tuple[str, str]:
        probe = capture.probe
        room = capture.auditorium
        source_venue_id = probe.source_venue_id or probe.venue_id
        source_screen_id = room.screen_id or probe.source_screen_id or ""
        if source_screen_id:
            alias = c.execute(
                """SELECT auditorium_id FROM auditorium_aliases
                   WHERE source=? AND source_venue_id=? AND source_screen_id=?""",
                (probe.source, source_venue_id, source_screen_id),
            ).fetchone()
            if alias:
                return str(alias["auditorium_id"]), "source_id_verified"
            identity = f"{probe.venue_id}|{probe.source}|{source_venue_id}|{source_screen_id}"
            status = "source_id_verified"
        else:
            # A map without a source room id is an observation, not proof that
            # another showtime with the same geometry used the same room.
            identity = f"{probe.venue_id}|unidentified|{probe.probe_id}|{captured_at}"
            status = "unidentified_observation"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        return f"aud_{digest}", status

    def put_seat_capture(
        self,
        capture: SeatCapture,
        *,
        screening_id: str | None = None,
    ) -> dict[str, object]:
        """Persist raw evidence, static geometry, and live state atomically."""
        probe = capture.probe
        room = capture.auditorium
        self.put_seat_probe(probe)
        captured_at = capture.captured_at.astimezone(UTC).isoformat()
        layout_hash = layout_fingerprint(room)
        static_payload = static_layout_payload(room)
        live_payload = availability_payload(room)
        body_hash = hashlib.sha256(capture.raw_payload).hexdigest()
        capture_seed = (
            f"{probe.probe_id}|{captured_at}|{body_hash}|{capture.source_url or ''}"
        )
        raw_capture_id = f"capture_{hashlib.sha256(capture_seed.encode()).hexdigest()[:24]}"
        local_path = self._raw_capture_path(
            body_hash, capture.raw_payload, capture.raw_content_type
        )
        observed_screening = (
            screening_id
            or str(probe.metadata.get("screening_id") or "")
            or f"{probe.source}:{probe.showtime_id}"
        )

        with self.tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO raw_captures
                   (capture_id, probe_id, source, url, status_code, content_type,
                    body_hash, body, local_path, captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    raw_capture_id,
                    probe.probe_id,
                    probe.ticketing_platform or probe.source,
                    capture.source_url or probe.booking_url,
                    capture.status_code,
                    capture.raw_content_type,
                    body_hash,
                    capture.raw_payload if local_path is None else None,
                    local_path,
                    captured_at,
                ),
            )
            auditorium_id, verification = self._auditorium_id(c, capture, captured_at)
            c.execute(
                """INSERT INTO auditoriums
                   (auditorium_id, venue_id, display_name, first_seen, last_seen,
                    verification_status)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(auditorium_id) DO UPDATE SET
                     display_name=COALESCE(excluded.display_name,
                                           auditoriums.display_name),
                     last_seen=excluded.last_seen,
                     verification_status=excluded.verification_status""",
                (
                    auditorium_id,
                    probe.venue_id,
                    room.name,
                    captured_at,
                    captured_at,
                    verification,
                ),
            )
            source_screen_id = room.screen_id or probe.source_screen_id or ""
            if source_screen_id:
                c.execute(
                    """INSERT INTO auditorium_aliases
                       (source, source_venue_id, source_screen_id, auditorium_id,
                        first_seen, last_seen)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(source, source_venue_id, source_screen_id)
                       DO UPDATE SET auditorium_id=excluded.auditorium_id,
                                     last_seen=excluded.last_seen""",
                    (
                        probe.source,
                        probe.source_venue_id or probe.venue_id,
                        source_screen_id,
                        auditorium_id,
                        captured_at,
                        captured_at,
                    ),
                )
            c.execute(
                """INSERT INTO layouts
                   (layout_hash, source_layout_id, seat_count, normalized_payload,
                    first_seen, last_seen)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(layout_hash) DO UPDATE SET
                     source_layout_id=COALESCE(excluded.source_layout_id,
                                               layouts.source_layout_id),
                     last_seen=excluded.last_seen""",
                (
                    layout_hash,
                    capture.source_layout_id,
                    room.capacity,
                    json.dumps(static_payload, sort_keys=True, separators=(",", ":")),
                    captured_at,
                    captured_at,
                ),
            )
            c.executemany(
                """INSERT OR IGNORE INTO layout_seats
                   (layout_hash, row_label, row_index, col_label, col_index, kind,
                    x, y, aisle_adjacent, module_id, module_position, module_size,
                    module_required)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        layout_hash,
                        seat["row_label"],
                        seat["row_index"],
                        seat["col_label"],
                        seat["col_index"],
                        seat["kind"],
                        seat["x"],
                        seat["y"],
                        int(bool(seat["aisle_adjacent"])),
                        seat["module_id"],
                        seat["module_position"],
                        seat["module_size"],
                        int(bool(seat["module_required"])),
                    )
                    for seat in static_payload["seats"]
                ],
            )
            current = c.execute(
                """SELECT layout_hash FROM auditorium_layouts
                   WHERE auditorium_id=? AND valid_until IS NULL
                   ORDER BY valid_from DESC LIMIT 1""",
                (auditorium_id,),
            ).fetchone()
            if current is None or current["layout_hash"] != layout_hash:
                c.execute(
                    """UPDATE auditorium_layouts SET valid_until=?
                       WHERE auditorium_id=? AND valid_until IS NULL""",
                    (captured_at, auditorium_id),
                )
                c.execute(
                    """INSERT INTO auditorium_layouts
                       (auditorium_id, layout_hash, valid_from, valid_until)
                       VALUES (?,?,?,NULL)""",
                    (auditorium_id, layout_hash, captured_at),
                )
            c.execute(
                """INSERT INTO seat_observations
                   (screening_id, probe_id, auditorium_id, layout_hash,
                    raw_capture_id, captured_at, payload)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    observed_screening,
                    probe.probe_id,
                    auditorium_id,
                    layout_hash,
                    raw_capture_id,
                    captured_at,
                    json.dumps(live_payload, sort_keys=True, separators=(",", ":")),
                ),
            )
            c.execute(
                "INSERT OR REPLACE INTO seat_snapshots VALUES (?,?,?,?,?)",
                (
                    observed_screening,
                    captured_at,
                    room.available,
                    room.capacity,
                    json.dumps({
                        "geometry_confidence": room.geometry_confidence,
                        "screen_id": room.screen_id,
                        "screen_name": room.name,
                        "row_count": room.row_count,
                        "row_lengths": list(room.row_lengths),
                        "has_grid": room.has_grid,
                        "layout_hash": layout_hash,
                        "auditorium_id": auditorium_id,
                        "raw_capture_id": raw_capture_id,
                        "source": probe.ticketing_platform or probe.source,
                        "source_url": capture.source_url or probe.booking_url,
                        "evidence_scope": "screening-seat-map",
                        "observed_at": captured_at,
                    }, sort_keys=True),
                ),
            )
            c.execute(
                """UPDATE seat_probes
                   SET status='captured', attempts=attempts+1,
                       last_failure_code=NULL, last_seen=? WHERE probe_id=?""",
                (captured_at, probe.probe_id),
            )
            self._insert_venue_observation(
                c,
                venue_id=probe.venue_id,
                kind="room_layout",
                subject_key=auditorium_id,
                payload={
                    "screening_id": observed_screening,
                    "screen_id": source_screen_id,
                    "screen_name": room.name,
                    "layout_hash": layout_hash,
                    "available": room.available,
                    "capacity": room.capacity,
                    "raw_capture_id": raw_capture_id,
                },
                source=probe.ticketing_platform or probe.source,
                source_url=capture.source_url or probe.booking_url,
                observed_at=captured_at,
                evidence_scope="screening-seat-map",
                confidence=1.0,
            )
        return {
            "probe_id": probe.probe_id,
            "auditorium_id": auditorium_id,
            "layout_hash": layout_hash,
            "raw_capture_id": raw_capture_id,
            "raw_capture_path": local_path,
        }

    def reuse_stored_geometry(self, capture: SeatCapture) -> SeatCapture:
        """Reconcile a live capture with the current durable room layout.

        Availability is never loaded from SQLite here. When a live source
        supplies a full grid, the stored topology is reused only after its
        static fingerprint matches exactly; the live statuses are then laid
        over that topology. When a live source supplies counts but no grid,
        the cached room contributes only row lengths. This keeps normal search
        from pretending that an old availability observation is current.
        """
        probe = capture.probe
        room = capture.auditorium
        source_screen_id = room.screen_id or probe.source_screen_id or ""
        if not source_screen_id:
            return capture
        row = self._conn.execute(
            """SELECT l.normalized_payload
               FROM auditorium_aliases aa
               JOIN auditorium_layouts al
                 ON al.auditorium_id=aa.auditorium_id
                AND al.valid_until IS NULL
               JOIN layouts l ON l.layout_hash=al.layout_hash
               WHERE aa.source=? AND aa.source_venue_id=?
                 AND aa.source_screen_id=?
               ORDER BY al.valid_from DESC LIMIT 1""",
            (
                probe.source,
                probe.source_venue_id or probe.venue_id,
                source_screen_id,
            ),
        ).fetchone()
        if row is None:
            return capture
        try:
            cached = json.loads(row["normalized_payload"])
        except (TypeError, json.JSONDecodeError):
            return capture

        if room.has_grid:
            if static_layout_payload(room) != cached:
                # A changed source layout must flow through persistence so the
                # current version is closed and a new interval is created.
                return capture
            statuses = {
                (seat.row_label, seat.row_index, seat.col_label, seat.col_index):
                    seat.status
                for seat in room.seats
            }
            seats = tuple(
                Seat(
                    row_label=str(item["row_label"]),
                    row_index=int(item["row_index"]),
                    col_label=str(item["col_label"]),
                    col_index=int(item["col_index"]),
                    status=statuses[(
                        str(item["row_label"]), int(item["row_index"]),
                        str(item["col_label"]), int(item["col_index"]),
                    )],
                    kind=SeatKind(str(item["kind"])),
                    x=float(item["x"]),
                    y=float(item["y"]),
                    aisle_adjacent=bool(item["aisle_adjacent"]),
                    module_id=item.get("module_id"),
                    module_position=item.get("module_position"),
                    module_size=item.get("module_size"),
                    module_required=bool(item.get("module_required")),
                )
                for item in cached.get("seats", [])
            )
            return replace(capture, auditorium=Auditorium(
                venue_id=room.venue_id,
                screen_id=room.screen_id,
                seats=seats,
                geometry_confidence=float(cached.get(
                    "geometry_confidence", room.geometry_confidence
                )),
                reported_available=room.reported_available,
                name=room.name,
                reported_capacity=room.reported_capacity,
                row_lengths=tuple(cached.get("row_lengths") or room.row_lengths),
            ))

        if room.row_lengths:
            return capture
        row_counts: dict[int, int] = {}
        for item in cached.get("seats", []):
            if str(item.get("kind")) != SeatKind.BLOCKED.value:
                index = int(item["row_index"])
                row_counts[index] = row_counts.get(index, 0) + 1
        row_lengths = tuple(row_counts[index] for index in sorted(row_counts))
        if not row_lengths:
            row_lengths = tuple(int(value) for value in cached.get("row_lengths", []))
        if not row_lengths:
            return capture
        return replace(capture, auditorium=Auditorium(
            venue_id=room.venue_id,
            screen_id=room.screen_id,
            seats=(),
            geometry_confidence=float(cached.get(
                "geometry_confidence", room.geometry_confidence
            )),
            reported_available=room.reported_available,
            name=room.name,
            reported_capacity=room.reported_capacity,
            row_lengths=row_lengths,
        ))

    def record_harvest_failure(
        self,
        run_id: str,
        probe: SeatProbe,
        exc: Exception,
        *,
        stage: str = "fetch",
        status_code: int | None = None,
    ) -> dict[str, object]:
        self.put_seat_probe(probe)
        failure = seat_failure(exc)
        captured_at = _now()
        raw_payload = getattr(exc, "raw_payload", None)
        source_url = getattr(exc, "source_url", None) or probe.booking_url
        effective_status = status_code or getattr(exc, "status_code", None)
        raw_capture_id = None
        raw_capture_path = None
        body_hash = None
        if raw_payload is not None:
            body_hash = hashlib.sha256(raw_payload).hexdigest()
            raw_capture_id = (
                "capture_"
                + hashlib.sha256(
                    f"failure|{probe.probe_id}|{captured_at}|{body_hash}".encode()
                ).hexdigest()[:24]
            )
            raw_capture_path = self._raw_capture_path(
                body_hash,
                raw_payload,
                getattr(exc, "raw_content_type", "application/octet-stream"),
            )
            failure["context"] = {
                **dict(failure["context"]),
                "raw_capture_id": raw_capture_id,
                "raw_capture_path": raw_capture_path,
            }
        with self.tx() as c:
            if raw_capture_id is not None:
                c.execute(
                    """INSERT INTO raw_captures
                       (capture_id, probe_id, source, url, status_code, content_type,
                        body_hash, body, local_path, captured_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        raw_capture_id,
                        probe.probe_id,
                        probe.ticketing_platform or probe.source,
                        source_url,
                        effective_status,
                        getattr(exc, "raw_content_type", "application/octet-stream"),
                        body_hash,
                        raw_payload if raw_capture_path is None else None,
                        raw_capture_path,
                        captured_at,
                    ),
                )
            c.execute(
                """INSERT INTO harvest_failures
                   (run_id, probe_id, provider, stage, code, retryable, action,
                    message, source_url, status_code, raw_capture_id, context,
                    captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    probe.probe_id,
                    probe.ticketing_platform or probe.source,
                    stage,
                    failure["code"],
                    int(bool(failure["retryable"])),
                    failure["action"],
                    failure["message"],
                    source_url,
                    effective_status,
                    raw_capture_id,
                    json.dumps(failure["context"], sort_keys=True, default=str),
                    captured_at,
                ),
            )
            c.execute(
                """UPDATE seat_probes
                   SET status=?, attempts=attempts+1, last_failure_code=?, last_seen=?
                   WHERE probe_id=?""",
                (
                    "retry" if failure["retryable"] else "failed",
                    failure["code"],
                    captured_at,
                    probe.probe_id,
                ),
            )
        return failure

    def create_harvest_run(
        self,
        run_id: str,
        *,
        venue_id: str | None,
        city: str | None,
        date_start: str,
        date_end: str,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO harvest_runs
                   (run_id, venue_id, city, date_start, date_end, started_at)
                   VALUES (?,?,?,?,?,?)""",
                (run_id, venue_id, city, date_start, date_end, _now()),
            )

    def finish_harvest_run(
        self,
        run_id: str,
        *,
        showtimes_discovered: int,
        probes_attempted: int,
        maps_captured: int,
        screens_discovered: int,
        failure_counts: dict[str, int],
        completion_status: str,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE harvest_runs SET
                   finished_at=?, showtimes_discovered=?, probes_attempted=?,
                   maps_captured=?, screens_discovered=?, failures=?,
                   failure_counts=?, completion_status=? WHERE run_id=?""",
                (
                    _now(),
                    showtimes_discovered,
                    probes_attempted,
                    maps_captured,
                    screens_discovered,
                    sum(failure_counts.values()),
                    json.dumps(failure_counts, sort_keys=True),
                    completion_status,
                    run_id,
                ),
            )

    def rooms(self, venue_id: str) -> list[dict[str, object]]:
        rows = self._conn.execute(
            """SELECT a.*, al.layout_hash, l.seat_count, l.normalized_payload,
                      al.valid_from
               FROM auditoriums a
               LEFT JOIN auditorium_layouts al
                 ON al.auditorium_id=a.auditorium_id AND al.valid_until IS NULL
               LEFT JOIN layouts l ON l.layout_hash=al.layout_hash
               WHERE a.venue_id=? ORDER BY a.display_name, a.auditorium_id""",
            (venue_id,),
        ).fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            aliases = self._conn.execute(
                """SELECT source, source_venue_id, source_screen_id
                   FROM auditorium_aliases WHERE auditorium_id=?
                   ORDER BY source, source_screen_id""",
                (row["auditorium_id"],),
            ).fetchall()
            item = dict(row)
            item["aliases"] = [dict(alias) for alias in aliases]
            item["layout"] = (
                json.loads(item.pop("normalized_payload"))
                if item.get("normalized_payload") else None
            )
            out.append(item)
        return out

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

    def seen_canonical_ids(self, watch_id: str,
                           canonical_ids: list[str]) -> set[str]:
        """Return canonical show ids already seen, including old aliases.

        Existing databases predate canonical ids and contain provider handles
        in ``watch_seen``.  Joining through ``screening_aliases`` lets the
        identity upgrade happen without replaying every currently listed
        showing as a new alert.
        """
        if not canonical_ids:
            return set()
        marks = ",".join("?" * len(canonical_ids))
        rows = self._conn.execute(
            f"""SELECT DISTINCT
                   CASE WHEN ws.screening_id IN ({marks})
                        THEN ws.screening_id ELSE a.canonical_id END AS canonical_id
                FROM watch_seen ws
                LEFT JOIN screening_aliases a
                  ON a.source_screening_id=ws.screening_id
                WHERE ws.watch_id=?
                  AND (ws.screening_id IN ({marks})
                       OR a.canonical_id IN ({marks}))""",
            (*canonical_ids, watch_id, *canonical_ids, *canonical_ids),
        ).fetchall()
        return {row["canonical_id"] for row in rows if row["canonical_id"]}

    def watch_states(self, watch_id: str) -> dict[str, dict]:
        rows = self._conn.execute(
            "SELECT screening_id, state FROM watch_states WHERE watch_id=?",
            (watch_id,),
        ).fetchall()
        return {row["screening_id"]: json.loads(row["state"]) for row in rows}

    def watch_states_for(self, watch_id: str,
                         canonical_ids: list[str]) -> dict[str, dict]:
        """Load latest state by canonical id, resolving legacy aliases."""
        if not canonical_ids:
            return {}
        wanted = set(canonical_ids)
        marks = ",".join("?" * len(canonical_ids))
        rows = self._conn.execute(
            f"""SELECT ws.screening_id, ws.state, ws.observed_at,
                       a.canonical_id AS alias_canonical_id
                FROM watch_states ws
                LEFT JOIN screening_aliases a
                  ON a.source_screening_id=ws.screening_id
                WHERE ws.watch_id=?
                  AND (ws.screening_id IN ({marks})
                       OR a.canonical_id IN ({marks}))
                ORDER BY ws.observed_at DESC""",
            (watch_id, *canonical_ids, *canonical_ids),
        ).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            canonical_id = (
                row["screening_id"]
                if row["screening_id"] in wanted
                else row["alias_canonical_id"]
            )
            if canonical_id in wanted and canonical_id not in out:
                out[canonical_id] = json.loads(row["state"])
        return out

    def watch_state_observed_at_for(
        self, watch_id: str, canonical_ids: list[str]
    ) -> dict[str, str]:
        """Return the observation timestamp for each current watch state.

        A state alert is keyed to the previous observation as well as the new
        state. That makes a sold-out -> available transition alert again after
        a later available -> sold-out -> available cycle, while repeated polls
        of the same state remain idempotent.
        """
        if not canonical_ids:
            return {}
        marks = ",".join("?" * len(canonical_ids))
        rows = self._conn.execute(
            f"""SELECT ws.screening_id, ws.observed_at,
                       a.canonical_id AS alias_canonical_id
                FROM watch_states ws
                LEFT JOIN screening_aliases a
                  ON a.source_screening_id=ws.screening_id
                WHERE ws.watch_id=?
                  AND (ws.screening_id IN ({marks})
                       OR a.canonical_id IN ({marks}))
                ORDER BY ws.observed_at DESC""",
            (watch_id, *canonical_ids, *canonical_ids),
        ).fetchall()
        wanted = set(canonical_ids)
        out: dict[str, str] = {}
        for row in rows:
            canonical_id = (
                row["screening_id"]
                if row["screening_id"] in wanted
                else row["alias_canonical_id"]
            )
            if canonical_id in wanted and canonical_id not in out:
                out[canonical_id] = row["observed_at"]
        return out

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

    def hit_history(
        self,
        watch_id: str,
        *,
        user_id: str = DEFAULT_USER,
        limit: int = 100,
    ) -> list[dict]:
        """Return a user's durable alert history, newest first.

        A watch is an evidence stream, not just a boolean notification. Keeping
        the full history inspectable makes it possible to understand what
        changed, replay a missed drop, and audit a webhook without reaching
        into SQLite directly.
        """
        rows = self._conn.execute(
            """SELECT h.hit_id, h.watch_id, h.screening_id, h.event_key,
                      h.payload, h.created_at, h.delivered
                 FROM watch_hits h
                 JOIN watches w ON w.watch_id=h.watch_id
                WHERE h.watch_id=? AND w.user_id=?
                ORDER BY h.created_at DESC, h.hit_id DESC LIMIT ?""",
            (watch_id, user_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in rows
        ]

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
    def put_venue_geo(
        self,
        venue_id: str,
        chain: str,
        name: str | None,
        lat: float | None,
        lon: float | None,
        *,
        source_venue_id: str | None = None,
        source_slug: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO venue_geo
                   (venue_id, chain, name, lat, lon, source_venue_id,
                    source_slug, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(venue_id) DO UPDATE SET
                     chain=excluded.chain,
                     name=COALESCE(excluded.name, venue_geo.name),
                     lat=COALESCE(excluded.lat, venue_geo.lat),
                     lon=COALESCE(excluded.lon, venue_geo.lon),
                     source_venue_id=COALESCE(excluded.source_venue_id,
                                              venue_geo.source_venue_id),
                     source_slug=COALESCE(excluded.source_slug,
                                          venue_geo.source_slug),
                     updated_at=excluded.updated_at""",
                (
                    venue_id,
                    chain,
                    name,
                    lat,
                    lon,
                    source_venue_id,
                    source_slug,
                    _now(),
                ),
            )

    def venue_geo(self, chain: str) -> dict[str, dict]:
        rows = self._conn.execute(
            "SELECT * FROM venue_geo WHERE chain=?", (chain,)
        ).fetchall()
        return {r["venue_id"]: dict(r) for r in rows}

    def put_directory_venues(self, venues) -> None:
        """Persist provider-discovered directory records in one transaction."""
        if not venues:
            return
        now = _now()
        with self.tx() as c:
            c.executemany(
                """INSERT INTO directory_venues
                   (venue_id, name, chain, tz, lat, lon, market, city, state,
                    ticketing_platform, url, venue_type, markup, notes, source,
                    source_url, observed_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(venue_id) DO UPDATE SET
                     name=excluded.name, chain=excluded.chain, tz=excluded.tz,
                     lat=COALESCE(excluded.lat, directory_venues.lat),
                     lon=COALESCE(excluded.lon, directory_venues.lon),
                     market=COALESCE(excluded.market, directory_venues.market),
                     city=COALESCE(excluded.city, directory_venues.city),
                     state=COALESCE(excluded.state, directory_venues.state),
                     ticketing_platform=COALESCE(excluded.ticketing_platform,
                                                 directory_venues.ticketing_platform),
                     url=COALESCE(excluded.url, directory_venues.url),
                     venue_type=excluded.venue_type,
                     markup=COALESCE(excluded.markup, directory_venues.markup),
                     notes=COALESCE(excluded.notes, directory_venues.notes),
                     source=excluded.source,
                     source_url=COALESCE(excluded.source_url, directory_venues.source_url),
                     observed_at=excluded.observed_at,
                     updated_at=excluded.updated_at""",
                [
                    (
                        venue.venue_id,
                        venue.name,
                        venue.chain,
                        venue.tz,
                        venue.point.lat if venue.point else None,
                        venue.point.lon if venue.point else None,
                        venue.market,
                        venue.city,
                        venue.state,
                        venue.ticketing_platform,
                        venue.url,
                        venue.venue_type,
                        venue.markup,
                        venue.notes,
                        venue.source,
                        venue.source_url,
                        venue.observed_at or now,
                        now,
                    )
                    for venue in venues
                ],
            )
            for venue in venues:
                self._insert_venue_observation(
                    c,
                    venue_id=venue.venue_id,
                    kind="directory",
                    subject_key="directory",
                    payload=venue.to_dict(),
                    source=venue.source,
                    source_url=venue.source_url,
                    observed_at=venue.observed_at or now,
                    evidence_scope=(
                        "routing-config"
                        if venue.source == "independent-registry"
                        else "venue-directory"
                    ),
                    confidence=1.0,
                )

    def directory_venues(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM directory_venues ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [dict(row) for row in rows]

    def record_venue_observation(
        self,
        venue_id: str,
        *,
        kind: str,
        subject_key: str,
        payload: dict,
        source: str,
        source_url: str | None = None,
        observed_at: str | None = None,
        evidence_scope: str,
        confidence: float = 1.0,
    ) -> None:
        """Append one source-backed venue observation."""
        self.record_venue_observations([{
            "venue_id": venue_id,
            "kind": kind,
            "subject_key": subject_key,
            "payload": payload,
            "source": source,
            "source_url": source_url,
            "observed_at": observed_at,
            "evidence_scope": evidence_scope,
            "confidence": confidence,
        }])

    def record_venue_observations(self, observations: list[dict]) -> None:
        """Append a batch of source-backed venue observations atomically."""
        if not observations:
            return
        fallback_observed_at = _now()
        with self.tx() as c:
            for item in observations:
                self._insert_venue_observation(
                    c,
                    venue_id=item["venue_id"],
                    kind=item["kind"],
                    subject_key=item["subject_key"],
                    payload=item["payload"],
                    source=item["source"],
                    source_url=item.get("source_url"),
                    observed_at=item.get("observed_at") or fallback_observed_at,
                    evidence_scope=item["evidence_scope"],
                    confidence=item.get("confidence", 1.0),
                )

    def venue_evidence(self, venue_id: str) -> dict:
        """Return grouped, source-linked evidence for one venue.

        Presentation claims are grouped by structured label but retain every
        source and URL that observed them. This is intentionally not called a
        hardware inventory: the evidence may be a single showing and has to be
        read with its scope and freshness.
        """
        rows = self._conn.execute(
            """SELECT * FROM venue_observations
                WHERE venue_id=? ORDER BY observed_at DESC, observation_id DESC""",
            (venue_id,),
        ).fetchall()
        by_kind: dict[str, dict] = {}
        presentations: dict[str, dict] = {}
        rooms: dict[str, dict] = {}
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            kind = item["kind"]
            summary = by_kind.setdefault(kind, {
                "kind": kind,
                "observations": 0,
                "first_seen": item["observed_at"],
                "last_seen": item["observed_at"],
                "sources": set(),
                "source_urls": set(),
                "evidence_scopes": set(),
            })
            summary["observations"] += 1
            summary["first_seen"] = min(summary["first_seen"], item["observed_at"])
            summary["last_seen"] = max(summary["last_seen"], item["observed_at"])
            summary["sources"].add(item["source"])
            if item["source_url"]:
                summary["source_urls"].add(item["source_url"])
            summary["evidence_scopes"].add(item["evidence_scope"])

            if kind == "screening_presentation":
                key = item["subject_key"]
                claim = presentations.setdefault(key, {
                    **payload,
                    "label": payload.get("label") or key,
                    "observations": 0,
                    "first_seen": item["observed_at"],
                    "last_seen": item["observed_at"],
                    "sources": set(),
                    "source_urls": set(),
                    "evidence_scope": item["evidence_scope"],
                    "confidence": item["confidence"],
                })
                claim["observations"] += 1
                claim["first_seen"] = min(claim["first_seen"], item["observed_at"])
                claim["last_seen"] = max(claim["last_seen"], item["observed_at"])
                claim["sources"].add(item["source"])
                if item["source_url"]:
                    claim["source_urls"].add(item["source_url"])
                claim["confidence"] = max(claim["confidence"], item["confidence"])
            elif kind == "room_observation":
                key = item["subject_key"]
                room = rooms.setdefault(key, {
                    "room_id": key,
                    "name": payload.get("screen_name"),
                    "observations": 0,
                    "first_seen": item["observed_at"],
                    "last_seen": item["observed_at"],
                    "capacities": [],
                    "available": None,
                    "capacity": None,
                    "rows": payload.get("row_count"),
                    "geometry_confidence": payload.get("geometry_confidence"),
                    "sources": set(),
                    "source_urls": set(),
                    "evidence_scopes": set(),
                })
                room["observations"] += 1
                room["first_seen"] = min(room["first_seen"], item["observed_at"])
                if item["observed_at"] >= room["last_seen"]:
                    room["last_seen"] = item["observed_at"]
                    room["available"] = payload.get("available")
                    room["capacity"] = payload.get("capacity")
                    room["rows"] = payload.get("row_count") or room["rows"]
                    if payload.get("geometry_confidence") is not None:
                        room["geometry_confidence"] = payload["geometry_confidence"]
                if payload.get("capacity") is not None:
                    room["capacities"].append(int(payload["capacity"]))
                room["sources"].add(item["source"])
                if item["source_url"]:
                    room["source_urls"].add(item["source_url"])
                room["evidence_scopes"].add(item["evidence_scope"])

        def clean(value):
            if isinstance(value, set):
                return sorted(value)
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        kinds = [clean(value) for value in by_kind.values()]
        clean_rooms = []
        for room in rooms.values():
            capacities = sorted(room.pop("capacities"))
            room["capacity_min"] = capacities[0] if capacities else None
            room["capacity_max"] = capacities[-1] if capacities else None
            room["capacity_median"] = (
                capacities[len(capacities) // 2]
                if capacities and len(capacities) % 2
                else (
                    round((capacities[len(capacities) // 2 - 1]
                           + capacities[len(capacities) // 2]) / 2, 1)
                    if capacities else None
                )
            )
            clean_rooms.append(clean(room))
        return {
            "venue_id": venue_id,
            "observations": sum(item["observations"] for item in kinds),
            "by_kind": sorted(kinds, key=lambda item: item["kind"]),
            "presentations": sorted(
                (clean(value) for value in presentations.values()),
                key=lambda item: (-item["observations"], item["label"]),
            ),
            "rooms": sorted(
                clean_rooms,
                key=lambda item: (-item["observations"], item["room_id"]),
            ),
            "permanent_hardware_claims": 0,
            "hardware_status": "observations-only",
            "caveat": (
                "Observed presentation and room data describe source-backed "
                "showings. They are not a complete permanent room inventory."
            ),
        }

    def evidence_overview(self) -> dict:
        """Aggregate evidence coverage for the API, MCP, and local app."""
        rows = self._conn.execute(
            """SELECT kind, COUNT(*) AS observations,
                       COUNT(DISTINCT venue_id) AS venues,
                       MIN(observed_at) AS first_seen,
                       MAX(observed_at) AS last_seen,
                       COUNT(DISTINCT source) AS sources
                FROM venue_observations GROUP BY kind ORDER BY kind"""
        ).fetchall()
        total = sum(row["observations"] for row in rows)
        return {
            "observations": total,
            "venues": len({
                row["venue_id"] for row in self._conn.execute(
                    "SELECT DISTINCT venue_id FROM venue_observations"
                ).fetchall()
            }),
            "by_kind": [dict(row) for row in rows],
            "permanent_hardware_claims": 0,
            "status": "observations-only",
            "caveat": (
                "Coverage is the set of source observations captured locally; "
                "absence means unknown, not absent."
            ),
        }

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

    # ---------------------------------------------------------- search runs
    def record_search_run(
        self,
        run_id: str,
        *,
        spec: str,
        started_at: str,
        finished_at: str,
        duration_ms: float,
        considered: int,
        result_count: int,
        seatmaps_fetched: int,
        complete: bool,
        provider_stats: list[dict] | tuple[dict, ...] = (),
        errors: list[str] | tuple[str, ...] = (),
        clipped: list[str] | tuple[str, ...] = (),
        user_id: str = DEFAULT_USER,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO search_runs
                   (run_id, user_id, spec, started_at, finished_at, duration_ms,
                    considered, result_count, seatmaps_fetched, complete,
                    provider_stats, errors, clipped)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    user_id,
                    spec,
                    started_at,
                    finished_at,
                    round(duration_ms, 3),
                    considered,
                    result_count,
                    seatmaps_fetched,
                    int(complete),
                    json.dumps(list(provider_stats), sort_keys=True),
                    json.dumps(list(errors)),
                    json.dumps(list(clipped)),
                ),
            )

    def get_search_run(self, run_id: str, *, user_id: str = DEFAULT_USER) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM search_runs WHERE run_id=? AND user_id=?",
            (run_id, user_id),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for key in ("provider_stats", "errors", "clipped"):
            out[key] = json.loads(out[key])
        out["complete"] = bool(out["complete"])
        return out

    def recent_search_runs(
        self, *, user_id: str = DEFAULT_USER, limit: int = 20
    ) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM search_runs WHERE user_id=? ORDER BY started_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 100))),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("provider_stats", "errors", "clipped"):
                item[key] = json.loads(item[key])
            item["complete"] = bool(item["complete"])
            out.append(item)
        return out

    def provider_health(
        self, *, user_id: str = DEFAULT_USER, limit: int = 100
    ) -> list[dict]:
        """Summarize source freshness from the persisted search audit trail."""
        rows = self._conn.execute(
            "SELECT finished_at, provider_stats FROM search_runs "
            "WHERE user_id=? ORDER BY finished_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 1000))),
        ).fetchall()
        summary: dict[str, dict] = {}
        for row in rows:
            try:
                stats = json.loads(row["provider_stats"])
            except (TypeError, json.JSONDecodeError):
                continue
            for stat in stats:
                chain = stat.get("chain", "unknown")
                item = summary.setdefault(chain, {
                    "chain": chain,
                    "runs": 0,
                    "ok_runs": 0,
                    "error_runs": 0,
                    "degraded_runs": 0,
                    "not_in_scope_runs": 0,
                    "clipped_runs": 0,
                    "screenings": 0,
                    "last_run": row["finished_at"],
                    "last_status": stat.get("status"),
                    "last_error": stat.get("error") or (stat.get("errors") or [None])[0],
                    "last_clipped": list(stat.get("clipped") or []),
                    "duration_ms_total": 0.0,
                })
                item["runs"] += 1
                status = stat.get("status")
                if status == "ok":
                    item["ok_runs"] += 1
                elif status == "error":
                    item["error_runs"] += 1
                elif status == "degraded":
                    item["degraded_runs"] += 1
                elif status == "not_in_scope":
                    item["not_in_scope_runs"] += 1
                if stat.get("clipped"):
                    item["clipped_runs"] += 1
                item["screenings"] += stat.get("screenings", 0) or 0
                item["duration_ms_total"] += stat.get("duration_ms", 0.0) or 0.0
        out = []
        for item in summary.values():
            runs = item.pop("runs")
            total = item.pop("duration_ms_total")
            item["runs"] = runs
            item["recent_error_runs"] = item["error_runs"] + item["degraded_runs"]
            item["recent_clipped_runs"] = item["clipped_runs"]
            item["average_duration_ms"] = round(total / runs, 2) if runs else 0.0
            item["health"] = (
                "error" if item["last_status"] == "error"
                else "degraded" if item["last_status"] == "degraded"
                else "healthy" if item["last_status"] == "ok"
                else "not_observed"
            )
            out.append(item)
        return sorted(out, key=lambda item: item["chain"])

    def inventory_overview(self, *, user_id: str | None = None) -> dict:
        """Aggregate the local evidence store without touching the network."""
        totals = self._conn.execute(
            """SELECT COUNT(*) AS screenings,
                      COUNT(DISTINCT work_id) AS works,
                      COUNT(DISTINCT venue_id) AS venues,
                      MIN(starts_at_utc) AS earliest,
                      MAX(starts_at_utc) AS latest,
                      MAX(last_seen) AS last_seen
                 FROM screenings"""
        ).fetchone()
        by_chain = self._conn.execute(
            """SELECT chain, COUNT(*) AS screenings,
                      COUNT(DISTINCT venue_id) AS venues,
                      COUNT(DISTINCT work_id) AS works
                 FROM screenings GROUP BY chain ORDER BY screenings DESC"""
        ).fetchall()
        by_availability = self._conn.execute(
            """SELECT availability, COUNT(*) AS screenings
                 FROM screenings GROUP BY availability ORDER BY screenings DESC"""
        ).fetchall()
        by_format = self._conn.execute(
            """SELECT presentation, COUNT(*) AS screenings
                 FROM screenings GROUP BY presentation ORDER BY screenings DESC
                 LIMIT 30"""
        ).fetchall()
        if user_id is None:
            watches = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active
                     FROM watches"""
            ).fetchone()
            hits = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN delivered=0 THEN 1 ELSE 0 END) AS pending
                     FROM watch_hits"""
            ).fetchone()
        else:
            watches = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active
                     FROM watches WHERE user_id=?""",
                (user_id,),
            ).fetchone()
            hits = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN h.delivered=0 THEN 1 ELSE 0 END) AS pending
                     FROM watch_hits h JOIN watches w USING (watch_id)
                    WHERE w.user_id=?""",
                (user_id,),
            ).fetchone()
        return {
            "screenings": totals["screenings"] or 0,
            "works": totals["works"] or 0,
            "venues": totals["venues"] or 0,
            "earliest": totals["earliest"],
            "latest": totals["latest"],
            "last_seen": totals["last_seen"],
            "by_chain": [dict(row) for row in by_chain],
            "by_availability": [dict(row) for row in by_availability],
            "by_format": [dict(row) for row in by_format],
            "watches": {
                "total": watches["total"] or 0,
                "active": watches["active"] or 0,
            },
            "alerts": {
                "total": hits["total"] or 0,
                "pending": hits["pending"] or 0,
            },
        }

    def inventory_by_venue(self, venue_id: str) -> dict:
        row = self._conn.execute(
            """WITH latest_seats AS (
                       SELECT screening_id, available, capacity, captured_at,
                              ROW_NUMBER() OVER (
                                  PARTITION BY screening_id
                                  ORDER BY captured_at DESC
                              ) AS position
                         FROM seat_snapshots
                   )
                   SELECT s.venue_id, s.chain, COUNT(*) AS screenings,
                      COUNT(DISTINCT s.work_id) AS works,
                      MIN(s.starts_at_utc) AS earliest,
                      MAX(s.starts_at_utc) AS latest,
                      SUM(CASE WHEN s.availability IN ('sellable','almost_full')
                               THEN 1 ELSE 0 END) AS sellable,
                      SUM(CASE WHEN s.availability='sold_out' THEN 1 ELSE 0 END) AS sold_out,
                      COUNT(ls.screening_id) AS seat_screenings,
                      COALESCE(SUM(ls.available), 0) AS seats_available,
                      COALESCE(SUM(ls.capacity), 0) AS seats_capacity,
                      MAX(ls.captured_at) AS seat_data_last_seen,
                      MAX(s.last_seen) AS last_seen
                 FROM screenings s
                 LEFT JOIN latest_seats ls
                   ON ls.screening_id=s.screening_id AND ls.position=1
                WHERE s.venue_id=? GROUP BY s.venue_id, s.chain""",
            (venue_id,),
        ).fetchone()
        return dict(row) if row else {
            "venue_id": venue_id,
            "screenings": 0,
            "works": 0,
            "sellable": 0,
            "sold_out": 0,
            "seat_screenings": 0,
            "seats_available": 0,
            "seats_capacity": 0,
        }

    def room_profiles(self, venue_id: str) -> list[dict]:
        """Summarize observed auditorium capacities for one venue.

        A room profile is derived only from the latest seat observation for
        each screening. It is deliberately labelled observed: a cinema can
        have rooms that have never been queried, and a source may expose a
        count without exposing a stable room identifier.
        """
        rows = self._conn.execute(
            """WITH latest AS (
                         SELECT ss.screening_id, ss.captured_at, ss.available,
                                ss.capacity, ss.payload,
                                ROW_NUMBER() OVER (
                                    PARTITION BY ss.screening_id
                                    ORDER BY ss.captured_at DESC
                                ) AS position
                           FROM seat_snapshots ss
                           JOIN screenings s ON s.screening_id=ss.screening_id
                          WHERE s.venue_id=?
                       )
                       SELECT screening_id, captured_at, available, capacity, payload
                         FROM latest WHERE position=1
                        ORDER BY captured_at DESC""",
            (venue_id,),
        ).fetchall()
        profiles: dict[str, dict] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            room_id = str(payload.get("screen_id") or row["screening_id"])
            profile = profiles.setdefault(room_id, {
                "room_id": room_id,
                "name": payload.get("screen_name"),
                "observed_showings": 0,
                "observation_count": 0,
                "capacities": [],
                "available": None,
                "rows": payload.get("row_count"),
                "geometry_confidence": payload.get("geometry_confidence"),
                "last_seen": row["captured_at"],
                "sources": set(),
                "source_urls": set(),
                "evidence_scopes": set(),
            })
            profile["observed_showings"] += 1
            profile["observation_count"] += 1
            profile["capacities"].append(int(row["capacity"]))
            if profile["available"] is None:
                profile["available"] = int(row["available"])
            profile["last_seen"] = max(profile["last_seen"], row["captured_at"])
            if profile["rows"] is None:
                profile["rows"] = payload.get("row_count")
            if profile["geometry_confidence"] is None:
                profile["geometry_confidence"] = payload.get("geometry_confidence")
            if payload.get("source"):
                profile["sources"].add(payload["source"])
            if payload.get("source_url"):
                profile["source_urls"].add(payload["source_url"])
            if payload.get("evidence_scope"):
                profile["evidence_scopes"].add(payload["evidence_scope"])

        out = []
        for profile in profiles.values():
            capacities = sorted(profile.pop("capacities"))
            middle = len(capacities) // 2
            median = capacities[middle] if len(capacities) % 2 else round(
                (capacities[middle - 1] + capacities[middle]) / 2, 1
            )
            profile.update({
                "capacity_min": capacities[0],
                "capacity_max": capacities[-1],
                "capacity_median": median,
                "source": "source-linked seat observations",
                "evidence_status": "observed",
                "sources": sorted(profile["sources"]),
                "source_urls": sorted(profile["source_urls"]),
                "evidence_scopes": sorted(profile["evidence_scopes"]),
            })
            out.append(profile)
        return sorted(out, key=lambda item: (-item["capacity_max"], item["room_id"]))

    def inventory_analytics(self, *, group_by: str = "chain", limit: int = 100) -> list[dict]:
        """Aggregate indexed evidence by a safe, documented dimension.

        The query intentionally uses only observations already in the local
        store. Seat totals are taken from the latest snapshot for each
        screening, so a venue does not look artificially full because every
        historical poll is summed together.
        """
        try:
            expression = _INVENTORY_GROUPS[group_by]
            label_expression = _INVENTORY_LABELS[group_by]
        except KeyError as exc:
            allowed = ", ".join(sorted(_INVENTORY_GROUPS))
            raise ValueError(f"group_by must be one of: {allowed}") from exc
        rows = self._conn.execute(
            f"""WITH latest_seats AS (
                         SELECT screening_id, available, capacity, captured_at,
                                ROW_NUMBER() OVER (
                                    PARTITION BY screening_id
                                    ORDER BY captured_at DESC
                                ) AS position
                           FROM seat_snapshots
                       )
                       SELECT {expression} AS group_key,
                              {label_expression} AS group_label,
                              COUNT(*) AS screenings,
                              COUNT(DISTINCT s.work_id) AS works,
                              COUNT(DISTINCT s.venue_id) AS venues,
                              SUM(CASE WHEN s.availability IN ('sellable','almost_full')
                                       THEN 1 ELSE 0 END) AS sellable,
                              SUM(CASE WHEN s.availability='sold_out'
                                       THEN 1 ELSE 0 END) AS sold_out,
                              COUNT(ls.screening_id) AS seat_screenings,
                              COALESCE(SUM(ls.available), 0) AS seats_available,
                              COALESCE(SUM(ls.capacity), 0) AS seats_capacity,
                              MAX(COALESCE(ls.captured_at, s.last_seen)) AS last_seen
                         FROM screenings s
                         LEFT JOIN directory_venues d ON d.venue_id=s.venue_id
                         LEFT JOIN latest_seats ls
                           ON ls.screening_id=s.screening_id AND ls.position=1
                        GROUP BY {expression}
                        ORDER BY screenings DESC, group_key
                        LIMIT ?""",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["seat_coverage"] = round(
                item["seat_screenings"] / item["screenings"], 4
            ) if item["screenings"] else 0.0
            item["seat_fill"] = round(
                1 - item["seats_available"] / item["seats_capacity"], 4
            ) if item["seats_capacity"] else None
            out.append(item)
        return out

    def recent_seat_snapshots(self, *, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            """SELECT screening_id, captured_at, available, capacity
                 FROM seat_snapshots ORDER BY captured_at DESC LIMIT ?""",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [dict(row) for row in rows]
