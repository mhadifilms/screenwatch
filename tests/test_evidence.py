from __future__ import annotations

from datetime import UTC, datetime

from screenwatch.identity.work import Work
from screenwatch.models import Availability, Brand, Presentation, Projection
from screenwatch.ranking.candidate import Screening
from screenwatch.seating.render import build_auditorium
from screenwatch.service.store import Store
from screenwatch.service.venues import Venue


def _screening() -> Screening:
    starts = datetime(2026, 8, 2, 20, tzinfo=UTC)
    return Screening(
        screening_id="amc:evidence-1",
        work=Work(work_id="tmdb:evidence", title="Evidence Film"),
        venue_id="amc-evidence-1",
        venue_name="AMC Evidence 1",
        chain="amc",
        starts_at_utc=starts,
        starts_at_local=starts.replace(tzinfo=None),
        presentation=Presentation(
            Projection.DIGITAL_LASER, Brand.IMAX, "1.43"
        ),
        availability=Availability.SELLABLE,
        deeplink="https://www.amctheatres.com/movie-theatres/test/showtimes",
        sources=("amc:showtimes-rsc", "amc:showtimes-dom"),
    )


def test_directory_observation_keeps_source_url_and_timestamp():
    store = Store.memory()
    venue = Venue(
        venue_id="amc-evidence-1",
        name="AMC Evidence 1",
        chain="amc",
        url="https://www.amctheatres.com/movie-theatres/test/amc-evidence-1",
        source="amc:sitemap-theatres",
        source_url="https://www.amctheatres.com/sitemaps/sitemap-theatres.xml",
    )

    store.put_directory_venues([venue])
    row = store.directory_venues()[0]
    evidence = store.venue_evidence(venue.venue_id)

    assert row["source"] == "amc:sitemap-theatres"
    assert row["source_url"].endswith("sitemap-theatres.xml")
    assert evidence["by_kind"][0]["kind"] == "directory"
    assert evidence["by_kind"][0]["source_urls"]
    store.close()


def test_screening_and_room_evidence_are_grouped_without_becoming_hardware_facts():
    store = Store.memory()
    screening = _screening()
    store.put_work(screening.work)
    store.upsert_screening(
        screening.screening_id,
        canonical_id=screening.canonical_screening_id,
        work_id=screening.work.work_id,
        venue_id=screening.venue_id,
        chain=screening.chain,
        starts_at_utc=screening.starts_at_utc,
        presentation=screening.presentation.describe(),
        availability=screening.availability.value,
        deeplink=screening.deeplink,
    )
    room = build_auditorium(screening.venue_id, "imax-1", ["....", "...."])
    store.put_seat_snapshot(
        screening.screening_id,
        room.available,
        room.capacity,
        {
            "geometry_confidence": room.geometry_confidence,
            "screen_id": room.screen_id,
            "screen_name": "IMAX 1",
            "row_count": room.row_count,
            "row_lengths": list(room.row_lengths),
            "has_grid": room.has_grid,
        },
        venue_id=screening.venue_id,
        source="amc:seat-map",
        source_url=screening.deeplink,
    )
    for source in screening.sources:
        store.record_venue_observation(
            screening.venue_id,
            kind="screening_presentation",
            subject_key='{"brand":"imax","projection":"digital_laser"}',
            payload={"label": screening.presentation.describe()},
            source=source,
            source_url=screening.deeplink,
            evidence_scope="screening",
        )

    evidence = store.venue_evidence(screening.venue_id)
    profiles = store.room_profiles(screening.venue_id)

    assert {item["kind"] for item in evidence["by_kind"]} == {
        "room_observation", "screening_presentation"
    }
    assert evidence["permanent_hardware_claims"] == 0
    assert evidence["presentations"][0]["sources"] == sorted(screening.sources)
    assert evidence["rooms"][0]["room_id"] == "imax-1"
    assert evidence["rooms"][0]["capacity"] == room.capacity
    assert evidence["rooms"][0]["available"] == room.available
    assert profiles[0]["room_id"] == "imax-1"
    assert profiles[0]["source_urls"] == [screening.deeplink]
    assert profiles[0]["evidence_status"] == "observed"
    store.close()
