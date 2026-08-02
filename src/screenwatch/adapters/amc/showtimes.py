"""AMC showtimes, parsed two independent ways from one fetch.

The showtimes route renders both server HTML and an RSC flight payload
carrying the same screenings. Parsing both costs one request and catches the
failure that matters most: a parser that still returns plausible-looking rows
after the site changed underneath it.

Screening identity comes from the `aria-describedby` chain AMC emits for
accessibility, which is a happy accident of a read surface - structured,
nested, and legally load-bearing, so it changes far less often than markup:

    the-odyssey-76238
    the-odyssey-76238-amc-lincoln-square-13
    the-odyssey-76238-amc-lincoln-square-13-imax70mm
    the-odyssey-76238-amc-lincoln-square-13-imax70mm-0
    the-odyssey-76238-amc-lincoln-square-13-imax70mm-0-attributes

Token 0 gives movie slug + AMC movie id, token 1 adds the venue slug, and
token 2's remainder is the format token. Deriving the format by subtracting
token 1 from token 2 avoids guessing where the venue slug ends - venue slugs
contain digits and hyphens, so any split heuristic would be wrong.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ... import presentation as pres
from ...models import Availability, FactKey, Observation, Presentation
from ...rsc import extract_flight
from ...identity.normalize import from_slug as title_from_slug
from ...transport import Transport
from ..base import ParseError

BASE = "https://www.amctheatres.com"

_STATUS = {
    "sellable": Availability.SELLABLE,
    "almostfull": Availability.ALMOST_FULL,
    "sellingfast": Availability.ALMOST_FULL,
    "soldout": Availability.SOLD_OUT,
    "notsellable": Availability.SOLD_OUT,
}

# RSC: a showtime object immediately followed by its aria chain.
_RSC_SHOWTIME = re.compile(
    r'"showtime":(\{"showtimeId":\d+.*?\}),"aria-describedby":"([^"]+)"', re.S
)
_SHOWTIME_JSON = re.compile(
    r'"showtimeId":(\d+).*?"status":"([^"]+)".*?"showDateTimeUtc":"([^"]+)"', re.S
)

# A showtime renders one of three ways, and the id hides somewhere different
# in each:
#   sellable, plain      <a id="N" href="/showtimes/N"><time>..</time> </a>
#   sellable, annotated  <a id="N" ...><time/><span class="sr-only">..</span></a>
#                        <div id="N-details">
#   sold out             <button disabled><time/><span class="sr-only">Sold Out</span>
#                        </button><div id="N-details">
# The disabled button carries no id and no aria chain at all, so both the
# leading id and the trailing details div are optional - but at least one must
# appear. Anchoring on href instead would silently drop every sold-out
# screening, which is exactly the set you watch for returned seats.
_DOM_GROUP = re.compile(r'id="([a-z0-9][a-z0-9\-]*?)-(\d+)-attributes"')
_DOM_TIME = re.compile(
    r'(?:\bid="(\d+)"[^>]*)?>\s*<time dateTime="([^"]+)">.*?</time>'
    r'\s*(?:<span class="sr-only">([^<]*)</span>)?\s*'
    r"</(?:a|button)>"
    r'(?:\s*<div id="(\d+)-details")?',
    re.S,
)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _classify(token: str, venue_id: str, *, strict: bool) -> Presentation:
    try:
        return pres.classify_token("amc", token, venue_id=venue_id)
    except pres.UnknownFormatError:
        if strict:
            raise
        # Runtime must survive one novel token; CI must not. Substring probing
        # reads concatenated chain tokens that prose patterns cannot.
        return pres.refine(pres.classify_token_fuzzy(token), venue_id)


def _split_aria(aria: str) -> tuple[str, str, str, str]:
    """(movie_id, venue_id, format_token, movie_slug) from the aria chain.

    The slug matters as much as the id: AMC's numeric ids are product ids, and
    identity resolution needs a *title* to work with. `the-odyssey-76238`
    carries both, and without the slug every screening resolves to the string
    "76238" and matches no search query at all.
    """
    tokens = aria.split()
    if len(tokens) < 3:
        raise ParseError(f"aria chain too short to identify a screening: {aria!r}")

    movie_ref, venue_ref, format_ref = tokens[0], tokens[1], tokens[2]

    if "-" not in movie_ref:
        raise ParseError(f"no movie id in aria token {movie_ref!r}")
    movie_slug, movie_id = movie_ref.rsplit("-", 1)

    if not venue_ref.startswith(movie_ref + "-"):
        raise ParseError(f"aria token 1 {venue_ref!r} does not extend {movie_ref!r}")
    venue_id = venue_ref[len(movie_ref) + 1 :]

    if not format_ref.startswith(venue_ref + "-"):
        raise ParseError(f"aria token 2 {format_ref!r} does not extend {venue_ref!r}")
    return movie_id, venue_id, format_ref[len(venue_ref) + 1 :], movie_slug


class AmcShowtimesRsc:
    """Tier 1: the flight payload. Richest and least presentational."""

    chain = "amc"
    source = "amc:showtimes-rsc"
    tier = 1

    def fetch(self, transport: Transport, *, venue_id: str, market: str, date: str,
              format_filter: str = "all") -> str:
        url = (
            f"{BASE}/movie-theatres/{market}/{venue_id}"
            f"/showtimes/all/{date}/{venue_id}/{format_filter}"
        )
        return transport.get(url).text

    def parse(self, raw: str, *, observed_at: datetime | None = None,
              strict: bool = True) -> list[Observation]:
        observed_at = observed_at or datetime.now(timezone.utc)
        payload = extract_flight(raw)

        out: list[Observation] = []
        for m in _RSC_SHOWTIME.finditer(payload):
            blob, aria = m.group(1), m.group(2)
            fields = _SHOWTIME_JSON.search(blob)
            if not fields:
                raise ParseError(f"showtime object missing expected fields: {blob[:160]}")
            showtime_id, status, when = fields.groups()
            movie_id, venue_id, token, slug = _split_aria(aria)

            out.append(
                Observation(
                    key=FactKey(venue_id, movie_id, _parse_utc(when)),
                    source=self.source, tier=self.tier, observed_at=observed_at,
                    presentation=_classify(token, venue_id, strict=strict),
                    availability=_STATUS.get(
                        pres.normalize_token(status), Availability.UNKNOWN
                    ),
                    title=title_from_slug(slug),
                    external_id=showtime_id,
                    deeplink=f"{BASE}/showtimes/{showtime_id}",
                )
            )

        if not out:
            raise ParseError(
                "RSC payload contained no showtimes - either the shape changed or "
                "this is not a showtimes page. Refusing to report an empty day."
            )
        return out


class AmcShowtimesDom:
    """Tier 1: the rendered markup. Same fetch, independent parse path."""

    chain = "amc"
    source = "amc:showtimes-dom"
    tier = 1

    def fetch(self, transport: Transport, **ctx) -> str:
        return AmcShowtimesRsc().fetch(transport, **ctx)

    def parse(self, raw: str, *, venue_id: str | None = None,
              observed_at: datetime | None = None,
              strict: bool = True) -> list[Observation]:
        observed_at = observed_at or datetime.now(timezone.utc)
        venue_id = venue_id or self._infer_venue_id(raw)

        groups = [(m.start(), m.group(1)) for m in _DOM_GROUP.finditer(raw)]
        if not groups:
            raise ParseError(
                "no format groups in rendered DOM - selector drift or a "
                "challenge page. Refusing to report an empty day."
            )

        out: list[Observation] = []
        for m in _DOM_TIME.finditer(raw):
            anchor_id, when, sr_text, details_id = m.groups()
            showtime_id = anchor_id or details_id
            if showtime_id is None:
                raise ParseError(
                    f"showtime at {when} carries no id in either position - "
                    "the showtime markup changed"
                )
            if anchor_id and details_id and anchor_id != details_id:
                raise ParseError(
                    f"showtime id disagreement: anchor {anchor_id} vs details {details_id}"
                )

            slug = self._group_for(groups, m.start())
            if slug is None:
                raise ParseError(f"showtime {showtime_id} precedes any format group")
            movie_id, token, movie_slug = self._split_group_slug(slug, venue_id)

            low = (sr_text or "").lower()
            if "sold out" in low:
                availability = Availability.SOLD_OUT
            elif "almost full" in low:
                availability = Availability.ALMOST_FULL
            else:
                availability = Availability.SELLABLE

            out.append(
                Observation(
                    key=FactKey(venue_id, movie_id, _parse_utc(when)),
                    source=self.source, tier=self.tier, observed_at=observed_at,
                    presentation=_classify(token, venue_id, strict=strict),
                    availability=availability,
                    title=title_from_slug(movie_slug),
                    external_id=showtime_id,
                    deeplink=f"{BASE}/showtimes/{showtime_id}",
                )
            )

        if not out:
            raise ParseError(
                "format groups present but no showtimes parsed - the showtime "
                "markup changed. Refusing to report an empty day."
            )
        return out

    @staticmethod
    def _group_for(groups: list[tuple[int, str]], pos: int) -> str | None:
        best = None
        for start, slug in groups:
            if start > pos:
                break
            best = slug
        return best

    @staticmethod
    def _split_group_slug(slug: str, venue_id: str) -> tuple[str, str, str]:
        """`{movie-slug}-{movie-id}-{venue-id}-{format}` -> (id, format, slug).

        Split on the known venue id rather than guessing, because both movie
        slugs and venue slugs contain hyphens and trailing digits.
        """
        marker = f"-{venue_id}-"
        if marker not in slug:
            raise ParseError(f"group slug {slug!r} does not contain venue {venue_id!r}")
        movie_ref, _, token = slug.partition(marker)
        if "-" not in movie_ref:
            raise ParseError(f"no movie id in group slug {slug!r}")
        movie_slug, movie_id = movie_ref.rsplit("-", 1)
        return movie_id, token, movie_slug

    @staticmethod
    def _infer_venue_id(raw: str) -> str:
        """Recover the venue id from any aria chain on the page.

        Sellable showtimes still carry the full chain, and a page with zero
        sellable showtimes is one where we would rather fail loudly.
        """
        m = re.search(r'aria-describedby="([^"\s]+) ([^"\s]+) [^"]*"', raw)
        if not m:
            raise ParseError(
                "cannot infer venue_id - no aria chains present. Pass venue_id= "
                "explicitly when parsing a fully sold-out page."
            )
        movie_ref, venue_ref = m.group(1), m.group(2)
        if not venue_ref.startswith(movie_ref + "-"):
            raise ParseError(f"aria token {venue_ref!r} does not extend {movie_ref!r}")
        return venue_ref[len(movie_ref) + 1 :]
