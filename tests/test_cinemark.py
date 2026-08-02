"""Cinemark adapter and provider, plus the robots.txt enforcement it forced.

Cinemark is the first source whose operator explicitly asks automated clients
to stay off a path — `/TicketSeatMap`, which is exactly where the seat data
lives. Those tests are as much about honouring that as about parsing.
"""

from __future__ import annotations

from datetime import date, timezone

import pytest

from screenwatch.adapters.cinemark.showtimes import (
    CinemarkChallenged,
    CinemarkParseError,
    CinemarkShowtimes,
    classify_print_type,
    parse_runtime,
    timezone_for,
)
from screenwatch.identity.work import WorkRef
from screenwatch.models import Attribute, Brand, Projection
from screenwatch.providers.cinemark import CinemarkProvider
from screenwatch.ranking.spec import DateWindow, GeoPoint, LocationSpec, SearchSpec
from screenwatch.robots import DisallowedByRobots, RobotsCache
from screenwatch.seating.model import SeatDataUnavailable
from screenwatch.service.venues import Venue

SLUG = "tx-san-antonio/cinemark-san-antonio-16"
CHALLENGE = "<html><title>Just a moment...</title></html>"


@pytest.fixture(scope="module")
def theatre_html():
    from conftest import FIXTURES

    return (FIXTURES / "cinemark" / "theatre-san-antonio-16.html").read_text()


@pytest.fixture(scope="module")
def seatmap_html():
    from conftest import FIXTURES

    return (FIXTURES / "cinemark" / "seatmap-546676.html").read_text()


class _ServingRobots:
    def get(self, url, **kw):
        return type("R", (), {
            "status_code": 200,
            "text": "User-agent: *\nDisallow: /ticketseatmap/\n",
        })()


class FakeRobots:
    """Records what a crawler-mode client would skip, blocks nothing."""

    DISALLOWED = ("/tickets/", "/ticketseatmap", "/shoppingcart")

    def __init__(self):
        self.skipped = []

    def check_fetch(self, url: str) -> None:
        if any(d in url.lower() for d in self.DISALLOWED):
            self.skipped.append(url)

    @staticmethod
    def check_link(url: str) -> str:
        return url


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.urls: list[str] = []

    def get(self, url, **kw):
        self.urls.append(url)
        return type("R", (), {"status_code": 200, "text": self.body})()


class TestParsing:
    def test_parses_the_full_day(self, theatre_html):
        """`?showDate=` is load-bearing: without it the page returns a handful
        of 'starting soon' times instead of the day."""
        assert len(CinemarkShowtimes().parse_showtimes(theatre_html)) > 60

    def test_joins_markup_to_the_json_model(self, theatre_html):
        shows = CinemarkShowtimes().parse_showtimes(theatre_html)
        titled = [s for s in shows if s.title]
        assert titled and all(s.movie_id and s.showtime_id for s in titled)

    def test_runtime_comes_through_for_the_identity_tiebreaker(self, theatre_html):
        shows = [s for s in CinemarkShowtimes().parse_showtimes(theatre_html) if s.title]
        assert any(s.runtime_min for s in shows)

    @pytest.mark.parametrize(
        "text,expected",
        [("2 hr 25 min", 145), ("1 hr", 60), ("95 min", 95), ("", None), ("soon", None)],
    )
    def test_runtime_parsing(self, text, expected):
        assert parse_runtime(text) == expected

    def test_theatre_coordinates_come_from_the_static_map_url(self, theatre_html):
        """Not a Google maps link — a Bing virtualearth tile, which the first
        attempt at this regex missed entirely."""
        theatre = CinemarkShowtimes().parse_theatre(theatre_html, SLUG)
        assert theatre.lat == pytest.approx(29.487356)
        assert theatre.lon == pytest.approx(-98.587382)

    def test_venue_id_does_not_double_the_prefix(self, theatre_html):
        theatre = CinemarkShowtimes().parse_theatre(theatre_html, SLUG)
        assert theatre.venue_id == "cinemark-san-antonio-16"

    def test_challenge_is_its_own_error(self):
        with pytest.raises(CinemarkChallenged):
            CinemarkShowtimes().parse_showtimes(CHALLENGE)

    def test_models_without_showtimes_raise_rather_than_return_empty(self):
        html = '<div data-json-model="{&quot;cinemarkMovieId&quot;: 1}"></div>'
        with pytest.raises(CinemarkParseError, match="showDate"):
            CinemarkShowtimes().parse_showtimes(html)

    def test_theatre_slugs_from_sitemap(self):
        xml = ("<urlset><url><loc>https://www.cinemark.com/theatres/tx-x/cinemark-a</loc>"
               "</url><url><loc>https://www.cinemark.com/movies/b</loc></url>"
               "<url><loc>https://www.cinemark.com/theatres/</loc></url></urlset>")
        assert CinemarkShowtimes().theatre_slugs(xml) == ["tx-x/cinemark-a"]


