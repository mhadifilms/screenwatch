"""Agile Ticketing links, read from a venue's own site.

Agile WebSales is the other big art-house engine (the Coolidge, and a long
tail of nonprofits and festivals). Its signature is:

    https://{store host}/websales/pages/ticketsearchcriteria.aspx?evtinfo={id}~{guid}

Same idea as the Vista extractor, but Agile's markup differs in three ways
that matter:

* the time is nested inside the anchor (`showtime-ticket__time`), not the
  anchor's direct text - so the innermost text has to be dug out;
* a `sales-state--{State}` class on the wrapper gives real availability,
  which Vista links do not carry;
* a `showtime-ticket__venue` span names the screen, which is worth keeping
  because a rep house's 70mm room is not its digital one.

Rep-house titles carry the format in prose - the Coolidge lists "The Odyssey
in 70mm" - so titles go through the shared presentation classifier, which
already guards the "shot on 35mm" false positive.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime

from ..vista.links import nearest_date_before, parse_clock

# The wrapper is optional. The Coolidge wraps each link in a
# `sales-state--` div with the time in a nested span; IFC Center emits a bare
# anchor whose own text is the time. Requiring the wrapper matched one venue
# and silently missed the other, so the anchor alone is the anchor of the
# pattern and the wrapper is only consulted for its sales state.
AGILE_LINK = re.compile(
    r'<a\s+[^>]*href="(?P<url>https?://(?P<host>[^/"]+)/websales/pages/'
    r'ticketsearchcriteria\.aspx\?evtinfo=(?P<event>[^"&~]+)[^"]*)"[^>]*>'
    r'(?P<rest>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# How far back to look for the wrapper that carries the sales state.
#
# Bounded by the *previous anchor* as well as by this character count, because
# a fixed window alone let state bleed forwards: a showtime with no wrapper of
# its own inherited the state of whatever preceded it, so an available show
# sitting after a sold-out one was reported sold out and never surfaced.
# Suppressing a real seat is the worst failure this module can have.
_WRAPPER_LOOKBACK = 400
_STATE = re.compile(r"sales-state--(\w+)", re.IGNORECASE)
_TIME_SPAN = re.compile(r'showtime-ticket__time[^>]*>\s*([^<]{1,24})', re.IGNORECASE)
_VENUE_SPAN = re.compile(r'showtime-ticket__venue[^>]*>\s*([^<]{1,24})', re.IGNORECASE)
_TITLE = re.compile(
    r'film-card__title[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})', re.IGNORECASE
)
_ANY_TITLE = re.compile(
    r'<(?:h[1-6]|a)[^>]*class="[^"]*title[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})',
    re.IGNORECASE,
)
# Last resort: a bare heading. IFC Center marks films with a plain
# `<h3><a href="/films/jimmy/">Jimmy</a></h3>` and no title class at all, so
# requiring one found 140 valid links and threw every one away.
_ANY_HEADING = re.compile(r"<h[1-6][^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})", re.IGNORECASE)


# Agile's own vocabulary. Anything unrecognised stays unknown rather than
# being guessed at, so a new state cannot silently read as "on sale".
#
# `AfterEvent` and `AfterSalesBeforeEvent` only became visible once the
# state-bleed bug was fixed - the inherited state had been masking them. Both
# mean the showing cannot be bought, which is distinct from sold out: the
# seats may well be empty, they are just no longer for sale.
ON_SALE = {"duringsales", "onsale", "beforesales"}
SOLD_OUT = {"soldout"}
CLOSED = {"afterevent", "aftersalesbeforeevent", "salesclosed", "cancelled"}


@dataclass(frozen=True)
class AgileShowtime:
    event_id: str
    host: str
    url: str
    title: str
    starts_at_local: datetime
    screen: str | None
    sales_state: str

    @property
    def _state(self) -> str:
        return self.sales_state.lower().replace("-", "").replace("_", "")

    @property
    def sold_out(self) -> bool:
        return self._state in SOLD_OUT

    @property
    def on_sale(self) -> bool:
        return self._state in ON_SALE

    @property
    def closed(self) -> bool:
        """Sales have ended, or the screening already happened.

        Not the same as sold out - the room may be half empty - but equally
        not something to offer, so the provider drops these entirely.
        """
        return self._state in CLOSED


def has_agile_links(html: str) -> bool:
    return AGILE_LINK.search(html) is not None


def _nearest_before(pattern: re.Pattern[str], html: str, pos: int) -> str | None:
    best = None
    for m in pattern.finditer(html, 0, pos):
        best = m
    return html_lib.unescape(best.group(1)).strip() if best else None


def extract(html: str, *, default_date: date_cls) -> list[AgileShowtime]:
    out: list[AgileShowtime] = []
    previous_end = 0
    for match in AGILE_LINK.finditer(html):
        # Bookkeeping first, so an anchor that is skipped below still closes
        # the window for the next one - a wrapper before a skipped link
        # belongs to that link, not to whatever follows it.
        window_start = max(previous_end, match.start() - _WRAPPER_LOOKBACK, 0)
        previous_end = match.end()
        inner = match.group("rest")

        # Nested span first (Coolidge), then the anchor's own text (IFC).
        span = _TIME_SPAN.search(inner)
        raw_time = span.group(1) if span else re.sub(r"<[^>]+>", " ", inner)

        on = nearest_date_before(html, match.start(), default_date)
        when = parse_clock(html_lib.unescape(raw_time), on)
        if when is None:
            continue                      # a "more info" link, not a showtime

        title = (
            _nearest_before(_TITLE, html, match.start())
            or _nearest_before(_ANY_TITLE, html, match.start())
            or _nearest_before(_ANY_HEADING, html, match.start())
        )
        if not title:
            continue

        # The sales state lives on a wrapper that may or may not exist, so it
        # is read from the span between the previous showtime link and this
        # one - never further back. Taking the *last* match in that span
        # matters too: `search` is leftmost-first, so with two wrappers in
        # range the farther one used to win.
        state = None
        for candidate in _STATE.finditer(html, window_start, match.start()):
            state = candidate
        venue = _VENUE_SPAN.search(inner)
        out.append(
            AgileShowtime(
                event_id=html_lib.unescape(match.group("event")),
                host=match.group("host"),
                url=html_lib.unescape(match.group("url")).replace("&amp;", "&"),
                title=title,
                starts_at_local=when,
                screen=html_lib.unescape(venue.group(1)).strip() if venue else None,
                sales_state=state.group(1) if state else "unknown",
            )
        )
    return out
