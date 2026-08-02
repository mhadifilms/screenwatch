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
from screenwatch.ranking.spec import DateWindow, SearchSpec
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


class TestAgileMarkupVariants:
    """Two Agile venues, two different shapes.

    The Coolidge wraps each link in a `sales-state--` div with the time in a
    nested span. IFC Center emits a bare anchor whose own text is the time,
    under a plain `<h3>` with no title class, grouped by a textual "Sun Aug 2"
    heading rather than an ISO attribute.

    The first cut required the Coolidge's shape on all three counts and threw
    away all 140 of IFC's valid links.
    """

    IFC_SHAPE = """
    <div class="daily-schedule sun active">
      <h3>Sun Aug 2</h3>
      <ul><li><div class="details"><h3><a href="/films/jimmy/">Jimmy</a></h3>
        <ul class="times"><li>
          <a href="https://tickets.ifccenter.com/websales/pages/ticketsearchcriteria.aspx?evtinfo=570910~guid&#038; ">2:50 PM </a>
        </li></ul>
      </div></li></ul>
    </div>
    """

    def extract(self, html, on=date(2026, 8, 1)):
        from screenwatch.adapters.agile.links import extract

        return extract(html, default_date=on)

    def test_a_bare_anchor_without_a_wrapper_is_matched(self):
        assert len(self.extract(self.IFC_SHAPE)) == 1

    def test_time_falls_back_to_the_anchor_text(self):
        show = self.extract(self.IFC_SHAPE)[0]
        assert (show.starts_at_local.hour, show.starts_at_local.minute) == (14, 50)

    def test_a_plain_heading_supplies_the_title(self):
        assert self.extract(self.IFC_SHAPE)[0].title == "Jimmy"

    def test_a_textual_day_heading_supplies_the_date(self):
        """No ISO attribute anywhere on the page."""
        assert self.extract(self.IFC_SHAPE)[0].starts_at_local.date() == date(2026, 8, 2)

    def test_missing_sales_state_is_unknown_not_assumed_on_sale(self):
        show = self.extract(self.IFC_SHAPE)[0]
        assert show.sales_state == "unknown"
        assert not show.on_sale and not show.sold_out

    def test_the_coolidge_shape_still_works(self):
        shows = self.extract(AGILE_PAGE, on=date(2026, 8, 2))
        assert len(shows) == 2 and shows[0].on_sale and shows[1].sold_out


class TestTextualDayHeadings:
    def parse(self, html, default=date(2026, 8, 1)):
        from screenwatch.adapters.vista.links import nearest_date_before

        return nearest_date_before(html, len(html), default)

    def test_reads_a_written_day_heading(self):
        assert self.parse("<h3>Sun Aug 2</h3>") == date(2026, 8, 2)

    def test_an_iso_container_wins_when_it_is_closer(self):
        html = '<h3>Sun Aug 2</h3><div id="day-2026-08-05">'
        assert self.parse(html) == date(2026, 8, 5)

    def test_a_textual_heading_wins_when_it_is_closer(self):
        html = '<div id="day-2026-08-05"></div><h3>Sun Aug 9</h3>'
        assert self.parse(html) == date(2026, 8, 9)

    def test_a_december_listing_read_in_january_rolls_forward(self):
        """A textual heading carries no year; assuming the current one would
        date a January listing eleven months into the past."""
        assert self.parse("<h3>Mon Jan 5</h3>", default=date(2026, 12, 28)) == \
               date(2027, 1, 5)

    def test_nonsense_dates_fall_back(self):
        assert self.parse("<h3>Sun Feb 31</h3>") == date(2026, 8, 1)


LISTING_PAGE = """
<h2>Now Playing</h2>
<div class="playing-this-week-block__film">
  <div class="playing-this-week-block_film-title"><h4><a href="/film/a-life-illuminated/">A Life Illuminated</a></h4></div>
  <div class="playing-this-week-block__showtimes">
    <p class="playing-this-week-block__date">Sunday, August 2, 2026</p>
    <p><a href="/film/a-life-illuminated//#showtimes">12:50 PM</a></p>
  </div>
</div>
"""

HIDDEN_DATE_PAGE = """
<h3>Theatre &amp; Box Office</h3>
<h4>Hercules</h4>
<a class="btn" href="/order/add-tickets/15690/nojs">
  <h4><span class="visually-hidden">Sunday, Aug 2</span>11:00am</h4> BUY TICKETS</a>
"""


