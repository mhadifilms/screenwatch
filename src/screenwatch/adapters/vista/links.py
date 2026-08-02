"""Vista ticket links, read from a venue's own site.

Vista is the ticketing engine behind an enormous share of cinemas - Regal,
Metrograph, and a long tail of art houses - and its web ticketing has a
signature URL:

    https://{host}/Ticketing/visSelectTickets.aspx?cinemacode={code}&txtSessionId={id}

Venues embed those links directly in their showtime listings, next to the
displayed time. That makes them extractable *generically*: no per-venue
parser, just the link plus the two things adjacent to it - the anchor's own
text (the showtime) and the nearest preceding film title.

This is deliberately structural rather than CSS-based. Class names differ
between venues and change on redesigns; "the anchor text of a Vista link is
the showtime, and the nearest heading before it is the film" holds across
sites because it is how listings are *shaped*, not how they are styled.

The date comes from a container id or a `data-` attribute where one exists,
falling back to a date supplied by the caller. Times are local wall clock -
Vista links carry no timezone, so the venue's zone is applied upstream.

Dates, times and titles all come from `listing_common`. They used to be
duplicated here, and the copies drifted: this one still resolved titles by
pattern priority rather than proximity - the bug that once put the same film
name on all of Roxie's showtimes - and knew only two of the three ways a venue
spells out the day.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date as date_cls, datetime

from ..listing_common import (
    DATE_CONTAINER,
    nearest_date_before,
    nearest_title_before,
    parse_clock,
)

__all__ = [
    "DATE_CONTAINER",
    "VISTA_LINK",
    "VistaParseError",
    "VistaShowtime",
    "extract",
    "has_vista_links",
    "nearest_date_before",
    "parse_clock",
]

VISTA_LINK = re.compile(
    r'href="(?P<url>https?://(?P<host>[^/"]+)/Ticketing/visSelectTickets\.aspx'
    r'\?[^"]*cinemacode=(?P<cinema>\d+)[^"]*txtSessionId=(?P<session>\d+)[^"]*)"'
    r'[^>]*>(?P<label>[^<]{1,40})</a>',
    re.IGNORECASE,
)

class VistaParseError(ValueError):
    pass


@dataclass(frozen=True)
class VistaShowtime:
    cinema_code: str
    session_id: str
    host: str
    url: str
    title: str
    starts_at_local: datetime
    label: str

    @property
    def screening_key(self) -> str:
        return f"{self.cinema_code}-{self.session_id}"


def extract(html: str, *, default_date: date_cls) -> list[VistaShowtime]:
    """Every Vista showtime link on the page, with its film and time.

    `default_date` is used for links that sit outside any dated container -
    single-day listing pages, which are common.
    """
    out: list[VistaShowtime] = []
    for match in VISTA_LINK.finditer(html):
        label = html_lib.unescape(match.group("label")).strip()

        # A day container around the link wins over the caller's default;
        # multi-day listings group by date this way.
        on = nearest_date_before(html, match.start(), default_date)

        when = parse_clock(label, on)
        if when is None:
            continue          # a "Buy Tickets" button rather than a time

        title = nearest_title_before(html, match.start())
        if not title:
            continue

        out.append(
            VistaShowtime(
                cinema_code=match.group("cinema"),
                session_id=match.group("session"),
                host=match.group("host"),
                url=html_lib.unescape(match.group("url")),
                title=title,
                starts_at_local=when,
                label=label,
            )
        )
    return out


def has_vista_links(html: str) -> bool:
    return VISTA_LINK.search(html) is not None
