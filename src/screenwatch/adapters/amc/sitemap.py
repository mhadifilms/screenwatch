"""Tier 0: sitemaps. The tripwire, and the only AMC surface not behind Queue-it.

Measured 2026-08-02: /movie-theatres/* returns a Queue-it interstitial to an
impersonated client, while /sitemap.xml and /sitemaps/*.xml return content to
plain curl. So this surface can be polled far more often than any other, and
it is what should be running at 30s cadence on on-sale morning.

It carries no showtimes. What it carries is *existence*: a movie slug
appearing, or a lastmod advancing, is the earliest cheap signal that something
changed - at which point you spend one guarded request to find out what.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from ...transport import Transport

BASE = "https://www.amctheatres.com"
MOVIES = f"{BASE}/sitemaps/sitemap-movies.xml"
THEATRES = f"{BASE}/sitemaps/sitemap-theatres.xml"

_LOC = re.compile(r"<loc>([^<]+)</loc>")
_ENTRY = re.compile(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", re.DOTALL)
_MOVIE_SLUG = re.compile(r"/movies/([a-z0-9\-]+?)(?:-(\d+))?$")
_THEATRE_BLOCK = re.compile(r"<url\b.*?</url>", re.IGNORECASE | re.DOTALL)
_THEATRE_URL = re.compile(
    r"/movie-theatres/([^/]+)/([^/?#]+)$", re.IGNORECASE
)
_ATTRIBUTE = re.compile(
    r'<Attribute\s+name="([^"]+)">([^<]*)</Attribute>',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MovieEntry:
    slug: str
    movie_id: str | None
    url: str
    lastmod: str | None

    @property
    def key(self) -> str:
        return self.movie_id or self.slug


@dataclass(frozen=True)
class TheatreEntry:
    """One official AMC theatre directory row from the sitemap PageMap."""

    theatre_id: str
    name: str
    market: str
    slug: str
    url: str
    city: str | None
    state: str | None
    postal_code: str | None
    address: str | None
    latitude: float | None
    longitude: float | None

    @property
    def venue_id(self) -> str:
        return self.slug if self.slug.startswith("amc-") else f"amc-{self.slug}"

    @property
    def key(self) -> str:
        return self.theatre_id or self.venue_id


class AmcSitemap:
    chain = "amc"
    source = "amc:sitemap"
    tier = 0

    def fetch(self, transport: Transport, *, which: str = "movies") -> str:
        url = MOVIES if which == "movies" else THEATRES
        return transport.get(url, conditional=True).text

    def parse(self, raw: str) -> list[MovieEntry]:
        out = []
        for url, lastmod in _ENTRY.findall(raw):
            if (m := _MOVIE_SLUG.search(url)) is None:
                continue
            out.append(
                MovieEntry(slug=m.group(1), movie_id=m.group(2), url=url,
                           lastmod=lastmod or None)
            )
        if not out:
            raise ValueError("sitemap parsed to zero movie entries - shape changed")
        return out

    def parse_theatres(self, raw: str) -> list[TheatreEntry]:
        """Parse the national theatre sitemap, including its PageMap data.

        The URL alone proves that a page exists. The embedded PageMap is the
        official source for the display name, theatre id, city/state, and
        coordinates. Rows missing a stable id or coordinates are retained only
        when the required identity fields are present; no coordinates are
        guessed from the market slug.
        """
        out: list[TheatreEntry] = []
        for block in _THEATRE_BLOCK.findall(raw):
            loc_match = _LOC.search(block)
            if not loc_match:
                continue
            url = loc_match.group(1).strip()
            path_match = _THEATRE_URL.search(url)
            if not path_match:
                continue
            attributes = dict(_ATTRIBUTE.findall(block))
            theatre_id = attributes.get("theatreId", "").strip()
            slug = path_match.group(2).strip()
            if not theatre_id or not slug:
                continue

            def number(name: str, values: dict[str, str] = attributes) -> float | None:
                value = values.get(name, "").strip()
                try:
                    return float(value) if value else None
                except ValueError:
                    return None

            out.append(
                TheatreEntry(
                    theatre_id=theatre_id,
                    name=attributes.get("title", "").strip() or slug,
                    market=path_match.group(1).strip(),
                    slug=slug,
                    url=url,
                    city=attributes.get("city", "").strip() or None,
                    state=attributes.get("state", "").strip() or None,
                    postal_code=attributes.get("postalCode", "").strip() or None,
                    address=attributes.get("addressLine1", "").strip() or None,
                    latitude=number("latitude"),
                    longitude=number("longitude"),
                )
            )
        if not out:
            raise ValueError("sitemap parsed to zero theatre entries - shape changed")
        return out

    @staticmethod
    def digest(raw: str) -> str:
        """Content hash for change detection when the server sends no ETag."""
        return hashlib.sha256("\n".join(sorted(_LOC.findall(raw))).encode()).hexdigest()

    @staticmethod
    def find(entries: list[MovieEntry], *, title_contains: str) -> list[MovieEntry]:
        needle = title_contains.lower().replace(" ", "-")
        return [e for e in entries if needle in e.slug]


@dataclass
class Tripwire:
    """Fires when the watched movie's sitemap entry appears or changes.

    Deliberately stateful and dumb: it answers 'is it worth spending a guarded
    request right now', nothing more.
    """

    title_contains: str
    _seen: dict[str, str | None] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._seen is None:
            self._seen = {}

    def check(self, entries: list[MovieEntry], *, now: datetime) -> list[str]:
        reasons = []
        for entry in AmcSitemap.find(entries, title_contains=self.title_contains):
            if entry.key not in self._seen:
                reasons.append(f"new sitemap entry {entry.slug} ({entry.url})")
            elif self._seen[entry.key] != entry.lastmod:
                reasons.append(
                    f"lastmod advanced for {entry.slug}: "
                    f"{self._seen[entry.key]} -> {entry.lastmod}"
                )
            self._seen[entry.key] = entry.lastmod
        return reasons
