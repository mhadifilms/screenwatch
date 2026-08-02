"""Own-site showtime listings — the venues on no shared ticketing platform.

Vista and Agile cover most art houses, but some sell through their own site
and there is no platform signature to key on. What they still share is a
shape: **a link whose text is a time**. Roxie wraps "12:50 PM" in an anchor to
its film page; Music Box wraps "11:00am" in an anchor to
`/order/add-tickets/{id}`.

Two conditions keep this safe, and the first alone is not enough. The time
must be **a link**, because page copy is full of times and matching bare text
would drag all of it in. But links carry prose too: `<a href="/visit">Box
office open 7:00 PM daily</a>` satisfied the link rule and was emitted as a
screening of whatever film happened to sit above it, with `/visit` as the
booking URL. So the link's text must also be *essentially just the time* -
`ACCEPTABLE_EXTRAS` allows the call-to-action words that really do appear
beside one ("11:00am BUY TICKETS") and nothing else.

Dates come from whichever signal the venue offers. Music Box puts the day in
a `visually-hidden` span inside the anchor, for screen readers; that is the
most reliable source on the page, for the same reason AMC's aria chains are.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from urllib.parse import urljoin

from ..listing_common import (
    find_clock,
    nearest_date_before,
    nearest_title_before,
    parse_written_date,
    strip_tags,
)

# Anchors, with their inner markup kept so an accessibility date span inside
# can be read before it is stripped.
_ANCHOR = re.compile(r'<a\b[^>]*href="(?P<href>[^"#][^"]*)"[^>]*>(?P<inner>.{0,300}?)</a>',
                     re.IGNORECASE | re.DOTALL)
# Matched with its closing tag so the whole element can be removed. Capturing
# only the opening tag left the date text behind, and " Sunday, Aug 2 11:00am"
# does not parse as a time - Music Box's twelve showtimes all silently failed.
_HIDDEN_DATE = re.compile(
    r'<(?P<tag>[a-z]+)[^>]*class="[^"]*(?:visually-hidden|sr-only|screen-reader)'
    r'[^"]*"[^>]*>(?P<text>[^<]{4,40})</(?P=tag)>',
    re.IGNORECASE,
)
# Section furniture that a bare-heading fallback would otherwise pick up.
_NOT_A_FILM = re.compile(
    r"^(now playing|coming soon|showtimes?|this week|box.?office|theatre|theater|"
    r"tickets?|calendar|schedule|events?|menu|home)\b",
    re.IGNORECASE,
)

# What may legitimately sit beside a time inside a showtime link. Anything
# else means the anchor is prose, not a showtime.
#
# Checked against the *residue* after the time is removed, so the test is
# "what is left over", not "does this look like a sentence". A whitelist beats
# a stopword list here: the failure mode to prevent is unknown prose slipping
# through, and only a closed set gives that.
ACCEPTABLE_EXTRAS = re.compile(
    r"^(?:buy|get|book|reserve|purchase|select|tickets?|seats?|showtime|now|"
    r"available|sold\s*out|matinee|am|pm|\W)*$",
    re.IGNORECASE,
)
_TIME_TOKEN = re.compile(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b", re.IGNORECASE)


def is_showtime_label(label: str) -> bool:
    """Is this link's text a showtime rather than a sentence containing one?"""
    residue = _TIME_TOKEN.sub(" ", label or "", count=1)
    return bool(ACCEPTABLE_EXTRAS.match(residue.strip()))


@dataclass(frozen=True)
class ListingShowtime:
    url: str
    title: str
    starts_at_local: datetime
    label: str

    @property
    def key(self) -> str:
        return f"{self.title}|{self.starts_at_local.isoformat()}"


def extract(html: str, *, default_date: date_cls, base_url: str = "") -> list[ListingShowtime]:
    """Every clickable showtime on a venue's own listing page."""
    out: list[ListingShowtime] = []
    seen: set[str] = set()

    for match in _ANCHOR.finditer(html):
        inner = match.group("inner")

        # An accessibility span inside the anchor beats any container guess:
        # it is written for a screen reader, so it is unambiguous by design.
        hidden = _HIDDEN_DATE.search(inner)
        on = (
            (hidden and parse_written_date(hidden.group("text"), default_date))
            or nearest_date_before(html, match.start(), default_date)
        )
        label = strip_tags(_HIDDEN_DATE.sub(" ", inner))

        when = find_clock(label, on)
        if when is None:
            continue
        if not is_showtime_label(label):
            continue          # prose that happens to contain a time

        title = nearest_title_before(html, match.start())
        if not title or _NOT_A_FILM.match(title):
            continue

        url = html_lib.unescape(match.group("href"))
        if base_url:
            url = urljoin(base_url, url)

        show = ListingShowtime(url=url, title=title, starts_at_local=when, label=label)
        if show.key in seen:
            continue          # the same showing linked twice, e.g. poster + time
        seen.add(show.key)
        out.append(show)

    return out


def has_listing_showtimes(html: str, *, default_date: date_cls) -> bool:
    return bool(extract(html, default_date=default_date))
