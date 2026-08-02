"""Regal provider.

The distinctive problem here is that Cloudflare's challenge is *intermittent*.
A single 403 means nothing; the same URL usually succeeds on the second or
third try. So every fetch goes through a bounded retry, and a challenge that
survives it is reported as a provider error rather than silently reducing the
result set - a chain that quietly returns nothing looks exactly like a chain
with nothing on.

Venue discovery is one page load: `graph.regmovies.com` renders the homepage
for any path, and that homepage carries all 402 theatres with coordinates.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from curl_cffi import requests

from ..adapters.regal.showtimes import (
    DIRECTORY,
    RegalChallenged,
    RegalPerformance,
    RegalShowtimes,
    RegalTheatre,
)
from ..identity.resolve import WorkResolver
from ..models import Availability, Brand, Presentation, Projection
from ..presentation import (
    UnknownFormatError,
    assume_digital,
    classify_token,
    classify_token_fuzzy,
)
from ..ranking.candidate import Option, Screening
from ..ranking.spec import GeoPoint, SearchSpec
from ..seating.model import Auditorium, SeatDataUnavailable
from ..seating.sources.regal import BOOKING_API, RegalSeatSource
from ..service.venues import Venue
from ..transport import Transport


class RegalProvider:
    chain = "regal"

    def __init__(
        self,
        work_resolver: WorkResolver | None = None,
        *,
        max_venues: int = 4,
        max_days: int = 1,
        retries: int = 5,
        backoff_s: float = 1.5,
        session: requests.Session | None = None,
        booking_api: str = BOOKING_API,
        browser=None,
    ) -> None:
        self.adapter = RegalShowtimes()
        self.seats = RegalSeatSource()
        self.booking_api = booking_api
        self._browser = browser
        self.work_resolver = work_resolver or WorkResolver()
        self.max_venues = max_venues
        self.max_days = max_days
        self.retries = retries
        self.backoff_s = backoff_s
        self._session = session or requests.Session(impersonate="chrome131")
        self._theatres: list[RegalTheatre] | None = None

    # ------------------------------------------------------------------
    def _get(self, url: str) -> str:
        """Fetch through the intermittent challenge.

        Deliberately a plain bounded retry, not a challenge solver: if
        Cloudflare escalates from "managed challenge" to an interactive one,
        this gives up and says so rather than trying to defeat it.
        """
        last = ""
        for attempt in range(self.retries):
            response = self._session.get(url, timeout=30)
            last = response.text
            if response.status_code == 200 and "Just a moment" not in last:
                return last
            time.sleep(self.backoff_s * (attempt + 1))
        raise RegalChallenged(
            f"Cloudflare challenge persisted across {self.retries} attempts: {url}"
        )

    def theatres(self) -> list[RegalTheatre]:
        if self._theatres is None:
            self._theatres = self.adapter.parse_theatres(self._get(DIRECTORY))
        return self._theatres

    def discover(self, spec: SearchSpec) -> list[Venue]:
        return [
            Venue(
                venue_id=t.venue_id,
                name=t.name,
                chain=self.chain,
                tz=t.tz,
                point=GeoPoint(t.lat, t.lon) if t.lat is not None else None,
                market=t.path_name,          # the URL segment, not a real market
            )
            for t in self.theatres()
        ]

    # ------------------------------------------------------------------
    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        by_id = {t.venue_id: t for t in self.theatres()}
        today = datetime.now(timezone.utc).date()
        window = spec.window(today)

        out: list[Screening] = []
        for venue in venues[: self.max_venues]:
            theatre = by_id.get(venue.venue_id)
            if theatre is None:
                continue
            html = self._get(self.adapter.theatre_url(theatre.path_name))
            for perf in self.adapter.parse_showtimes(html):
                if not window.contains(perf.starts_at_local.date()):
                    continue
                resolution = self.work_resolver.resolve(
                    "regal", perf.movie_code, perf.title
                )
                if not resolution.analysis.is_bookable:
                    continue
                out.append(
                    Screening(
                        screening_id=f"regal:{perf.performance_id}",
                        work=resolution.work,
                        venue_id=venue.venue_id,
                        venue_name=venue.name,
                        chain=self.chain,
                        starts_at_utc=perf.starts_at_utc,
                        starts_at_local=perf.starts_at_local,
                        presentation=self._presentation(perf, venue.venue_id),
                        availability=(
                            Availability.SOLD_OUT if perf.sold_out
                            else Availability.SELLABLE
                        ),
                        deeplink=perf.deeplink(),
                        distance_km=venue.distance_km(spec.location.origin),
                        screen_id=perf.auditorium,
                        sources=(self.adapter.source,),
                    )
                )
        return out

    def _presentation(self, perf: RegalPerformance, venue_id: str) -> Presentation:
        """Fold a flat attribute list into one Presentation.

        Regal mixes presentation, accessibility and ticketing policy into the
        same list, so the projection/brand from the strongest token is kept
        while every token's attributes are unioned in.
        """
        base = Presentation()
        attrs: set = set()
        for token in perf.attributes:
            piece = self._classify(token, venue_id)
            attrs |= piece.attrs
            if base.projection is Projection.UNKNOWN and piece.projection is not Projection.UNKNOWN:
                base = base.with_(projection=piece.projection)
            if base.brand is Brand.NONE and piece.brand is not Brand.NONE:
                base = base.with_(brand=piece.brand)
        return assume_digital(
            base.with_(attrs=frozenset(attrs), raw=" ".join(perf.attributes))
        )

    @staticmethod
    def _classify(token: str, venue_id: str) -> Presentation:
        try:
            return classify_token("regal", token, venue_id=venue_id)
        except UnknownFormatError:
            return classify_token_fuzzy(token)

    # ------------------------------------------------------------------
    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium:
        """Not available.

        `SeatAllocationType: "2"` says seats are reserved, so a layout exists,
        but it is not in the hydration blob and the ticketing flow behind it
        is the part Cloudflare guards hardest.
        """
        raise SeatDataUnavailable(
            "Regal seat maps are not in the page payload; the ticketing flow "
            "behind them is challenge-guarded"
        )
