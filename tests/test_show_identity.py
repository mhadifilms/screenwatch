from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime

from screenwatch.identity.work import Work, WorkRef
from screenwatch.models import Availability, Brand, Presentation, Projection
from screenwatch.ranking.candidate import Screening
from screenwatch.ranking.spec import DateWindow, SearchSpec
from screenwatch.seating.model import SeatDataUnavailable
from screenwatch.service.search import SearchService
from screenwatch.service.store import Store
from screenwatch.service.venues import VenueDirectory
from screenwatch.service.watch import WatchService

WORK = Work("tmdb:999", "Dune: Part Three", year=2026)
PRESENTATION = Presentation(
    projection=Projection.FILM_70MM_15PERF,
    brand=Brand.IMAX,
    aspect="1.43",
)


def showing(source_id: str, *, hour: int = 20, chain: str = "amc") -> Screening:
    start = datetime(2026, 8, 2, hour, tzinfo=UTC)
    return Screening(
        screening_id=source_id,
        work=WORK,
        venue_id="amc-metreon-16",
        venue_name="AMC Metreon 16",
        chain=chain,
        starts_at_utc=start,
        starts_at_local=start.replace(tzinfo=None),
        presentation=PRESENTATION,
        availability=Availability.SELLABLE,
        deeplink=f"https://tickets.test/{source_id}",
    )


def test_provider_handles_do_not_change_canonical_show_id():
    amc = showing("amc:old-handle")
    regal = replace(amc, screening_id="regal:new-handle", chain="regal")

    assert amc.canonical_screening_id == regal.canonical_screening_id
    assert showing("amc:other", hour=21).canonical_screening_id != (
        amc.canonical_screening_id
    )


def test_equivalent_provider_listings_collapse_but_keep_sources():
    amc = showing("amc:1")
    regal = replace(amc, screening_id="regal:2", chain="regal", sources=("regal",))

    merged = SearchService.unify_screenings([amc, regal])

    assert len(merged) == 1
    assert merged[0].canonical_screening_id == amc.canonical_screening_id
    assert set(merged[0].sources) == {"regal"}


class _Provider:
    chain = "amc"

    def __init__(self, screenings):
        self.screenings_in_scope = list(screenings)

    def screenings(self, spec, venues, transport):
        return list(self.screenings_in_scope)

    def fetch_seats(self, option, transport):
        raise SeatDataUnavailable("no seat map in identity test")


def test_watch_tracks_provider_id_rotation_as_the_same_showing():
    store = Store.memory()
    provider = _Provider([showing("amc:old-handle")])
    search = SearchService(
        [provider], store=store, directory=VenueDirectory(), transport=object()
    )
    watches = WatchService(search, store)
    spec = SearchSpec(
        work=WorkRef(work_id=WORK.work_id),
        date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)),
    )
    watch_id = watches.create(spec, "identity", today=date(2026, 8, 2))

    provider.screenings_in_scope = [showing("amc:new-handle")]
    assert watches.run(watch_id, today=date(2026, 8, 2)) == []

    canonical = showing("amc:new-handle").canonical_screening_id
    assert set(store.aliases_for_screening(canonical)) == {
        "amc:old-handle", "amc:new-handle",
    }
    store.close()


def test_legacy_screenings_table_gets_canonical_column(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE screenings (
            screening_id TEXT PRIMARY KEY,
            work_id TEXT,
            venue_id TEXT NOT NULL,
            chain TEXT NOT NULL,
            starts_at_utc TEXT NOT NULL,
            presentation TEXT NOT NULL,
            availability TEXT NOT NULL,
            deeplink TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        """
    )
    conn.close()

    store = Store(path)
    columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(screenings)")
    }
    assert "canonical_id" in columns
    assert store._conn.execute(
        "SELECT name FROM sqlite_master WHERE name='screening_aliases'"
    ).fetchone()
    store.close()
