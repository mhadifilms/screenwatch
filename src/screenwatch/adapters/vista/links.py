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
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date as date_cls, datetime, time as time_cls

VISTA_LINK = re.compile(
    r'href="(?P<url>https?://(?P<host>[^/"]+)/Ticketing/visSelectTickets\.aspx'
    r'\?[^"]*cinemacode=(?P<cinema>\d+)[^"]*txtSessionId=(?P<session>\d+)[^"]*)"'
    r'[^>]*>(?P<label>[^<]{1,40})</a>',
    re.I,
)

# `calendar-list-day-2026-08-02`, `day-2026-08-02`, `data-date="2026-08-02"`.
_DATE_ANCHOR = re.compile(
    r'(?:id|data-date|data-vars-ga-label)="[^"]*?(\d{4}-\d{2}-\d{2})[^"]*"'
)
_TITLE = re.compile(
    r'<(?:h[1-6]|a)[^>]*class="[^"]*title[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})',
    re.I,
)
_ANY_HEADING = re.compile(r"<h[1-6][^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})", re.I)
_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.I)


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


def parse_clock(label: str, on: date_cls) -> datetime | None:
    """'11:00am' -> a local datetime on `on`. None if it is not a time."""
    match = _TIME.match(html_lib.unescape(label))
    if not match:
        return None
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "p":
        hour += 12
    return datetime.combine(on, time_cls(hour, minute))


def _nearest_before(pattern: re.Pattern[str], html: str, pos: int) -> str | None:
    best = None
    for m in pattern.finditer(html, 0, pos):
        best = m
    return html_lib.unescape(best.group(1)).strip() if best else None


def extract(html: str, *, default_date: date_cls) -> list[VistaShowtime]:
    """Every Vista showtime link on the page, with its film and time.

    `default_date` is used for links that sit outside any dated container -
    single-day listing pages, which are common.
    """
    out: list[VistaShowtime] = []
    for match in VISTA_LINK.finditer(html):
        label = html_lib.unescape(match.group("label")).strip()

        # A date container before the link wins over the caller's default;
        # multi-day listings group by date this way.
        raw_date = _nearest_before(_DATE_ANCHOR, html, match.start())
        try:
            on = date_cls.fromisoformat(raw_date) if raw_date else default_date
        except ValueError:
            on = default_date

        when = parse_clock(label, on)
        if when is None:
            continue          # a "Buy Tickets" button rather than a time

        title = (
            _nearest_before(_TITLE, html, match.start())
            or _nearest_before(_ANY_HEADING, html, match.start())
        )
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
