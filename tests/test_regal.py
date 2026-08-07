"""Regal adapter and provider, replayed against captured hydration blobs.

Two behaviours get the most attention: the Cloudflare challenge, which is
*intermittent* and so must be retried rather than treated as a shape change,
and the flat `PerformanceAttributes` list, which mixes presentation,
accessibility and ticketing policy into one array.
"""

from __future__ import annotations

from datetime import UTC, date

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


@pytest.fixture(scope="module")
def seatmap_html():
    from conftest import FIXTURES

    return (FIXTURES / "regal" / "seatmap-times-square.html").read_text()


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
        assert perf.starts_at_utc.tzinfo is UTC
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
                          date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)))
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

    def test_browser_fetches_the_rendered_movie_page(self, directory_html, seatmap_html):
        from datetime import datetime

        from screenwatch.adapters.regal.showtimes import RegalPerformance
        from screenwatch.browser import BrowserResponse

        class FakeBrowser:
            def __init__(self):
                self.calls = []

            def visit(self, url, **kw):
                self.calls.append((url, kw))
                return BrowserResponse(url=url, status=200, text=seatmap_html)

        browser = FakeBrowser()
        provider = RegalProvider(
            session=FakeSession([directory_html]), backoff_s=0, browser=browser
        )
        provider._theatres = [type("T", (), {
            "venue_id": "regal-x", "theatre_code": "1929"
        })()]
        provider._seat_requests["regal:259528"] = RegalPerformance(
            performance_id="259528", theatre_code="1929", movie_code="HO00021207",
            title="Spider-Man: Brand New Day",
            starts_at_utc=datetime(2026, 8, 3, 2, 30),
            starts_at_local=datetime(2026, 8, 2, 22, 30), auditorium="5",
            attributes=("3D",), sold_out=False,
        )
        option = type("O", (), {"screening": type("S", (), {
            "venue_id": "regal-x", "screening_id": "regal:259528",
            "screen_id": "5",
        })()})()

        room = provider.fetch_seats(option, transport=None)

        assert room.available == 8
        assert len(room.seats) == 13
        assert browser.calls == [(
            "https://www.regmovies.com/movies/"
            "spiderman-brand-new-day-ho00021207?date=2026-08-02&site=1929&id=259528",
            {"wait_for_selector": "button[id^='seat-']", "wait_timeout_ms": 12_000},
        )]


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
            def visit(url, **kw):
                from screenwatch.browser import BrowserResponse

                return BrowserResponse(url=url, status=403,
                                       text=TestBlockVsChallenge.BLOCK)

        provider = RegalProvider(session=FakeSession(["x"]), backoff_s=0,
                                 browser=BlockedBrowser())
        provider._theatres = [type("T", (), {
            "venue_id": "regal-x", "theatre_code": "1929"})()]
        from datetime import datetime

        from screenwatch.adapters.regal.showtimes import RegalPerformance

        provider._seat_requests["regal:1"] = RegalPerformance(
            performance_id="1", theatre_code="1929", movie_code="HO1", title="X",
            starts_at_utc=datetime(2026, 8, 2, 21),
            starts_at_local=datetime(2026, 8, 2, 17), auditorium="1",
            attributes=(), sold_out=False,
        )
        option = type("O", (), {"screening": type("S", (), {
            "venue_id": "regal-x", "screening_id": "regal:1", "screen_id": ""})()})()
        with pytest.raises(SeatDataUnavailable, match="blocked by Cloudflare"):
            provider.fetch_seats(option, transport=None)


