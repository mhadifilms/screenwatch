"""Independent venues: schema.org first, Vista ticket links second.

The two failure modes this pins are the ones that make a venue silently
vanish — decorative markup being read as "nothing on", and a Vista-backed
venue yielding nothing because only the JSON-LD path was tried.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from screenwatch.adapters.vista.links import (
    extract,
    has_vista_links,
    parse_clock,
)
from screenwatch.identity.work import WorkRef
from screenwatch.providers.independent import IndependentProvider, load_venues
from screenwatch.ranking.spec import DateWindow, GeoPoint, LocationSpec, SearchSpec
from screenwatch.seating.model import SeatDataUnavailable

VISTA_PAGE = """
<div class="calendar-list-day" id="calendar-list-day-2026-08-02">
  <div class="item"><h4><a href="/film/?id=1" class="title">Good Morning</a></h4>
    <div class="showtimes">
      <a href="https://t.metrograph.com/Ticketing/visSelectTickets.aspx?cinemacode=9999&txtSessionId=30418" title="Buy Tickets">11:00am</a>
      <a href="https://t.metrograph.com/Ticketing/visSelectTickets.aspx?cinemacode=9999&txtSessionId=30419" title="Buy Tickets">7:35pm</a>
    </div></div>
</div>
<div class="calendar-list-day" id="calendar-list-day-2026-08-03">
  <div class="item"><h4><a href="/film/?id=2" class="title">Sholay</a></h4>
    <div class="showtimes">
      <a href="https://t.metrograph.com/Ticketing/visSelectTickets.aspx?cinemacode=9999&txtSessionId=30420" title="Buy Tickets">1:00pm</a>
    </div></div>