class TestPrintTypePhrases:
    """Cinemark gives an English phrase, not a token."""

    @pytest.mark.parametrize(
        "phrase,check",
        [
            ("Luxury Lounger RealD 3D", lambda p: Attribute.THREE_D in p.attrs),
            ("Luxury Lounger RealD 3D", lambda p: Attribute.RECLINERS in p.attrs),
            ("Cinemark XD", lambda p: p.brand is Brand.PLF),
            ("Standard Format Luxury Lounger", lambda p: p.projection is Projection.DIGITAL),
            ("D-BOX Standard Format", lambda p: p.brand is Brand.DBOX),
            ("IMAX with Laser", lambda p: p.brand is Brand.IMAX),
        ],
    )
    def test_phrases_classify(self, phrase, check):
        presentation, _ = classify_print_type(phrase)
        assert check(presentation)

    def test_longest_phrase_wins(self):
        """'Cinemark XD' must not be read as bare 'XD' plus leftovers, and
        'RealD 3D' must not double-count."""
        xd, leftover = classify_print_type("Cinemark XD")
        assert xd.brand is Brand.PLF and not leftover

    def test_unknown_phrase_is_reported_as_leftover(self):
        """The token tables raise on the unknown; phrase matching cannot, so
        it returns leftovers instead — same guarantee, different mechanism."""
        _, leftover = classify_print_type("Cinemark Quantum Vision")
        assert leftover == {"quantum", "vision"}

    def test_every_phrase_in_the_fixture_is_known(self, theatre_html):
        shows = CinemarkShowtimes().parse_showtimes(theatre_html)
        unknown = {}
        for show in shows:
            _, leftover = classify_print_type(show.print_type)
            if leftover:
                unknown[show.print_type] = leftover
        assert not unknown, f"unclassified Cinemark phrases: {unknown}"


class TestTimezones:
    def test_state_prefix_gives_the_zone(self):
        assert timezone_for("tx-san-antonio/x") == "America/Chicago"
        assert timezone_for("ca-los-angeles/x") == "America/Los_Angeles"
        assert timezone_for("ny-nyc/x") == "America/New_York"

    def test_unknown_state_falls_back_rather_than_crashing(self):
        assert timezone_for("zz-nowhere/x") == "America/Chicago"

    def test_local_time_is_converted_not_relabelled(self, theatre_html):
        """The first cut set tzinfo=UTC on a local wall clock, silently
        shifting every Cinemark showing by hours."""
        provider = CinemarkProvider(robots=FakeRobots())
        theatre = provider.adapter.parse_theatre(theatre_html, SLUG)
        spec = SearchSpec(work=WorkRef(query="*"),
                          date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)))
        venue = Venue(theatre.venue_id, theatre.name, "cinemark", market=SLUG)
        show = provider._to_screenings(spec, venue, theatre, theatre_html)[0]
        assert show.starts_at_utc.hour != show.starts_at_local.hour
        assert show.starts_at_utc.tzinfo is timezone.utc


