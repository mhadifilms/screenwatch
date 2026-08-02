"""Shared parsing for cinema listing pages.

Three extractors now read venue listings - Vista links, Agile links, and the
generic own-site one - and they were duplicating the same three questions:
what time is this, what film is it, and what day is it under. Those live here.

The date question is the awkward one. Venues express the day in at least four
incompatible ways, and every listing page also renders a date *picker* whose
entries are indistinguishable from day headings unless you look at what kind
of element they sit on.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import date as date_cls
from datetime import datetime
from datetime import time as time_cls

# ---------------------------------------------------------------- times ---

_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.IGNORECASE)
# The same shape, findable inside longer text. Word-bounded so "1:30pm" in
# "Doors 1:30pm" matches but a version string or price does not.
_TIME_IN_TEXT = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.IGNORECASE)


def _build(match, on: date_cls) -> datetime:
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "p":
        hour += 12
    return datetime.combine(on, time_cls(hour, minute))


def parse_clock(label: str, on: date_cls) -> datetime | None:
    """Strict: the whole label must be a time. '11:00am' yes, 'Buy' no."""
    match = _TIME.match(html_lib.unescape(label or ""))
    return _build(match, on) if match else None


def find_clock(text: str, on: date_cls) -> datetime | None:
    """Lenient: the first time *inside* the text.

    Some venues put more than the time in the link - Music Box's reads
    "11:00am BUY TICKETS", which strict matching rejected, silently dropping
    all twelve of its showtimes. Only used where the element is already known
    to be a showtime link, so the looseness cannot pull in page copy.
    """
    match = _TIME_IN_TEXT.search(html_lib.unescape(text or ""))
    return _build(match, on) if match else None


# ---------------------------------------------------------------- dates ---

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}

# A date that *groups* showtimes, e.g. `<div id="calendar-list-day-2026-08-02">`.
#
# Restricted to block containers on purpose. Every listing page also carries a
# date picker whose entries look identical in isolation - Metrograph's is
# `<a id="day-selector-day-2026-08-02">`, the Coolidge's is
# `<td id="showtimes_calendar-2026-09-05">`. Without this filter the nearest
# preceding date was a picker entry, which dated the Coolidge's whole schedule
# to September. Day groupings are block elements; navigation is anchors and
# table cells.
DATE_CONTAINER = re.compile(
    r"<(?:div|section|article|li|ul|main)\b[^>]*"
    r'(?:id|data-date|data-day|data-vars-ga-label)="[^"]*?'
    r'(\d{4}-\d{2}-\d{2})[^"]*"'
)

# "Sun Aug 2", "Sunday, August 2, 2026", "Sunday, Aug 2" - written days, with
# or without a year. Used as a heading and inline (accessibility spans).
_WRITTEN_DAY = re.compile(
    r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*[,\s_-]+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s_-]+"
    r"(\d{1,2})(?:\s*,\s*(\d{4}))?",
    re.IGNORECASE,
)

# A day grouping whose attribute spells the day out rather than in ISO -
# Metrograph's is `<div id="day_Sun_Aug_2" class="film_day">`. Same role as
# `DATE_CONTAINER`, same block-element restriction and for the same reason:
# the picker right above it is `<li><a data-day="Sun_Aug_2">`, and taking that
# would pick whichever day the picker happens to list last.
#
# Without this, Metrograph's 183 showtimes all carried the caller's default
# date - its Aug 8 screenings claimed to be on Aug 2.
WRITTEN_CONTAINER = re.compile(
    r"<(?:div|section|article|main)\b[^>]*"
    r'(?:id|data-date|data-day)="[^"]*?'
    r"((?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*[_\s-]"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[_\s-]\d{1,2})",
    re.IGNORECASE,
)

# A heading naming the day. Inner markup is tolerated and stripped before
# parsing, because venues break the number out for styling - Metrograph's
# screen-reader heading is `<h5>Sun Aug <span class="day-number">2</span></h5>`,
# which a contiguous-text pattern reads as "Sun Aug" and discards.
_HEADING = re.compile(r"<h[1-6][^>]*>(.{0,120}?)</h[1-6]>", re.IGNORECASE | re.DOTALL)


def parse_written_date(text: str, default: date_cls) -> date_cls | None:
    """'Sunday, Aug 2' or 'Sunday, August 2, 2026' -> a date.

    A day without a year takes the default's year, then rolls forward if that
    would place it far in the past - a January listing read in late December
    is next month, not eleven months ago.
    """
    match = _WRITTEN_DAY.search(html_lib.unescape(text or ""))
    if not match:
        return None
    month = _MONTHS[match.group(1).lower()[:3]]
    day = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else default.year
    try:
        candidate = date_cls(year, month, day)
    except ValueError:
        return None
    if match.group(3) is None and (default - candidate).days > 300:
        candidate = candidate.replace(year=year + 1)
    return candidate


def _iso_or_none(raw: str) -> date_cls | None:
    try:
        return date_cls.fromisoformat(raw)
    except ValueError:
        return None


def nearest_date_before(html: str, pos: int, default: date_cls) -> date_cls:
    """The day this position sits under, or `default`.

    Three signals, all meaning the same thing and none universal: an ISO date
    on a block container, a written day on a block container, and a heading
    naming the day. The closest one above `pos` wins, because "the day this
    showtime is listed under" is a question about proximity, not about which
    markup style the venue chose. An ISO date breaks a tie, being the only one
    that cannot be ambiguous about the year.
    """
    best_pos, best_date = -1, None
    for pattern, convert in (
        (DATE_CONTAINER, _iso_or_none),
        (WRITTEN_CONTAINER, lambda raw: parse_written_date(raw, default)),
        (_HEADING, lambda raw: parse_written_date(strip_tags(raw), default)),
    ):
        for m in pattern.finditer(html, 0, pos):
            if m.start() < best_pos:
                continue
            when = convert(m.group(1))
            if when is None:
                continue
            # `>` not `>=`: ties go to the pattern listed first, and ISO is
            # listed first precisely so it wins them.
            if m.start() > best_pos:
                best_pos, best_date = m.start(), when
    return best_date if best_date is not None else default


# --------------------------------------------------------------- titles ---

TITLE_CLASSED = re.compile(
    r'<(?:h[1-6]|a)[^>]*class="[^"]*title[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})',
    re.IGNORECASE,
)
ANY_HEADING = re.compile(r"<h[1-6][^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})", re.IGNORECASE)


# Headings that group or label showtimes rather than name a film. A listing
# page is full of them, and they sit *between* the film title and its links -
# which is exactly where a proximity search looks.
NOT_A_FILM = re.compile(
    r"^(now playing|coming soon|showtimes?|this week|next week|box.?office|"
    r"theatre|theater|tickets?|calendar|schedule|events?|menu|home|"
    r"today|tomorrow|series|our )\b",
    re.IGNORECASE,
)


# A day heading whose number was broken out for styling. Metrograph writes
# `<h5>Sun Aug <span class="day-number">2</span></h5>`, so the heading pattern
# captures "Sun Aug " - a written day missing the one part that makes it parse
# as a date, which is exactly the fragment that then poses as a film title.
#
# Matching a bare weekday too ("Sunday") costs the occasional real title -
# *Friday* (1995) - but a one-word weekday heading in a listing is a day
# grouping far more often than it is a film, and the cost of being wrong the
# other way is every showtime under it losing its film.
_DAY_LABEL = re.compile(
    r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*"
    r"(?:[,\s]*$|[,\s]+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))",
    re.IGNORECASE,
)


def is_film_title(text: str) -> bool:
    """Could this heading be a film, or is it page furniture?

    A day heading is the case that matters. Metrograph marks each group with
    `<h5 class="sr-only">Sun Aug 2</h5>`, which sits between the film title
    and its showtime links; treating it as the nearest title collapsed 114
    distinct films to seven dates wearing film names.
    """
    text = (text or "").strip()
    if len(text) < 2 or NOT_A_FILM.match(text) or _DAY_LABEL.match(text):
        return False
    return _WRITTEN_DAY.search(text) is None


def nearest_match_before(pattern: re.Pattern[str], html: str, pos: int) -> str | None:
    best = None
    for m in pattern.finditer(html, 0, pos):
        best = m
    return html_lib.unescape(best.group(1)).strip() if best else None


def nearest_title_before(html: str, pos: int, *extra: re.Pattern[str]) -> str | None:
    """The film title closest above `pos`.

    Proximity decides, not pattern order. Trying each pattern in turn and
    returning the first that matched *anywhere* meant one `title`-classed hero
    banner near the top of the page won over every nearer heading - Roxie came
    back with the same film name on all nineteen of its showtimes.

    Specificity only breaks ties at the same position - which is what the
    `extra` patterns are for, being listed first.

    Page furniture is skipped rather than returned: day headings in
    particular sit between a film and its showtime links, so the *nearest*
    heading is very often not a film at all.
    """
    best_pos, best_text = -1, None
    for pattern in (*extra, TITLE_CLASSED, ANY_HEADING):
        for m in pattern.finditer(html, 0, pos):
            # `>` not `>=`: at the same position the earlier - more specific -
            # pattern keeps the tie, which is why `extra` is listed first.
            if m.start() <= best_pos:
                continue
            text = html_lib.unescape(m.group(1)).strip()
            if not is_film_title(text):
                continue
            best_pos, best_text = m.start(), text
    return best_text


def strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment or "")).strip()
