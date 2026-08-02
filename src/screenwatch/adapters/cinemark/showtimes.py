"""Cinemark, from the rendered theatre page.

Not a SPA - a classic ASP.NET/Umbraco site - so there is no hydration blob to
read. What it does have is a hybrid worth exploiting:

* `data-json-model` attributes carry a JSON record per movie block, with the
  Cinemark movie id, title, and *runtime* - the last of which is exactly the
  tiebreaker the identity resolver wants when two films share a title.
* The showtimes themselves render as `<div class="showtime"
  data-print-type-name="...">` wrapping an anchor whose query string holds
  TheaterId, ShowtimeId and the local start time.

Two site-specific facts that shape the adapter:

* `?showDate=YYYY-MM-DD` is required. Without it the page returns a handful of
  "starting soon" times (6 in testing) instead of the full day (75).
* Format arrives as an English *phrase*, not a token: "Luxury Lounger RealD
  3D", "Standard Format Luxury Lounger". So it is phrase-matched, longest
  first, and every phrase's attributes union together.

**robots.txt forbids `/TicketSeatMap`**, which is where each showtime's link
points. That URL is therefore surfaced as a booking deeplink for the user to
click and never fetched by this code - see `screenwatch.robots`.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from ...models import Attribute, Brand, Presentation, Projection

BASE = "https://www.cinemark.com"
SITEMAP = BASE + "/sitemap.xml"
THEATRE = BASE + "/theatres/{slug}?showDate={date}"

_CHALLENGE = "Just a moment"

_JSON_MODEL = re.compile(r'data-json-model="([^"]{20,})"')
_SHOWTIME = re.compile(
    r'<div class="showtime"[^>]*data-print-type-name="([^"]*)"[^>]*>\s*'
    r'<a[^>]*href="(/TicketSeatMap/\?[^"]+)"',
    re.DOTALL,
)
# Coordinates only appear inside the static-map image URL - a Bing
# virtualearth tile, not a Google maps link, which the first attempt assumed.
_MAPS_COORDS = re.compile(
    r"(?:virtualearth|maps)[^\"']{0,200}?/(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)"
)
_RUNTIME = re.compile(r"(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?", re.IGNORECASE)

# Longest first so "RealD 3D" wins over "3D" and "Cinemark XD" over "XD".
_PHRASES: list[tuple[str, Presentation]] = [
    ("cinemark xd", Presentation(Projection.DIGITAL, Brand.PLF)),
    ("imax with laser", Presentation(Projection.DIGITAL_LASER, Brand.IMAX)),
    ("dolby cinema", Presentation(Projection.DIGITAL_LASER, Brand.DOLBY_CINEMA)),
    ("standard format", Presentation(Projection.DIGITAL)),
    ("luxury lounger", Presentation(attrs=frozenset({Attribute.RECLINERS}))),
    ("closed caption", Presentation(attrs=frozenset({Attribute.CLOSED_CAPTION}))),
    ("open caption", Presentation(attrs=frozenset({Attribute.OPEN_CAPTION}))),
    ("descriptive narration", Presentation(attrs=frozenset({Attribute.AUDIO_DESCRIPTION}))),
    ("reald 3d", Presentation(attrs=frozenset({Attribute.THREE_D}))),
    ("screenx", Presentation(Projection.DIGITAL, Brand.SCREENX)),
    ("d-box", Presentation(Projection.DIGITAL, Brand.DBOX)),
    ("dbox", Presentation(Projection.DIGITAL, Brand.DBOX)),
    ("4dx", Presentation(Projection.DIGITAL, Brand.FOURDX)),
    ("imax", Presentation(Projection.DIGITAL, Brand.IMAX)),
    ("70mm", Presentation(Projection.FILM_70MM)),
    ("35mm", Presentation(Projection.FILM_35MM)),
    ("atmos", Presentation(attrs=frozenset({Attribute.ATMOS}))),
    ("subtitled", Presentation(attrs=frozenset({Attribute.SUBTITLED}))),
    ("3d", Presentation(attrs=frozenset({Attribute.THREE_D}))),
    ("xd", Presentation(Projection.DIGITAL, Brand.PLF)),
]

# Phrases that appear in the same attribute string but say nothing about the
# presentation. Listed so a genuinely new phrase is detectable.
_IGNORED_PHRASES = (
    "no passes", "assisted listening device", "assisted listening",
    "reserved seating", "recliner", "matinee",
    # The chain's own name is a prefix on several brands ("Cinemark XD"); left
    # in place it reports as an unknown word every time a phrase does not match.
    "cinemark",
)


# Cinemark gives a local wall-clock time and no timezone. The theatre slug is
# prefixed with the state ("tx-san-antonio/..."), which is enough to place all
# but a handful of theatres correctly. States that genuinely straddle a zone
# boundary resolve to their most populous one; the error is bounded at one
# hour and only affects cross-chain tie-ordering, never the local-time
# reasoning that ranking actually uses.
STATE_TZ = {
    "ak": "America/Anchorage", "al": "America/Chicago", "ar": "America/Chicago",
    "az": "America/Phoenix", "ca": "America/Los_Angeles", "co": "America/Denver",
    "ct": "America/New_York", "dc": "America/New_York", "de": "America/New_York",
    "fl": "America/New_York", "ga": "America/New_York", "hi": "Pacific/Honolulu",
    "ia": "America/Chicago", "id": "America/Boise", "il": "America/Chicago",
    "in": "America/Indiana/Indianapolis", "ks": "America/Chicago",
    "ky": "America/New_York", "la": "America/Chicago", "ma": "America/New_York",
    "md": "America/New_York", "me": "America/New_York", "mi": "America/Detroit",
    "mn": "America/Chicago", "mo": "America/Chicago", "ms": "America/Chicago",
    "mt": "America/Denver", "nc": "America/New_York", "nd": "America/Chicago",
    "ne": "America/Chicago", "nh": "America/New_York", "nj": "America/New_York",
    "nm": "America/Denver", "nv": "America/Los_Angeles", "ny": "America/New_York",
    "oh": "America/New_York", "ok": "America/Chicago", "or": "America/Los_Angeles",
    "pa": "America/New_York", "pr": "America/Puerto_Rico", "ri": "America/New_York",
    "sc": "America/New_York", "sd": "America/Chicago", "tn": "America/Chicago",
    "tx": "America/Chicago", "ut": "America/Denver", "va": "America/New_York",
    "vt": "America/New_York", "wa": "America/Los_Angeles", "wi": "America/Chicago",
    "wv": "America/New_York", "wy": "America/Denver",
}
DEFAULT_TZ = "America/Chicago"

# Approximate state centroids, used only to narrow 308 theatre slugs down to a
# plausible handful before any page is fetched. Cinemark's sitemap carries no
# coordinates, and its slugs are state-prefixed ("tx-san-antonio/..."), so this
# is the one geographic signal available for free. Precision does not matter:
# it is a pre-filter, and real coordinates replace it as pages get visited.
STATE_CENTROID = {
    "ak": (64.2, -152.3), "al": (32.8, -86.8), "ar": (34.9, -92.4),
    "az": (34.3, -111.7), "ca": (37.2, -119.5), "co": (39.0, -105.5),
    "ct": (41.6, -72.7), "dc": (38.9, -77.0), "de": (39.0, -75.5),
    "fl": (28.6, -82.4), "ga": (32.6, -83.4), "hi": (20.3, -156.4),
    "ia": (42.0, -93.5), "id": (44.4, -114.6), "il": (40.0, -89.2),
    "in": (39.9, -86.3), "ks": (38.5, -98.4), "ky": (37.5, -85.3),
    "la": (31.1, -92.0), "ma": (42.3, -71.8), "md": (39.0, -76.8),
    "me": (45.4, -69.2), "mi": (44.3, -85.4), "mn": (46.3, -94.3),
    "mo": (38.4, -92.5), "ms": (32.7, -89.7), "mt": (47.0, -109.6),
    "nc": (35.5, -79.4), "nd": (47.4, -100.5), "ne": (41.5, -99.8),
    "nh": (43.7, -71.6), "nj": (40.2, -74.7), "nm": (34.4, -106.1),
    "nv": (39.3, -116.6), "ny": (42.9, -75.5), "oh": (40.3, -82.8),
    "ok": (35.6, -97.5), "or": (43.9, -120.6), "pa": (40.9, -77.8),
    "pr": (18.2, -66.4), "ri": (41.7, -71.6), "sc": (33.9, -80.9),
    "sd": (44.4, -100.2), "tn": (35.8, -86.4), "tx": (31.5, -99.3),
    "ut": (39.3, -111.7), "va": (37.5, -78.9), "vt": (44.1, -72.7),
    "wa": (47.4, -120.5), "wi": (44.6, -89.7), "wv": (38.6, -80.6),
    "wy": (43.0, -107.6),
}

# Half-width of a large US state, in km. Added to the search radius so a
# theatre near a state border is not excluded by its centroid being far away.
STATE_SLACK_KM = 450.0


def state_of(slug: str) -> str:
    return slug.split("-", 1)[0].lower()


def timezone_for(slug: str) -> str:
    """IANA zone from the state prefix of a theatre slug."""
    state = slug.split("-", 1)[0].lower()
    return STATE_TZ.get(state, DEFAULT_TZ)


class CinemarkParseError(ValueError):
    pass


class CinemarkChallenged(CinemarkParseError):
    """Cloudflare served a challenge. Retry; do not go hunting for a new shape."""


@dataclass(frozen=True)
class CinemarkTheatre:
    theater_id: str
    slug: str
    name: str
    lat: float | None
    lon: float | None

    @property
    def venue_id(self) -> str:
        # The last slug segment already starts with "cinemark-" for most
        # theatres, so prefixing unconditionally yields cinemark-cinemark-*.
        leaf = self.slug.rsplit("/", 1)[-1]
        return leaf if leaf.startswith("cinemark-") else f"cinemark-{leaf}"


@dataclass(frozen=True)
class CinemarkShowtime:
    showtime_id: str
    theater_id: str
    movie_id: str
    title: str
    runtime_min: int | None
    starts_at_local: datetime
    print_type: str
    # The seat-map href exactly as the page wrote it.
    #
    # Kept rather than rebuilt from the parts. Cinemark's link carries four
    # parameters - TheaterId, ShowtimeId, CinemarkMovieId and Showtime - and a
    # reconstruction using only the first two is answered with a redirect to
    # the homepage. Verified: the two-parameter form lands on "Cinemark
    # Theatres | Movie Times"; the full one on "Cinemark - Reserve Your Seats"
    # with the seat markup present.
    seat_map_path: str = ""

    def deeplink(self) -> str:
        """The page a person opens to pick seats and buy.

        The full four-parameter URL the site itself links to. Rebuilding it
        from theater and showtime alone produced a link that redirected to the
        homepage - a booking link that silently goes nowhere is worse than no
        link, because it looks like it worked.
        """
        if self.seat_map_path:
            return BASE + self.seat_map_path
        return (
            f"{BASE}/TicketSeatMap/?TheaterId={self.theater_id}"
            f"&ShowtimeId={self.showtime_id}"
        )


def _theatre_name(html: str, slug: str) -> str:
    """A displayable theatre name.

    The page title is SEO copy - "Movie Theater In NW San Antonio, TX:
    Cinemark San Antonio 16" - so the real name is the part after the last
    colon. Falls back to prettifying the slug rather than showing the user a
    marketing sentence.
    """
    title = (re.search(r"<title>([^<|]+)", html) or [None, ""])[1].strip()
    if ":" in title:
        candidate = title.rsplit(":", 1)[1].strip()
        if candidate:
            return candidate
    if title and len(title) < 60:
        return title
    leaf = slug.rsplit("/", 1)[-1].replace("-", " ")
    return leaf.title()


def parse_runtime(text: str) -> int | None:
    """'2 hr 25 min' -> 145. Feeds the identity resolver's tiebreaker."""
    if not text:
        return None
    match = _RUNTIME.search(text)
    if not match or not any(match.groups()):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    total = hours * 60 + minutes
    return total or None


