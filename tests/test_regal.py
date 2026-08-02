"""Regal adapter and provider, replayed against captured hydration blobs.

Two behaviours get the most attention: the Cloudflare challenge, which is
*intermittent* and so must be retried rather than treated as a shape change,
and the flat `PerformanceAttributes` list, which mixes presentation,
accessibility and ticketing policy into one array.
"""

from __future__ import annotations

from datetime import date, timezone

import pytest

from screenwatch.adapters.regal.showtimes import (
    RegalChallenged,
    RegalParseError,
    RegalShowtimes,
    extract_next_data,
)
from screenwatch.identity.work import WorkRef
from screenwatch.models import Attribute, Availability, Brand, Projection
from screenwatch.presentation import known_tokens, normalize_token
from screenwatch.providers.regal import RegalProvider
from screenwatch.ranking.spec import DateWindow, SearchSpec
from screenwatch.seating.model import SeatDataUnavailable


@pytest.fixture(scope="module")
def directory_html():
    from conftest import FIXTURES

    return (FIXTURES / "regal" / "directory.html").read_text()


@pytest.fixture(scope="module")
def theatre_html():
    from conftest import FIXTURES

    return (FIXTURES / "regal" / "theatre-times-square.html").read_text()


CHALLENGE = "<html><head><title>Just a moment...</title></head><body></body></html>"