class TestOwnSiteListings:
    """Venues on no shared platform still share one shape: a link whose text
    is a time. Requiring the *link* is what keeps it safe — page copy is full
    of times ("doors at 7:00 PM") and matching bare text would drag them in.
    """

    def extract(self, html, on=date(2026, 8, 2), base=""):
        from screenwatch.adapters.generic.listing import extract

        return extract(html, default_date=on, base_url=base)

    def test_finds_a_clickable_showtime(self):
        shows = self.extract(LISTING_PAGE)
        assert len(shows) == 1
        assert shows[0].starts_at_local == datetime(2026, 8, 2, 12, 50)

    def test_title_is_the_nearest_heading_not_a_far_off_banner(self):
        """Pattern priority once beat proximity, and Roxie came back with the
        same film name on all 34 of its showtimes."""
        assert self.extract(LISTING_PAGE)[0].title == "A Life Illuminated"

    def test_section_furniture_is_not_mistaken_for_a_film(self):
        page = '<h2>Now Playing</h2><a href="/x">7:00 PM</a>'
        assert self.extract(page) == []

    def test_relative_urls_are_resolved(self):
        show = self.extract(LISTING_PAGE, base="https://roxie.com")[0]
        assert show.url.startswith("https://roxie.com/")

    def test_bare_text_times_are_ignored(self):
        """Only clickable times count."""
        assert self.extract("<h4>Hercules</h4><p>Doors at 7:00 PM</p>") == []

    def test_a_hidden_accessibility_date_beats_any_container_guess(self):
        """Music Box puts the day in a visually-hidden span for screen
        readers, which makes it the most reliable signal on the page."""
        show = self.extract(HIDDEN_DATE_PAGE, on=date(2026, 1, 1))[0]
        assert show.starts_at_local.date() == date(2026, 8, 2)

    def test_extra_link_text_does_not_defeat_the_time(self):
        """The anchor reads "11:00am BUY TICKETS"; strict whole-string
        matching rejected it and dropped all twelve Music Box showtimes."""
        show = self.extract(HIDDEN_DATE_PAGE)[0]
        assert (show.starts_at_local.hour, show.starts_at_local.minute) == (11, 0)

    def test_the_same_showing_linked_twice_is_deduped(self):
        doubled = LISTING_PAGE + LISTING_PAGE.split("<h2>Now Playing</h2>")[1]
        assert len(self.extract(doubled)) == 1


class TestWrittenDates:
    def parse(self, text, default=date(2026, 8, 1)):
        from screenwatch.adapters.listing_common import parse_written_date

        return parse_written_date(text, default)

    def test_short_and_long_month_names(self):
        assert self.parse("Sunday, Aug 2") == date(2026, 8, 2)
        assert self.parse("Sunday, August 2, 2026") == date(2026, 8, 2)

    def test_an_explicit_year_is_honoured(self):
        assert self.parse("Fri, Jan 3, 2027") == date(2027, 1, 3)

    def test_a_yearless_january_read_in_december_rolls_forward(self):
        assert self.parse("Mon Jan 5", default=date(2026, 12, 28)) == date(2027, 1, 5)

    def test_nonsense_returns_none(self):
        assert self.parse("Sunday, Feb 31") is None
        assert self.parse("not a date") is None


class TestBrowserFetchFlag:
    def test_browser_venues_use_the_browser(self):
        """Music Box serves 1.3KB of Sucuri JavaScript to a plain client."""
        calls = []

        class FakeBrowser:
            @staticmethod
            def text(url):
                calls.append(url)
                return HIDDEN_DATE_PAGE

        p = IndependentProvider(venues=[{
            "venue_id": "music-box", "name": "Music Box", "url": "https://mb.test/",
            "tz": "America/Chicago", "fetch": "browser",
        }])
        import screenwatch.providers.independent as mod

        original = mod.shared_browser
        mod.shared_browser = lambda **kw: FakeBrowser()
        try:
            spec = SearchSpec(work=WorkRef(query="*"),
                              date_window=DateWindow(date(2026, 8, 1), date(2026, 8, 9)))
            shows = p.screenings(spec, p.discover(spec), FakeTransport("<html></html>"))
        finally:
            mod.shared_browser = original

        assert calls == ["https://mb.test/"]
        assert len(shows) == 1

    def test_plain_venues_do_not_start_a_browser(self):
        p = IndependentProvider(venues=[{
            "venue_id": "roxie", "name": "Roxie", "url": "https://roxie.test/",
            "tz": "America/Los_Angeles",
        }])
        spec = SearchSpec(work=WorkRef(query="*"),
                          date_window=DateWindow(date(2026, 8, 1), date(2026, 8, 9)))
        assert len(p.screenings(spec, p.discover(spec), FakeTransport(LISTING_PAGE))) == 1


