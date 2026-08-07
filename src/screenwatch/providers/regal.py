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
from datetime import timedelta

from curl_cffi import requests

from ..adapters.regal.showtimes import (
    DIRECTORY,
    RegalBlocked,
    RegalChallenged,
    RegalPerformance,
    RegalShowtimes,
    RegalTheatre,
    RegalUnavailable,
)
from ..browser import BrowserUnavailable, shared_browser
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
from .scope import WATCH_MAX_DAYS, ScopeReporting

# Statuses where trying again is reasonable. Anything else is a settled
# answer, and sleeping five times before repeating it helps nobody.
_RETRYABLE = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 524})

# Cloudflare's interstitial, in the forms it has actually been served here.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "_cf_chl_opt",
)


def _challenged(body: str) -> bool:
    lowered = (body or "")[:4000].lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


class RegalProvider(ScopeReporting):
    chain = "regal"

    def __init__(
        self,
        work_resolver: WorkResolver | None = None,
        *,
        max_venues: int = 4,
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
        # Regal's page is date-addressable. Keep the same bounded horizon as
        # the other providers, with ticket watches allowed to look farther
        # ahead so a future release cannot disappear behind a 7-day cap.
        self.max_days = 7
        self.max_venues = max_venues
        self.retries = retries
        self.backoff_s = backoff_s
        self._session = session or requests.Session(impersonate="chrome131")
        self._theatres: list[RegalTheatre] | None = None
        # Keep the source's exact title and movie code beside each screening.
        # Search may later replace `Screening.work.title` with a richer title
        # from another provider; Regal's movie route must use Regal's own
        # spelling.
        self._seat_requests: dict[str, RegalPerformance] = {}

    # ------------------------------------------------------------------
    def _get(self, url: str) -> str:
        """Fetch through the intermittent challenge.

        Deliberately a plain bounded retry, not a challenge solver: if
        Cloudflare escalates from "managed challenge" to an interactive one,
        this gives up and says so rather than trying to defeat it.

        The three ways this fails are kept apart, because they need different
        responses and the caller cannot tell them apart from a bare
        "challenged":

        * a **challenge** clears on retry, so it is worth retrying;
        * a **path rule** (403 on /api/*) never clears, so retrying is waste;
        * a **network error** is about us, not them.

        Retrying a 404 or a 500 is equally pointless, so only a challenge and
        the transient 5xx-and-429 family are retried at all.
        """
        last_problem = "no attempt made"
        for attempt in range(self.retries):
            try:
                response = self._session.get(url, timeout=30)
            except Exception as exc:                            # noqa: BLE001
                # curl_cffi raises its own error types; catching the base is
                # deliberate, since a DNS failure and a TLS failure both mean
                # "try again" here.
                last_problem = f"{type(exc).__name__}: {exc}"
            else:
                body = response.text
                if response.status_code == 200 and not _challenged(body):
                    return body
                if _challenged(body):
                    # Checked before the status code, because the challenge is
                    # served *as* a 403 - reading the status first turned every
                    # retryable challenge into a hard failure.
                    last_problem = "Cloudflare challenge"
                elif response.status_code == 403:
                    # A 403 with no challenge markup is a firewall rule. It
                    # will still be there in five seconds.
                    raise RegalBlocked(
                        f"Regal denies this path outright (HTTP 403, no "
                        f"challenge markup): {url}"
                    )
                elif response.status_code not in _RETRYABLE:
                    raise RegalUnavailable(f"HTTP {response.status_code} from {url}")
                else:
                    last_problem = f"HTTP {response.status_code}"

            # No sleep after the final attempt - there is nothing left to wait
            # for, and it added `backoff_s * retries` to every failure.
            if attempt < self.retries - 1:
                time.sleep(self.backoff_s * (attempt + 1))

        raise RegalChallenged(
            f"persisted across {self.retries} attempts "
            f"({last_problem}): {url}"
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

        self._reset_scope()
        self._seat_requests.clear()
        out: list[Screening] = []
        for venue in self._clip_venues(venues):
            theatre = by_id.get(venue.venue_id)
            if theatre is None:
                continue
            window = spec.window(venue.today())   # the venue's date, not UTC's
            day_cap = max(self.max_days, WATCH_MAX_DAYS) if spec.include_sold_out else None
            days = self._clip_days(_days(window.start, window.end), cap=day_cap)
            for day in days:
                # Regal's public query uses US date formatting even though
                # the showtime payload returns ISO local timestamps.
                html = self._get(
                    self.adapter.theatre_url(
                        theatre.path_name, day.strftime("%m-%d-%Y")
                    )
                )
                for perf in self.adapter.parse_showtimes(html):
                    # Some responses include adjacent days. Do not duplicate
                    # a performance when that happens.
                    if perf.starts_at_local.date() != day:
                        continue
                    resolution = self.work_resolver.resolve(
                        "regal", perf.movie_code, perf.title
                    )
                    if not resolution.analysis.is_bookable:
                        continue
                    screening_id = f"regal:{perf.performance_id}"
                    self._seat_requests[screening_id] = perf
                    out.append(
                        Screening(
                            screening_id=screening_id,
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
                            deeplink=perf.deeplink(theatre.path_name),
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
        """Read the rendered seat grid from Regal's public movie page.

        This is the exact route the theatre page opens when a user clicks a
        showtime. It is a browser visit because the seat grid is produced by
        React after load; no seat is clicked and no hold is created.
        """
        screening = option.screening
        theatre = self._theatre_code_for(screening.venue_id)
        if not theatre:
            raise SeatDataUnavailable(
                f"unknown Regal theatre code for {screening.venue_id}"
            )
        session_id = screening.screening_id.split(":", 1)[-1]
        perf = self._seat_requests.get(screening.screening_id)
        if perf is None:
            raise SeatDataUnavailable(
                f"Regal performance metadata missing for {screening.screening_id}; "
                "run a fresh search before fetching seats"
            )
        url = self.seats.movie_url(
            title=perf.title,
            movie_code=perf.movie_code,
            date=perf.starts_at_local.date().isoformat(),
            theatre_code=theatre,
            performance_id=session_id,
        )

        try:
            browser = self._browser or shared_browser()
            response = browser.visit(
                url,
                wait_for_selector="button[id^='seat-']",
                wait_timeout_ms=12_000,
            )
        except BrowserUnavailable as exc:
            raise SeatDataUnavailable(f"browser transport unavailable: {exc}") from exc
        except Exception as exc:
            raise SeatDataUnavailable(
                f"Regal seat page via browser failed: {type(exc).__name__}: {exc}"
            ) from exc

        if response.blocked:
            raise SeatDataUnavailable(
                f"Regal seat page blocked by Cloudflare (HTTP {response.status})"
            )
        if response.challenged or response.status >= 400:
            raise SeatDataUnavailable(
                f"Regal seat page unavailable (HTTP {response.status})"
            )
        return self.seats.parse(
            response.text,
            venue_id=screening.venue_id,
            screen_id=screening.screen_id or "",
        )

    def _theatre_code_for(self, venue_id: str) -> str | None:
        return next(
            (t.theatre_code for t in self.theatres() if t.venue_id == venue_id), None
        )


def _days(start, end) -> list:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
