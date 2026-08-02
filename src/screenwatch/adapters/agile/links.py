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
from datetime import date as date_cls, datetime

from ..vista.links import nearest_date_before, parse_clock

AGILE_LINK = re.compile(
    r'<div[^>]*class="[^"]*(?:agiletix|sales-state--)[^"]*"[^>]*>\s*'
    r'<a\s+href="(?P<url>https?://(?P<host>[^/"]+)/websales/pages/'
    r'ticketsearchcriteria\.aspx\?evtinfo=(?P<event>[^"&~]+)[^"]*)"'
    r'(?P<rest>.*?)</a>',
    re.I | re.S,
)
_STATE = re.compile(r"sales-state--(\w+)", re.I)
_TIME_SPAN = re.compile(r'showtime-ticket__time[^>]*>\s*([^<]{1,24})', re.I)
_VENUE_SPAN = re.compile(r'showtime-ticket__venue[^>]*>\s*([^<]{1,24})', re.I)
_TITLE = re.compile(
    r'film-card__title[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})', re.I
)
_ANY_TITLE = re.compile(
    r'<(?:h[1-6]|a)[^>]*class="[^"]*title[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})',
    re.I,
)


# Agile's own vocabulary. Anything unrecognised is treated as unknown rather
# than guessed at, so a new state cannot silently read as "on sale".
ON_SALE = {"duringsales", "onsale"}
SOLD_OUT = {"soldout", "sold_out"}


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
    def sold_out(self) -> bool:
        return self.sales_state.lower().replace("-", "") in SOLD_OUT

    @property
    def on_sale(self) -> bool:
        return self.sales_state.lower().replace("-", "") in ON_SALE


def has_agile_links(html: str) -> bool:
    return AGILE_LINK.search(html) is not None


def _nearest_before(pattern: re.Pattern[str], html: str, pos: int) -> str | None:
    best = None
    for m in pattern.finditer(html, 0, pos):
        best = m
    return html_lib.unescape(best.group(1)).strip() if best else None


def extract(html: str, *, default_date: date_cls) -> list[AgileShowtime]:
    out: list[AgileShowtime] = []
    for match in AGILE_LINK.finditer(html):
        inner = match.group("rest")

        time_text = _TIME_SPAN.search(inner)
        if not time_text:
            continue                      # a "more info" link, not a showtime

        on = nearest_date_before(html, match.start(), default_date)

        when = parse_clock(html_lib.unescape(time_text.group(1)), on)
        if when is None:
            continue

        title = (
            _nearest_before(_TITLE, html, match.start())
            or _nearest_before(_ANY_TITLE, html, match.start())
        )
        if not title:
            continue

        state = _STATE.search(match.group(0))
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