class TestAgileStateDoesNotBleed:
    """A showtime with no wrapper of its own must not inherit its
    neighbour's sales state.

    A fixed-character lookback let an *available* show sitting after a
    sold-out one report as sold out — suppressing a real seat, which is the
    worst thing this module can do.
    """

    MIXED = """
    <h3 class="film-card__title">The Odyssey</h3>
    <div class="views-row-active-agiletix sales-state--SoldOut">
      <a href="https://store.coolidge.org/websales/pages/ticketsearchcriteria.aspx?evtinfo=1~g&amp;">
        <span class="showtime-ticket__time">1:00pm</span></a>
    </div>
    <div class="views-row-active-agiletix">
      <a href="https://store.coolidge.org/websales/pages/ticketsearchcriteria.aspx?evtinfo=2~g&amp;">
        <span class="showtime-ticket__time">4:00pm</span></a>
    </div>
    """

    def shows(self):
        from screenwatch.adapters.agile.links import extract

        return extract(self.MIXED, default_date=date(2026, 8, 2))

    def test_the_wrapped_show_keeps_its_state(self):
        assert self.shows()[0].sold_out

    def test_the_unwrapped_show_does_not_inherit_it(self):
        later = self.shows()[1]
        assert not later.sold_out, "an available showing was suppressed as sold out"
        assert later.sales_state == "unknown"

    def test_the_nearest_wrapper_wins_not_the_farthest(self):
        """`search` is leftmost-first; with two wrappers in range the farther
        one used to decide."""
        from screenwatch.adapters.agile.links import extract

        html = self.MIXED.replace(
            '<div class="views-row-active-agiletix">',
            '<div class="views-row-active-agiletix sales-state--DuringSales">',
        )
        assert extract(html, default_date=date(2026, 8, 2))[1].on_sale

    def test_the_real_coolidge_page_still_parses(self):
        assert len(self.__class__ and TestAgileMarkupVariants().extract(AGILE_PAGE,
                    on=date(2026, 8, 2))) == 2


class TestAgileClosedStates:
    """States that only became visible once the bleed was fixed."""

    def show(self, state):
        from screenwatch.adapters.agile.links import extract

        html = f'''
        <h3 class="film-card__title">Sholay</h3>
        <div class="views-row-active-agiletix sales-state--{state}">
          <a href="https://s.test/websales/pages/ticketsearchcriteria.aspx?evtinfo=1~g&amp;">
            <span class="showtime-ticket__time">1:00pm</span></a>
        </div>'''
        return extract(html, default_date=date(2026, 8, 2))[0]

    def test_after_event_is_closed_not_sold_out(self):
        """The room may be half empty; it is simply no longer for sale."""
        s = self.show("AfterEvent")
        assert s.closed and not s.sold_out and not s.on_sale

    def test_sales_ended_before_the_event_is_also_closed(self):
        assert self.show("AfterSalesBeforeEvent").closed

    def test_during_sales_is_open(self):
        s = self.show("DuringSales")
        assert s.on_sale and not s.closed

    def test_sold_out_stays_sold_out(self):
        s = self.show("SoldOut")
        assert s.sold_out and not s.closed

    def test_an_unknown_state_is_none_of_the_three(self):
        s = self.show("SomethingNew")
        assert not (s.on_sale or s.sold_out or s.closed)

    def test_closed_showings_are_not_offered(self):
        html = f'''
        <h3 class="film-card__title">Sholay</h3>
        <div class="views-row-active-agiletix sales-state--AfterEvent">
          <a href="https://s.test/websales/pages/ticketsearchcriteria.aspx?evtinfo=1~g&amp;">
            <span class="showtime-ticket__time">1:00pm</span></a>
        </div>'''
        p = IndependentProvider(venues=[{
            "venue_id": "coolidge", "name": "Coolidge", "url": "https://c.test/",
            "tz": "America/New_York"}])
        spec = SearchSpec(work=WorkRef(query="*"),
                          date_window=DateWindow(date(2026, 8, 1), date(2026, 8, 9)))
        assert p.screenings(spec, p.discover(spec), FakeTransport(html)) == []