class FakeSession:
    """Serves a scripted sequence so the retry loop can be exercised."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        body = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return type("R", (), {"status_code": 403 if body is CHALLENGE else 200,
                              "text": body})()


class TestDirectory:
    def test_parses_the_national_theatre_list(self, directory_html):
        theatres = RegalShowtimes().parse_theatres(directory_html)
        assert len(theatres) > 350
        assert all(t.theatre_code and t.path_name for t in theatres)

    def test_theatres_carry_coordinates_and_timezone(self, directory_html):
        theatres = RegalShowtimes().parse_theatres(directory_html)
        located = [t for t in theatres if t.lat is not None]
        assert len(located) > 350
        assert all(t.tz for t in located)

    def test_venue_id_does_not_double_the_prefix(self, directory_html):
        """`path_name` already starts with "regal-", so naive prefixing
        produced regal-regal-times-square-1929."""
        theatres = RegalShowtimes().parse_theatres(directory_html)
        assert not any(t.venue_id.startswith("regal-regal-") for t in theatres)

    def test_empty_directory_raises(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{}}}</script>'
        with pytest.raises(RegalParseError, match="no theatres"):
            RegalShowtimes().parse_theatres(html)


class TestShowtimes:
    def test_parses_nested_performances(self, theatre_html):
        perfs = RegalShowtimes().parse_showtimes(theatre_html)
        assert len(perfs) > 40
        assert all(p.performance_id and p.title for p in perfs)

    def test_utc_and_local_are_distinct(self, theatre_html):
        perf = RegalShowtimes().parse_showtimes(theatre_html)[0]
        assert perf.starts_at_utc.tzinfo is timezone.utc
        assert perf.starts_at_local.tzinfo is None

    def test_stop_sales_marks_sold_out(self, theatre_html):
        perfs = RegalShowtimes().parse_showtimes(theatre_html)
        assert any(p.sold_out for p in perfs), "fixture should contain sold-out shows"

    def test_attributes_are_captured_verbatim(self, theatre_html):
        perfs = RegalShowtimes().parse_showtimes(theatre_html)
        seen = {a for p in perfs for a in p.attributes}
        assert {"2D", "CC", "Reserved-Selected"} <= seen


class TestChallengeHandling:
    def test_challenge_is_its_own_error_type(self):
        with pytest.raises(RegalChallenged):
            extract_next_data(CHALLENGE)

    def test_challenge_is_a_parse_error_so_one_except_catches_both(self):
        with pytest.raises(RegalParseError):
            extract_next_data(CHALLENGE)

    def test_retry_clears_an_intermittent_challenge(self, directory_html):
        """The live site 403s then succeeds; a single attempt would report
        Regal as having nothing on."""
        session = FakeSession([CHALLENGE, CHALLENGE, directory_html])
        provider = RegalProvider(session=session, backoff_s=0)
        assert len(provider.theatres()) > 350
        assert session.calls == 3

    def test_persistent_challenge_gives_up_loudly(self):
        session = FakeSession([CHALLENGE])
        provider = RegalProvider(session=session, retries=3, backoff_s=0)
        with pytest.raises(RegalChallenged, match="persisted across 3"):
            provider.theatres()

    def test_missing_next_data_is_not_reported_as_a_challenge(self):
        with pytest.raises(RegalParseError, match="no __NEXT_DATA__"):
            extract_next_data("<html><body>something else</body></html>")


class TestPresentation:
    def provider(self, directory_html, theatre_html):
        return RegalProvider(session=FakeSession([directory_html]), backoff_s=0), theatre_html

    def screenings(self, directory_html, theatre_html):
        session = FakeSession([directory_html])
        provider = RegalProvider(session=session, backoff_s=0, max_venues=1)
        provider.theatres()
        session.responses = [theatre_html]
        session.calls = 0
        spec = SearchSpec(work=WorkRef(query="*"),
                          date_window=DateWindow(date(2020, 1, 1), date(2030, 1, 1)))
        venues = [v for v in provider.discover(spec)
                  if v.venue_id == "regal-times-square-1929"]
        return provider.screenings(spec, venues, transport=None)

    def test_produces_screenings(self, directory_html, theatre_html):
        assert len(self.screenings(directory_html, theatre_html)) > 40

    def test_premium_brands_are_classified(self, directory_html, theatre_html):
        brands = {s.presentation.brand for s in self.screenings(directory_html, theatre_html)}
        assert Brand.PLF in brands or Brand.FOURDX in brands

    def test_three_d_shows_still_get_a_projection(self, directory_html, theatre_html):
        """Regal tags '2D' explicitly but leaves it implicit on 3D showings,
        which left them with projection UNKNOWN and an unreadable label."""
        threed = [s for s in self.screenings(directory_html, theatre_html)
                  if Attribute.THREE_D in s.presentation.attrs]
        assert threed
        assert all(s.presentation.projection is not Projection.UNKNOWN for s in threed)

    def test_the_default_never_invents_a_film_print(self, directory_html, theatre_html):
        """Defaulting to DIGITAL is safe; defaulting to film would send you
        across a city for a DCP."""
        for s in self.screenings(directory_html, theatre_html):
            assert not s.presentation.projection.is_film or "mm" in s.presentation.raw

    def test_accessibility_attributes_survive(self, directory_html, theatre_html):
        attrs = {a for s in self.screenings(directory_html, theatre_html)
                 for a in s.presentation.attrs}
        assert Attribute.CLOSED_CAPTION in attrs
        assert Attribute.AUDIO_DESCRIPTION in attrs

    def test_sold_out_is_carried_through(self, directory_html, theatre_html):
        avail = {s.availability for s in self.screenings(directory_html, theatre_html)}
        assert Availability.SOLD_OUT in avail

    def test_every_attribute_in_the_fixture_is_known(self, theatre_html):
        """Guard against Regal adding a format token unnoticed."""
        perfs = RegalShowtimes().parse_showtimes(theatre_html)
        tokens = {normalize_token(a) for p in perfs for a in p.attributes}
        assert not tokens - known_tokens("regal"), (
            f"unclassified Regal tokens: {sorted(tokens - known_tokens('regal'))}"
        )


class TestSeats:
    def test_an_unknown_venue_is_reported_not_guessed(self, directory_html):
        """Without a theatre code there is no URL to build, and inventing one
        would send the request to the wrong cinema."""
        provider = RegalProvider(session=FakeSession([directory_html]), backoff_s=0)
        option = type("O", (), {"screening": type("S", (), {
            "venue_id": "regal-not-a-real-venue", "screening_id": "regal:1",
            "screen_id": "",
        })()})()
        with pytest.raises(SeatDataUnavailable, match="unknown Regal theatre code"):
            provider.fetch_seats(option, transport=None)


class TestBlockVsChallenge:
    """Cloudflare 403s look alike but mean opposite things.

    A challenge resolves if you wait and retry. A block is a firewall rule:
    the same request fails forever from this IP, and retrying only deepens
    the hole. Regal's booking API returns the latter, and the retry budget
    was being burned against a wall.
    """

    CHALLENGE = "<html><title>Just a moment...</title></html>"
    BLOCK = ("<html><title>Attention Required! | Cloudflare</title>"
             "<p>Sorry, you have been blocked</p></html>")

    def response(self, text):
        from screenwatch.browser import BrowserResponse

        return BrowserResponse(url="https://x", status=403, text=text)

    def test_a_challenge_is_challenged_not_blocked(self):
        r = self.response(self.CHALLENGE)
        assert r.challenged and not r.blocked and r.denied

    def test_a_block_is_blocked_not_challenged(self):
        r = self.response(self.BLOCK)
        assert r.blocked and not r.challenged and r.denied

    def test_a_normal_response_is_neither(self):
        r = self.response('{"seats": []}')
        assert not r.denied

    def test_the_provider_names_a_block_as_unretryable(self):
        from screenwatch.seating.model import SeatDataUnavailable

        class BlockedBrowser:
            @staticmethod
            def fetch_json(url, origin=None):
                from screenwatch.browser import BrowserResponse

                return BrowserResponse(url=url, status=403,
                                       text=TestBlockVsChallenge.BLOCK)

        provider = RegalProvider(session=FakeSession(["x"]), backoff_s=0,
                                 browser=BlockedBrowser())
        provider._theatres = [type("T", (), {
            "venue_id": "regal-x", "theatre_code": "1929"})()]
        option = type("O", (), {"screening": type("S", (), {
            "venue_id": "regal-x", "screening_id": "regal:1", "screen_id": ""})()})()
        with pytest.raises(SeatDataUnavailable, match="not a solvable challenge"):
            provider.fetch_seats(option, transport=None)
