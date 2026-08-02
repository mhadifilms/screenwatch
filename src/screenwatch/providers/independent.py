"""Independent venues, via schema.org markup.

The long-tail provider. Art houses and single-screen cinemas have no shared
backend, but many publish `ScreeningEvent` markup because SEO demands it, and
nobody bot-protects their own SEO. One adapter covers all of them.

Venues are configured in `data/independent_venues.json` rather than
discovered: there is no registry of independent cinemas to crawl, and a
curated list of the ones a user actually cares about is both cheaper and more
honest than pretending to enumerate them.

Reality check, measured: a meaningful fraction publish markup that validates
but carries no usable `startDate`. Film Forum is the canonical example - 56
`ScreeningEvent` nodes, every one with `"startDate": ""`. Those raise
`IncompleteStructuredData`, which this provider surfaces as a named error
rather than an empty result, because "this venue needs an HTML fallback" and
"this venue is dark tonight" must never look the same.

So there are two strategies, tried in order:

  1. schema.org `ScreeningEvent` markup;
  2. **Vista ticket links** - `visSelectTickets.aspx?cinemacode=&txtSessionId=`
     anchors that Vista-backed venues embed beside each showtime. Metrograph
     yields 183 showtimes across 20 dates this way, with no per-venue parser.
  3. **Agile WebSales links** - `ticketsearchcriteria.aspx?evtinfo=` anchors,
     the other big art-house engine. These additionally carry a real sales
     state and the screen name. The Coolidge yields 24 showtimes including
     "The Odyssey in 70mm" on screen MH1 - the rep-house film-print case the
     whole presentation model exists for.
  4. **Own-site listings** - venues on no shared platform at all. What they
     still share is a link whose text is a time. Roxie: 34. Music Box: 12.

Some venues sit behind a JS interstitial (Music Box uses Sucuri, which serves
1.3KB of obfuscated JavaScript to a plain client). Those are marked
`fetch: browser` in the config and read through the browser transport.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..adapters.agile.links import extract as extract_agile
from ..adapters.agile.links import has_agile_links
from ..adapters.base import ParseError
from ..adapters.generic.jsonld import IncompleteStructuredData, JsonLdScreenings
from ..adapters.generic.listing import extract as extract_listing
from ..adapters.vista.links import extract as extract_vista
from ..adapters.vista.links import has_vista_links
from ..browser import BrowserUnavailable, shared_browser
from ..identity.resolve import WorkResolver
from ..models import Availability
from ..presentation import assume_digital
from ..ranking.candidate import Option, Screening
from ..ranking.spec import GeoPoint, SearchSpec
from ..seating.model import Auditorium, SeatDataUnavailable
from ..service.venues import Venue
from ..transport import Transport
from .scope import ScopeReporting

_DATA = pathlib.Path(__file__).resolve().parents[1] / "data"


def load_venues(path: pathlib.Path | None = None) -> list[dict]:
    path = path or (_DATA / "independent_venues.json")
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("venues", [])


class IndependentProvider(ScopeReporting):
    chain = "independent"

    def __init__(
        self,
        work_resolver: WorkResolver | None = None,
        *,
        venues: list[dict] | None = None,
        max_venues: int | None = None,
    ) -> None:
        self.work_resolver = work_resolver or WorkResolver()
        self._config = venues if venues is not None else load_venues()
        # Uncapped by default. The list is curated by hand rather than
        # crawled, so every venue in it is one the user asked for; a default of
        # six silently ignored the seventh.
        self.max_venues = max_venues
        self.incomplete: dict[str, str] = {}

    # ------------------------------------------------------------------
    def discover(self, spec: SearchSpec) -> list[Venue]:
        return [
            Venue(
                venue_id=row["venue_id"],
                name=row.get("name", row["venue_id"]),
                chain=self.chain,
                tz=row.get("tz"),
                point=(
                    GeoPoint(row["lat"], row["lon"])
                    if row.get("lat") is not None else None
                ),
                market=row.get("url"),          # the page to read, not a market
            )
            for row in self._config
        ]

    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        by_id = {row["venue_id"]: row for row in self._config}

        self._reset_scope()
        out: list[Screening] = []
        for venue in self._clip_venues(venues):
            row = by_id.get(venue.venue_id)
            if not row or not row.get("url"):
                continue
            try:
                html = self._fetch(row, transport)
            except Exception:                                   # noqa: BLE001
                continue

            # "Today" is the venue's today, not UTC's.
            #
            # A page that omits the date - most own-site listings - has its
            # showtimes stamped with `today`, and the search window is measured
            # from the same day. Taking that from UTC put a San Francisco venue
            # a day ahead for the seven hours after 5pm local, so the evening's
            # showings were dated tomorrow and a "tonight" search returned
            # nothing at exactly the time someone would run it.
            tz = ZoneInfo(venue.tz) if venue.tz else UTC
            today = datetime.now(tz).date()
            window = spec.window(today)
            adapter = JsonLdScreenings(venue.venue_id, url=row["url"])
            observations = []
            try:
                observations = adapter.parse(html, strict=False)
            except IncompleteStructuredData as exc:
                # Named, not swallowed: decorative markup needs a fallback,
                # which is a different problem from a quiet night.
                self.incomplete[venue.venue_id] = str(exc)
            except ParseError:
                pass
            except Exception:                                   # noqa: BLE001
                pass

            if not observations and has_vista_links(html):
                out.extend(self._from_vista(spec, venue, html, tz, window, today))
                continue
            if not observations and has_agile_links(html):
                out.extend(self._from_agile(spec, venue, html, tz, window, today))
                continue
            if not observations:
                found = self._from_listing(spec, venue, html, tz, window, today,
                                           row["url"])
                if found:
                    out.extend(found)
                    continue
            for obs in observations:
                local = obs.key.starts_at_utc.astimezone(tz).replace(tzinfo=None)
                if not window.contains(local.date()):
                    continue
                resolution = self.work_resolver.resolve(
                    venue.venue_id, obs.key.movie_id, obs.title or obs.key.movie_id
                )
                if not resolution.analysis.is_bookable:
                    continue
                out.append(
                    Screening(
                        screening_id=f"{venue.venue_id}:{obs.key.movie_id}"
                                     f":{int(obs.key.starts_at_utc.timestamp())}",
                        work=resolution.work,
                        venue_id=venue.venue_id,
                        venue_name=venue.name,
                        chain=self.chain,
                        starts_at_utc=obs.key.starts_at_utc,
                        starts_at_local=local,
                        presentation=assume_digital(obs.presentation),
                        availability=obs.availability,
                        deeplink=obs.deeplink,
                        distance_km=venue.distance_km(spec.location.origin),
                        sources=(obs.source,),
                    )
                )
        return out

    def _from_vista(self, spec, venue, html, tz, window, today) -> list[Screening]:
        """Reconstruct listings from embedded Vista ticket links."""
        out: list[Screening] = []
        for show in extract_vista(html, default_date=today):
            if not window.contains(show.starts_at_local.date()):
                continue
            resolution = self.work_resolver.resolve(
                venue.venue_id, show.title, show.title
            )
            if not resolution.analysis.is_bookable:
                continue
            out.append(
                Screening(
                    screening_id=f"vista:{show.screening_key}",
                    work=resolution.work,
                    venue_id=venue.venue_id,
                    venue_name=venue.name,
                    chain=self.chain,
                    starts_at_utc=show.starts_at_local.replace(tzinfo=tz)
                                                       .astimezone(UTC),
                    starts_at_local=show.starts_at_local,
                    presentation=assume_digital(resolution.analysis.presentation),
                    availability=Availability.UNKNOWN,
                    deeplink=show.url,
                    distance_km=venue.distance_km(spec.location.origin),
                    sources=("vista:links",),
                )
            )
        return out

    def _fetch(self, row: dict, transport: Transport) -> str:
        """Read a venue page, through a browser when the config says so.

        Sucuri and friends serve a JavaScript interstitial to plain clients -
        Music Box returns 1.3KB of obfuscated script instead of its listings.
        """
        if row.get("fetch") == "browser":
            try:
                return shared_browser().text(row["url"])
            except BrowserUnavailable:
                return transport.get(row["url"]).text
        return transport.get(row["url"]).text

    def _from_listing(self, spec, venue, html, tz, window, today, base_url) -> list[Screening]:
        """Reconstruct listings from a venue's own clickable showtimes."""
        out: list[Screening] = []
        for show in extract_listing(html, default_date=today, base_url=base_url):
            if not window.contains(show.starts_at_local.date()):
                continue
            resolution = self.work_resolver.resolve(
                venue.venue_id, show.title, show.title
            )
            if not resolution.analysis.is_bookable:
                continue
            out.append(
                Screening(
                    screening_id=f"listing:{venue.venue_id}:"
                                 f"{int(show.starts_at_local.timestamp())}:"
                                 f"{resolution.analysis.match_key}",
                    work=resolution.work,
                    venue_id=venue.venue_id,
                    venue_name=venue.name,
                    chain=self.chain,
                    starts_at_utc=show.starts_at_local.replace(tzinfo=tz)
                                                       .astimezone(UTC),
                    starts_at_local=show.starts_at_local,
                    presentation=assume_digital(resolution.analysis.presentation),
                    availability=Availability.UNKNOWN,
                    deeplink=show.url,
                    distance_km=venue.distance_km(spec.location.origin),
                    sources=("listing:own-site",),
                )
            )
        return out

    def _from_agile(self, spec, venue, html, tz, window, today) -> list[Screening]:
        """Reconstruct listings from Agile WebSales links.

        Richer than Vista: a real sales state and the screen name come along
        for free, so sold-out showings are known rather than assumed.
        """
        out: list[Screening] = []
        for show in extract_agile(html, default_date=today):
            if not window.contains(show.starts_at_local.date()):
                continue
            if show.closed:
                continue          # sales ended or already screened
            resolution = self.work_resolver.resolve(
                venue.venue_id, show.event_id, show.title
            )
            if not resolution.analysis.is_bookable:
                continue
            out.append(
                Screening(
                    screening_id=f"agile:{show.host}:{show.event_id}",
                    work=resolution.work,
                    venue_id=venue.venue_id,
                    venue_name=venue.name,
                    chain=self.chain,
                    starts_at_utc=show.starts_at_local.replace(tzinfo=tz)
                                                       .astimezone(UTC),
                    starts_at_local=show.starts_at_local,
                    presentation=assume_digital(resolution.analysis.presentation),
                    availability=(
                        Availability.SOLD_OUT if show.sold_out
                        else Availability.SELLABLE if show.on_sale
                        else Availability.UNKNOWN
                    ),
                    deeplink=show.url,
                    distance_km=venue.distance_km(spec.location.origin),
                    screen_id=show.screen or "",
                    sources=("agile:links",),
                )
            )
        return out

    # ------------------------------------------------------------------
    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium:
        """Independents almost never publish seat data.

        Many are unreserved seating entirely, and the ones that do reserve run
        a ticketing platform - Elevent, Agile, Veezi - whose adapter is the
        right place for seats, not this generic one.
        """
        raise SeatDataUnavailable(
            "schema.org markup carries no seat data; a platform adapter is "
            "needed for reserved-seating independents"
        )
