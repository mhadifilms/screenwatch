"""Alamo Drafthouse adapter and provider, replayed against a captured market.

The distinctive check here is `test_table_covers_the_apis_own_taxonomy`. Alamo
ships its format and attribute vocabulary in the same payload as the data, so
unlike every other source the table can be verified against the API's own
declaration instead of waiting to meet an unknown token in production.
"""

from __future__ import annotations

import json
from datetime import date, timezone

import pytest

from screenwatch.adapters.alamo.schedule import (
    ALAMO_TOKENS,
    AlamoSchedule,
    AlamoScheduleParseError,
)
from screenwatch.identity.work import WorkRef
from screenwatch.models import Attribute, Availability, Brand, Projection
from screenwatch.providers.alamo import AlamoProvider
from screenwatch.ranking.spec import DateWindow, GeoPoint, LocationSpec, SearchSpec
from screenwatch.seating.model import SeatDataUnavailable


@pytest.fixture(scope="module")
def payload():
    from conftest import FIXTURES

    return json.loads((FIXTURES / "alamo" / "schedule-nyc.json").read_text())


@pytest.fixture(scope="module")
def parsed(payload):
    return AlamoSchedule().parse(payload, "nyc")


class FakeSession:
    """Serves the captured market for any URL; records what was asked for."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self

    status_code = 200

    def json(self):
        return self.payload


class TestSchedule:
    def test_parses_cinemas_with_coordinates(self, parsed):
        cinemas, _ = parsed
        assert len(cinemas) == 3
        assert {c.venue_id for c in cinemas} == {
            "alamo-lower-manhattan", "alamo-staten-island", "alamo-downtown-brooklyn"
        }
        assert all(c.lat and c.lon and c.tz for c in cinemas)

    def test_parses_sessions_with_titles_joined_from_presentations(self, parsed):
        _, sessions = parsed
        assert sessions
        assert all(s.title and s.title != s.presentation_slug.replace("-", " ")
                   for s in sessions[:5])

    def test_local_and_utc_times_both_survive(self, parsed):
        _, sessions = parsed
        s = sessions[0]
        assert s.starts_at_utc.tzinfo is timezone.utc
        assert s.starts_at_local.tzinfo is None

    def test_table_covers_the_apis_own_taxonomy(self, payload):
        """The payload declares its own format and attribute vocabulary, so an
        unknown token is catchable at capture time rather than in production."""
        assert AlamoSchedule.unknown_formats(payload) == set()

    def test_empty_payload_raises(self):
        with pytest.raises(AlamoScheduleParseError, match="no data block"):
            AlamoSchedule().parse({}, "nyc")

    def test_missing_cinemas_raises_rather_than_returning_nothing(self):
        with pytest.raises(AlamoScheduleParseError, match="no cinemas"):
            AlamoSchedule().parse(
                {"data": {"market": [{"cinemas": []}], "sessions": []}}, "nyc"
            )

    def test_hidden_sessions_are_skipped(self, payload):
        doctored = json.loads(json.dumps(payload))
        for s in doctored["data"]["sessions"]:
            s["isHidden"] = True
        _, sessions = AlamoSchedule().parse(doctored, "nyc")
        assert sessions == []


class TestProvider:
    def provider(self, payload):
        return AlamoProvider(markets=("nyc",), session=FakeSession(payload))

    def spec(self, **kw):
        kw.setdefault("date_window", DateWindow(date(2020, 1, 1), date(2030, 1, 1)))
        return SearchSpec(work=WorkRef(query="*"), **kw)

    def test_discovers_its_own_venues(self, payload):
        """Alamo ships coordinates for every cinema, so the provider is not
        limited to the hand-maintained seed table."""
        venues = self.provider(payload).discover(self.spec())
        assert len(venues) == 3
        assert all(v.chain == "alamo" and v.point for v in venues)

    def test_one_request_covers_a_whole_market(self, payload):
        session = FakeSession(payload)
        provider = AlamoProvider(markets=("nyc",), session=session)
        spec = self.spec()
        venues = provider.discover(spec)
        provider.screenings(spec, venues, transport=object())
        assert len(session.calls) == 1, "market payload must be fetched once and cached"

    def test_produces_screenings_for_discovered_venues(self, payload):
        provider = self.provider(payload)
        spec = self.spec()
        screenings = provider.screenings(spec, provider.discover(spec), transport=object())
        assert screenings
        assert all(s.chain == "alamo" and s.deeplink for s in screenings)

    def test_sold_out_sessions_are_marked(self, payload):
        doctored = json.loads(json.dumps(payload))
        doctored["data"]["sessions"][0]["status"] = "SOLDOUT"
        provider = AlamoProvider(markets=("nyc",), session=FakeSession(doctored))
        spec = self.spec()
        screenings = provider.screenings(spec, provider.discover(spec), transport=object())
        assert any(s.availability is Availability.SOLD_OUT for s in screenings)

    def test_merges_format_slug_with_session_attributes(self, payload):
        """Alamo files open-caption as a *format* and Atmos as an *attribute*.
        A 70mm Atmos screening loses half its description unless both are
        folded in."""
        doctored = json.loads(json.dumps(payload))
        target = doctored["data"]["sessions"][0]
        target["formatSlug"] = "70mm"
        target["sessionAttributeSlugs"] = ["70MM", "Atmos"]
        provider = AlamoProvider(markets=("nyc",), session=FakeSession(doctored))
        spec = self.spec()
        screenings = provider.screenings(spec, provider.discover(spec), transport=object())
        match = next(s for s in screenings if s.screening_id.endswith(str(target["sessionId"])))
        assert match.presentation.projection is Projection.FILM_70MM
        assert Attribute.ATMOS in match.presentation.attrs

    def test_hdr_by_barco_is_a_premium_format(self):
        assert ALAMO_TOKENS["hdr"].brand is Brand.PLF

    def test_venues_outside_the_search_are_not_returned(self, payload):
        provider = self.provider(payload)
        spec = self.spec()
        only = [v for v in provider.discover(spec) if v.venue_id == "alamo-staten-island"]
        screenings = provider.screenings(spec, only, transport=object())
        assert {s.venue_id for s in screenings} == {"alamo-staten-island"}

    def test_nearest_markets_are_preferred_when_an_origin_is_given(self, payload):
        provider = AlamoProvider(markets=("nyc",), session=FakeSession(payload),
                                 max_markets=1)
        spec = self.spec(location=LocationSpec(origin=GeoPoint(40.71, -74.00),
                                               radius_km=50))
        assert provider._relevant_markets(spec) == ["nyc"]

    def test_seats_are_unavailable_and_say_why(self, payload):
        provider = self.provider(payload)
        spec = self.spec()
        [option] = [
            type("O", (), {"screening": s})()
            for s in provider.screenings(spec, provider.discover(spec), transport=object())[:1]
        ]
        with pytest.raises(SeatDataUnavailable, match="not exposed"):
            provider.fetch_seats(option, transport=object())
