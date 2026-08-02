"""Cinemark provider.

Two things are unusual here.

**robots.txt is enforced, not just noted.** Cinemark explicitly disallows
`/tickets/`, `/ticketseatmap/` and `/shoppingcart`. Every fetch goes through
`ROBOTS.check_fetch` so a future change to this file cannot quietly start
crawling those paths, and `fetch_seats` refuses outright rather than
attempting the seat map that lives behind one of them.

**Venue discovery is two-stage and therefore lazy.** The sitemap is
unchallenged and lists 308 theatre slugs, but carries no coordinates - those
only appear on the theatre page itself. Fetching 308 pages to build a
directory would be absurd, so discovery returns slug-only venues and
coordinates are filled in as pages are actually visited. Venues without
coordinates are still admitted by `LocationSpec` when the user names them.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from curl_cffi import requests

from zoneinfo import ZoneInfo

from ..adapters.cinemark.showtimes import (
    SITEMAP,
    CinemarkChallenged,
    CinemarkShowtime,
    CinemarkShowtimes,
    CinemarkTheatre,
    STATE_CENTROID,
    STATE_SLACK_KM,
    classify_print_type,
    state_of,
    timezone_for,
)
from ..identity.resolve import WorkResolver
from ..models import Availability, Presentation
from ..presentation import assume_digital
from ..ranking.candidate import Option, Screening
from ..ranking.spec import GeoPoint, SearchSpec
from ..robots import ROBOTS
from ..seating.model import Auditorium, SeatDataUnavailable
from ..seating.sources.cinemark import CinemarkSeatSource
from ..service.venues import Venue
from ..transport import Transport


class CinemarkProvider:
    chain = "cinemark"

    def __init__(
        self,
        work_resolver: WorkResolver | None = None,
        *,
        max_venues: int = 3,
        max_days: int = 1,
        retries: int = 5,
        backoff_s: float = 1.5,
        session: requests.Session | None = None,
        robots=ROBOTS,
        store=None,
        bootstrap_limit: int = 8,
    ) -> None:
        self.store = store
        self.bootstrap_limit = bootstrap_limit
        self.adapter = CinemarkShowtimes()
        self.seats = CinemarkSeatSource()
        self.work_resolver = work_resolver or WorkResolver()
        self.max_venues = max_venues
        self.max_days = max_days
        self.retries = retries
        self.backoff_s = backoff_s
        self.robots = robots
        self._session = session or requests.Session(impersonate="chrome131")
        self._slugs: list[str] | None = None
        self._theatres: dict[str, CinemarkTheatre] = {}

    # ------------------------------------------------------------------
    def _get(self, url: str) -> str:
        """Fetch, refusing anything robots.txt puts off limits."""
        self.robots.check_fetch(url)
        for attempt in range(self.retries):
            response = self._session.get(url, timeout=30)
            if response.status_code == 200 and "Just a moment" not in response.text:
                return response.text
            time.sleep(self.backoff_s * (attempt + 1))
        raise CinemarkChallenged(
            f"Cloudflare challenge persisted across {self.retries} attempts: {url}"
        )

    def slugs(self) -> list[str]:
        if self._slugs is None:
            self._slugs = self.adapter.theatre_slugs(self._get(SITEMAP))
        return self._slugs

    def discover(self, spec: SearchSpec) -> list[Venue]:
        """Slug-only venues; coordinates arrive as pages get visited.

        The sitemap has no geography, and fetching 308 theatre pages to build
        a directory would cost more than the whole rest of a search. Venues
        already visited keep their coordinates, so a repeated search in the
        same region gets progressively better distance ranking.
        """
        self._load_persisted_geo()
        plausible = self._plausible_slugs(spec)
        self._bootstrap_geo(plausible, spec)

        out: list[Venue] = []
        for slug in plausible:
            venue_id = self._venue_id_for(slug)
            leaf = slug.rsplit("/", 1)[-1]
            known = self._theatres.get(venue_id)
            out.append(
                Venue(
                    venue_id=venue_id,
                    name=known.name if known else leaf.replace("-", " ").title(),
                    chain=self.chain,
                    point=(
                        GeoPoint(known.lat, known.lon)
                        if known and known.lat is not None else None
                    ),
                    market=slug,          # the full path, needed to build the URL
                )
            )
        return out

    def _load_persisted_geo(self) -> None:
        """Coordinates learned in an earlier run, so the bootstrap is once-ever."""
        if self.store is None or self._theatres:
            return
        for venue_id, row in self.store.venue_geo(self.chain).items():
            if row.get("lat") is None:
                continue
            self._theatres[venue_id] = CinemarkTheatre(
                theater_id="", slug=row.get("name") or venue_id,
                name=row.get("name") or venue_id, lat=row["lat"], lon=row["lon"],
            )

    def _bootstrap_geo(self, slugs: list[str], spec: SearchSpec) -> None:
        """Learn coordinates for a few unvisited venues.

        Cinemark hides geography on the theatre page, so without this the
        first search in a region contributes nothing: coordinate-less venues
        are rejected by LocationSpec, so they are never visited, so they never
        gain coordinates. A bounded bootstrap breaks that, and persistence
        means it happens once rather than every run.
        """
        if spec.location.origin is None:
            return
        unknown = [
            slug for slug in slugs
            if self._venue_id_for(slug) not in self._theatres
        ][: self.bootstrap_limit]

        for slug in unknown:
            try:
                html = self._get(self.adapter.theatre_url(slug, spec.window(
                    datetime.now(timezone.utc).date()).start.isoformat()))
                theatre = self.adapter.parse_theatre(html, slug)
            except Exception:                                  # noqa: BLE001
                continue
            self._theatres[theatre.venue_id] = theatre
            if self.store is not None:
                self.store.put_venue_geo(
                    theatre.venue_id, self.chain, theatre.name,
                    theatre.lat, theatre.lon,
                )

    @staticmethod
    def _venue_id_for(slug: str) -> str:
        leaf = slug.rsplit("/", 1)[-1]
        return leaf if leaf.startswith("cinemark-") else f"cinemark-{leaf}"

    def _plausible_slugs(self, spec: SearchSpec) -> list[str]:
        """Narrow 308 slugs to the states that could plausibly be in range.

        Without this, lazy discovery is self-defeating: venues have no
        coordinates until visited, `LocationSpec` rejects coordinate-less
        venues whenever an origin is given, so Cinemark never gets visited and
        never contributes anything. State centroids break that cycle offline.
        """
        origin = spec.location.origin
        slugs = self.slugs()
        if origin is None:
            return slugs

        reach = spec.location.radius_km + STATE_SLACK_KM
        ranked: list[tuple[float, str]] = []
        for slug in slugs:
            centroid = STATE_CENTROID.get(state_of(slug))
            if centroid is None:
                continue
            distance = origin.km_to(GeoPoint(*centroid))
            if distance > reach:
                continue
            # A city name in the spec is worth far more than a state centroid,
            # because Cinemark slugs carry the city verbatim
            # ("tx-san-antonio/..."). Sorting matched cities first is what makes
            # the bounded bootstrap land on venues the user can actually reach.
            city = (spec.location.city or "").lower().replace(" ", "-")
            matched = bool(city) and city in slug
            ranked.append((0.0 if matched else distance, slug))

        ranked.sort()
        near = [slug for _, slug in ranked]
        # Never return nothing on geography alone - a bad centroid guess should
        # degrade to "try anyway", not to silent absence.
        return near or slugs

    # ------------------------------------------------------------------
    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        today = datetime.now(timezone.utc).date()
        window = spec.window(today)
        days = [
            window.start + timedelta(days=i)
            for i in range(min((window.end - window.start).days + 1, self.max_days))
        ]

        out: list[Screening] = []
        for venue in venues[: self.max_venues]:
            if not venue.market:
                continue
            for day in days:
                html = self._get(self.adapter.theatre_url(venue.market, day.isoformat()))
                theatre = self.adapter.parse_theatre(html, venue.market)
                self._theatres[theatre.venue_id] = theatre
                out.extend(self._to_screenings(spec, venue, theatre, html))
        return out

    def _to_screenings(self, spec, venue, theatre, html) -> list[Screening]:
        # Cinemark publishes wall-clock time with no zone; the slug's state
        # prefix supplies it.
        tz = ZoneInfo(timezone_for(theatre.slug))
        point = GeoPoint(theatre.lat, theatre.lon) if theatre.lat is not None else None
        distance = (
            spec.location.origin.km_to(point)
            if point and spec.location.origin else None
        )

        out: list[Screening] = []
        for show in self.adapter.parse_showtimes(html):
            if not show.title:
                continue      # a showtime whose movie model did not render
            resolution = self.work_resolver.resolve(
                "cinemark", show.movie_id, show.title,
                hint_runtime_min=show.runtime_min,
            )
            if not resolution.analysis.is_bookable:
                continue
            out.append(
                Screening(
                    screening_id=f"cinemark:{show.showtime_id}",
                    work=resolution.work,
                    venue_id=theatre.venue_id,
                    venue_name=theatre.name,
                    chain=self.chain,
                    starts_at_utc=show.starts_at_local.replace(tzinfo=tz).astimezone(timezone.utc),
                    starts_at_local=show.starts_at_local,
                    presentation=self._presentation(show),
                    availability=Availability.UNKNOWN,
                    deeplink=self.robots.check_link(show.deeplink()),
                    distance_km=round(distance, 2) if distance is not None else None,
                    sources=(self.adapter.source,),
                )
            )
        return out

    @staticmethod
    def _presentation(show: CinemarkShowtime) -> Presentation:
        presentation, _leftover = classify_print_type(show.print_type)
        return assume_digital(presentation)

    # ------------------------------------------------------------------
    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium:
        """Full per-seat grid from the seat-picker page.

        A plain GET returns every seat with its availability. No login, no
        cart, and crucially no hold - a hold is created by *selecting* a seat,
        which this never does.
        """
        screening = option.screening
        theater_id = self._theater_id_for(screening.venue_id)
        showtime_id = screening.screening_id.split(":", 1)[-1]
        if not theater_id:
            raise SeatDataUnavailable(
                f"unknown Cinemark theater id for {screening.venue_id}"
            )
        html = self._get(self.seats.url(theater_id, showtime_id))
        return self.seats.parse(
            html, venue_id=screening.venue_id, screen_id=screening.screen_id or ""
        )

    def _theater_id_for(self, venue_id: str) -> str | None:
        known = self._theatres.get(venue_id)
        return known.theater_id if known else None
