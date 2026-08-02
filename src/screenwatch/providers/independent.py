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

Between them, Vista and Agile cover a large majority of US art houses without
a line of venue-specific code.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..adapters.base import ParseError
from ..adapters.generic.jsonld import IncompleteStructuredData, JsonLdScreenings
from ..adapters.agile.links import extract as extract_agile, has_agile_links
from ..adapters.vista.links import extract as extract_vista, has_vista_links
from ..identity.resolve import WorkResolver
from ..models import Availability
from ..presentation import assume_digital
from ..ranking.candidate import Option, Screening
from ..ranking.spec import GeoPoint, SearchSpec
from ..seating.model import Auditorium, SeatDataUnavailable
from ..service.venues import Venue
from ..transport import Transport

_DATA = pathlib.Path(__file__).resolve().parents[1] / "data"


def load_venues(path: pathlib.Path | None = None) -> list[dict]:
    path = path or (_DATA / "independent_venues.json")
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("venues", [])


class IndependentProvider:
    chain = "independent"

    def __init__(
        self,
        work_resolver: WorkResolver | None = None,
        *,
        venues: list[dict] | None = None,
        max_venues: int = 6,
    ) -> None:
        self.work_resolver = work_resolver or WorkResolver()
        self._config = venues if venues is not None else load_venues()
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
        today = datetime.now(timezone.utc).date()
        window = spec.window(today)
        by_id = {row["venue_id"]: row for row in self._config}

        out: list[Screening] = []
        for venue in venues[: self.max_venues]:
            row = by_id.get(venue.venue_id)
            if not row or not row.get("url"):
                continue
            try:
                html = transport.get(row["url"]).text
            except Exception:                                   # noqa: BLE001
                continue

            tz = ZoneInfo(venue.tz) if venue.tz else timezone.utc
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
                                                       .astimezone(timezone.utc),
                    starts_at_local=show.starts_at_local,
                    presentation=assume_digital(resolution.analysis.presentation),
                    availability=Availability.UNKNOWN,
                    deeplink=show.url,
                    distance_km=venue.distance_km(spec.location.origin),
                    sources=("vista:links",),
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
                                                       .astimezone(timezone.utc),
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