class TestProvider:
    def provider(self, **kw):
        return CinemarkProvider(robots=FakeRobots(), backoff_s=0, **kw)

    def spec(self, **kw):
        kw.setdefault("date_window", DateWindow(date(2026, 8, 2), date(2026, 8, 2)))
        return SearchSpec(work=WorkRef(query="*"), **kw)

    def screenings(self, theatre_html):
        provider = self.provider()
        theatre = provider.adapter.parse_theatre(theatre_html, SLUG)
        venue = Venue(theatre.venue_id, theatre.name, "cinemark", market=SLUG)
        return provider._to_screenings(self.spec(), venue, theatre, theatre_html)

    def test_produces_screenings(self, theatre_html):
        assert len(self.screenings(theatre_html)) > 60

    def test_untitled_showtimes_are_dropped_not_emitted_blank(self, theatre_html):
        assert all(s.work.title for s in self.screenings(theatre_html))

    def test_distance_is_computed_from_page_coordinates(self, theatre_html):
        provider = self.provider()
        theatre = provider.adapter.parse_theatre(theatre_html, SLUG)
        spec = self.spec(location=LocationSpec(origin=GeoPoint(29.42, -98.49),
                                               radius_km=60))
        venue = Venue(theatre.venue_id, theatre.name, "cinemark", market=SLUG)
        shows = provider._to_screenings(spec, venue, theatre, theatre_html)
        assert shows[0].distance_km and shows[0].distance_km < 30

    def test_no_projection_defaults_to_digital_never_film(self, theatre_html):
        for show in self.screenings(theatre_html):
            assert show.presentation.projection is not Projection.UNKNOWN
            assert not show.presentation.projection.is_film

    def test_slug_discovery_is_lazy_about_coordinates(self):
        """308 theatre pages is far too many to fetch just to build a
        directory, so discovery returns slug-only venues."""
        xml = "".join(
            f"<url><loc>https://www.cinemark.com/theatres/tx-a/cinemark-{i}</loc></url>"
            for i in range(5)
        )
        provider = self.provider(session=FakeSession(f"<urlset>{xml}</urlset>"))
        venues = provider.discover(self.spec())
        assert len(venues) == 5
        assert all(v.point is None and v.market for v in venues)


class TestRobots:
    """Advisory, not blocking.

    robots.txt is a crawler convention. This tool fetches the page a user
    asked about, at the volume that user would generate by clicking, so
    enforcement is off by default and the seat map is fetched normally.
    """

    def test_enforcement_is_off_by_default(self):
        cache = RobotsCache(session=_ServingRobots())
        cache.check_fetch("https://example.test/ticketseatmap/")   # no raise
        assert cache.skipped == ["https://example.test/ticketseatmap/"]

    def test_opting_in_restores_blocking_for_crawler_mode(self):
        """Kept for the scheduler, which polls on a timer with no human in
        the loop and is the one genuinely crawler-shaped part of the system."""
        cache = RobotsCache(session=_ServingRobots(), enforce=True)
        with pytest.raises(DisallowedByRobots):
            cache.check_fetch("https://example.test/ticketseatmap/")

    def test_a_disallowed_url_is_still_offered_as_a_human_link(self, theatre_html):
        shows = TestProvider().screenings(theatre_html)
        assert all("/TicketSeatMap/" in s.deeplink for s in shows)

    def test_missing_robots_txt_means_unrestricted(self):
        class NoRobots:
            def get(self, url, **kw):
                return type("R", (), {"status_code": 404, "text": ""})()

        cache = RobotsCache(session=NoRobots())
        assert cache.allowed("https://example.test/anything")

    def test_unreachable_robots_txt_fails_open(self):
        class Broken:
            def get(self, url, **kw):
                raise ConnectionError("down")

        assert RobotsCache(session=Broken()).allowed("https://example.test/x")

    def test_disallow_is_still_parsed_correctly(self):
        cache = RobotsCache(session=_ServingRobots())
        assert not cache.allowed("https://example.test/ticketseatmap/?x=1")
        assert cache.allowed("https://example.test/theatres/somewhere")

    def test_robots_is_fetched_once_per_host(self):
        calls = []

        class Counting:
            def get(self, url, **kw):
                calls.append(url)
                return type("R", (), {"status_code": 200, "text": "User-agent: *\n"})()

        cache = RobotsCache(session=Counting())
        cache.allowed("https://example.test/a")
        cache.allowed("https://example.test/b")
        assert len(calls) == 1


