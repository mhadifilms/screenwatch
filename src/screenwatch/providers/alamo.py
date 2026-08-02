"""Alamo provider.

Unlike AMC, Alamo tells you where its cinemas are - the schedule payload
carries coordinates, timezone and status for every venue in a market. So this
provider *discovers* its venues rather than relying on the seed table, which
is why `Provider` grew an optional `discover()` step.

One request per market covers every cinema in it, so the fetch loop is over
markets, not venues. That makes Alamo dramatically cheaper than AMC per
screening returned.
"""

from __future__ import annotations

from curl_cffi import requests

from ..adapters.alamo.schedule import (
    KNOWN_MARKETS,
    AlamoCinema,
    AlamoSchedule,
    AlamoScheduleParseError,
    AlamoSession,
)
from ..identity.resolve import WorkResolver
from ..models import Availability, Presentation
from ..presentation import (
    UnknownFormatError,
    assume_digital,
    classify_token,
    classify_token_fuzzy,
)
from ..ranking.candidate import Option, Screening
from ..ranking.spec import GeoPoint, SearchSpec
from ..seating.model import Auditorium, SeatDataUnavailable
from ..service.venues import Venue, local_today
from ..transport import Transport


class AlamoProvider:
    chain = "alamo"

    def __init__(
        self,
        work_resolver: WorkResolver | None = None,
        *,
        markets: tuple[str, ...] = KNOWN_MARKETS,
        max_markets: int = 4,
        session: requests.Session | None = None,
    ) -> None:
        self.adapter = AlamoSchedule()
        self.work_resolver = work_resolver or WorkResolver()
        self.markets = markets
        self.max_markets = max_markets
        # Its own session: this API is unguarded, so borrowing the shared
        # Queue-it-aware transport would spend that pacing budget pointlessly.
        self._session = session or requests.Session(impersonate="chrome131")
        self._cache: dict[str, tuple[list[AlamoCinema], list[AlamoSession]]] = {}

    # ------------------------------------------------------------------
    def _market(self, market: str):
        if market not in self._cache:
            response = self._session.get(self.adapter.url(market), timeout=30)
            if response.status_code != 200:
                raise AlamoScheduleParseError(
                    f"alamo:{market}: HTTP {response.status_code}"
                )
            self._cache[market] = self.adapter.parse(response.json(), market)
        return self._cache[market]

    def _relevant_markets(self, spec: SearchSpec) -> list[str]:
        """Markets worth fetching, nearest first when an origin is known.

        Costs one request per market, so an unbounded national sweep is not
        on. Ordering by the closest cinema in each market means the budget is
        spent where the user actually is.
        """
        origin = spec.location.origin
        if origin is None:
            return list(self.markets[: self.max_markets])

        scored: list[tuple[float, str]] = []
        for market in self.markets:
            try:
                cinemas, _ = self._market(market)
            except Exception:                                   # noqa: BLE001
                continue
            distances = [
                origin.km_to(GeoPoint(c.lat, c.lon))
                for c in cinemas if c.lat is not None and c.lon is not None
            ]
            if distances:
                scored.append((min(distances), market))
        scored.sort()
        return [m for _, m in scored[: self.max_markets]]

    def discover(self, spec: SearchSpec) -> list[Venue]:
        """Venues this provider knows about, for the directory to filter."""
        out: list[Venue] = []
        for market in self._relevant_markets(spec):
            try:
                cinemas, _ = self._market(market)
            except Exception:                                   # noqa: BLE001
                continue
            out.extend(
                Venue(
                    venue_id=c.venue_id,
                    name=f"Alamo Drafthouse {c.name}",
                    chain=self.chain,
                    tz=c.tz,
                    point=GeoPoint(c.lat, c.lon) if c.lat is not None else None,
                    market=c.market,
                )
                for c in cinemas if c.status == "OPEN"
            )
        return out

    # ------------------------------------------------------------------
    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        wanted = {v.venue_id: v for v in venues}
        if not wanted:
            return []

        out: list[Screening] = []
        windows: dict[str, object] = {}
        for market in {v.market for v in venues if v.market}:
            try:
                cinemas, sessions = self._market(market)
            except Exception:                                   # noqa: BLE001
                continue
            by_id = {c.cinema_id: c for c in cinemas}

            for session in sessions:
                cinema = by_id.get(session.cinema_id)
                if cinema is None or cinema.venue_id not in wanted:
                    continue
                # Each cinema's window is anchored on its own date - a market
                # can straddle zones, and UTC is nobody's - see `local_today`.
                # Cached per zone: a market call returns ~1100 sessions.
                if cinema.tz not in windows:
                    windows[cinema.tz] = spec.window(local_today(cinema.tz))
                if not windows[cinema.tz].contains(session.starts_at_local.date()):
                    continue

                resolution = self.work_resolver.resolve(
                    "alamo", session.presentation_slug, session.title
                )
                if not resolution.analysis.is_bookable:
                    continue

                venue = wanted[cinema.venue_id]
                out.append(
                    Screening(
                        screening_id=f"alamo:{session.session_id}",
                        work=resolution.work,
                        venue_id=cinema.venue_id,
                        venue_name=venue.name,
                        chain=self.chain,
                        starts_at_utc=session.starts_at_utc,
                        starts_at_local=session.starts_at_local,
                        presentation=self._presentation(session, cinema.venue_id),
                        availability=(
                            Availability.SOLD_OUT if session.sold_out
                            else Availability.SELLABLE
                        ),
                        deeplink=session.deeplink(cinema.slug),
                        distance_km=venue.distance_km(spec.location.origin),
                        screen_id=str(session.screen_number or ""),
                        sources=(self.adapter.source,),
                    )
                )
        return out

    def _presentation(self, session: AlamoSession, venue_id: str) -> Presentation:
        """Merge the format slug with the session attributes.

        Alamo splits presentation across two fields - `open-caption` is filed
        as a *format* while `Atmos` is an *attribute* - so both have to be
        folded in or a 70mm Atmos screening loses half its description.
        """
        base = self._classify(session.format_slug, venue_id)
        attrs = set(base.attrs)
        for slug in session.attribute_slugs:
            attrs |= self._classify(slug, venue_id).attrs
        return assume_digital(base.with_(attrs=frozenset(attrs), raw=session.format_slug))

    @staticmethod
    def _classify(token: str, venue_id: str) -> Presentation:
        if not token:
            return Presentation()
        try:
            return classify_token("alamo", token, venue_id=venue_id)
        except UnknownFormatError:
            return classify_token_fuzzy(token)

    # ------------------------------------------------------------------
    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium:
        """Not available.

        Every Alamo session reports `reservedSeating: true`, so seat maps
        exist - but they are not served by the schedule API, and the obvious
        session endpoints reject GET. Raising the specific exception keeps
        Alamo options ranked on availability rather than dropped.
        """
        raise SeatDataUnavailable(
            "Alamo seat maps are not exposed by the public schedule API"
        )
