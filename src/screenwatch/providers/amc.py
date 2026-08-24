"""AMC provider: existing adapters, resolver and venue directory, wired up.

A provider is the seam between a chain's quirks and the uniform pipeline. It
owns fetching, cross-source corroboration, and identity resolution for its
chain, and hands back plain `Screening`s that rank identically to every other
source's.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..adapters.amc.showtimes import AmcShowtimesDom, AmcShowtimesRsc
from ..adapters.amc.sitemap import AmcSitemap
from ..adapters.base import ParseError
from ..adapters.cinemark.showtimes import STATE_TZ
from ..identity.resolve import WorkResolver
from ..models import Observation
from ..presentation import UnknownFormatError, assume_digital
from ..ranking.candidate import Option, Screening
from ..ranking.spec import GeoPoint, SearchSpec
from ..resolver import Resolver
from ..seating.capture import SeatCapture, SeatProbe
from ..seating.model import Auditorium, SeatDataUnavailable
from ..seating.sources.amc import AmcSeatSource
from ..service.venues import Venue
from ..transport import Transport
from .scope import WATCH_MAX_DAYS, ScopeReporting


class AmcProvider(ScopeReporting):
    chain = "amc"

    def __init__(self, work_resolver: WorkResolver | None = None,
                 *, max_days: int = 7, max_venues: int = 6,
                 seats: AmcSeatSource | None = None) -> None:
        self.seats = seats or AmcSeatSource()
        self.rsc = AmcShowtimesRsc()
        self.dom = AmcShowtimesDom()
        self.sitemap = AmcSitemap()
        self.fuser = Resolver()
        self.work_resolver = work_resolver or WorkResolver()
        self.max_days = max_days
        self.max_venues = max_venues
        self._theatre_ids: dict[str, str] = {}

    def discover(self, spec: SearchSpec) -> list[Venue]:
        """Discover the national AMC directory from AMC's official sitemap.

        This is intentionally cheap compared with showtime discovery: one
        sitemap request returns venue existence, stable theatre ids, routing
        slugs, and coordinates for the whole chain. Geography is filtered by
        ``VenueDirectory`` after this method returns, so the source-backed
        graph is national even when an individual search is local.
        """
        entries = self.sitemap.parse_theatres(
            self.sitemap.fetch(self._transport_for_discovery(), which="theatres")
        )
        self._theatre_ids.update(
            {entry.venue_id: entry.theatre_id for entry in entries}
        )
        return [
            Venue(
                venue_id=entry.venue_id,
                name=entry.name,
                chain=self.chain,
                tz=STATE_TZ.get((entry.state or "").lower(), "America/Chicago"),
                point=(
                    GeoPoint(entry.latitude, entry.longitude)
                    if entry.latitude is not None and entry.longitude is not None
                    else None
                ),
                market=entry.market,
                city=entry.city,
                state=entry.state,
                url=entry.url,
                source="amc:sitemap-theatres",
                source_url=entry.url,
            )
            for entry in entries
        ]

    def _transport_for_discovery(self) -> Transport:
        """Use the service transport when one has been injected.

        ``discover`` is called without a transport argument by the service;
        keeping this tiny lazy accessor preserves the provider protocol and
        avoids constructing a Queue-it session for fixture-only callers.
        """
        if not hasattr(self, "_discovery_transport"):
            self._discovery_transport = Transport()
        return self._discovery_transport

    # ------------------------------------------------------------------
    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        self._reset_scope()
        out: list[Screening] = []
        for venue in self._clip_venues(venues, exhaustive=spec.exhaustive):
            if not venue.market:
                continue          # cannot build a showtimes URL without the market
            # Anchored on the venue's own date, not UTC's - see `local_today`.
            window = spec.window(venue.today())
            day_cap = (
                None
                if spec.exhaustive
                else max(self.max_days, WATCH_MAX_DAYS) if spec.include_sold_out else None
            )
            for day in self._clip_days(
                _days(window.start, window.end),
                cap=day_cap,
                exhaustive=spec.exhaustive,
            ):
                out.extend(self._one_day(spec, venue, day, transport))
        return out

    def _one_day(
        self, spec: SearchSpec, venue: Venue, day: date, transport: Transport
    ) -> list[Screening]:
        try:
            html = self.rsc.fetch(
                transport, venue_id=venue.venue_id, market=venue.market,
                date=day.isoformat(),
            )
        except Exception as exc:                            # noqa: BLE001
            self._note_error(
                f"venue {venue.venue_id} {day.isoformat()} fetch failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return []

        observations: list[Observation] = []
        for adapter, kwargs in ((self.rsc, {}), (self.dom, {"venue_id": venue.venue_id})):
            try:
                observations.extend(adapter.parse(html, strict=False, **kwargs))
            except (ParseError, UnknownFormatError):
                # One parser drifting must not blind us to the other - that is
                # the entire reason both exist.
                continue

        if not observations:
            return []

        now = datetime.now(UTC)
        facts = self.fuser.resolve(observations, now=now)
        tz = ZoneInfo(venue.tz) if venue.tz else UTC

        screenings = []
        for fact in facts:
            resolution = self.work_resolver.resolve(
                "amc", fact.key.movie_id, fact.title or fact.key.movie_id
            )
            if not resolution.analysis.is_bookable:
                continue          # private rentals are not seats you can buy
            screenings.append(
                Screening(
                    screening_id=f"amc:{fact.external_id}",
                    work=resolution.work,
                    venue_id=venue.venue_id,
                    venue_name=venue.name,
                    chain=self.chain,
                    starts_at_utc=fact.key.starts_at_utc,
                    starts_at_local=fact.key.starts_at_utc.astimezone(tz).replace(tzinfo=None),
                    presentation=assume_digital(fact.presentation),
                    availability=fact.availability,
                    deeplink=fact.deeplink,
                    distance_km=venue.distance_km(spec.location.origin),
                    sources=fact.sources,
                    seat_probe=SeatProbe(
                        source=self.chain,
                        venue_id=venue.venue_id,
                        source_venue_id=(
                            self._theatre_ids.get(venue.venue_id) or venue.venue_id
                        ),
                        showtime_id=str(fact.external_id),
                        booking_url=fact.deeplink,
                        starts_at_local=fact.key.starts_at_utc.astimezone(tz)
                                                             .replace(tzinfo=None),
                        title=fact.title,
                        source_screen_id=None,
                        metadata={
                            "screening_id": f"amc:{fact.external_id}",
                            "venue_name": venue.name,
                        },
                    ),
                )
            )
        return screenings

    # ------------------------------------------------------------------
    def fetch(self, probe: SeatProbe, transport: Transport) -> SeatCapture:
        """Seat grid from AMC's public GraphQL schema.

        Deliberately does not go through `transport`: this endpoint is not
        behind Queue-it and needs no warm session, so borrowing the shared
        transport would spend its pacing budget for nothing.

        A sold-out showing returns no layout at all, so this raises for those.
        Watching a sold-out screening for returns therefore has to key off the
        showtime `status` flipping back, not off seat-level diffs.
        """
        payload = self.seats._post(int(probe.showtime_id), 30)
        raw_payload = json.dumps(payload, sort_keys=True).encode()
        try:
            room = self.seats.parse(
                payload, showtime_id=probe.showtime_id, venue_id=probe.venue_id
            )
        except SeatDataUnavailable as exc:
            exc.with_capture(
                raw_payload,
                content_type="application/json",
                source_url=self.seats.endpoint,
                status_code=200,
            )
            raise
        return SeatCapture(
            probe=probe,
            auditorium=room,
            raw_payload=raw_payload,
            raw_content_type="application/json",
            source_url=self.seats.endpoint,
            status_code=200,
        )

    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium:
        """Compatibility wrapper for callers that still hold an Option."""
        return self.fetch(option.screening.durable_seat_probe(), transport).auditorium


def _days(start: date, end: date) -> list[date]:
    """Every day in the window. Capping is `ScopeReporting._clip_days`' job,
    so that the days dropped can be reported rather than silently absent."""
    span = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(span)]