class TestGeographyBootstrap:
    """Cinemark hides coordinates on the theatre page, which creates a
    chicken-and-egg: LocationSpec rejects coordinate-less venues, so they are
    never visited, so they never gain coordinates and the chain contributes
    nothing. These tests pin the way out of that.
    """

    SITEMAP = "<urlset>" + "".join(
        f"<url><loc>https://www.cinemark.com/theatres/{state}-{city}/cinemark-{city}-{i}</loc></url>"
        for i, (state, city) in enumerate(
            [("tx", "san-antonio"), ("tx", "abilene"), ("ca", "los-angeles"),
             ("ny", "buffalo"), ("me", "portland")]
        )
    ) + "</urlset>"

    def provider(self, store=None, **kw):
        return CinemarkProvider(
            robots=FakeRobots(), backoff_s=0, store=store,
            session=FakeSession(self.SITEMAP), **kw
        )

    def spec(self, **loc):
        return SearchSpec(
            work=WorkRef(query="*"),
            date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)),
            location=LocationSpec(**loc),
        )

    def test_state_centroids_narrow_the_candidate_set(self):
        """307 slugs is far too many to bootstrap; state geography is the one
        signal available before any page is fetched."""
        provider = self.provider(bootstrap_limit=0)
        spec = self.spec(origin=GeoPoint(29.42, -98.49), radius_km=60)
        slugs = provider._plausible_slugs(spec)
        assert any("tx-" in s for s in slugs)
        assert not any("me-portland" in s for s in slugs), "Maine is not near Texas"

    def test_a_city_hint_sorts_matching_slugs_first(self):
        """The decisive fix: alphabetical order put Abilene ahead of San
        Antonio, so a bounded bootstrap learned the wrong venues."""
        provider = self.provider(bootstrap_limit=0)
        spec = self.spec(origin=GeoPoint(29.42, -98.49), radius_km=60,
                         city="San Antonio")
        assert "san-antonio" in provider._plausible_slugs(spec)[0]

    def test_no_origin_means_no_filtering(self):
        provider = self.provider(bootstrap_limit=0)
        assert len(provider._plausible_slugs(self.spec())) == 5

    def test_bad_geography_degrades_to_trying_rather_than_silence(self):
        """A centroid miss must not make the chain silently vanish."""
        provider = self.provider(bootstrap_limit=0)
        spec = self.spec(origin=GeoPoint(-33.9, 151.2), radius_km=10)  # Sydney
        assert provider._plausible_slugs(spec) == provider.slugs()

    def test_learned_coordinates_persist_across_runs(self, store=None):
        from screenwatch.service.store import Store

        store = Store.memory()
        store.put_venue_geo("cinemark-san-antonio-0", "cinemark",
                            "Cinemark San Antonio", 29.48, -98.58)

        provider = self.provider(store=store, bootstrap_limit=0)
        spec = self.spec(origin=GeoPoint(29.42, -98.49), radius_km=60)
        located = [v for v in provider.discover(spec) if v.point]
        assert [v.venue_id for v in located] == ["cinemark-san-antonio-0"]
        store.close()

    def test_bootstrap_is_bounded(self):
        """Unbounded, this would fetch hundreds of pages per search."""
        from screenwatch.service.store import Store

        store = Store.memory()
        fetched = []

        class CountingProvider(CinemarkProvider):
            def _get(self, url):
                fetched.append(url)
                return TestGeographyBootstrap.SITEMAP

        provider = CountingProvider(robots=FakeRobots(), backoff_s=0,
                                    store=store, bootstrap_limit=2)
        try:
            provider.discover(self.spec(origin=GeoPoint(29.42, -98.49), radius_km=60))
        except Exception:
            pass
        theatre_fetches = [u for u in fetched if "showDate=" in u]
        assert len(theatre_fetches) <= 2
        store.close()

    def test_search_service_hands_its_store_to_the_provider(self):
        """Persistence only helps if the provider is actually given a store."""
        from screenwatch.service.search import SearchService
        from screenwatch.service.store import Store

        store = Store.memory()
        provider = self.provider()
        assert provider.store is None
        SearchService([provider], store=store)
        assert provider.store is store
        store.close()


