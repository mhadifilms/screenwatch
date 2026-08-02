"""AMC provider: existing adapters, resolver and venue directory, wired up.

A provider is the seam between a chain's quirks and the uniform pipeline. It
owns fetching, cross-source corroboration, and identity resolution for its
chain, and hands back plain `Screening`s that rank identically to every other
source's.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..adapters.amc.showtimes import AmcShowtimesDom, AmcShowtimesRsc
from ..adapters.base import ParseError
from ..identity.resolve import WorkResolver
from ..models import Observation
from ..presentation import UnknownFormatError, assume_digital
from ..ranking.candidate import Option, Screening
from ..ranking.spec import SearchSpec
from ..resolver import Resolver
from ..seating.model import Auditorium, SeatDataUnavailable
from ..seating.sources.amc import AmcSeatSource
from ..service.venues import Venue
from ..transport import Transport


class AmcProvider:
    chain = "amc"

    def __init__(self, work_resolver: WorkResolver | None = None,
                 *, max_days: int = 7, max_venues: int = 6,
                 seats: AmcSeatSource | None = None) -> None:
        self.seats = seats or AmcSeatSource()
        self.rsc = AmcShowtimesRsc()
        self.dom = AmcShowtimesDom()
        self.fuser = Resolver()
        self.work_resolver = work_resolver or WorkResolver()
        self.max_days = max_days
        self.max_venues = max_venues

    # ------------------------------------------------------------------
    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        today = datetime.now(timezone.utc).date()
        window = spec.window(today)
        days = _days(window.start, window.end, self.max_days)

        out: list[Screening] = []
        for venue in venues[: self.max_venues]:
            if not venue.market:
                continue          # cannot build a showtimes URL without the market
            for day in days:
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
        except Exception:
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

        now = datetime.now(timezone.utc)
        facts = self.fuser.resolve(observations, now=now)
        tz = ZoneInfo(venue.tz) if venue.tz else timezone.utc

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
                )
            )
        return screenings

    # ------------------------------------------------------------------
    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium:
        """Seat grid from AMC's public GraphQL schema.

        Deliberately does not go through `transport`: this endpoint is not
        behind Queue-it and needs no warm session, so borrowing the shared
        transport would spend its pacing budget for nothing.

        A sold-out showing returns no layout at all, so this raises for those.
        Watching a sold-out screening for returns therefore has to key off the
        showtime `status` flipping back, not off seat-level diffs.
        """
        showtime_id = option.screening.screening_id.split(":", 1)[-1]
        return self.seats.fetch(showtime_id, venue_id=option.screening.venue_id)


def _days(start: date, end: date, limit: int) -> list[date]:
    span = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(min(span, limit))]
