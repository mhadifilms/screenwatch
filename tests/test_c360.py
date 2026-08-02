"""Cinema360 — the platform behind Apple Cinemas.

The interesting part is what this source can and cannot tell you. It publishes
exact `totalSeatsSold` per showing and the real auditorium layout, but
`totalAvailable` is always zero and the per-seat occupancy lives behind a
booking hold. So availability is *derived* (capacity minus sold) and group
feasibility is *estimated*, and the tests pin both of those honestly.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from screenwatch.adapters.c360.schedule import (
    C360_TOKENS,
    C360ParseError,
    C360Schedule,
    iana_timezone,
)
from screenwatch.identity.work import WorkRef
from screenwatch.models import Availability, Brand, Projection
from screenwatch.providers.c360 import C360Provider
from screenwatch.ranking.candidate import Option
from screenwatch.ranking.spec import DateWindow, SearchSpec
from screenwatch.seating.model import SeatDataUnavailable


def fixture(name):
    from conftest import FIXTURES

    return json.loads((FIXTURES / "c360" / name).read_text())


@pytest.fixture(scope="module")
def shows_payload():
    return fixture("advance-shows-white-plains.json")


@pytest.fixture(scope="module")
def screen_payload():
    return fixture("screen-auditorium-7.json")


@pytest.fixture(scope="module")
def locations_payload():
    return fixture("locations.json")


class FakeSession:
    """Serves fixtures by URL fragment, and can simulate the CF challenge."""

    CHALLENGE = "<html><title>Just a moment...</title></html>"

    def __init__(self, routes, challenges=0):
        self.routes = routes
        self.challenges = challenges
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        if self.challenges > 0:
            self.challenges -= 1
            return type("R", (), {"status_code": 403, "text": self.CHALLENGE})()
        for fragment, payload in self.routes.items():
            if fragment in url:
                body = payload if isinstance(payload, str) else json.dumps(payload)
                return type("R", (), {"status_code": 200, "text": body,
                                      "json": lambda self, p=payload: p})()
        # The SPA shell is what an unmatched route really returns.
        return type("R", (), {"status_code": 200,
                              "text": "<!DOCTYPE html><html>C360OnlineSWeb</html>"})()


def provider(shows, screen, locations, **kw):
    session = FakeSession({
        "GetLocationsByCompanyId": locations,
        "GetAdvanceShows": shows,
        "GetScreenById": screen,
        "applecinemas.com/": "<html>landing</html>",
    }, challenges=kw.pop("challenges", 0))
    return C360Provider(session=session, backoff_s=0, **kw), session


class TestSchedule:
    def test_flattens_movie_screen_showtime_nesting(self, shows_payload):
        shows = C360Schedule().parse_shows(shows_payload, "loc1")
        assert shows and all(s.show_id and s.screen_id for s in shows)

    def test_sold_counts_are_real(self, shows_payload):
        """`totalSeatsSold` is populated; it is the only real occupancy signal
        this platform gives."""
        shows = C360Schedule().parse_shows(shows_payload, "loc1")
        assert any(s.seats_sold > 0 for s in shows)

    def test_total_available_is_not_trusted(self, shows_payload):
        """`totalAvailable` is populated for far-future advance bookings and
        zero for everything in the next weeks - i.e. zero exactly when you
        need it. Reading it as "seats remaining" would mark a wide-open
        showing sold out, so remaining seats come from capacity minus sold."""
        shows = C360Schedule().parse_shows(shows_payload, "loc1")
        near = [s for s in shows if s.starts_at_local.year == 2026]
        assert near, "fixture should contain near-term shows"
        assert all(s.seats_available == 0 for s in near)
        assert any(s.seats_sold > 0 for s in near), "but sold counts are real"

    def test_disabled_showtimes_are_skipped(self):
        payload = [{"movieName": "X", "movieID": "1", "screens": [
            {"screenID": "s", "showTimes": [
                {"showID": "a", "showTime": "2026-08-02T10:00:00", "disabled": True},
                {"showID": "b", "showTime": "2026-08-02T12:00:00", "disabled": False},
            ]}]}]
        shows = C360Schedule().parse_shows(payload, "loc1")
        assert [s.show_id for s in shows] == ["b"]

    def test_runtime_is_parsed_from_hh_mm(self, shows_payload):
        shows = C360Schedule().parse_shows(shows_payload, "loc1")
        assert any(s.runtime_min and s.runtime_min > 60 for s in shows)

    def test_locations_carry_iana_timezones(self, locations_payload):
        locs = C360Schedule().parse_locations(locations_payload)
        assert locs[0].tz == "America/New_York"
        assert locs[0].venue_id.startswith("c360-")

    def test_windows_timezone_names_are_translated(self):
        assert iana_timezone("Pacific Standard Time") == "America/Los_Angeles"
        assert iana_timezone("nonsense") == "America/New_York"

    def test_empty_locations_raise(self):
        with pytest.raises(C360ParseError, match="no locations"):
            C360Schedule().parse_locations([])


class TestScreenGeometry:
    def test_rows_decode_to_label_and_count(self, screen_payload):
        screen = C360Schedule().parse_screen(screen_payload)
        assert screen.rows[0] == ("A", 11)
        assert len(screen.rows) == 9

    def test_capacity_excludes_broken_and_house_seats(self, screen_payload):
        """104 physical seats, 4 broken and 4 house - selling 104 would be a
        lie that inflates every feasibility estimate."""
        screen = C360Schedule().parse_screen(screen_payload)
        assert screen.total_seats == 104
        assert screen.bookable_seats == 96

    def test_unparseable_rows_raise(self):
        with pytest.raises(C360ParseError, match="no parseable rows"):
            C360Schedule().parse_screen({"screen_ID": "s", "rows": ["???"]})


class TestFormats:
    def test_imax_70mm_is_distinguished(self):
        assert C360_TOKENS["imax70mm"].projection is Projection.FILM_70MM_15PERF

    def test_acx_is_a_premium_format(self):
        assert C360_TOKENS["acx"].brand is Brand.PLF

    def test_infinity_vision_is_laser(self):
        assert C360_TOKENS["acxinfinityvision"].projection is Projection.DIGITAL_LASER

    def test_show_properties_merge_into_attributes(self, shows_payload):
        adapter = C360Schedule()
        shows = adapter.parse_shows(shows_payload, "loc1")
        with_props = [s for s in shows if s.properties]
        assert with_props
        presentation = adapter.classify(with_props[0])
        assert presentation.attrs

    def test_table_covers_the_platforms_declared_vocabulary(self):
        """`GetScreenSettingOfAllLocations` publishes the format names, so the
        table is checkable against the source rather than against luck."""
        declared = [{"screenProperties": [
            {"screenSettingName": n} for n in
            ["2D", "3D", "IMAX", "IMAX 3D", "IMAX 70MM", "ACX", "ACX 3D",
             "ACX Infinity Vision", "ACX Dolby Atmos", "Screen X", "4DX",
             "Dolby Atmos", "Open Caption", "Sensory Friendly", "REALD 3D",
             "3D HFR", "3D HFR ACX DOLBY ATMOS", "ScreenX Infinity Vision"]
        ]}]
        assert C360Schedule.unknown_formats(declared) == set()


class TestProvider:
    def spec(self, **kw):
        kw.setdefault("date_window", DateWindow(date(2026, 8, 2), date(2026, 8, 2)))
        kw.setdefault("party_size", 4)
        return SearchSpec(work=WorkRef(query="*"), **kw)

    def test_warms_the_session_before_calling_the_api(self, shows_payload, screen_payload,
                                                      locations_payload):
        """Cloudflare issues its cookies on the landing page, not the API -
        calling an endpoint cold gets a challenge."""
        p, session = provider(shows_payload, screen_payload, locations_payload)
        p.locations()
        assert session.urls[0].endswith("applecinemas.com/")

    def test_retries_through_an_intermittent_challenge(self, shows_payload, screen_payload,
                                                       locations_payload):
        p, session = provider(shows_payload, screen_payload, locations_payload,
                              challenges=2)
        assert p.locations()

    def test_the_spa_shell_is_treated_as_an_error_not_success(self, shows_payload,
                                                              screen_payload,
                                                              locations_payload):
        """An unmatched route returns the Angular shell with HTTP 200, which
        would otherwise read as 'this venue has nothing on'."""
        p, _ = provider(shows_payload, screen_payload, locations_payload)
        with pytest.raises(C360ParseError, match="SPA shell"):
            p._json("/Nonexistent/Route")

    def test_availability_is_unknown_until_capacity_is_known(self, shows_payload,
                                                             screen_payload,
                                                             locations_payload):
        """Sold counts alone cannot say whether a show is full; that needs the
        screen record, which is a phase-B fetch."""
        p, _ = provider(shows_payload, screen_payload, locations_payload)
        venues = p.discover(self.spec())
        shows = p.screenings(self.spec(), venues[:1], transport=None)
        assert shows
        assert {s.availability for s in shows} <= {
            Availability.UNKNOWN, Availability.SELLABLE
        }

    def test_seat_fetch_returns_shape_and_counts_not_a_grid(self, shows_payload,
                                                            screen_payload,
                                                            locations_payload):
        p, _ = provider(shows_payload, screen_payload, locations_payload)
        venues = p.discover(self.spec())
        show = next(s for s in p.screenings(self.spec(), venues[:1], transport=None)
                    if s.seats_sold is not None)
        auditorium = p.fetch_seats(Option(screening=show), transport=None)

        assert not auditorium.has_grid, "C360 never reveals which seats are taken"
        assert auditorium.has_shape, "but it does reveal the room's shape"
        assert auditorium.capacity == 96
        assert auditorium.available == 96 - show.seats_sold
        assert len(auditorium.row_lengths) == 9

    def test_a_showing_without_a_screen_id_is_unavailable(self, shows_payload,
                                                          screen_payload,
                                                          locations_payload):
        from screenwatch.ranking.candidate import Screening
        from screenwatch.identity.work import Work

        p, _ = provider(shows_payload, screen_payload, locations_payload)
        bare = Screening(
            screening_id="c360:x", work=Work("w", "X"), venue_id="v", venue_name="V",
            chain="c360", starts_at_utc=datetime.now(timezone.utc),
            starts_at_local=datetime.now(), presentation=C360_TOKENS["2d"],
            screen_id=None,
        )
        with pytest.raises(SeatDataUnavailable, match="no screen id"):
            p.fetch_seats(Option(screening=bare), transport=None)

    def test_screen_records_are_cached(self, shows_payload, screen_payload,
                                       locations_payload):
        p, session = provider(shows_payload, screen_payload, locations_payload)
        p.screen("abc")
        p.screen("abc")
        assert sum("GetScreenById" in u for u in session.urls) == 1


class TestPhaseBIntegration:
    def test_estimated_options_are_scored_and_labelled(self, shows_payload,
                                                       screen_payload,
                                                       locations_payload):
        """The payoff: phase B produces a real judgement for a chain that has
        no seat grid at all."""
        from screenwatch.ranking.coarse import coarse_rank
        from screenwatch.ranking.fine import fine_rank

        p, _ = provider(shows_payload, screen_payload, locations_payload)
        spec = SearchSpec(work=WorkRef(query="*"), party_size=4,
                          date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)),
                          include_sold_out=True)
        venues = p.discover(spec)
        shows = p.screenings(spec, venues[:1], transport=None)
        options = coarse_rank(shows, spec)
        ranked = fine_rank(options, spec, lambda o: p.fetch_seats(o, None), top_k=3)

        estimated = [o for o in ranked if o.seat_data == "estimated"]
        assert estimated, "expected estimation, not a hard failure"
        best = estimated[0]
        assert best.feasibility is not None
        assert 0.0 <= best.can_sit_together_probability <= 1.0
        assert best.components["group_cohesion"] < 1.0, "an estimate must not beat a grid"


class TestSeatListCoercion:
    """Seat lists arrive as bare strings on most screens and as objects on
    some. Assuming strings raised `unhashable type: dict` against a live
    auditorium — a crash mid-search, not a degraded result."""

    def base(self, **kw):
        payload = {"screen_ID": "s", "screen_Name": "n", "rows": ["A10", "B10"]}
        payload.update(kw)
        return C360Schedule().parse_screen(payload)

    def test_string_seat_lists(self):
        assert self.base(broken_Seats=["A1", "A2"]).broken == {"A1", "A2"}

    def test_object_seat_lists(self):
        screen = self.base(blankSeats=[{"seatId": "B3"}, {"name": "B4"}])
        assert screen.blank == {"B3", "B4"}

    def test_mixed_lists(self):
        assert self.base(house_Seats=["A1", {"seatName": "A2"}]).house == {"A1", "A2"}

    def test_unrecognised_objects_are_dropped_not_fatal(self):
        assert self.base(unavailable=[{"unexpected": 1}]).unavailable == frozenset()

    def test_capacity_still_subtracts_object_form_blanks(self):
        screen = self.base(blankSeats=[{"seatId": "B3"}], broken_Seats=["A1"])
        assert screen.bookable_seats == 18
