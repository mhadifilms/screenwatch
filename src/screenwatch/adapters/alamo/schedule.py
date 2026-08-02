"""Alamo Drafthouse: one open JSON request per market.

The cheapest adapter in the set by a wide margin. `/s/mother/v2/schedule/
market/<slug>` needs no auth, no session and no impersonation, and returns
the entire market in one document: cinemas with coordinates, the format
taxonomy, the session-attribute taxonomy, every presentation and every
session. NYC alone is 1152 sessions.

Because the payload carries its own taxonomy, this adapter can *check* its
format table against what the API declares rather than assuming - see
`unknown_formats`, which the tests assert is empty for a captured market.

No seat surface was found. Every session reports `reservedSeating: true`, so
seat maps exist, but they are not served from this API and the endpoints that
would obviously hold them reject GET. Seats therefore stay unavailable for
Alamo until that is reconned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ...models import Attribute, Brand, Presentation, Projection
from ...presentation import normalize_token, register_chain

BASE = "https://drafthouse.com"
SCHEDULE = BASE + "/s/mother/v2/schedule/market/{market}"

# Discovered by probing; Alamo publishes no market index. `tools/alamo_markets.py`
# re-derives it. Not guaranteed exhaustive - a missing market is invisible, so
# the tool prints what it found rather than silently trusting this list.
KNOWN_MARKETS = (
    "austin", "boston", "charlottesville", "chicago", "corpus-christi", "denver",
    "dfw", "indianapolis", "los-angeles", "northern-virginia", "nyc", "omaha",
    "raleigh", "san-antonio", "sf", "springfield", "st-louis", "winchester",
    "yonkers",
)


def _p(projection=Projection.UNKNOWN, brand=Brand.NONE, attrs=()):
    return Presentation(projection=projection, brand=brand, attrs=frozenset(attrs))


ALAMO_TOKENS: dict[str, Presentation] = {
    "2ddigital": _p(Projection.DIGITAL),
    "digital": _p(Projection.DIGITAL),
    "hdr": _p(Projection.DIGITAL_LASER, Brand.PLF),   # "HDR by Barco"
    "35mm": _p(Projection.FILM_35MM),
    "70mm": _p(Projection.FILM_70MM),
    "opencaption": _p(attrs=[Attribute.OPEN_CAPTION]),
    "atmos": _p(attrs=[Attribute.ATMOS]),
    "3d": _p(attrs=[Attribute.THREE_D]),
}

# Real slugs that carry no presentation meaning. Menu tie-ins and audience
# labels, not formats.
ALAMO_IGNORED = frozenset({
    "kf", "bd", "qrmenu1", "qrmenu2", "qrmenu4", "qrmenu7", "qrmenu9",
})

register_chain("alamo", ALAMO_TOKENS, ALAMO_IGNORED)

_STATUS_SELLABLE = "ONSALE"
_STATUS_SOLD_OUT = "SOLDOUT"


@dataclass(frozen=True)
class AlamoCinema:
    cinema_id: str
    slug: str
    name: str
    lat: float | None
    lon: float | None
    tz: str
    market: str
    status: str

    @property
    def venue_id(self) -> str:
        return f"alamo-{self.slug}"


@dataclass(frozen=True)
class AlamoSession:
    session_id: str
    cinema_id: str
    presentation_slug: str
    title: str
    starts_at_utc: datetime
    starts_at_local: datetime
    format_slug: str
    attribute_slugs: tuple[str, ...]
    status: str
    screen_number: int | None
    reserved_seating: bool

    @property
    def sold_out(self) -> bool:
        return self.status == _STATUS_SOLD_OUT

    def deeplink(self, cinema_slug: str) -> str:
        return f"{BASE}/tickets/{cinema_slug}/{self.session_id}"


class AlamoScheduleParseError(ValueError):
    pass


def _dt(value: str, *, utc: bool) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if utc:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return parsed.replace(tzinfo=None)


class AlamoSchedule:
    chain = "alamo"
    source = "alamo:schedule"
    tier = 0

    def url(self, market: str) -> str:
        return SCHEDULE.format(market=market)

    def parse(self, payload: dict[str, Any], market: str) -> tuple[
        list[AlamoCinema], list[AlamoSession]
    ]:
        data = payload.get("data")
        if not data:
            raise AlamoScheduleParseError(f"alamo:{market}: payload has no data block")

        markets = data.get("market") or []
        if not markets:
            raise AlamoScheduleParseError(f"alamo:{market}: no market in payload")

        cinemas = [
            AlamoCinema(
                cinema_id=str(c["id"]),
                slug=c["slug"],
                name=c["name"],
                lat=_maybe_float(c.get("latitude")),
                lon=_maybe_float(c.get("longitude")),
                tz=c.get("timeZoneName") or "America/Chicago",
                market=market,
                status=c.get("status", "UNKNOWN"),
            )
            for c in markets[0].get("cinemas") or []
        ]

        titles = {
            p["slug"]: (p.get("show") or {}).get("title") or p["slug"]
            for p in data.get("presentations") or []
        }

        sessions = []
        for raw in data.get("sessions") or []:
            if raw.get("isHidden"):
                continue
            slug = raw.get("presentationSlug") or ""
            sessions.append(
                AlamoSession(
                    session_id=str(raw["sessionId"]),
                    cinema_id=str(raw["cinemaId"]),
                    presentation_slug=slug,
                    title=titles.get(slug, slug.replace("-", " ")),
                    starts_at_utc=_dt(raw["showTimeUtc"], utc=True),
                    starts_at_local=_dt(raw["showTimeClt"], utc=False),
                    format_slug=raw.get("formatSlug") or "",
                    attribute_slugs=tuple(raw.get("sessionAttributeSlugs") or ()),
                    status=raw.get("status", ""),
                    screen_number=raw.get("screenNumber"),
                    reserved_seating=bool(raw.get("reservedSeating")),
                )
            )

        if not cinemas:
            raise AlamoScheduleParseError(f"alamo:{market}: no cinemas - shape changed")
        return cinemas, sessions

    @staticmethod
    def declared_tokens(payload: dict[str, Any]) -> set[str]:
        """Every format and attribute slug the API itself declares.

        The payload ships its own taxonomy, which is a rare luxury: instead of
        waiting to meet an unknown token in the wild, the table can be checked
        against the source's own vocabulary.
        """
        data = payload.get("data") or {}
        out = {normalize_token(f["slug"]) for f in data.get("formats") or []}
        out |= {normalize_token(a["slug"]) for a in data.get("sessionAttributes") or []}
        return out

    @classmethod
    def unknown_formats(cls, payload: dict[str, Any]) -> set[str]:
        known = set(ALAMO_TOKENS) | ALAMO_IGNORED
        return cls.declared_tokens(payload) - known


def _maybe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
