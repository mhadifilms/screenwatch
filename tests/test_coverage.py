from __future__ import annotations

from datetime import date

import pytest

from screenwatch.identity.work import WorkRef
from screenwatch.providers.scope import ScopeReporting
from screenwatch.ranking.spec import GeoPoint, LocationSpec, SearchSpec
from screenwatch.service.search import SearchService
from screenwatch.service.store import Store
from screenwatch.service.venues import Venue, VenueDirectory


def test_coverage_modes_make_scope_explicit():
    assert SearchSpec(work=WorkRef(query="x")).exhaustive is False
    assert SearchSpec(
        work=WorkRef(query="x"),
        location=LocationSpec(city="San Francisco"),
    ).exhaustive is True
    assert SearchSpec(
        work=WorkRef(query="x"),
        location=LocationSpec(origin=GeoPoint(37.78, -122.4)),
        coverage="nearby",
    ).exhaustive is False
    assert SearchSpec(work=WorkRef(query="x"), coverage="exhaustive").exhaustive is True

    with pytest.raises(ValueError, match="coverage"):
        SearchSpec(work=WorkRef(query="x"), coverage="guess")


def test_exhaustive_scope_bypasses_local_caps():
    class Capped(ScopeReporting):
        chain = "cinemark"
        max_venues = 1
        max_days = 1

    provider = Capped()
    venues = [Venue(venue_id=f"v{i}", name=str(i), chain="cinemark") for i in range(3)]
    days = [date(2026, 8, day) for day in range(2, 6)]

    provider._reset_scope()
    assert provider._clip_venues(venues, exhaustive=True) == venues
    assert provider._clip_days(days, exhaustive=True) == days
    assert provider.clipped == ()


def test_search_service_passes_exhaustive_mode_to_discovery():
    class Discovering:
        chain = "independent"

        def __init__(self):
            self.full_calls = []

        def discover(self, spec, *, full=False):
            self.full_calls.append(full)
            return [Venue(
                venue_id="indie-one",
                name="Indie One",
                chain=self.chain,
                city="San Francisco",
                point=GeoPoint(37.78, -122.4),
            )]

        def screenings(self, spec, venues, transport):
            return []

    provider = Discovering()
    store = Store.memory()
    service = SearchService(
        [provider], store=store, directory=VenueDirectory(), transport=object()
    )

    result = service.search(
        SearchSpec(
            work=WorkRef(query="x"),
            location=LocationSpec(city="San Francisco"),
        ),
        today=date(2026, 8, 2),
    )

    assert provider.full_calls == [True]
    assert result.coverage == "exhaustive"
    assert result.provider_stats[0]["coverage"] == "exhaustive"
    store.close()
