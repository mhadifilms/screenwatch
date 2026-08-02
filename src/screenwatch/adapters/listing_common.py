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
from datetime import date as date_cls, datetime, time as time_cls

# ---------------------------------------------------------------- times ---

_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.I)
# The same shape, findable inside longer text. Word-bounded so "1:30pm" in
# "Doors 1:30pm" matches but a version string or price does not.
_TIME_IN_TEXT = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.I)


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
    r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*[,\s]+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:\s*,\s*(\d{4}))?",
    re.I,
)
_WRITTEN_DAY_HEADING = re.compile(
    r"<h[1-6][^>]*>\s*(" + _WRITTEN_DAY.pattern + ")", re.I
)


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


def nearest_date_before(html: str, pos: int, default: date_cls) -> date_cls:
    """The day this position sits under, or `default`.

    An ISO block container wins when it is the closer of the two signals;
    otherwise a written day heading is used.
    """
    iso = text = None
    for m in DATE_CONTAINER.finditer(html, 0, pos):
        iso = m
    for m in _WRITTEN_DAY_HEADING.finditer(html, 0, pos):
        text = m

    if iso is not None and (text is None or text.start() < iso.start()):
        try:
            return date_cls.fromisoformat(iso.group(1))
        except ValueError:
            return default
    if text is not None:
        return parse_written_date(text.group(1), default) or default
    return default


# --------------------------------------------------------------- titles ---

TITLE_CLASSED = re.compile(
    r'<(?:h[1-6]|a)[^>]*class="[^"]*title[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})',
    re.I,
)
ANY_HEADING = re.compile(r"<h[1-6][^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,120})", re.I)


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

    Specificity only breaks ties at the same position.
    """
    best_pos, best_text = -1, None
    for rank, pattern in enumerate((*extra, TITLE_CLASSED, ANY_HEADING)):
        for m in pattern.finditer(html, 0, pos):
            if m.start() >= best_pos:
                best_pos, best_text = m.start(), html_lib.unescape(m.group(1)).strip()
    return best_text


def strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment or "")).strip()