class TestRegalSeatPlanParsing:
    """Pin both the old Vista payload and the live rendered page shape."""

    def parse(self, payload, **kw):
        from screenwatch.seating.sources.regal import RegalSeatSource

        return RegalSeatSource.parse(payload, venue_id="regal-x", **kw)

    def plan(self, seats, *, area_key="Areas", row_key="Rows", seat_key="Seats"):
        return {"SeatLayoutData": {area_key: [{row_key: [
            {"PhysicalName": "A", "RowIndex": 0, seat_key: seats}
        ]}]}}

    def seat(self, col, status, kind=0):
        return {"Id": str(col + 1), "ColumnIndex": col, "Status": status,
                "SeatType": kind}

    def test_parses_a_row_of_available_seats(self):
        from screenwatch.seating.model import SeatStatus

        room = self.parse(self.plan([self.seat(i, 0) for i in range(4)]))
        assert len(room.seats) == 4
        assert all(s.status is SeatStatus.AVAILABLE for s in room.seats)
        assert room.available == 4

    def test_accepts_the_camelcase_spelling_too(self):
        payload = {"seatLayoutData": {"areas": [{"rows": [
            {"physicalName": "A", "rowIndex": 0,
             "seats": [{"id": "1", "columnIndex": 0, "status": 0, "seatType": 0}]}
        ]}]}}
        assert len(self.parse(payload).seats) == 1

    def test_sold_and_house_seats_are_distinguished(self):
        from screenwatch.seating.model import SeatStatus

        room = self.parse(self.plan([
            self.seat(0, 0), self.seat(1, 1), self.seat(2, 3), self.seat(3, 4)
        ]))
        assert [s.status for s in sorted(room.seats, key=lambda s: s.col_index)] == [
            SeatStatus.AVAILABLE, SeatStatus.SOLD,
            SeatStatus.UNAVAILABLE, SeatStatus.UNAVAILABLE,
        ]

    def test_seat_kinds_are_mapped(self):
        from screenwatch.seating.model import SeatKind

        room = self.parse(self.plan([self.seat(0, 0, 1), self.seat(1, 0, 2)]))
        kinds = {s.kind for s in room.seats}
        assert kinds == {SeatKind.WHEELCHAIR, SeatKind.COMPANION}

    def test_an_unknown_status_raises_rather_than_defaulting_to_sold(self):
        """Defaulting hid every seat behind a schema change - silently
        reporting a full house is the one failure this module must not have."""
        from screenwatch.seating.model import SeatDataUnavailable

        with pytest.raises(SeatDataUnavailable, match="unrecognised"):
            self.parse(self.plan([self.seat(0, "Quantum")]))

    def test_html_instead_of_json_is_named_as_such(self):
        from screenwatch.seating.model import SeatDataUnavailable

        with pytest.raises(SeatDataUnavailable, match="HTML"):
            self.parse("<html>Just a moment...</html>")

    def test_parses_the_rendered_movie_page(self, seatmap_html):
        from screenwatch.seating.model import SeatKind, SeatStatus

        room = self.parse(seatmap_html, screen_id="5")

        assert room.screen_id == "5"
        assert room.available == 8
        assert sum(s.status is SeatStatus.SOLD for s in room.seats) == 5
        assert sum(s.kind is SeatKind.COMPANION for s in room.seats) == 2
        assert sum(s.kind is SeatKind.WHEELCHAIR for s in room.seats) == 2
        assert {s.row_label for s in room.seats} == {"A", "B", "C"}

    def test_movie_url_matches_the_route_the_site_uses(self):
        from screenwatch.seating.sources.regal import RegalSeatSource

        url = RegalSeatSource().movie_url(
            title="Spider-Man: Brand New Day",
            movie_code="HO00021207",
            date="2026-08-02",
            theatre_code="1929",
            performance_id="259528",
        )
        assert url == (
            "https://www.regmovies.com/movies/"
            "spiderman-brand-new-day-ho00021207?date=2026-08-02&site=1929&id=259528"
        )

    def test_an_error_payload_is_surfaced(self):
        from screenwatch.seating.model import SeatDataUnavailable

        with pytest.raises(SeatDataUnavailable, match="seat plan error"):
            self.parse({"errorCode": 5, "errorMessage": "no session"})

    def test_no_areas_is_not_an_empty_room(self):
        from screenwatch.seating.model import SeatDataUnavailable

        with pytest.raises(SeatDataUnavailable, match="no seating areas"):
            self.parse({"SeatLayoutData": {"Areas": []}})

    def test_general_admission_is_reported_not_returned_as_zero_seats(self):
        from screenwatch.seating.model import SeatDataUnavailable

        with pytest.raises(SeatDataUnavailable, match="general admission"):
            self.parse(self.plan([]))

    def test_the_url_carries_the_apps_own_bypass_flag(self):
        from screenwatch.seating.sources.regal import RegalSeatSource

        url = RegalSeatSource().url("0123", "9876")
        assert "theatreCode=0123" in url and "sessionId=9876" in url
        assert url.endswith("&bypass=true")


