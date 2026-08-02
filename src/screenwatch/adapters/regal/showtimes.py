"""Regal, via the Next.js `__NEXT_DATA__` hydration blob.

Recon notes (2026-08-02):

* `www.regmovies.com` sits behind a Cloudflare managed challenge that is
  **intermittent** rather than absolute - a retry loop clears it, usually
  within two or three attempts. That is why this adapter is paired with a
  retrying fetcher rather than a single request.
* `graph.regmovies.com` is a catch-all that renders the homepage for every
  path. Useless for showtimes, but it carries `fullTheatreData` - all 402
  Regal theatres with coordinates, timezone and codes - which makes venue
  discovery a single page load.
* A theatre page's `pageProps.showtimes` holds the day's schedule, nested
  `showtimes[] -> Film[] -> Performances[]`.
* `StopSales: true` is the sold-out flag.
* Format lives in `PerformanceAttributes`, a flat list of strings mixing
  presentation ("RPX", "4DX", "3D"), accessibility ("CC", "DV", "OC") and
  policy ("No Passes"). Only the first two kinds mean anything here.

No seat surface was found: `SeatAllocationType: "2"` says seats are reserved,
but the layout is not in the hydration blob.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ...models import Attribute, Brand, Presentation, Projection
from ...presentation import register_chain

BASE = "https://www.regmovies.com"
DIRECTORY = "https://graph.regmovies.com/theatres"
THEATRE = BASE + "/theatres/{path_name}"

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_CHALLENGE = "Just a moment"


def _p(projection=Projection.UNKNOWN, brand=Brand.NONE, attrs=()):
    return Presentation(projection=projection, brand=brand, attrs=frozenset(attrs))


REGAL_TOKENS: dict[str, Presentation] = {
    "2d": _p(Projection.DIGITAL),
    "3d": _p(attrs=[Attribute.THREE_D]),
    "rpx": _p(Projection.DIGITAL, Brand.PLF),
    "4dx": _p(Projection.DIGITAL, Brand.FOURDX),
    "screenx": _p(Projection.DIGITAL, Brand.SCREENX),
    "hdr": _p(Projection.DIGITAL_LASER, Brand.PLF),
    "imax": _p(Projection.DIGITAL, Brand.IMAX),
    "imax70mm": _p(Projection.FILM_70MM_15PERF, Brand.IMAX),
    "70mm": _p(Projection.FILM_70MM),
    "35mm": _p(Projection.FILM_35MM),
    "dolbycinema": _p(Projection.DIGITAL_LASER, Brand.DOLBY_CINEMA),
    "cc": _p(attrs=[Attribute.CLOSED_CAPTION]),
    "oc": _p(attrs=[Attribute.OPEN_CAPTION]),
    "dv": _p(attrs=[Attribute.AUDIO_DESCRIPTION]),     # descriptive video
    "subtitled": _p(attrs=[Attribute.SUBTITLED]),
    "recliner": _p(attrs=[Attribute.RECLINERS]),
    "reservedselected": _p(attrs=[Attribute.RESERVED_SEATING]),
    "atmos": _p(attrs=[Attribute.ATMOS]),
}

# Policy and furniture labels that share the attribute list but say nothing
# about the presentation.
REGAL_IGNORED = frozenset({
    "nopasses", "nopasspt", "stadium", "vip", "luxurylounger", "openingday",
    "advancescreening", "matinee",
})

register_chain("regal", REGAL_TOKENS, REGAL_IGNORED)


class RegalParseError(ValueError):
    pass


class RegalChallenged(RegalParseError):
    """Cloudflare served a challenge instead of content.

    Distinct from a parse failure because the fix is different: retry, do not
    go looking for a changed page shape.
    """


@dataclass(frozen=True)
class RegalTheatre:
    theatre_code: str
    name: str
    path_name: str
    city: str
    state: str
    lat: float | None
    lon: float | None
    tz: str

    @property
    def venue_id(self) -> str:
        # path_name already begins with "regal-" for every theatre, so
        # prefixing again would produce regal-regal-times-square-1929.
        slug = self.path_name
        return slug if slug.startswith("regal-") else f"regal-{slug}"


@dataclass(frozen=True)
class RegalPerformance:
    performance_id: str
    theatre_code: str
    movie_code: str
    title: str
    starts_at_utc: datetime
    starts_at_local: datetime
    auditorium: str
    attributes: tuple[str, ...]
    sold_out: bool

    def deeplink(self) -> str:
        return f"{BASE}/showtimes/{self.performance_id}"


def extract_next_data(html: str) -> dict:
    if _CHALLENGE in html:
        raise RegalChallenged("Cloudflare challenge served instead of content")
    match = _NEXT_DATA.search(html)
    if not match:
        raise RegalParseError(
            "no __NEXT_DATA__ block - Regal may have moved to App Router, "
            "or this is an error page"
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RegalParseError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc


def _dt(value: str, *, utc: bool) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if utc:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return parsed.replace(tzinfo=None)


class RegalShowtimes:
    chain = "regal"
    source = "regal:nextdata"
    tier = 1

    def theatre_url(self, path_name: str, date: str | None = None) -> str:
        url = THEATRE.format(path_name=path_name)
        return f"{url}?date={date}" if date else url

    # ------------------------------------------------------------------
    def parse_theatres(self, html: str) -> list[RegalTheatre]:
        """The national directory, from any Regal page's hydration blob."""
        props = extract_next_data(html).get("props", {}).get("pageProps", {})
        rows = props.get("fullTheatreData") or []
        out = [
            RegalTheatre(
                theatre_code=str(t.get("theatre_code") or ""),
                name=t.get("name") or "",
                path_name=t.get("path_name") or "",
                city=t.get("city") or "",
                state=t.get("state") or "",
                lat=_maybe_float(t.get("latitude")),
                lon=_maybe_float(t.get("longitude")),
                tz=t.get("iana_timezone") or "America/New_York",
            )
            for t in rows
            if t.get("theatre_code") and t.get("path_name")
        ]
        if not out:
            raise RegalParseError("no theatres in fullTheatreData - shape changed")
        return out

    def parse_showtimes(self, html: str) -> list[RegalPerformance]:
        props = extract_next_data(html).get("props", {}).get("pageProps", {})
        days = props.get("showtimes") or []

        out: list[RegalPerformance] = []
        for day in days:
            theatre_code = str(day.get("TheatreCode") or "")
            for film in day.get("Film") or []:
                title = film.get("Title") or ""
                movie_code = film.get("MasterMovieCode") or ""
                for perf in film.get("Performances") or []:
                    utc = perf.get("UtcShowTime")
                    local = perf.get("CalendarShowTime")
                    if not utc or not local:
                        continue
                    out.append(
                        RegalPerformance(
                            performance_id=str(perf.get("PerformanceId") or ""),
                            theatre_code=theatre_code,
                            movie_code=movie_code,
                            title=title,
                            starts_at_utc=_dt(utc, utc=True),
                            starts_at_local=_dt(local, utc=False),
                            auditorium=str(perf.get("Auditorium") or ""),
                            attributes=tuple(perf.get("PerformanceAttributes") or ()),
                            sold_out=bool(perf.get("StopSales")),
                        )
                    )

        if days and not out:
            raise RegalParseError(
                "showtimes present but no performances parsed - nesting changed"
            )
        return out


def _maybe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