class TestListingRejectsProse:
    """A link is necessary but not sufficient.

    `<a href="/visit">Box office open 7:00 PM daily</a>` satisfied the
    link rule and was emitted as a screening of whatever film sat above it,
    with /visit as the booking URL.
    """

    def extract(self, html):
        from screenwatch.adapters.generic.listing import extract

        return extract(html, default_date=date(2026, 8, 2))

    @pytest.mark.parametrize("label", ["12:50 PM", "11:00am BUY TICKETS",
                                       "7:35pm Get Tickets", "1:00pm — SOLD OUT"])
    def test_real_showtime_labels_are_accepted(self, label):
        from screenwatch.adapters.generic.listing import is_showtime_label

        assert is_showtime_label(label)

    @pytest.mark.parametrize("label", [
        "Box office open 7:00 PM daily",
        "Doors at 7:00 PM",
        "Join us 7:00 PM for a Q&A with the director",
        "Our cafe closes at 9:00 PM",
    ])
    def test_prose_containing_a_time_is_rejected(self, label):
        from screenwatch.adapters.generic.listing import is_showtime_label

        assert not is_showtime_label(label)

    def test_the_reported_false_positive_no_longer_fires(self):
        html = ('<h3>Jimmy</h3><p>synopsis</p>'
                '<a href="/visit">Box office open 7:00 PM daily</a>')
        assert self.extract(html) == []

    def test_a_genuine_showtime_beside_prose_still_lands(self):
        html = ('<h3>Jimmy</h3>'
                '<a href="/visit">Box office open 7:00 PM daily</a>'
                '<a href="/buy/1">2:50 PM</a>')
        shows = self.extract(html)
        assert len(shows) == 1 and shows[0].url == "/buy/1"