class TestTheatreName:
    def test_name_is_extracted_from_seo_title(self, theatre_html):
        """The page title is marketing copy; the theatre name is the tail."""
        theatre = CinemarkShowtimes().parse_theatre(theatre_html, SLUG)
        assert theatre.name == "Cinemark San Antonio 16"
        assert "Movie Theater In" not in theatre.name

    def test_falls_back_to_the_slug_when_the_title_is_useless(self):
        html = '<title>' + "x" * 200 + '</title><a href="?TheaterId=9">t</a>'
        theatre = CinemarkShowtimes().parse_theatre(html, "tx-x/cinemark-foo-bar-12")
        assert theatre.name == "Cinemark Foo Bar 12"


class TestSeatMaps:
    """A plain GET of the seat picker returns the full grid. No login, no
    cart, and no hold — a hold happens when a seat is *selected*, which this
    never does.
    """

    def auditorium(self, html):
        from screenwatch.seating.sources.cinemark import CinemarkSeatSource

        return CinemarkSeatSource.parse(html, venue_id="cinemark-san-antonio-16",
                                        screen_id="5")

    def test_parses_a_real_grid(self, seatmap_html):
        a = self.auditorium(seatmap_html)
        assert a.has_grid and len(a.seats) == 68
        assert a.geometry_confidence == 1.0

    def test_availability_comes_from_the_available_attribute(self, seatmap_html):
        """The class name mirrors it, but `available` is the authority —
        physical-distance buffers carry a normal seat type and are unavailable."""
        a = self.auditorium(seatmap_html)
        assert a.available == 34 and a.capacity == 68

    def test_grid_position_comes_from_the_info_attribute(self, seatmap_html):
        """`info` is rowLabel,seatNumber,rowIndex,colIndex,showtimeId — so no
        position has to be inferred."""
        a = self.auditorium(seatmap_html)
        first = a.rows()[0][0]
        assert first.row_label.isalpha() and first.col_label.isdigit()

    def test_blank_seats_are_dropped_so_they_become_aisles(self, seatmap_html):
        a = self.auditorium(seatmap_html)
        assert any(s.aisle_adjacent for s in a.seats)

    def test_accessible_seat_types_survive(self, seatmap_html):
        from screenwatch.seating.model import SeatKind

        kinds = {s.kind for s in self.auditorium(seatmap_html).seats}
        assert SeatKind.WHEELCHAIR in kinds or SeatKind.COMPANION in kinds

    def test_group_finding_works_on_the_real_room(self, seatmap_html):
        from screenwatch.seating.groups import find_groups

        best = find_groups(self.auditorium(seatmap_html), 4)[0]
        assert best.complete and best.cohesion.is_together

    def test_a_challenge_page_raises_rather_than_parsing_to_empty(self):
        from screenwatch.seating.sources.cinemark import CinemarkSeatSource

        with pytest.raises(SeatDataUnavailable, match="Cloudflare"):
            CinemarkSeatSource.parse(CHALLENGE, venue_id="v")

    def test_a_page_with_no_seats_raises(self):
        from screenwatch.seating.sources.cinemark import CinemarkSeatSource

        with pytest.raises(SeatDataUnavailable, match="no seat buttons"):
            CinemarkSeatSource.parse("<html><body>nothing</body></html>", venue_id="v")
