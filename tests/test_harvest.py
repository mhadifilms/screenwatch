"""Durable auditorium persistence and exhaustive harvesting."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from screenwatch.identity.work import Work
from screenwatch.models import Availability, Presentation
from screenwatch.ranking.candidate import Screening
from screenwatch.seating.capture import (
    SeatCapture,
    SeatProbe,
    layout_fingerprint,
)
from screenwatch.seating.model import Auditorium, ParserDrift, Seat, SeatStatus
from screenwatch.service.harvest import HarvestService
from screenwatch.service.search import SearchService
from screenwatch.service.store import Store
from screenwatch.service.venues import VenueDirectory


def probe(showtime: str = "show-1", screen: str = "1") -> SeatProbe:
    return SeatProbe(
        source="test",
        venue_id="venue-1",
        source_venue_id="source-venue-1",
        showtime_id=showtime,
        booking_url=f"https://tickets.test/{showtime}",
        starts_at_local=datetime(2026, 8, 24, 20),
        title="Example",
        source_screen_id=screen,
        metadata={"screening_id": f"test:{showtime}"},
    )


def room(status: SeatStatus, *, screen: str = "1", extra: bool = False) -> Auditorium:
    seats = [
        Seat(
            row_label="A", row_index=0, col_label="1", col_index=0,
            status=status, x=-1.0, y=0.0,
        ),
    ]
    if extra:
        seats.append(Seat(
            row_label="A", row_index=0, col_label="2", col_index=1,
            status=SeatStatus.AVAILABLE, x=1.0, y=0.0,
        ))
    return Auditorium(
        venue_id="venue-1",
        screen_id=screen,
        name=f"Auditorium {screen}",
        seats=tuple(seats),
    )


class TestDurableLayouts:
    def test_availability_does_not_change_the_static_fingerprint(self):
        assert layout_fingerprint(room(SeatStatus.AVAILABLE)) == layout_fingerprint(
            room(SeatStatus.SOLD)
        )

    def test_full_maps_raw_evidence_and_aliases_survive(self):
        store = Store.memory()
        first = SeatCapture(
            probe=probe(),
            auditorium=room(SeatStatus.AVAILABLE),
            raw_payload=b'{"capture":1}',
            raw_content_type="application/json",
            captured_at=datetime(2026, 8, 24, 20, tzinfo=UTC),
        )
        second = SeatCapture(
            probe=probe(),
            auditorium=room(SeatStatus.SOLD),
            raw_payload=b'{"capture":2}',
            raw_content_type="application/json",
            captured_at=datetime(2026, 8, 24, 21, tzinfo=UTC),
        )
        first_ids = store.put_seat_capture(first)
        second_ids = store.put_seat_capture(second)

        assert first_ids["auditorium_id"] == second_ids["auditorium_id"]
        assert first_ids["layout_hash"] == second_ids["layout_hash"]
        assert store._conn.execute("SELECT COUNT(*) FROM layout_seats").fetchone()[0] == 1
        assert store._conn.execute("SELECT COUNT(*) FROM seat_observations").fetchone()[0] == 2
        assert store._conn.execute("SELECT COUNT(*) FROM raw_captures").fetchone()[0] == 2
        [saved] = store.rooms("venue-1")
        assert saved["aliases"][0]["source_screen_id"] == "1"
        assert saved["layout"]["seats"][0]["row_label"] == "A"

    def test_a_changed_layout_creates_a_version_interval(self):
        store = Store.memory()
        store.put_seat_capture(SeatCapture(
            probe=probe(), auditorium=room(SeatStatus.AVAILABLE),
            captured_at=datetime(2026, 8, 24, 20, tzinfo=UTC),
        ))
        store.put_seat_capture(SeatCapture(
            probe=probe(), auditorium=room(SeatStatus.AVAILABLE, extra=True),
            captured_at=datetime(2026, 8, 25, 20, tzinfo=UTC),
        ))
        rows = store._conn.execute(
            "SELECT valid_until FROM auditorium_layouts ORDER BY valid_from"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["valid_until"] is not None
        assert rows[1]["valid_until"] is None

    def test_search_reuses_only_static_geometry_not_stale_availability(self):
        store = Store.memory()
        store.put_seat_capture(SeatCapture(
            probe=probe(), auditorium=room(SeatStatus.SOLD),
            captured_at=datetime(2026, 8, 24, 20, tzinfo=UTC),
        ))
        live_counts = SeatCapture(
            probe=probe("show-2"),
            auditorium=Auditorium(
                venue_id="venue-1",
                screen_id="1",
                reported_available=1,
                reported_capacity=1,
                geometry_confidence=0.0,
            ),
            captured_at=datetime(2026, 8, 25, 20, tzinfo=UTC),
        )

        reconciled = store.reuse_stored_geometry(live_counts).auditorium

        assert not reconciled.has_grid
        assert reconciled.row_lengths == (1,)
        assert reconciled.available == 1

    def test_failed_parser_body_is_persisted_with_the_failure(self):
        store = Store.memory()
        failure = ParserDrift("changed schema").with_capture(
            b'{"unexpected":true}',
            content_type="application/json",
            source_url="https://tickets.test/map",
            status_code=200,
        )

        store.record_harvest_failure("run-1", probe(), failure)

        row = store._conn.execute(
            "SELECT raw_capture_id, context FROM harvest_failures"
        ).fetchone()
        raw = store._conn.execute(
            "SELECT body, url, status_code FROM raw_captures"
        ).fetchone()
        assert row["raw_capture_id"]
        assert json.loads(row["context"])["raw_capture_id"] == row["raw_capture_id"]
        assert raw["body"] == b'{"unexpected":true}'
        assert raw["url"] == "https://tickets.test/map"
        assert raw["status_code"] == 200


class HarvestProvider:
    chain = "test"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def screenings(self, spec, venues, transport):
        work = Work(work_id="work", title="Example")
        base = datetime(2026, 8, 24, 12)
        return [
            Screening(
                screening_id=f"test:show-{index}",
                work=work,
                venue_id="venue-1",
                venue_name="Venue One",
                chain=self.chain,
                starts_at_utc=(base + timedelta(days=index)).replace(tzinfo=UTC),
                starts_at_local=base + timedelta(days=index),
                presentation=Presentation(),
                availability=Availability.SELLABLE,
                screen_id=str(index),
                seats_sold=0,
                seat_probe=probe(f"show-{index}", str(index)),
            )
            for index in (1, 2, 3)
        ]

    def fetch(self, seat_probe, transport):
        self.calls.append(seat_probe.showtime_id)
        return SeatCapture(
            probe=seat_probe,
            auditorium=room(SeatStatus.AVAILABLE, screen=seat_probe.source_screen_id),
            raw_payload=seat_probe.showtime_id.encode(),
            raw_content_type="text/plain",
        )


def test_harvester_attempts_every_probe_without_the_search_budget():
    provider = HarvestProvider()
    store = Store.memory()
    directory = VenueDirectory({
        "venue-1": {
            "name": "Venue One",
            "chain": "test",
            "city": "Los Angeles",
        }
    })
    search = SearchService(
        [provider], store=store, directory=directory, transport=object()
    )
    result = HarvestService(search).harvest(
        venue_id="venue-1", days=10, today=date(2026, 8, 24)
    )

    assert result.showtimes_discovered == 3
    assert result.probes_attempted == 3
    assert result.maps_captured == 3
    assert result.screens_discovered == 3
    assert provider.calls == ["show-3", "show-2", "show-1"]
    assert result.complete
