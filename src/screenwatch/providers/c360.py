"""Cinema360 provider — Apple Cinemas today, any C360 operator tomorrow.

The company id is the only thing tying this to Apple Cinemas; point it at
another C360 deployment and the same code works.

This is the one non-AMC source where phase B has something real to work with.
It publishes exact per-showing seat counts *and* the auditorium's shape, which
is enough to estimate whether a party can sit together even though it never
says which individual seats are taken. See `seating/estimate.py` for why that
estimate is deliberately pessimistic.
"""

from __future__ import annotations

import time
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from curl_cffi import requests

from ..adapters.c360.schedule import (
    ADVANCE_SHOWS,
    APPLE_COMPANY_ID,
    BASE,
    LOCATIONS,
    SCREEN_BY_ID,
    C360Location,
    C360ParseError,
    C360Schedule,
    C360Screen,
    C360Show,
)
from ..identity.resolve import WorkResolver
from ..models import Availability
from ..presentation import assume_digital
from ..ranking.candidate import Option, Screening
from ..ranking.spec import GeoPoint, SearchSpec
from ..seating.model import Auditorium, SeatDataUnavailable
from ..service.venues import Venue, local_today
from ..transport import Transport
from .scope import ScopeReporting

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "referer": BASE + "/",
}


class C360Provider(ScopeReporting):
    chain = "c360"

    def __init__(
        self,
        work_resolver: WorkResolver | None = None,
        *,
        company_id: str = APPLE_COMPANY_ID,
        base: str = BASE,
        max_venues: int = 6,
        max_days: int = 7,
        retries: int = 4,
        backoff_s: float = 2.0,
        session: requests.Session | None = None,
        store=None,
    ) -> None:
        self.adapter = C360Schedule()
        self.work_resolver = work_resolver or WorkResolver()
        self.company_id = company_id
        self.base = base
        self.max_venues = max_venues
        self.max_days = max_days
        self.retries = retries
        self.backoff_s = backoff_s
        self.store = store
        self._session = session or requests.Session(impersonate="chrome131")
        self._warmed = False
        self._locations: list[C360Location] | None = None
        self._screens: dict[str, C360Screen] = {}

    # ------------------------------------------------------------------
    def _warm(self) -> None:
        """Cloudflare issues its cookies on the landing page, not the API.

        Calling an endpoint cold gets a challenge; calling it after one
        landing-page load succeeds. This is the whole trick, and it is why the
        session must be reused rather than rebuilt per request.
        """
        if self._warmed:
            return
        for attempt in range(self.retries):
            response = self._session.get(self.base + "/", timeout=30)
            if response.status_code == 200 and "Just a moment" not in response.text:
                self._warmed = True
                return
            time.sleep(self.backoff_s * (attempt + 1))
        raise C360ParseError(
            f"could not warm a session against {self.base} - Cloudflare challenge"
        )

    def _json(self, path: str):
        self._warm()
        response = self._session.get(self.base + path, headers=API_HEADERS, timeout=40)
        if response.status_code != 200:
            raise C360ParseError(f"c360 {path}: HTTP {response.status_code}")
        text = response.text.lstrip()
        if not text.startswith(("[", "{")):
            # The SPA serves its HTML shell for any unmatched route, so a
            # wrong path looks like success. Catching it here turns a silent
            # empty result into a loud one.
            raise C360ParseError(
                f"c360 {path}: got the SPA shell, not JSON - route shape changed"
            )
        return response.json()

    # ------------------------------------------------------------------
    def locations(self) -> list[C360Location]:
        if self._locations is None:
            self._locations = self.adapter.parse_locations(
                self._json(LOCATIONS.format(company=self.company_id))
            )
        return self._locations

    def discover(self, spec: SearchSpec) -> list[Venue]:
        """C360 gives city and state but no coordinates.

        Rather than geocode, venues are published without a point and admitted
        by name or city; `LocationSpec` lets coordinate-less venues through
        when no origin is set, and the store keeps any point learned elsewhere.
        """
        known = self.store.venue_geo(self.chain) if self.store else {}
        out = []
        for loc in self.locations():
            row = known.get(loc.venue_id) or {}
            point = (
                GeoPoint(row["lat"], row["lon"])
                if row.get("lat") is not None else None
            )
            out.append(
                Venue(
                    venue_id=loc.venue_id,
                    name=loc.name,
                    chain=self.chain,
                    tz=loc.tz,
                    point=point,
                    market=loc.location_id,       # the API key, not a market
                )
            )
        return out

    # ------------------------------------------------------------------
    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        self._reset_scope()
        by_id = {loc.venue_id: loc for loc in self.locations()}
        out: list[Screening] = []

        for venue in self._clip_venues(venues):
            loc = by_id.get(venue.venue_id)
            if loc is None:
                continue
            tz = ZoneInfo(loc.tz)
            # The location's own zone is better than the venue record's here,
            # and the window has to be anchored on its date - see `local_today`.
            window = spec.window(local_today(loc.tz))
            days = self._clip_days([
                window.start + timedelta(days=i)
                for i in range((window.end - window.start).days + 1)
            ])
            for day in days:
                payload = self._json(
                    ADVANCE_SHOWS.format(location=loc.location_id, date=day.isoformat())
                )
                for show in self.adapter.parse_shows(payload, loc.location_id):
                    if show.starts_at_local.date() != day:
                        continue
                    screening = self._to_screening(spec, venue, loc, show, tz)
                    if screening is not None:
                        out.append(screening)
        return out

    def _to_screening(self, spec, venue, loc, show: C360Show, tz) -> Screening | None:
        if not show.title:
            return None
        resolution = self.work_resolver.resolve(
            "c360", show.movie_id, show.title, hint_runtime_min=show.runtime_min
        )
        if not resolution.analysis.is_bookable:
            return None

        # `totalSeatsSold` is populated and real; `totalAvailable` is always 0
        # in this feed, so remaining seats come from capacity minus sold - and
        # capacity needs the screen record, which is a phase-B fetch. Coarse
        # ranking therefore only distinguishes "definitely empty" from
        # "unknown", and phase B resolves the rest exactly.
        availability = (
            Availability.SELLABLE if show.seats_sold == 0 else Availability.UNKNOWN
        )

        return Screening(
            screening_id=f"c360:{show.show_id}",
            work=resolution.work,
            venue_id=venue.venue_id,
            venue_name=loc.name,
            chain=self.chain,
            starts_at_utc=show.starts_at_local.replace(tzinfo=tz).astimezone(UTC),
            starts_at_local=show.starts_at_local,
            presentation=assume_digital(self.adapter.classify(show)),
            availability=availability,
            deeplink=show.deeplink(),
            distance_km=venue.distance_km(spec.location.origin),
            screen_id=show.screen_id,
            sources=(self.adapter.source,),
            seats_sold=show.seats_sold,
        )

    # ------------------------------------------------------------------
    def screen(self, screen_id: str) -> C360Screen:
        if screen_id not in self._screens:
            self._screens[screen_id] = self.adapter.parse_screen(
                self._json(SCREEN_BY_ID.format(screen=screen_id))
            )
        return self._screens[screen_id]

    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium:
        """Room shape plus an exact free-seat count - but not a seat grid.

        C360 publishes the auditorium layout and how many seats are sold, and
        stops there. Which individual seats are taken lives behind `HoldSeats`,
        reachable only by creating a hold - a write against their booking
        system, which this project does not do.

        So the Auditorium comes back with `row_lengths` and counts and no
        seats, and the ranker estimates group feasibility instead of drawing a
        map it cannot actually see.
        """
        screening = option.screening
        if not screening.screen_id:
            raise SeatDataUnavailable("c360 showing carries no screen id")

        shape = self.screen(screening.screen_id)
        capacity = shape.bookable_seats
        sold = screening.seats_sold
        if not capacity or sold is None:
            raise SeatDataUnavailable(
                f"no usable seat count for c360 show {screening.screening_id}"
            )
        available = max(capacity - sold, 0)

        return Auditorium(
            venue_id=screening.venue_id,
            screen_id=shape.screen_id,
            seats=(),
            geometry_confidence=0.0,
            reported_available=available,
            reported_capacity=capacity,
            row_lengths=tuple(count for _, count in shape.rows),
            name=shape.name,
        )


