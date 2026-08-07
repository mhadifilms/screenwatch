from __future__ import annotations

import json
from pathlib import Path

from screenwatch.adapters.openstreetmap.cinemas import US_BBOXES, OsmCinemaDirectory
from screenwatch.identity.work import WorkRef
from screenwatch.providers.independent import IndependentProvider
from screenwatch.ranking.spec import SearchSpec
from screenwatch.service.store import Store
from screenwatch.service.venues import Venue

FIXTURE = Path(__file__).parent / "fixtures" / "openstreetmap" / "cinemas.json"


def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeDirectory(OsmCinemaDirectory):
    def __init__(self, records):
        self.records = list(records)
        self.calls: list[str] = []

    def nearby(self, transport, point, radius_km):
        self.calls.append("nearby")
        return list(self.records)

    def national(self, transport):
        self.calls.append("national")
        return list(self.records)


class FakeTransport:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return type("Response", (), {"status_code": 200, "text": self.raw})()


def test_parser_normalizes_nodes_ways_and_contact_fields():
    rows = OsmCinemaDirectory().parse(payload())

    assert [row.name for row in rows] == [
        "The Roxie",
        "The Indie House",
        "AMC Metreon 16",
        "Hawaii Picture Palace",
    ]
    assert rows[0].website == "https://roxie.com"
    assert rows[1].osm_url.endswith("/way/202")
    assert rows[1].address == "Film Avenue"
    assert rows[3].state == "HI"


def test_national_directory_deduplicates_stable_osm_identity():
    directory = OsmCinemaDirectory()
    raw = payload()
    transport = FakeTransport(json.dumps(raw))

    rows = directory.national(transport)

    assert len(rows) == 4
    assert transport.calls == len(US_BBOXES)


def test_independent_discovery_uses_osm_and_excludes_known_chains():
    records = OsmCinemaDirectory().parse(payload())
    directory = FakeDirectory(records)
    provider = IndependentProvider(
        venues=[],
        osm_directory=directory,
        discovery_transport=object(),
    )

    venues = provider.discover(
        SearchSpec(work=WorkRef(query="x"), coverage="exhaustive"),
        full=True,
    )

    assert directory.calls == ["national"]
    assert {venue.name for venue in venues} == {
        "The Roxie",
        "The Indie House",
        "Hawaii Picture Palace",
    }
    roxie = next(venue for venue in venues if venue.name == "The Roxie")
    assert roxie.source == "osm:overpass"
    assert roxie.source_url.endswith("/node/101")
    assert roxie.url == "https://roxie.com"


def test_osm_does_not_duplicate_a_configured_venue():
    records = OsmCinemaDirectory().parse(payload())
    provider = IndependentProvider(
        venues=[{
            "venue_id": "roxie-sf",
            "name": "The Roxie",
            "url": "https://roxie.com/",
            "lat": 37.7648,
            "lon": -122.4222,
            "tz": "America/Los_Angeles",
        }],
        osm_directory=FakeDirectory(records),
        discovery_transport=object(),
    )

    venues = provider.discover(
        SearchSpec(work=WorkRef(query="x"), coverage="exhaustive"),
        full=True,
    )

    roxies = [venue for venue in venues if "Roxie" in venue.name]
    assert len(roxies) == 1
    assert roxies[0].venue_id == "roxie-sf"
    assert roxies[0].source == "independent-registry+osm:overpass"


def test_osm_failure_keeps_cached_or_configured_routing_visible():
    class BrokenDirectory(FakeDirectory):
        def national(self, transport):
            raise RuntimeError("overpass unavailable")

    provider = IndependentProvider(
        venues=[{
            "venue_id": "filmforum",
            "name": "Film Forum",
            "url": "https://filmforum.org/",
            "tz": "America/New_York",
        }],
        osm_directory=BrokenDirectory([]),
        discovery_transport=object(),
    )

    venues = provider.discover(
        SearchSpec(work=WorkRef(query="x"), coverage="exhaustive"),
        full=True,
    )

    assert [venue.venue_id for venue in venues] == ["filmforum"]
    assert provider.errors and "overpass unavailable" in provider.errors[0]


def test_persisted_osm_rows_are_reused_without_a_network_refresh():
    store = Store.memory()
    store.put_directory_venues([Venue(
        venue_id="independent-osm-node-101",
        name="The Roxie",
        chain="independent",
        point=OsmCinemaDirectory().parse(payload())[0].point,
        city="San Francisco",
        state="CA",
        url="https://roxie.com",
        source="osm:overpass",
        source_url="https://www.openstreetmap.org/node/101",
    )])
    provider = IndependentProvider(
        venues=[],
        store=store,
        osm_directory=FakeDirectory([]),
        discovery_transport=object(),
    )

    venues = provider.discover(SearchSpec(work=WorkRef(query="x")))

    assert [venue.venue_id for venue in venues] == ["independent-osm-node-101"]
    assert provider.osm.calls == []
    store.close()
