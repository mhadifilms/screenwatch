"""Cinema360 ("C360OnlineSWeb") — the platform Apple Cinemas runs on.

One adapter, every C360 venue. The site is an Angular SPA over an ASP.NET
JSON API, and the API is unauthenticated once a session has been warmed on the
landing page (Cloudflare issues the cookies there).

Endpoint map, recovered from the shipped bundle rather than guessed — the
route shape is `Kiosk/{action}/{locationId}/{date}`, which is why passing
dates as extra path segments silently returned the SPA's HTML fallback:

    Location/GetLocationsByCompanyId/{companyId}   all venues, with city/state
    Screen/GetScreenSettingOfAllLocations/{cid}    per-venue format vocabulary
    Kiosk/GetAdvanceShows/{locationId}/{date}      the day's schedule
    Screen/GetScreenById/{screenId}                auditorium geometry
    MovieSchedule/GetScheduleByShowId/{showId}     per-show detail

What this platform gives that no other source does: **exact seat counts per
showing** (`totalSeatsSold` / `totalAvailable`) alongside the real auditorium
shape. What it does not give is *which* seats are taken — that lives behind
`HoldSeats`, and reaching it would mean creating a hold, i.e. writing to their
booking system. So this adapter reports geometry and counts, and the ranker
estimates group feasibility from those rather than inventing a seat grid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from ...models import Attribute, Brand, Presentation, Projection

BASE = "https://www.applecinemas.com"
APPLE_COMPANY_ID = "f604d90"

LOCATIONS = "/Location/GetLocationsByCompanyId/{company}"
SCREEN_SETTINGS = "/Screen/GetScreenSettingOfAllLocations/{company}"
ADVANCE_SHOWS = "/Kiosk/GetAdvanceShows/{location}/{date}"
SCREEN_BY_ID = "/Screen/GetScreenById/{screen}"

# Windows timezone names, which is what C360 stores.
_WINDOWS_TZ = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
}

_ROW = re.compile(r"^([A-Za-z]+)(\d+)$")


def _p(projection=Projection.UNKNOWN, brand=Brand.NONE, attrs=()):
    return Presentation(projection=projection, brand=brand, attrs=frozenset(attrs))


# Taken verbatim from `GetScreenSettingOfAllLocations`, which is the platform's
# own declared vocabulary - so this table can be checked against the source
# rather than waiting to meet a surprise. "ACX" is Apple's premium large
# format; "Infinity Vision" is their laser branding.
C360_TOKENS: dict[str, Presentation] = {
    "2d": _p(Projection.DIGITAL),
    "3d": _p(Projection.DIGITAL, attrs=[Attribute.THREE_D]),
    "reald3d": _p(Projection.DIGITAL, attrs=[Attribute.THREE_D]),
    "3dhfr": _p(Projection.DIGITAL, attrs=[Attribute.THREE_D, Attribute.HFR]),
    "imax": _p(Projection.DIGITAL, Brand.IMAX),
    "imax3d": _p(Projection.DIGITAL, Brand.IMAX, attrs=[Attribute.THREE_D]),
    "imax70mm": _p(Projection.FILM_70MM_15PERF, Brand.IMAX),
    "acx": _p(Projection.DIGITAL, Brand.PLF),
    "acx3d": _p(Projection.DIGITAL, Brand.PLF, attrs=[Attribute.THREE_D]),
    "acxinfinityvision": _p(Projection.DIGITAL_LASER, Brand.PLF),
    "acxdolbyatmos": _p(Projection.DIGITAL, Brand.PLF, attrs=[Attribute.ATMOS]),
    "3dhfracxdolbyatmos": _p(Projection.DIGITAL, Brand.PLF,
                             attrs=[Attribute.THREE_D, Attribute.HFR, Attribute.ATMOS]),
    "screenx": _p(Projection.DIGITAL, Brand.SCREENX),
    "screenxinfinityvision": _p(Projection.DIGITAL_LASER, Brand.SCREENX),
    "4dx": _p(Projection.DIGITAL, Brand.FOURDX),
    "dolbyatmos": _p(attrs=[Attribute.ATMOS]),
    "opencaption": _p(attrs=[Attribute.OPEN_CAPTION]),
    "sensoryfriendly": _p(attrs=[Attribute.SENSORY_FRIENDLY]),
}

# `showProperties` is a parallel channel of accessibility and comfort flags.
C360_PROPERTIES: dict[str, Attribute] = {
    "closecaptioning": Attribute.CLOSED_CAPTION,
    "closedcaptioning": Attribute.CLOSED_CAPTION,
    "opencaption": Attribute.OPEN_CAPTION,
    "recliners": Attribute.RECLINERS,
    "dolbyatmos": Attribute.ATMOS,
    "reservedseating": Attribute.RESERVED_SEATING,
}


class C360ParseError(ValueError):
    pass


@dataclass(frozen=True)
class C360Location:
    location_id: str
    name: str
    city: str
    state: str
    tz: str

    @property
    def venue_id(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return f"c360-{slug}"


@dataclass(frozen=True)
class C360Show:
    show_id: str
    location_id: str
    screen_id: str
    screen_name: str
    movie_id: str
    title: str
    starts_at_local: datetime
    runtime_min: int | None
    formats: tuple[str, ...]
    properties: tuple[str, ...]
    seats_sold: int
    seats_available: int
    reserved_seating: bool

    @property
    def capacity_hint(self) -> int:
        """Only meaningful when the feed populates totalAvailable, which in
        practice it does not - real capacity comes from the screen record."""
        return self.seats_sold + self.seats_available

    def deeplink(self) -> str:
        return f"{BASE}/showtime/{self.show_id}"


@dataclass(frozen=True)
class C360Screen:
    """Auditorium geometry. Static - it does not vary per showing."""

    screen_id: str
    name: str
    rows: tuple[tuple[str, int], ...]         # (label, seat count)
    cols_reverse: bool
    rows_reverse: bool
    companion: frozenset[str] = field(default_factory=frozenset)
    accessible: frozenset[str] = field(default_factory=frozenset)
    broken: frozenset[str] = field(default_factory=frozenset)
    house: frozenset[str] = field(default_factory=frozenset)
    unavailable: frozenset[str] = field(default_factory=frozenset)
    blank: frozenset[str] = field(default_factory=frozenset)

    @property
    def total_seats(self) -> int:
        return sum(count for _, count in self.rows)

    @property
    def bookable_seats(self) -> int:
        blocked = self.broken | self.house | self.unavailable | self.blank
        return max(self.total_seats - len(blocked), 0)


def _seat_ids(value) -> frozenset[str]:
    """Coerce a C360 seat list to plain ids.

    These arrive as bare strings ("E3") on most screens but as objects on
    some - blankSeats in particular, which describes gaps rather than seats.
    Assuming strings raised `unhashable type: dict` against a live auditorium.
    """
    out: set[str] = set()
    for entry in value or ():
        if isinstance(entry, str):
            out.add(entry)
        elif isinstance(entry, dict):
            for key in ("seatId", "seatID", "seatName", "name", "id"):
                if entry.get(key):
                    out.add(str(entry[key]))
                    break
    return frozenset(out)


def iana_timezone(windows_name: str) -> str:
    return _WINDOWS_TZ.get((windows_name or "").strip(), "America/New_York")


def normalize(token: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (token or "").lower())


class C360Schedule:
    chain = "c360"
    source = "c360:kiosk"
    tier = 1

    # ------------------------------------------------------------------
    def parse_locations(self, payload: list[dict]) -> list[C360Location]:
        out = [
            C360Location(
                location_id=row["location_ID"],
                name=(row.get("location_Name") or "").strip(),
                city=row.get("city") or "",
                state=row.get("state") or "",
                tz=iana_timezone(row.get("timezone") or ""),
            )
            for row in payload or []
            if row.get("location_ID") and row.get("location_Name")
        ]
        if not out:
            raise C360ParseError("no locations in company payload - shape changed")
        return out

    def parse_shows(self, payload: list[dict], location_id: str) -> list[C360Show]:
        """Flatten movie -> screens[] -> showTimes[] into one list."""
        out: list[C360Show] = []
        for movie in payload or []:
            title = (movie.get("movieName") or movie.get("movieDisplayName") or "").strip()
            runtime = _runtime_minutes(movie.get("runtime"))
            for screen in movie.get("screens") or []:
                for show in screen.get("showTimes") or []:
                    when = show.get("showTime")
                    if not when or show.get("disabled"):
                        continue
                    out.append(
                        C360Show(
                            show_id=str(show.get("showID") or ""),
                            location_id=location_id,
                            screen_id=str(show.get("screenID") or screen.get("screenID") or ""),
                            screen_name=show.get("screenName") or screen.get("screenName") or "",
                            movie_id=str(movie.get("movieID") or ""),
                            title=title,
                            starts_at_local=datetime.fromisoformat(when),
                            runtime_min=runtime,
                            formats=tuple(show.get("screenInfo") or ()),
                            properties=tuple(
                                p.get("propertyName", "")
                                for p in show.get("showProperties") or []
                            ),
                            seats_sold=int(show.get("totalSeatsSold") or 0),
                            seats_available=int(show.get("totalAvailable") or 0),
                            reserved_seating=(show.get("seating") == "allocated"),
                        )
                    )
        return out

    def parse_screen(self, payload: dict) -> C360Screen:
        """Geometry. `rows` arrives as label+count strings: ["A11","B12",…]."""
        rows: list[tuple[str, int]] = []
        for entry in payload.get("rows") or []:
            match = _ROW.match(str(entry))
            if match:
                rows.append((match.group(1), int(match.group(2))))
        if not rows:
            raise C360ParseError(
                f"screen {payload.get('screen_ID')} has no parseable rows"
            )
        return C360Screen(
            screen_id=str(payload.get("screen_ID") or ""),
            name=payload.get("screen_Name") or "",
            rows=tuple(rows),
            cols_reverse=bool(payload.get("isColsReverse")),
            rows_reverse=bool(payload.get("isRowsReverse")),
            companion=_seat_ids(payload.get("companion")),
            accessible=_seat_ids(payload.get("hadicapped_Seats")),  # sic
            broken=_seat_ids(payload.get("broken_Seats")),
            house=_seat_ids(payload.get("house_Seats")),
            unavailable=_seat_ids(payload.get("unavailable")),
            blank=_seat_ids(payload.get("blankSeats")),
        )

    # ------------------------------------------------------------------
    def classify(self, show: C360Show) -> Presentation:
        """Merge screen formats with show properties.

        Both channels carry presentation information and neither is complete:
        `screenInfo` has "IMAX 70MM" but not Atmos, `showProperties` has
        Recliners and captioning but no format.
        """
        projection, brand = Projection.UNKNOWN, Brand.NONE
        attrs: set[Attribute] = set()

        for token in show.formats:
            hit = C360_TOKENS.get(normalize(token))
            if hit is None:
                continue
            attrs |= hit.attrs
            if projection is Projection.UNKNOWN and hit.projection is not Projection.UNKNOWN:
                projection = hit.projection
            if brand is Brand.NONE and hit.brand is not Brand.NONE:
                brand = hit.brand

        for name in show.properties:
            if (attr := C360_PROPERTIES.get(normalize(name))) is not None:
                attrs.add(attr)
        if show.reserved_seating:
            attrs.add(Attribute.RESERVED_SEATING)

        return Presentation(
            projection=projection, brand=brand, attrs=frozenset(attrs),
            raw=" / ".join(show.formats),
        )

    @staticmethod
    def unknown_formats(screen_settings: list[dict]) -> set[str]:
        """Format names the platform declares that the table does not cover."""
        declared = {
            normalize(prop.get("screenSettingName", ""))
            for row in screen_settings or []
            for prop in row.get("screenProperties") or []
        }
        return {d for d in declared if d and d not in C360_TOKENS}


def _runtime_minutes(value) -> int | None:
    """C360 stores runtime as 'HH:MM'."""
    if not value or ":" not in str(value):
        return None
    hours, _, minutes = str(value).partition(":")
    try:
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return None
