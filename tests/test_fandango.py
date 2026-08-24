"""Fandango's seat map, and the showing it belongs to.

The fixture is a live capture from Regal Hacienda Crossings, which is the case
that motivated this source: Regal's own seat page answers a Cloudflare firewall
block, and Fandango serves the same room over plain JSON.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from conftest import load
from screenwatch.seating.capture import SeatProbe
from screenwatch.seating.model import (
    AmbiguousShowtimeMatch,
    SeatDataUnavailable,
    SeatKind,
    SeatStatus,
)
from screenwatch.seating.sources.fandango import (
    FandangoSeatSource,
    FandangoShowtime,
    FandangoTheater,
    pick_showtime,
)


@pytest.fixture
def seatmap() -> dict:
    return json.loads(load("fandango", "seatmap.json"))


class FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status


class FakeTransport:
    """Records what was asked for, and answers from a script."""

    def __init__(self, answers: dict[str, FakeResponse] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get(self, url, *, conditional=False, headers=None):
        self.calls.append((url, headers))
        for fragment, response in self.answers.items():
            if fragment in url:
                return response
        return FakeResponse("", 404)


class TestParsingASeatMap:
    def test_reads_every_seat_with_its_kind(self, seatmap):
        room = FandangoSeatSource.parse(seatmap, venue_id="regal-x")
        assert len(room.seats) == 152
        kinds = {}
        for seat in room.seats:
            kinds[seat.kind] = kinds.get(seat.kind, 0) + 1
        assert kinds[SeatKind.WHEELCHAIR] == 5
        assert kinds[SeatKind.COMPANION] == 4
        assert kinds[SeatKind.STANDARD] == 104
        assert kinds[SeatKind.BLOCKED] == 39
        assert room.capacity == seatmap["totalSeatCount"]

    def test_status_follows_the_payloads_own_arithmetic(self, seatmap):
        """A is available, A+R is the inventory, so R is sold and O is neither.

        This is the mapping the counts in the payload imply, and getting it
        backwards would report a nearly-full house as nearly empty.
        """
        room = FandangoSeatSource.parse(seatmap, venue_id="regal-x")
        available = sum(1 for s in room.seats if s.status is SeatStatus.AVAILABLE)
        sold = sum(1 for s in room.seats if s.status is SeatStatus.SOLD)
        unavailable = sum(1 for s in room.seats if s.status is SeatStatus.UNAVAILABLE)

        assert available == seatmap["totalAvailableSeatCount"]
        assert available + sold == seatmap["totalSeatCount"]
        assert unavailable == 39           # excluded from the count entirely

    def test_labels_come_from_the_seat_id_not_the_grid(self, seatmap):
        """`A13` is row A seat 13, which is what a ticket prints."""
        room = FandangoSeatSource.parse(seatmap, venue_id="regal-x")
        labels = {s.id for s in room.seats}
        assert "A13" in labels
        # Row letters, not grid numbers.
        assert all(s.row_label.isalpha() for s in room.seats)

    def test_indices_are_dense_and_front_to_back(self, seatmap):
        room = FandangoSeatSource.parse(seatmap, venue_id="regal-x")
        rows = sorted({s.row_index for s in room.seats})
        assert rows == list(range(len(rows)))
        assert min(s.y for s in room.seats) == 0.0
        assert max(s.y for s in room.seats) == 1.0

    def test_the_room_names_its_auditorium(self, seatmap):
        room = FandangoSeatSource.parse(seatmap, venue_id="regal-x")
        assert room.screen_id == str(seatmap["auditoriumId"])
        assert room.name == f"Auditorium {seatmap['auditoriumId']}"
        # Observed, not inferred.
        assert room.geometry_confidence == 1.0

    def test_an_empty_map_is_unavailable_not_an_empty_room(self):
        with pytest.raises(SeatDataUnavailable):
            FandangoSeatSource.parse({"seats": []}, venue_id="regal-x")


class TestFindingTheTheater:
    SEARCH = (
        '<a href="/some-other-place-BBQQ1/theater-page">x</a>'
        '<a href="/regal-hacienda-crossings-screenx-imax-and-rpx-AAOPK/theater-page">y</a>'
    )

    def test_reads_the_upper_case_id_out_of_the_link(self):
        """The id is upper-case in the href while the slug is not.

        Matching only lower-case characters found nothing at all, which is the
        bug this pins.
        """
        source = FandangoSeatSource()
        transport = FakeTransport({"/search": FakeResponse(self.SEARCH)})
        theater = source.find_theater(transport, "Regal Hacienda Crossings")
        assert theater is not None
        assert theater.tms_id == "AAOPK"
        assert theater.path.endswith("/theater-page")

    def test_a_weak_match_is_no_match(self):
        """Returning the wrong theater is indistinguishable from being right."""
        source = FandangoSeatSource()
        transport = FakeTransport({"/search": FakeResponse(self.SEARCH)})
        assert source.find_theater(transport, "Alamo Drafthouse Brooklyn") is None

    def test_the_lookup_is_cached(self):
        source = FandangoSeatSource()
        transport = FakeTransport({"/search": FakeResponse(self.SEARCH)})
        source.find_theater(transport, "Regal Hacienda Crossings")
        source.find_theater(transport, "Regal Hacienda Crossings")
        assert len(transport.calls) == 1


class TestTheApiSession:
    THEATER = FandangoTheater(path="/t/theater-page", slug="t", tms_id="AAOPK")

    def test_sends_the_headers_the_endpoint_requires(self):
        """Without these the endpoint answers FORBIDDEN, not 404."""
        source = FandangoSeatSource()
        transport = FakeTransport({
            "/t/theater-page": FakeResponse("<html/>"),
            "/napi/seatMap/": FakeResponse('{"seats":[{"id":"A1","row":1,"column":1,'
                                           '"type":"standard","status":"A"}]}'),
        })
        source.seat_map(transport, self.THEATER, "v2-abc")
        napi = [c for c in transport.calls if "/napi/" in c[0]]
        assert napi, "the seat map was never requested"
        headers = napi[0][1] or {}
        assert headers.get("x-requested-with") == "XMLHttpRequest"
        assert "referer" in headers

    def test_visits_the_theater_page_first_for_cookies(self):
        source = FandangoSeatSource()
        transport = FakeTransport({
            "/t/theater-page": FakeResponse("<html/>"),
            "/napi/seatMap/": FakeResponse('{"seats":[{"id":"A1","row":1,"column":1,'
                                           '"type":"standard","status":"A"}]}'),
        })
        source.seat_map(transport, self.THEATER, "v2-abc")
        assert "/t/theater-page" in transport.calls[0][0]

    def test_an_error_body_is_reported_not_parsed(self):
        source = FandangoSeatSource()
        transport = FakeTransport({
            "/t/theater-page": FakeResponse("<html/>"),
            "/napi/seatMap/": FakeResponse(
                '{"error":"FORBIDDEN","errorMessage":"Session expired or invalid token"}'
            ),
        })
        with pytest.raises(SeatDataUnavailable, match="Session expired"):
            source.seat_map(transport, self.THEATER, "v2-abc")

    def test_showtimes_are_flattened_out_of_the_view_model(self):
        source = FandangoSeatSource()
        body = {
            "viewModel": {
                "movies": [{
                    "title": "The Odyssey",
                    "variants": [{
                        "amenityGroups": [{
                            "hasReservedSeating": True,
                            "showtimes": [
                                {"showtimeHashCode": "v2-a", "id": 1,
                                 "ticketingDate": "2026-08-21+14:10"},
                                {"id": 2},                      # no hash: skipped
                            ],
                        }],
                    }],
                }],
            }
        }
        transport = FakeTransport({
            "/t/theater-page": FakeResponse("<html/>"),
            "/napi/theaterMovieShowtimes/": FakeResponse(json.dumps(body)),
        })
        out = source.showtimes(transport, self.THEATER, date(2026, 8, 21), "regal")
        assert len(out) == 1
        assert out[0].hash_code == "v2-a"
        assert out[0].starts_at_local == datetime(2026, 8, 21, 14, 10)
        assert out[0].reserved_seating is True


class TestMatchingAShowing:
    def show(self, hh, mm, title="The Odyssey", code="v2-x"):
        return FandangoShowtime(
            hash_code=code, showtime_id="1", title=title,
            starts_at_local=datetime(2026, 8, 21, hh, mm), reserved_seating=True,
        )

    def test_matches_on_start_time(self):
        picked = pick_showtime(
            [self.show(11, 30, code="early"), self.show(14, 10, code="right")],
            title="The Odyssey",
            starts_at_local=datetime(2026, 8, 21, 14, 10),
        )
        assert picked.hash_code == "right"

    def test_title_breaks_a_tie_between_screens(self):
        picked = pick_showtime(
            [self.show(14, 10, title="Some Other Film", code="wrong"),
             self.show(14, 10, title="The Odyssey", code="right")],
            title="The Odyssey",
            starts_at_local=datetime(2026, 8, 21, 14, 10),
        )
        assert picked.hash_code == "right"

    def test_same_title_and_time_is_ambiguous_instead_of_first_wins(self):
        with pytest.raises(AmbiguousShowtimeMatch):
            pick_showtime(
                [self.show(14, 10, code="a"), self.show(14, 10, code="b")],
                title="The Odyssey",
                starts_at_local=datetime(2026, 8, 21, 14, 10),
            )

    def test_known_auditorium_resolves_an_ambiguous_time(self):
        picked = pick_showtime(
            [self.show(14, 10, code="a"), self.show(14, 10, code="b")],
            title="The Odyssey",
            starts_at_local=datetime(2026, 8, 21, 14, 10),
            source_screen_id="21",
            auditorium_id_for=lambda show: "21" if show.hash_code == "b" else "9",
        )
        assert picked.hash_code == "b"

    def test_a_distant_showing_is_not_a_match(self):
        assert pick_showtime(
            [self.show(18, 10)],
            title="The Odyssey",
            starts_at_local=datetime(2026, 8, 21, 14, 10),
        ) is None

    def test_without_a_start_time_it_refuses_to_guess(self):
        """Handing back the wrong auditorium is worse than handing back none."""
        assert pick_showtime(
            [self.show(14, 10)], title="The Odyssey", starts_at_local=None
        ) is None

    def test_no_candidates_is_no_match(self):
        assert pick_showtime([], title="x", starts_at_local=datetime.now()) is None

class TestRegalWithoutABrowser:
    """A deployment can ship without Chromium and still read Regal rooms.

    This is the shape the bridge's container ships in: Fandango needs no
    browser, so the browser tier is an optional extra. Before this, a missing
    Chromium raised straight past the fallback and the room was unreachable.
    """

    def test_a_missing_browser_falls_through_to_fandango(self, seatmap):
        import json as _json
        from datetime import datetime

        from screenwatch.browser import BrowserUnavailable
        from screenwatch.providers.regal import RegalProvider

        class NoBrowser:
            @staticmethod
            def visit(url, **kw):
                raise BrowserUnavailable("playwright is not installed")

        search_html = (
            '<a href="/regal-hacienda-crossings-screenx-imax-and-rpx-AAOPK/'
            'theater-page">y</a>'
        )
        showtimes = {
            "viewModel": {"movies": [{
                "title": "The Odyssey",
                "variants": [{"amenityGroups": [{
                    "hasReservedSeating": True,
                    "showtimes": [{"showtimeHashCode": "v2-a", "id": 9,
                                   "ticketingDate": "2026-08-02+17:00"}],
                }]}],
            }]}
        }
        transport = FakeTransport({
            "/search": FakeResponse(search_html),
            "/theater-page": FakeResponse("<html/>"),
            "/napi/theaterMovieShowtimes/": FakeResponse(_json.dumps(showtimes)),
            "/napi/seatMap/": FakeResponse(_json.dumps(seatmap)),
        })

        provider = RegalProvider(session=None, backoff_s=0, browser=NoBrowser())
        provider._theatres = [type("T", (), {
            "venue_id": "regal-x", "theatre_code": "1929",
            "name": "Regal Hacienda Crossings"})()]
        probe = SeatProbe(
            source="regal", venue_id="regal-x", source_venue_id="1929",
            showtime_id="1", booking_url=None,
            starts_at_local=datetime(2026, 8, 2, 17), title="The Odyssey",
            source_screen_id="21",
            metadata={
                "movie_code": "HO1",
                "source_title": "The Odyssey",
                "venue_name": "Regal Hacienda Crossings",
            },
        )

        room = provider.fetch(probe, transport=transport).auditorium
        assert len(room.seats) == 152
        assert room.geometry_confidence == 1.0