class TestWindowIsAnchoredOnVenueLocalDate:
    """A relative window is measured from the venue's day, not UTC's.

    5pm in San Francisco is already tomorrow in UTC, so a UTC anchor dated
    tonight's undated showings a day forward and a "tonight" search returned
    nothing at exactly the hour someone would run it.
    """

    def test_local_today_uses_the_venues_zone(self):
        from screenwatch.service.venues import local_today

        pacific = local_today("America/Los_Angeles")
        auckland = local_today("Pacific/Auckland")
        # Never more than a day apart, and at some hours genuinely different.
        assert abs((auckland - pacific).days) <= 1

    def test_unknown_zone_falls_back_to_utc_rather_than_raising(self):
        from datetime import datetime, timezone

        from screenwatch.service.venues import local_today

        assert local_today("Mars/Olympus") == datetime.now(timezone.utc).date()
        assert local_today(None) == datetime.now(timezone.utc).date()

    def test_venue_today_matches_its_zone(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from screenwatch.service.venues import Venue

        venue = Venue(venue_id="v", name="V", chain="independent",
                      tz="America/Los_Angeles")
        assert venue.today() == datetime.now(ZoneInfo("America/Los_Angeles")).date()

    def test_provider_dates_undated_showtimes_in_the_venues_day(self, monkeypatch):
        """The end-to-end consequence: an undated listing lands on local today."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from screenwatch.providers.independent import IndependentProvider
        from screenwatch.ranking.spec import SearchSpec, WorkRef

        html = '<h3>Sholay</h3><a href="/buy/1">7:30 PM</a>'
        rows = [{"venue_id": "roxie", "name": "Roxie", "url": "https://x/",
                 "tz": "America/Los_Angeles", "lat": 37.7, "lon": -122.4}]
        provider = IndependentProvider(venues=rows)
        monkeypatch.setattr(provider, "_fetch", lambda row, transport: html)

        spec = SearchSpec(work=WorkRef(query="Sholay"))
        found = provider.screenings(spec, provider.discover(spec), transport=None)

        local_today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        assert found and all(s.starts_at_local.date() == local_today for s in found)


class TestDayHeadingsAreNotFilms:
    """Day headings sit between a film and its links - exactly where a
    proximity search for the title looks.

    Metrograph writes `<h5 class="sr-only">Sun Aug <span>2</span></h5>` before
    each group, so the nearest heading to a showtime link is a date. Once
    Vista stopped keeping its own title lookup and started using the shared
    proximity one, 114 distinct films collapsed to seven dates wearing film
    names.
    """

    def title(self, html):
        from screenwatch.adapters.listing_common import nearest_title_before

        return nearest_title_before(html, len(html))

    def test_a_day_heading_between_film_and_link_is_skipped(self):
        html = '<h3>Good Morning</h3><h5 class="sr-only">Sun Aug 2</h5>'
        assert self.title(html) == "Good Morning"

    def test_a_day_heading_split_by_a_span_is_also_skipped(self):
        """The captured text is "Sun Aug " - a written day missing the part
        that makes it parse as a date, which is what made it pass as a film."""
        html = ('<h3>Good Morning</h3>'
                '<h5 class="sr-only">Sun Aug <span class="day-number">2</span></h5>')
        assert self.title(html) == "Good Morning"

    def test_section_furniture_is_skipped(self):
        html = "<h3>Vertigo</h3><h2>Now Playing</h2>"
        assert self.title(html) == "Vertigo"

    def test_a_real_film_still_wins_on_proximity(self):
        html = "<h1>Metrograph</h1><h3>Vertigo</h3><h3>La Notte</h3>"
        assert self.title(html) == "La Notte"


class TestWrittenDayContainers:
    """`<div id="day_Sun_Aug_2">` - a day grouping that spells the date out.

    Metrograph's only day signal. Unsupported, its 183 showtimes all carried
    the caller's default date: the Aug 8 screenings claimed to be on Aug 2.
    """

    def parse(self, html, default=date(2026, 8, 2)):
        from screenwatch.adapters.listing_common import nearest_date_before

        return nearest_date_before(html, len(html), default)

    def test_a_written_day_in_a_container_id_is_read(self):
        assert self.parse('<div id="day_Sun_Aug_8" class="film_day">') == date(2026, 8, 8)

    def test_the_picker_above_it_does_not_win(self):
        """The chooser is `<li><a data-day="Sat_Aug_8">`; anchors are not day
        groupings, or the last entry in the picker would date the whole page."""
        html = ('<ul class="film_day_chooser">'
                '<li><a data-day="Sun_Aug_2">Sun Aug 2</a></li>'
                '<li><a data-day="Sat_Aug_8">Sat Aug 8</a></li></ul>'
                '<div id="day_Sun_Aug_2" class="film_day">')
        assert self.parse(html) == date(2026, 8, 2)

    def test_a_heading_whose_number_is_in_a_span_still_parses(self):
        html = '<h5 class="sr-only">Sat Aug <span class="day-number">8</span></h5>'
        assert self.parse(html) == date(2026, 8, 8)

    def test_the_closest_signal_wins_regardless_of_style(self):
        html = ('<div id="day_Sun_Aug_2"></div>'
                '<div id="calendar-day-2026-08-09"></div>')
        assert self.parse(html) == date(2026, 8, 9)


class TestVistaAndAgileShareOneImplementation:
    """Both kept private copies of the date, time and title helpers, and the
    copies drifted - Vista's title lookup was still pattern-priority, the bug
    that put one film name on all of Roxie's showtimes."""

    def test_vista_resolves_titles_by_proximity_not_pattern_priority(self):
        from screenwatch.adapters.vista.links import extract

        html = (
            '<h1 class="page-title">Metrograph</h1>'
            "<h3>Vertigo</h3>"
            '<a href="https://t.metrograph.com/Ticketing/visSelectTickets.aspx'
            '?cinemacode=9999&txtSessionId=1">11:00am</a>'
            "<h3>La Notte</h3>"
            '<a href="https://t.metrograph.com/Ticketing/visSelectTickets.aspx'
            '?cinemacode=9999&txtSessionId=2">2:00pm</a>'
        )
        titles = [s.title for s in extract(html, default_date=date(2026, 8, 2))]
        assert titles == ["Vertigo", "La Notte"]

    def test_both_modules_use_the_shared_helpers(self):
        from screenwatch.adapters import listing_common
        from screenwatch.adapters.agile import links as agile
        from screenwatch.adapters.vista import links as vista

        for module in (vista, agile):
            assert module.nearest_date_before is listing_common.nearest_date_before
            assert module.parse_clock is listing_common.parse_clock