</div>
"""


class FakeTransport:
    def __init__(self, body):
        self.body = body

    def get(self, url, **kw):
        return type("R", (), {"text": self.body, "status_code": 200})()


class TestVistaLinks:
    def test_detects_vista_ticketing(self):
        assert has_vista_links(VISTA_PAGE)
        assert not has_vista_links("<html><a href='/tickets'>Buy</a></html>")

    def test_extracts_time_title_and_session(self):
        shows = extract(VISTA_PAGE, default_date=date(2026, 8, 2))
        assert len(shows) == 3
        first = shows[0]
        assert first.title == "Good Morning"
        assert first.session_id == "30418"
        assert first.cinema_code == "9999"
        assert first.starts_at_local == datetime(2026, 8, 2, 11, 0)

    def test_date_comes_from_the_containing_day_block(self):
        """Multi-day listings group by date; using one default for all would
        pile every showing onto today."""
        shows = extract(VISTA_PAGE, default_date=date(2026, 8, 2))
        assert shows[-1].starts_at_local.date() == date(2026, 8, 3)

    def test_the_title_is_the_nearest_preceding_one(self):
        shows = extract(VISTA_PAGE, default_date=date(2026, 8, 2))
        assert [s.title for s in shows] == ["Good Morning", "Good Morning", "Sholay"]

    def test_non_time_anchors_are_skipped(self):
        """Vista links also appear on 'Buy Tickets' buttons with no time."""
        page = VISTA_PAGE.replace(">11:00am<", ">Buy Tickets<")
        assert len(extract(page, default_date=date(2026, 8, 2))) == 2

    @pytest.mark.parametrize(
        "label,hour,minute",
        [("11:00am", 11, 0), ("7:35pm", 19, 35), ("12:00am", 0, 0),
         ("12:30pm", 12, 30), ("1 pm", 13, 0)],
    )
    def test_clock_parsing(self, label, hour, minute):
        parsed = parse_clock(label, date(2026, 8, 2))
        assert (parsed.hour, parsed.minute) == (hour, minute)

    def test_garbage_is_not_a_time(self):
        assert parse_clock("Sold Out", date(2026, 8, 2)) is None

    def test_a_page_with_no_links_yields_nothing(self):
        assert extract("<html></html>", default_date=date(2026, 8, 2)) == []


class TestProvider:
    def spec(self, **kw):
        kw.setdefault("date_window", DateWindow(date(2026, 8, 1), date(2026, 8, 9)))
        return SearchSpec(work=WorkRef(query="*"), **kw)

    def provider(self):
        return IndependentProvider(venues=[{
            "venue_id": "metrograph", "name": "Metrograph",
            "url": "https://metrograph.com/", "lat": 40.71, "lon": -73.98,
            "tz": "America/New_York",
        }])

    def test_falls_back_to_vista_when_there_is_no_usable_markup(self):
        """Without this, a Vista-backed art house contributes nothing at all."""
        p = self.provider()
        shows = p.screenings(self.spec(), p.discover(self.spec()),
                             FakeTransport(VISTA_PAGE))
        assert len(shows) == 3
        assert all(s.chain == "independent" for s in shows)

    def test_local_times_are_converted_to_utc(self):
        p = self.provider()
        show = p.screenings(self.spec(), p.discover(self.spec()),
                            FakeTransport(VISTA_PAGE))[0]
        assert show.starts_at_utc.tzinfo is timezone.utc
        assert show.starts_at_utc.hour != show.starts_at_local.hour

    def test_deeplink_is_the_vista_ticket_url(self):
        p = self.provider()
        show = p.screenings(self.spec(), p.discover(self.spec()),
                            FakeTransport(VISTA_PAGE))[0]
        assert "visSelectTickets.aspx" in show.deeplink

    def test_dates_outside_the_window_are_dropped(self):
        p = self.provider()
        spec = self.spec(date_window=DateWindow(date(2026, 8, 2), date(2026, 8, 2)))
        shows = p.screenings(spec, p.discover(spec), FakeTransport(VISTA_PAGE))
        assert {s.starts_at_local.date() for s in shows} == {date(2026, 8, 2)}

    def test_decorative_markup_is_recorded_not_silently_dropped(self):
        """Film Forum publishes 56 ScreeningEvent nodes with empty startDate.
        That is a venue needing a fallback, not a dark venue."""
        page = ('<script type="application/ld+json">'
                '{"@type":"ScreeningEvent","name":"X","startDate":""}</script>')
        p = self.provider()
        p.screenings(self.spec(), p.discover(self.spec()), FakeTransport(page))
        assert "metrograph" in p.incomplete
        assert "startDate" in p.incomplete["metrograph"]

    def test_a_dead_venue_does_not_break_the_others(self):
        class Broken:
            def get(self, url, **kw):
                raise ConnectionError("down")

        p = self.provider()
        assert p.screenings(self.spec(), p.discover(self.spec()), Broken()) == []

    def test_seats_are_unavailable_with_a_reason(self):
        p = self.provider()
        with pytest.raises(SeatDataUnavailable, match="platform adapter"):
            p.fetch_seats(object(), transport=None)


def test_shipped_venue_config_is_well_formed():
    venues = load_venues()
    assert venues
    for row in venues:
        assert row["venue_id"] and row["url"].startswith("http")
        assert row.get("tz")


AGILE_PAGE = """
<div id="day-2026-08-02">
  <h3 class="film-card__title">The Odyssey in 70mm</h3>
  <div class="views-row-active-agiletix sales-state--DuringSales">
    <a href="https://store.coolidge.org/websales/pages/ticketsearchcriteria.aspx?evtinfo=1026376~guid&amp;"
       class="showtime-ticket__button"><span class="showtime-ticket">
       <span class="showtime-ticket__time">11:00am</span>
       <span class="showtime-ticket__venue">MH1</span></span></a>
  </div>
  <div class="views-row-active-agiletix sales-state--SoldOut">
    <a href="https://store.coolidge.org/websales/pages/ticketsearchcriteria.aspx?evtinfo=1026377~guid&amp;"
       class="showtime-ticket__button"><span class="showtime-ticket">
       <span class="showtime-ticket__time">8:00pm</span>
       <span class="showtime-ticket__venue">MH1</span></span></a>
  </div>
</div>
"""


class TestAgileLinks:
    def test_detects_agile_ticketing(self):
        from screenwatch.adapters.agile.links import has_agile_links

        assert has_agile_links(AGILE_PAGE)
        assert not has_agile_links(VISTA_PAGE)

    def test_time_is_dug_out_of_the_nested_span(self):
        """Agile nests the time inside the anchor rather than using its own
        text, so an anchor-text parser finds nothing."""
        from screenwatch.adapters.agile.links import extract

        shows = extract(AGILE_PAGE, default_date=date(2026, 8, 2))
        assert len(shows) == 2
        assert shows[0].starts_at_local == datetime(2026, 8, 2, 11, 0)

    def test_sales_state_gives_real_availability(self):
        from screenwatch.adapters.agile.links import extract

        shows = extract(AGILE_PAGE, default_date=date(2026, 8, 2))
        assert shows[0].on_sale and not shows[0].sold_out
        assert shows[1].sold_out

    def test_unknown_states_are_not_guessed_as_on_sale(self):
        from screenwatch.adapters.agile.links import extract

        page = AGILE_PAGE.replace("sales-state--DuringSales", "sales-state--Whatever")
        show = extract(page, default_date=date(2026, 8, 2))[0]
        assert not show.on_sale and not show.sold_out

    def test_screen_name_is_captured(self):
        """A rep house's 70mm room is not its digital one."""
        from screenwatch.adapters.agile.links import extract

        assert extract(AGILE_PAGE, default_date=date(2026, 8, 2))[0].screen == "MH1"

    def test_format_in_the_title_is_classified(self):
        from screenwatch.adapters.agile.links import extract
        from screenwatch.models import Projection
        from screenwatch.presentation import classify_text

        show = extract(AGILE_PAGE, default_date=date(2026, 8, 2))[0]
        assert classify_text(show.title)[0].projection is Projection.FILM_70MM


