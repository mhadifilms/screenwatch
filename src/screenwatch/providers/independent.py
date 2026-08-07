"""Independent venues, via schema.org markup.

The long-tail provider. Art houses and single-screen cinemas have no shared
backend, but many publish `ScreeningEvent` markup because SEO demands it, and
nobody bot-protects their own SEO. One adapter covers all of them.

The national venue directory comes from OpenStreetMap's `amenity=cinema`
records, queried through Overpass and cached in the local directory. The
configuration file remains useful as a routing and parser-override registry
for venues whose websites need a known adapter, but it is not the coverage
boundary.

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
import urllib.parse
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..adapters.agile.links import extract as extract_agile
from ..adapters.agile.links import has_agile_links
from ..adapters.base import ParseError
from ..adapters.cinemark.showtimes import STATE_TZ
from ..adapters.generic.jsonld import IncompleteStructuredData, JsonLdScreenings
from ..adapters.generic.listing import extract as extract_listing
from ..adapters.openstreetmap.cinemas import OsmCinema, OsmCinemaDirectory
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

_STATE_NAMES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd",
    "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
    "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}
_CHAIN_MARKERS = (
    "amc ", "regal ", "cinemark", "alamo drafthouse", "apple cinemas",
    "studio movie grill", "harkins", "marcus theatres", "showplace icon",
    "landmark theatres", "angelika film center", "bow tie cinemas", "b&b theatres",
    "gqt movies", "mjr theatres", "silverspot cinema", "look cinemas",
    "reading cinemas", "hoyts", "cinepolis", "fandango at home",
)


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
        max_venues: int | None = 100,
        store=None,
        osm_directory: OsmCinemaDirectory | None = None,
        discovery_transport: Transport | None = None,
        osm_enabled: bool = True,
    ) -> None:
        self.work_resolver = work_resolver or WorkResolver()
        self._config = venues if venues is not None else load_venues()
        self.max_venues = max_venues
        self.store = store
        self.osm = osm_directory or OsmCinemaDirectory()
        self._discovery_transport = discovery_transport
        self.osm_enabled = osm_enabled
        self._osm_rows: dict[str, dict] = {}
        self.incomplete: dict[str, str] = {}

    # ------------------------------------------------------------------
    def discover(self, spec: SearchSpec, *, full: bool = False) -> list[Venue]:
        self._load_cached_osm_rows()
        rows = {
            row["venue_id"]: {
                **row,
                "source": "independent-registry",
                "source_url": row.get("url"),
            }
            for row in self._config
        }
        for row in self._osm_rows.values():
            key, merged = _merge_osm_row(rows, row)
            rows[key] = merged
        if self.osm_enabled:
            try:
                cinemas = self._discover_osm(spec, full=full or spec.exhaustive)
                for cinema in cinemas:
                    row = self._osm_row(cinema)
                    self._osm_rows[row["venue_id"]] = row
                    key, merged = _merge_osm_row(rows, row)
                    rows[key] = merged
            except Exception as exc:  # noqa: BLE001
                self._note_error(
                    f"OSM discovery failed: {type(exc).__name__}: {exc}"
                )
        self._venue_config = rows
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
                url=row.get("url"),
                venue_type=row.get("venue_type") or "cinema",
                markup=row.get("markup"),
                notes=row.get("notes"),
                source=row.get("source", "independent-registry"),
                source_url=row.get("source_url") or row.get("url"),
            )
            for row in rows.values()
        ]

    def _transport_for_discovery(self) -> Transport:
        if self._discovery_transport is None:
            self._discovery_transport = Transport(
                min_interval_s=1.0,
                user_agent=(
                    "screenwatch/1.0 (+https://github.com/mhadifilms/screenwatch)"
                ),
            )
        return self._discovery_transport

    def _discover_osm(self, spec: SearchSpec, *, full: bool) -> list[OsmCinema]:
        if not full and spec.location.origin is None:
            return [self._osm_cinema_from_row(row) for row in self._osm_rows.values()]
        transport = self._transport_for_discovery()
        if full:
            cinemas = self.osm.national(transport)
        else:
            cinemas = self.osm.nearby(
                transport,
                spec.location.origin,
                spec.location.radius_km,
            )
        return [cinema for cinema in cinemas if not _looks_like_chain(cinema)]

    @staticmethod
    def _osm_cinema_from_row(row: dict) -> OsmCinema:
        osm_type, _, osm_id = row["venue_id"].rpartition("-")
        try:
            numeric_id = int(osm_id)
        except ValueError:
            numeric_id = 0
        return OsmCinema(
            osm_type=osm_type.rsplit("-", 1)[-1] or "node",
            osm_id=numeric_id,
            name=row["name"],
            latitude=float(row["lat"]),
            longitude=float(row["lon"]),
            website=row.get("url"),
            city=row.get("city"),
            state=row.get("state"),
            postal_code=row.get("postal_code"),
            address=row.get("address"),
        )

    @staticmethod
    def _osm_row(cinema: OsmCinema) -> dict:
        details = [
            f"Address: {cinema.address}" if cinema.address else None,
            f"Operator: {cinema.operator}" if cinema.operator else None,
            f"Wikidata: {cinema.wikidata}" if cinema.wikidata else None,
        ]
        return {
            "venue_id": cinema.venue_id,
            "name": cinema.name,
            "url": cinema.website,
            "lat": cinema.latitude,
            "lon": cinema.longitude,
            "tz": _timezone_for_state(cinema.state),
            "city": cinema.city,
            "state": cinema.state,
            "postal_code": cinema.postal_code,
            "address": cinema.address,
            "operator": cinema.operator,
            "markup": "unknown",
            "notes": (
                "Directory metadata from OpenStreetMap. Online showtimes are "
                "attempted only when the mapped venue publishes a website. "
                + " ".join(detail for detail in details if detail)
            ),
            "source": "osm:overpass",
            "source_url": cinema.osm_url,
        }

    def _load_cached_osm_rows(self) -> None:
        if self.store is None:
            return
        for row in self.store.directory_venues():
            if row.get("source") != "osm:overpass" or row.get("venue_id") in self._osm_rows:
                continue
            if row.get("lat") is None or row.get("lon") is None:
                continue
            self._osm_rows[row["venue_id"]] = {
                **row,
                "source": "osm:overpass",
                "source_url": row.get("source_url"),
            }

    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]:
        by_id = getattr(self, "_venue_config", None) or {
            row["venue_id"]: row for row in self._config
        }

        self._reset_scope()
        out: list[Screening] = []
        for venue in self._clip_venues(venues, exhaustive=spec.exhaustive):
            row = by_id.get(venue.venue_id)
            if not row or not row.get("url"):
                continue
            try:
                html = self._fetch(row, transport)
            except Exception as exc:                            # noqa: BLE001
                self._note_error(
                    f"venue {venue.venue_id} fetch failed: {type(exc).__name__}: {exc}"
                )
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
                self._note_error(
                    f"venue {venue.venue_id} incomplete structured data: {exc}"
                )
            except ParseError:
                self._note_error(f"venue {venue.venue_id} structured data parse failed")
            except Exception as exc:                            # noqa: BLE001
                self._note_error(
                    f"venue {venue.venue_id} parser failed: {type(exc).__name__}: {exc}"
                )

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


def _looks_like_chain(cinema: OsmCinema) -> bool:
    haystack = f"{cinema.name} {cinema.operator or ''}".casefold()
    return any(marker in haystack for marker in _CHAIN_MARKERS)


def _url_host(value: str | None) -> str | None:
    if not value:
        return None
    host = urllib.parse.urlparse(value).netloc.casefold()
    return host.removeprefix("www.") or None


def _nearby_config(existing: dict, row: dict) -> bool:
    if not all(existing.get(key) is not None for key in ("lat", "lon")):
        return False
    if not all(row.get(key) is not None for key in ("lat", "lon")):
        return False
    return GeoPoint(float(existing["lat"]), float(existing["lon"])).km_to(
        GeoPoint(float(row["lat"]), float(row["lon"]))
    ) <= 0.75


def _merge_osm_row(rows: dict[str, dict], row: dict) -> tuple[str, dict]:
    """Keep one stable identity when OSM maps a configured venue too.

    The config owns parser/routing fields; OSM contributes a source-linked
    directory observation and current geography. Unmatched OSM records keep
    their stable object id so a later refresh cannot manufacture duplicates.
    """
    for venue_id, existing in rows.items():
        if not str(existing.get("source", "")).startswith("independent-registry"):
            continue
        same_site = (
            _url_host(existing.get("url"))
            and _url_host(existing.get("url")) == _url_host(row.get("url"))
        )
        same_place = (
            existing.get("name", "").casefold() == row.get("name", "").casefold()
            and _nearby_config(existing, row)
        )
        if not (same_site or same_place):
            continue
        merged = {
            **row,
            **existing,
            "venue_id": venue_id,
            "name": existing.get("name") or row.get("name"),
            "url": existing.get("url") or row.get("url"),
            "lat": row.get("lat") if row.get("lat") is not None else existing.get("lat"),
            "lon": row.get("lon") if row.get("lon") is not None else existing.get("lon"),
            "city": row.get("city") or existing.get("city"),
            "state": row.get("state") or existing.get("state"),
            "postal_code": row.get("postal_code") or existing.get("postal_code"),
            "address": row.get("address") or existing.get("address"),
            "source": "independent-registry+osm:overpass",
            "source_url": row.get("source_url"),
            "notes": (
                f"{existing.get('notes', '')} Directory identity corroborated by "
                f"{row.get('source_url')}."
            ).strip(),
        }
        return venue_id, merged
    return row["venue_id"], row


def _timezone_for_state(state: str | None) -> str:
    token = (state or "").strip().casefold()
    abbreviation = token if len(token) == 2 else _STATE_NAMES.get(token)
    return STATE_TZ.get(abbreviation or "", "America/Chicago")
