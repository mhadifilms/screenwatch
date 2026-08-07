"""SearchSpec - everything a caller can ask for, in one serializable object.

One object rather than a long parameter list, because the same shape is a
search request, a saved watch, and an MCP tool input. If they diverge, a watch
stops meaning the same thing as the search that created it.

Defaults are chosen so `SearchSpec(work=WorkRef(query="dune"))` is already a
sensible query: anywhere, any time in the next week, one ticket, no format
preference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

from ..identity.work import WorkRef
from ..models import Attribute, Preference

EARTH_RADIUS_KM = 6371.0


class Membership(Enum):
    """Exhibitor subscription programmes.

    Called "distributor" in the original request; the field is really the
    exhibitor's own programme, since that is what decides whether a given
    screening costs you anything.
    """

    AMC_ALIST = "amc_alist"
    REGAL_UNLIMITED = "regal_unlimited"
    CINEMARK_MOVIE_CLUB = "cinemark_movie_club"
    ALAMO_SEASON_PASS = "alamo_season_pass"

    @property
    def chain(self) -> str:
        return {
            Membership.AMC_ALIST: "amc",
            Membership.REGAL_UNLIMITED: "regal",
            Membership.CINEMARK_MOVIE_CLUB: "cinemark",
            Membership.ALAMO_SEASON_PASS: "alamo",
        }[self]


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float

    def km_to(self, other: GeoPoint) -> float:
        """Haversine. Straight-line, not drive time - good enough to rank."""
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dp = p2 - p1
        dl = math.radians(other.lon - self.lon)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class LocationSpec:
    """Where to look. Explicit venues always win over a radius.

    `allow` is not filtered by radius - naming a venue means you want it even
    if it is a two-hour drive, which is exactly the 70mm-pilgrimage case.

    `chains` and `venue_types` are shared search/watch filters. They are
    applied by the venue directory before a provider is asked for inventory,
    so an "independent art house only" watch does not waste requests on
    multiplexes and does not silently broaden later.
    """

    origin: GeoPoint | None = None
    radius_km: float = 40.0
    city: str | None = None
    allow: frozenset[str] = field(default_factory=frozenset)
    deny: frozenset[str] = field(default_factory=frozenset)
    chains: frozenset[str] = field(default_factory=frozenset)
    venue_types: frozenset[str] = field(default_factory=frozenset)

    def admits(self, venue_id: str, venue_point: GeoPoint | None) -> bool:
        if venue_id in self.deny:
            return False
        if venue_id in self.allow:
            return True
        if self.allow and not self.origin and not self.city:
            return False  # an explicit-venue-only search
        if self.origin and venue_point:
            return self.origin.km_to(venue_point) <= self.radius_km
        return not self.origin  # no geo info: admit and let ranking sort it


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"date window ends before it starts: {self.start}..{self.end}")

    @classmethod
    def tonight(cls, today: date) -> DateWindow:
        return cls(today, today)

    @classmethod
    def next_days(cls, today: date, days: int) -> DateWindow:
        return cls(today, today + timedelta(days=days))

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class TimeWindow:
    """An allowed local-time range, optionally restricted to certain weekdays.

    `end` before `start` means the window wraps past midnight - which is not
    an edge case here. "Tonight" routinely means showings that start at
    00:45 the next calendar day, and treating those as out of range is
    exactly the mistake that hides the late 3D show in the motivating case.
    """

    start: time = time(0, 0)
    end: time = time(23, 59)
    weekdays: frozenset[int] | None = None   # 0 = Monday

    @property
    def wraps(self) -> bool:
        return self.end < self.start

    def contains(self, when: datetime) -> bool:
        if self.weekdays is not None:
            day = when.weekday() if not self.wraps or when.time() >= self.start else (
                (when.weekday() - 1) % 7
            )
            if day not in self.weekdays:
                return False
        t = when.time()
        return (self.start <= t <= self.end) if not self.wraps else (
            t >= self.start or t <= self.end
        )


@dataclass(frozen=True)
class SeatingPrefs:
    """Seat wants. Applied in phase B, when a real seat grid exists."""

    together: bool = True                  # seat the whole party contiguously if possible
    allow_split: bool = True               # ...but accept a split rather than nothing
    avoid_front_rows: int = 2              # treat the first N rows as a last resort
    ideal_depth: float | None = None       # 0..1; None = use the venue default
    max_lateral: float = 1.0               # 0..1; how far off centre is acceptable
    require: frozenset[Attribute] = field(default_factory=frozenset)
    avoid_aisle: bool = False
    wheelchair_spaces: int = 0
    companion_seats: int = 0

    @property
    def needs_accessible_seating(self) -> bool:
        return self.wheelchair_spaces > 0 or self.companion_seats > 0


@dataclass(frozen=True)
class Budget:
    max_total_usd: float | None = None
    max_per_ticket_usd: float | None = None

    def admits(self, per_ticket: float | None, party_size: int) -> bool:
        if per_ticket is None:
            return True
        if self.max_per_ticket_usd is not None and per_ticket > self.max_per_ticket_usd:
            return False
        return not (
            self.max_total_usd is not None
            and per_ticket * party_size > self.max_total_usd
        )


@dataclass(frozen=True)
class Weights:
    """Ranking weights. Documented defaults, fully overridable per search.

    Tuned so the motivating case comes out right: with four people and a
    near-sellout, group cohesion and seat quality together outweigh a
    one-step format downgrade and a 75-minute delay.
    """

    # Phase A - screening-level
    format_fit: float = 1.0
    time_fit: float = 0.8
    lateness: float = 0.5
    distance_fit: float = 0.6
    membership_fit: float = 0.5
    availability_prior: float = 0.4
    venue_affinity: float = 0.3
    # Phase B - seat-level
    group_cohesion: float = 1.4
    seat_quality: float = 1.2
    party_fit: float = 2.0        # weighted hardest: not seating everyone is close to a dealbreaker

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class SearchSpec:
    work: WorkRef
    party_size: int = 1
    location: LocationSpec = field(default_factory=LocationSpec)
    date_window: DateWindow | None = None          # None = today .. +7
    time_windows: tuple[TimeWindow, ...] = ()      # empty = any time
    presentations: Preference | None = None        # None = no format preference
    # Treat `presentations` as a filter rather than a ranking.
    #
    # A search wants ranking: with everything sold out, a format you did not
    # ask for still beats not going, so an unmatched presentation is scored
    # low and kept. A *watch* wants the opposite. "Tell me when new 70mm IMAX
    # Dune tickets drop" is a request about 70mm IMAX, and firing an alert for
    # a standard digital showing is not a partial answer, it is the wrong one -
    # and it trains the user to ignore the alerts.
    strict_presentations: bool = False
    memberships: frozenset[Membership] = field(default_factory=frozenset)
    seating: SeatingPrefs = field(default_factory=SeatingPrefs)
    budget: Budget = field(default_factory=Budget)
    weights: Weights = field(default_factory=Weights)
    include_sold_out: bool = False                 # True for watches
    release_radar: bool = False                   # cheap catalog/sitemap signal
    max_seatmap_fetches: int = 10                  # phase B budget, in requests
    diversify_per_group: int = 2                   # same venue+format runs before others

    def __post_init__(self) -> None:
        if self.party_size < 1:
            raise ValueError("party_size must be at least 1")
        if self.max_seatmap_fetches < 0:
            raise ValueError("max_seatmap_fetches must not be negative")

    def window(self, today: date) -> DateWindow:
        return self.date_window or DateWindow.next_days(today, 7)

    def admits_time(self, when: datetime) -> bool:
        return not self.time_windows or any(w.contains(when) for w in self.time_windows)

    def covered_by_membership(self, chain: str) -> bool:
        return any(m.chain == chain for m in self.memberships)