class TestFetchLoopDistinguishesFailures:
    """A challenge, a firewall rule and a network error need different
    responses, and the caller cannot tell them apart from a bare
    "challenged"."""

    class Session:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.calls = 0

        def get(self, url, **kw):
            self.calls += 1
            item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
            if isinstance(item, Exception):
                raise item
            status, body = item
            return type("R", (), {"status_code": status, "text": body})()

    def provider(self, session, **kw):
        return RegalProvider(session=session, backoff_s=0, **kw)

    def test_a_bare_403_is_a_block_and_is_not_retried(self):
        from screenwatch.adapters.regal.showtimes import RegalBlocked

        session = self.Session((403, "<html>Forbidden</html>"))
        with pytest.raises(RegalBlocked, match="denies this path"):
            self.provider(session).theatres()
        assert session.calls == 1, "a firewall rule will not clear on retry"

    def test_a_403_carrying_a_challenge_is_retried(self, directory_html):
        session = self.Session((403, CHALLENGE), (200, directory_html))
        assert len(self.provider(session).theatres()) > 350
        assert session.calls == 2

    def test_a_404_is_not_retried_either(self):
        from screenwatch.adapters.regal.showtimes import RegalUnavailable

        session = self.Session((404, "nope"))
        with pytest.raises(RegalUnavailable, match="HTTP 404"):
            self.provider(session).theatres()
        assert session.calls == 1

    def test_a_500_is_retried(self, directory_html):
        session = self.Session((503, "busy"), (200, directory_html))
        assert len(self.provider(session).theatres()) > 350
        assert session.calls == 2

    def test_a_network_error_is_retried_then_reported(self):
        session = self.Session(OSError("connection reset"))
        with pytest.raises(RegalChallenged, match="connection reset"):
            self.provider(session, retries=3).theatres()
        assert session.calls == 3

    def test_no_sleep_after_the_final_attempt(self, monkeypatch):
        """It added backoff_s * retries to the latency of every failure."""
        slept = []
        monkeypatch.setattr("screenwatch.providers.regal.time.sleep", slept.append)

        session = self.Session((403, CHALLENGE))
        provider = RegalProvider(session=session, retries=3, backoff_s=1.0)
        with pytest.raises(RegalChallenged):
            provider.theatres()
        assert len(slept) == 2, "three attempts means two waits between them"


class TestBookingLinkPointsSomewhereReal:
    """The deeplink is the system's final output - it stops at a link and
    hands over. `/showtimes/{performance_id}` 404s: there is no
    per-performance page on regmovies.com, the booking flow is client-side
    routing into webbooking, and every path there renders the same shell.
    """

    def perf(self):
        from datetime import datetime

        from screenwatch.adapters.regal.showtimes import RegalPerformance

        return RegalPerformance(
            performance_id="260019", theatre_code="1929", movie_code="HO1",
            title="Spider-Man", starts_at_utc=datetime(2026, 8, 2, 21, 30),
            starts_at_local=datetime(2026, 8, 2, 17, 30), auditorium="9",
            attributes=(), sold_out=False,
        )

    def test_it_is_the_dated_theatre_page(self):
        link = self.perf().deeplink("regal-times-square-1929")
        assert link == ("https://www.regmovies.com/theatres/"
                        "regal-times-square-1929?date=2026-08-02")

    def test_it_never_returns_the_dead_performance_path(self):
        assert "/showtimes/260019" not in self.perf().deeplink("regal-x")

    def test_without_a_theatre_slug_it_degrades_to_the_directory(self):
        assert self.perf().deeplink() == "https://www.regmovies.com/theatres"

    def test_the_provider_passes_the_slug_through(self, directory_html, theatre_html):
        session = FakeSession([directory_html])
        provider = RegalProvider(session=session, backoff_s=0, max_venues=1)
        provider.theatres()
        session.responses, session.calls = [theatre_html], 0
        spec = SearchSpec(work=WorkRef(query="*"),
                          date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)))
        venues = [v for v in provider.discover(spec)
                  if v.venue_id == "regal-times-square-1929"]
        shows = provider.screenings(spec, venues, transport=None)
        assert shows
        assert all("/theatres/regal-times-square-1929?date=" in s.deeplink
                   for s in shows)
