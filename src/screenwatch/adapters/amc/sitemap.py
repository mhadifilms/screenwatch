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
_ENTRY = re.compile(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", re.S)
_MOVIE_SLUG = re.compile(r"/movies/([a-z0-9\-]+?)(?:-(\d+))?$")


@dataclass(frozen=True)
class MovieEntry:
    slug: str
    movie_id: str | None
    url: str
    lastmod: str | None

    @property
    def key(self) -> str:
        return self.movie_id or self.slug


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