def classify_print_type(print_type: str) -> tuple[Presentation, set[str]]:
    """Phrase-match a Cinemark format string.

    Returns the Presentation plus any leftover words, so a newly introduced
    format phrase is *detectable* rather than silently dropped - the same
    guarantee the token tables give the other chains.
    """
    text = " ".join(print_type.lower().split())
    remaining = text
    projection = Projection.UNKNOWN
    brand = Brand.NONE
    attrs: set[Attribute] = set()

    for phrase, value in _PHRASES:
        if phrase not in remaining:
            continue
        remaining = remaining.replace(phrase, " ")
        attrs |= value.attrs
        if projection is Projection.UNKNOWN and value.projection is not Projection.UNKNOWN:
            projection = value.projection
        if brand is Brand.NONE and value.brand is not Brand.NONE:
            brand = value.brand

    for phrase in _IGNORED_PHRASES:
        remaining = remaining.replace(phrase, " ")

    leftover = {w for w in remaining.split() if len(w) > 2}
    return (
        Presentation(projection=projection, brand=brand,
                     attrs=frozenset(attrs), raw=print_type),
        leftover,
    )


class CinemarkShowtimes:
    chain = "cinemark"
    source = "cinemark:theatre-page"
    tier = 1

    def theatre_url(self, slug: str, date: str) -> str:
        return THEATRE.format(slug=slug, date=date)

    # ------------------------------------------------------------------
    @staticmethod
    def _guard(html: str) -> None:
        if _CHALLENGE in html:
            raise CinemarkChallenged("Cloudflare challenge served instead of content")

    def parse_theatre(self, html: str, slug: str) -> CinemarkTheatre:
        self._guard(html)
        theater_ids = set(re.findall(r"TheaterId=(\d+)", html))
        if not theater_ids:
            raise CinemarkParseError(f"cinemark:{slug}: no TheaterId anywhere on page")

        coords = _MAPS_COORDS.search(html)
        name = _theatre_name(html, slug)
        return CinemarkTheatre(
            theater_id=sorted(theater_ids)[0],
            slug=slug,
            name=name,
            lat=float(coords.group(1)) if coords else None,
            lon=float(coords.group(2)) if coords else None,
        )

    def parse_showtimes(self, html: str) -> list[CinemarkShowtime]:
        """Join the per-movie JSON models to the rendered showtime divs.

        The join key is `CinemarkMovieId`, which appears in both - the model
        supplies title and runtime, the markup supplies time and format.
        """
        self._guard(html)

        movies: dict[str, dict] = {}
        for raw in _JSON_MODEL.findall(html):
            try:
                model = json.loads(html_lib.unescape(raw))
            except json.JSONDecodeError:
                continue
            if (movie_id := model.get("cinemarkMovieId")) is not None:
                movies[str(movie_id)] = model

        out: list[CinemarkShowtime] = []
        for print_type, href in _SHOWTIME.findall(html):
            params = dict(
                pair.split("=", 1)
                for pair in html_lib.unescape(href).split("?", 1)[-1].split("&")
                if "=" in pair
            )
            movie_id = params.get("CinemarkMovieId", "")
            model = movies.get(movie_id, {})
            when = params.get("Showtime", "")
            if not when:
                continue
            out.append(
                CinemarkShowtime(
                    showtime_id=params.get("ShowtimeId", ""),
                    theater_id=params.get("TheaterId", ""),
                    movie_id=movie_id,
                    title=model.get("movieTitle") or "",
                    runtime_min=parse_runtime(model.get("movieRunTime") or ""),
                    starts_at_local=datetime.fromisoformat(when),
                    print_type=html_lib.unescape(print_type),
                    seat_map_path=html_lib.unescape(href),
                )
            )

        if movies and not out:
            raise CinemarkParseError(
                "movie models present but no showtime divs parsed - markup "
                "changed, or showDate was omitted from the URL"
            )
        return out

    @staticmethod
    def theatre_slugs(sitemap_xml: str) -> list[str]:
        """Theatre page slugs from the sitemap - the one unchallenged surface."""
        slugs = [
            match.split("/theatres/", 1)[1].strip("/")
            for match in re.findall(r"<loc>([^<]+)</loc>", sitemap_xml)
            if "/theatres/" in match
        ]
        return sorted({s for s in slugs if s and "/" in s})