class TestAgileProvider:
    def provider(self):
        return IndependentProvider(venues=[{
            "venue_id": "coolidge-corner", "name": "Coolidge Corner Theatre",
            "url": "https://coolidge.org/", "lat": 42.34, "lon": -71.12,
            "tz": "America/New_York",
        }])

    def spec(self):
        return SearchSpec(work=WorkRef(query="*"),
                          date_window=DateWindow(date(2026, 8, 1), date(2026, 8, 9)))

    def test_agile_venues_produce_screenings(self):
        p = self.provider()
        shows = p.screenings(self.spec(), p.discover(self.spec()),
                             FakeTransport(AGILE_PAGE))
        assert len(shows) == 2

    def test_sold_out_survives_into_the_screening(self):
        from screenwatch.models import Availability

        p = self.provider()
        shows = p.screenings(self.spec(), p.discover(self.spec()),
                             FakeTransport(AGILE_PAGE))
        assert {s.availability for s in shows} == {
            Availability.SELLABLE, Availability.SOLD_OUT
        }

    def test_film_print_format_reaches_the_screening(self):
        from screenwatch.models import Projection

        p = self.provider()
        show = p.screenings(self.spec(), p.discover(self.spec()),
                            FakeTransport(AGILE_PAGE))[0]
        assert show.presentation.projection is Projection.FILM_70MM


class TestDateContainerVsPicker:
    """Every listing page carries a date *picker* as well as its listings, and
    in isolation a picker entry looks exactly like a day heading.

    Taking the nearest preceding date without distinguishing them dated the
    Coolidge's entire schedule to September — the last entry in its calendar
    widget — instead of today.
    """

    PICKER_THEN_SHOWS = """
    <table><tr>
      <td id="showtimes_calendar-2026-09-05" class="future">5</td>
    </tr></table>
    <h3 class="film-card__title">The Odyssey in 70mm</h3>
    <div class="views-row-active-agiletix sales-state--DuringSales">
      <a href="https://store.coolidge.org/websales/pages/ticketsearchcriteria.aspx?evtinfo=1~g&amp;">
        <span class="showtime-ticket__time">11:00am</span></a>
    </div>
    """

    GROUPED = """
    <a class="day-selector-day" id="day-selector-day-2026-09-05">5</a>
    <div class="calendar-list-day" id="calendar-list-day-2026-08-02">
      <h4><a class="title">Good Morning</a></h4>
      <a href="https://t.metrograph.com/Ticketing/visSelectTickets.aspx?cinemacode=9999&txtSessionId=1">11:00am</a>
    </div>
    """

    def test_a_table_cell_picker_is_not_treated_as_a_day_heading(self):
        from screenwatch.adapters.agile.links import extract

        show = extract(self.PICKER_THEN_SHOWS, default_date=date(2026, 8, 1))[0]
        assert show.starts_at_local.date() == date(2026, 8, 1)

    def test_an_anchor_picker_is_not_treated_as_a_day_heading(self):
        from screenwatch.adapters.vista.links import extract

        show = extract(self.GROUPED, default_date=date(2026, 8, 1))[0]
        assert show.starts_at_local.date() == date(2026, 8, 2), (
            "the wrapping div should win over the preceding anchor picker"
        )

    def test_a_real_block_container_still_sets_the_date(self):
        from screenwatch.adapters.vista.links import nearest_date_before

        html = '<div id="calendar-list-day-2026-08-02"><a>x</a>'
        assert nearest_date_before(html, len(html), date(2026, 1, 1)) == date(2026, 8, 2)

    def test_no_container_falls_back_to_the_default(self):
        from screenwatch.adapters.vista.links import nearest_date_before

        assert nearest_date_before("<html></html>", 5, date(2026, 8, 1)) == date(2026, 8, 1)

    def test_a_malformed_date_falls_back_rather_than_raising(self):
        from screenwatch.adapters.vista.links import nearest_date_before

        html = '<div id="day-2026-13-45">'
        assert nearest_date_before(html, len(html), date(2026, 8, 1)) == date(2026, 8, 1)
