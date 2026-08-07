"""Search orchestration: adapters -> identity -> ranking -> explanation.

This is the only layer the API and MCP transports talk to, so both get
identical behaviour by construction.

Providers are injected. That keeps the service testable without a network and
means adding a chain never touches this file - a provider turns a SearchSpec
into `Screening`s however it likes, and everything downstream is uniform.
"""

from __future__ import annotations

import inspect
import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Protocol

from ..adapters.amc.sitemap import AmcSitemap
from ..identity.resolve import WorkResolver, title_rank
from ..models import Availability
from ..ranking.candidate import Option, Screening
from ..ranking.coarse import coarse_rank
from ..ranking.diversify import diversify
from ..ranking.explain import annotate, compare, narrate
from ..ranking.fine import fine_rank
from ..ranking.spec import GeoPoint, SearchSpec
from ..seating.model import Auditorium, SeatDataUnavailable
from ..transport import Transport
from .serde import spec_to_json
from .store import DEFAULT_USER, Store
from .venues import Venue, VenueDirectory


class Provider(Protocol):
    """A source of screenings for a chain or platform."""

    chain: str

    def screenings(
        self, spec: SearchSpec, venues: list[Venue], transport: Transport
    ) -> list[Screening]: ...

    def fetch_seats(self, option: Option, transport: Transport) -> Auditorium: ...

    # Optional. Providers whose API knows where its own venues are (Alamo
    # ships coordinates for every cinema in a market) implement this so the
    # system is not limited to a hand-maintained routing registry.
    # def discover(self, spec: SearchSpec) -> list[Venue]: ...


@dataclass
class SearchResult:
    options: list[Option]
    spec: SearchSpec
    considered: int = 0
    seatmaps_fetched: int = 0
    unresolved_titles: tuple[str, ...] = ()
    provider_errors: tuple[str, ...] = ()
    # What the fast-path caps left unread. Not an error - the caps are
    # deliberate - but the difference between "nothing is on" and "nothing is
    # on in the part we looked at", which the caller has to be able to see.
    clipped: tuple[str, ...] = ()
    search_id: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float = 0.0
    provider_stats: tuple[dict, ...] = ()

    @property
    def complete(self) -> bool:
        """Did the search cover everything the spec asked for?"""
        return not self.clipped and not self.provider_errors

    @property
    def coverage(self) -> str:
        """Effective source coverage mode used by this result."""
        return "exhaustive" if self.spec.exhaustive else "nearby"

    @property
    def best(self) -> Option | None:
        return self.options[0] if self.options else None

    def narrate(self, limit: int = 3) -> str:
        return narrate(self.options, self.spec, limit=limit)

    def comparison(self) -> str | None:
        if len(self.options) < 2:
            return None
        return compare(self.options[0], self.options[1], self.spec)


class SearchService:
    def __init__(
        self,
        providers: list[Provider],
        *,
        store: Store | None = None,
        resolver: WorkResolver | None = None,
        directory: VenueDirectory | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.providers = providers
        # Providers that persist learned venue geography share the store, so
        # an expensive discovery is amortised across runs rather than repeated.
        self.store = store or Store.memory()
        for provider in providers:
            if getattr(provider, "store", "unset") is None:
                provider.store = self.store
        self.resolver = resolver or WorkResolver()
        self.directory = directory or VenueDirectory()
        self._hydrate_directory()
        self._transport = transport
        self._last_provider_stats: tuple[dict, ...] = ()

    def _hydrate_directory(self) -> None:
        """Merge provider discoveries from earlier processes into the graph."""
        rows = getattr(self.store, "directory_venues", lambda: [])()
        for row in rows:
            point = (
                GeoPoint(row["lat"], row["lon"])
                if row.get("lat") is not None and row.get("lon") is not None
                else None
            )
            self.directory.register([
                Venue(
                    venue_id=row["venue_id"],
                    name=row["name"],
                    chain=row["chain"],
                    tz=row.get("tz"),
                    point=point,
                    market=row.get("market"),
                    city=row.get("city"),
                    state=row.get("state"),
                    ticketing_platform=row.get("ticketing_platform"),
                    url=row.get("url"),
                    venue_type=row.get("venue_type") or "cinema",
                    markup=row.get("markup"),
                    notes=row.get("notes"),
                    source=row.get("source") or "store",
                    source_url=row.get("source_url"),
                    observed_at=row.get("observed_at"),
                )
            ])

    @staticmethod
    def _call_discover(provider, spec: SearchSpec, *, full: bool = False):
        """Call discovery with full-refresh support without breaking plugins."""
        discover = getattr(provider, "discover", None)
        if discover is None:
            return None
        if full:
            try:
                if "full" in inspect.signature(discover).parameters:
                    return discover(spec, full=True)
            except (TypeError, ValueError):
                # Builtins and unusual plugin callables may not expose a
                # signature. Falling back keeps the provider seam compatible.
                pass
        return discover(spec)

    def _register_discovered(self, discovered: list[Venue]) -> None:
        if not discovered:
            return
        observed_at = datetime.now(UTC).isoformat()
        discovered = [
            replace(venue, observed_at=venue.observed_at or observed_at)
            for venue in discovered
        ]
        self.directory.register(discovered)
        records = []
        for venue in discovered:
            record = self.directory.get(venue.venue_id)
            if record is not None:
                records.append(record)
        self.store.put_directory_venues(records)

    def discover_venues(self, spec: SearchSpec) -> dict:
        """Refresh provider venue metadata without requiring a film query."""
        started = time.perf_counter()
        stats: list[dict] = []
        errors: list[str] = []
        clipped: list[str] = []
        discovered_total = 0
        for provider in self.providers:
            reset_scope = getattr(provider, "_reset_scope", None)
            if reset_scope is not None:
                reset_scope()
            if spec.location.chains and provider.chain not in spec.location.chains:
                stats.append({
                    "chain": provider.chain,
                    "status": "not_in_scope",
                    "discovered": 0,
                    "coverage": "exhaustive",
                })
                continue
            discover = getattr(provider, "discover", None)
            if discover is None:
                stats.append({
                    "chain": provider.chain,
                    "status": "unsupported",
                    "discovered": 0,
                    "coverage": "exhaustive",
                })
                continue
            provider_started = time.perf_counter()
            try:
                discovered = self._call_discover(provider, spec, full=True) or []
                self._register_discovered(discovered)
                discovered_total += len(discovered)
                provider_errors = list(getattr(provider, "errors", ()))
                provider_clipped = list(getattr(provider, "clipped", ()))
                errors.extend(item for item in provider_errors if item not in errors)
                clipped.extend(item for item in provider_clipped if item not in clipped)
                stats.append({
                    "chain": provider.chain,
                    "status": "degraded" if provider_errors or provider_clipped else "ok",
                    "discovered": len(discovered),
                    "duration_ms": round((time.perf_counter() - provider_started) * 1000, 2),
                    "errors": provider_errors,
                    "clipped": provider_clipped,
                    "coverage": "exhaustive",
                })
            except Exception as exc:                       # noqa: BLE001
                message = f"{provider.chain} discovery: {type(exc).__name__}: {exc}"
                errors.append(message)
                provider_errors = [message, *getattr(provider, "errors", ())]
                provider_clipped = list(getattr(provider, "clipped", ()))
                clipped.extend(item for item in provider_clipped if item not in clipped)
                stats.append({
                    "chain": provider.chain,
                    "status": "error",
                    "discovered": 0,
                    "duration_ms": round((time.perf_counter() - provider_started) * 1000, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                    "errors": list(dict.fromkeys(provider_errors)),
                    "clipped": provider_clipped,
                    "coverage": "exhaustive",
                })
        return {
            "scope": "national_directory",
            "full_refresh": True,
            "coverage": "exhaustive",
            "discovered": discovered_total,
            "directory_total": len(self.directory.all()),
            "provider_stats": stats,
            "errors": errors,
            "clipped": clipped,
            "complete": not errors and not clipped,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def release_signals(self, spec: SearchSpec) -> list[dict]:
        """Read cheap catalog signals for a future release watch.

        AMC's movie sitemap is outside the Queue-it showtime surface and is
        useful before any theatre has published tickets. The result is a
        source-backed signal, not a claim that tickets are on sale; the normal
        showtime watch remains responsible for the actual bookable alert.
        """
        query = spec.work.query or ""
        if not query:
            return []
        sitemap = AmcSitemap()
        entries = sitemap.parse(sitemap.fetch(self.transport))
        return [
            {
                "source": sitemap.source,
                "entry_key": entry.key,
                "slug": entry.slug,
                "movie_id": entry.movie_id,
                "url": entry.url,
                "lastmod": entry.lastmod,
            }
            for entry in AmcSitemap.find(entries, title_contains=query)
        ]

    @property
    def transport(self) -> Transport:
        # Built lazily and kept warm: recreating it discards the accepted-queue
        # cookie and forces another Queue-it traversal on the next request.
        if self._transport is None:
            self._transport = Transport()
        return self._transport

    # ------------------------------------------------------------------
    def gather(
        self, spec: SearchSpec
    ) -> tuple[list[Screening], list[str], list[str]]:
        """Collect raw screenings from every provider the spec touches.

        Returns the screenings, the providers that failed, and what the
        providers that succeeded did not get to.
        """
        screenings: list[Screening] = []
        errors: list[str] = []
        clipped: list[str] = []
        provider_stats: list[dict] = []

        for provider in self.providers:
            provider_started = time.perf_counter()
            discovery_count = 0
            discovered: list[Venue] = []
            discovery_errors: list[str] = []
            discovery_clipped: list[str] = []
            reset_scope = getattr(provider, "_reset_scope", None)
            if reset_scope is not None:
                reset_scope()
            if spec.location.chains and provider.chain not in spec.location.chains:
                provider_stats.append({
                    "chain": provider.chain,
                    "status": "not_in_scope",
                    "venues": 0,
                    "discovered": 0,
                    "screenings": 0,
                    "duration_ms": round((time.perf_counter() - provider_started) * 1000, 2),
                    "clipped": [],
                })
                continue
            if discover := getattr(provider, "discover", None):
                try:
                    discovered = self._call_discover(
                        provider, spec, full=spec.exhaustive
                    ) or []
                    discovery_count = len(discovered)
                    self._register_discovered(discovered)
                except Exception as exc:                       # noqa: BLE001
                    discovery_errors.append(
                        f"{provider.chain} discovery: {type(exc).__name__}: {exc}"
                    )
                discovery_errors.extend(getattr(provider, "errors", ()))
                discovery_clipped.extend(getattr(provider, "clipped", ()))

            unlocated = []
            if (
                spec.location.origin is not None
                and spec.location.city is None
                and not spec.exhaustive
            ):
                unlocated = [
                    venue for venue in discovered
                    if venue.point is None and venue.venue_id not in spec.location.allow
                ]
            if unlocated:
                discovery_clipped.append(
                    f"{provider.chain}: {len(unlocated)} discovered venues have no "
                    "coordinates; add a city or explicitly allow a venue id to "
                    "include them in an origin-based search"
                )

            venues = self.directory.matching(
                spec.location,
                chain=provider.chain,
                include_unknown=spec.exhaustive,
            )
            provider_clipped = list(discovery_clipped)
            # A provider without a discover() hook may still be a valid
            # screening source (and may have its own venue identity in the
            # returned Screening objects). Give it a chance with an empty
            # directory input; providers that do implement discovery have
            # already told us that an empty match is meaningful and are kept
            # in the explicit not-in-scope branch below.
            if not venues and discover is not None:
                for message in discovery_errors:
                    if message not in errors:
                        errors.append(message)
                for message in provider_clipped:
                    if message not in clipped:
                        clipped.append(message)
                status = "error" if discovery_errors else (
                    "degraded" if provider_clipped else "not_in_scope"
                )
                provider_stats.append({
                    "chain": provider.chain,
                    "status": status,
                    "venues": 0,
                    "discovered": discovery_count,
                    "screenings": 0,
                    "duration_ms": round((time.perf_counter() - provider_started) * 1000, 2),
                    "errors": discovery_errors,
                    "clipped": provider_clipped,
                })
                continue
            try:
                found = provider.screenings(spec, venues, self.transport)
                if not venues and found:
                    # This fallback is for custom/legacy providers whose
                    # listing endpoint is intrinsically venue-scoped. The
                    # screening itself is the source-backed directory claim;
                    # it is not a hardware seed.
                    self._register_discovered([
                        Venue(
                            venue_id=s.venue_id,
                            name=s.venue_name,
                            chain=s.chain,
                            source=(s.sources[0] if s.sources else f"{s.chain}:screening"),
                            source_url=s.deeplink,
                        )
                        for s in found
                    ])
                screenings.extend(found)
                screening_errors = list(getattr(provider, "errors", ()))
                provider_errors = [*discovery_errors, *screening_errors]
                provider_errors = list(dict.fromkeys(provider_errors))
                provider_clipped.extend(getattr(provider, "clipped", ()))
                provider_clipped = list(dict.fromkeys(provider_clipped))
                for message in provider_errors:
                    if message not in errors:
                        errors.append(message)
                for message in provider_clipped:
                    if message not in clipped:
                        clipped.append(message)
                provider_stats.append({
                    "chain": provider.chain,
                    "status": "degraded" if provider_errors or provider_clipped else "ok",
                    "venues": len(venues),
                    "discovered": discovery_count,
                    "screenings": len(found),
                    "duration_ms": round((time.perf_counter() - provider_started) * 1000, 2),
                    "errors": provider_errors,
                    "clipped": provider_clipped,
                })
            except Exception as exc:                       # noqa: BLE001
                # One dead provider must not empty the whole search - but it
                # must be visible, because a silently missing chain looks
                # exactly like a chain with nothing on.
                provider_errors = [
                    *discovery_errors,
                    *getattr(provider, "errors", ()),
                    f"{provider.chain}: {type(exc).__name__}: {exc}",
                ]
                provider_errors = list(dict.fromkeys(provider_errors))
                provider_clipped.extend(getattr(provider, "clipped", ()))
                provider_clipped = list(dict.fromkeys(provider_clipped))
                for message in provider_errors:
                    if message not in errors:
                        errors.append(message)
                for message in provider_clipped:
                    if message not in clipped:
                        clipped.append(message)
                provider_stats.append({
                    "chain": provider.chain,
                    "status": "error",
                    "venues": len(venues),
                    "discovered": discovery_count,
                    "screenings": 0,
                    "duration_ms": round((time.perf_counter() - provider_started) * 1000, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                    "errors": provider_errors,
                    "clipped": provider_clipped,
                })

        effective_coverage = "exhaustive" if spec.exhaustive else "nearby"
        for stat in provider_stats:
            stat.setdefault("coverage", effective_coverage)
        self._last_provider_stats = tuple(provider_stats)

        return screenings, errors, clipped

    def filter_by_work(self, screenings: list[Screening], spec: SearchSpec) -> list[Screening]:
        """Keep only screenings of the requested film.

        Unbookable products - private theatre rentals, marathons - are
        already gone: every provider drops them at construction, on
        `resolution.analysis.is_bookable`, which is where the information
        actually is.

        There used to be a second check here that read `title_links` for a
        RENTAL kind. It never fired. It passed the internal `work_id` where
        the query wanted the *source's* product id, so the lookup could not
        match, and nothing wrote to that table anyway. A filter that cannot
        match is worse than no filter: it reads as a safeguard.
        """
        ref = spec.work
        out = []
        for s in screenings:
            if ref.work_id and s.work.work_id != ref.work_id:
                continue
            if ref.tmdb_id and s.work.tmdb_id != ref.tmdb_id:
                continue
            if ref.query and not _matches_query(s, ref.query):
                continue
            out.append(s)
        return out

    @staticmethod
    def unify_titles(screenings: list[Screening]) -> list[Screening]:
        """One display title per work, chosen after every source has spoken.

        Without a catalogue key, two chains describing one film agree on the
        `work_id` and disagree on the title: AMC derives its from a URL slug,
        so "Spider Man Brand New Day", while Regal ships the real
        "Spider-Man: Brand New Day". The user then sees whichever chain was
        polled first, which is not a property they should be able to observe.

        Deliberately a pass over the gathered set rather than a rule inside
        the resolver. The resolver sees products one at a time and cannot know
        that a better title is coming; here, all of them have arrived.
        """
        best: dict[str, str] = {}
        for s in screenings:
            work_id = s.work.work_id
            if work_id not in best or title_rank(s.work.title) > title_rank(best[work_id]):
                best[work_id] = s.work.title
        return [
            s if s.work.title == best[s.work.work_id]
            else replace(s, work=replace(s.work, title=best[s.work.work_id]))
            for s in screenings
        ]

    @staticmethod
    def unify_screenings(screenings: list[Screening]) -> list[Screening]:
        """Collapse duplicate listings of the same real-world showing.

        The provider id is not allowed to decide whether two listings are the
        same show.  A richer listing wins the seat/booking handle, while the
        structured metadata from all equivalent listings is retained.
        """
        grouped: dict[str, list[Screening]] = {}
        order: list[str] = []
        for screening in screenings:
            key = screening.canonical_screening_id
            if key not in grouped:
                order.append(key)
            grouped.setdefault(key, []).append(screening)

        out: list[Screening] = []
        for key in order:
            group = grouped[key]
            chosen = max(group, key=_screening_richness)
            sources = tuple(sorted({source for s in group for source in s.sources}))
            if len(group) == 1 and sources == chosen.sources:
                out.append(chosen)
                continue

            # A source that says sellable is useful evidence that this real
            # show can be bought even if another surface is stale.  Keep the
            # most useful known availability and fill missing links/counts
            # from the richer duplicate without changing the chosen provider
            # handle used for seat fetching.
            availability = _merged_availability(group)
            deeplink = chosen.deeplink or next(
                (s.deeplink for s in group if s.deeplink), None
            )
            out.append(replace(
                chosen,
                availability=availability,
                deeplink=deeplink,
                sources=sources,
                seats_available=next(
                    (s.seats_available for s in group
                     if s.seats_available is not None),
                    chosen.seats_available,
                ),
                seats_capacity=next(
                    (s.seats_capacity for s in group
                     if s.seats_capacity is not None),
                    chosen.seats_capacity,
                ),
                seats_sold=next(
                    (s.seats_sold for s in group if s.seats_sold is not None),
                    chosen.seats_sold,
                ),
            ))
        return out

    def search(
        self,
        spec: SearchSpec,
        *,
        today: date | None = None,
        user_id: str = DEFAULT_USER,
    ) -> SearchResult:
        started_clock = datetime.now().astimezone()
        started_perf = time.perf_counter()
        search_id = f"search_{uuid.uuid4().hex[:12]}"
        # Relative windows are user-facing calendar windows.  Anchoring them
        # to UTC makes a Bay Area watch jump to tomorrow at 5pm local time.
        today = today or datetime.now().astimezone().date()
        screenings, errors, clipped = self.gather(spec)
        screenings = self.filter_by_work(screenings, spec)

        window = spec.window(today)
        screenings = [
            s for s in screenings if window.contains(s.starts_at_local.date())
        ]
        screenings = self.unify_titles(screenings)
        if spec.strict_presentations and spec.presentations is not None:
            screenings = [
                s for s in screenings if spec.presentations.wants(s.presentation)
            ]

        # Register every raw handle before collapsing duplicates.  This is
        # what lets a watch migrate from an old provider id to the canonical
        # identity without replaying the current inventory as new.
        self.store.register_screening_aliases([
            (s.canonical_screening_id, s.screening_id, s.chain)
            for s in screenings
        ])
        screenings = self.unify_screenings(screenings)

        options = coarse_rank(screenings, spec)

        fetched = 0

        def fetch(option: Option) -> Auditorium:
            nonlocal fetched
            provider = self._provider_for(option.screening.chain)
            if provider is None:
                raise SeatDataUnavailable(f"no provider for {option.screening.chain}")
            try:
                auditorium = provider.fetch_seats(option, self.transport)
            except SeatDataUnavailable:
                raise
            except Exception as exc:
                # A seat fetch is an enrichment, never a precondition. Any
                # provider-specific failure - a Cloudflare challenge, a changed
                # schema, a timeout - degrades this one option to
                # availability-only ranking instead of failing the whole
                # search. Letting it propagate meant one blocked seat map
                # returned zero results for every chain.
                raise SeatDataUnavailable(
                    f"{option.screening.chain} seat fetch failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            fetched += 1
            self.store.put_seat_snapshot(
                option.screening.screening_id,
                auditorium.available,
                auditorium.capacity,
                {
                    "geometry_confidence": auditorium.geometry_confidence,
                    "screen_id": auditorium.screen_id,
                    "screen_name": auditorium.name,
                    "row_count": auditorium.row_count,
                    "row_lengths": list(auditorium.row_lengths),
                    "has_grid": auditorium.has_grid,
                },
                venue_id=option.screening.venue_id,
                source="|".join(option.screening.sources)
                if option.screening.sources else f"{option.screening.chain}:seat-map",
                source_url=option.screening.deeplink,
                evidence_scope="screening-seat-map",
            )
            return auditorium

        options = fine_rank(options, spec, fetch)
        options = diversify(options, per_group=spec.diversify_per_group)
        annotate(options, spec)
        self._persist(options)

        finished_clock = datetime.now().astimezone()
        result = SearchResult(
            options=options,
            spec=spec,
            considered=len(screenings),
            seatmaps_fetched=fetched,
            unresolved_titles=tuple(
                sorted({s.work.title for s in screenings if s.work.work_id.startswith("local:")})
            ),
            provider_errors=tuple(errors),
            clipped=tuple(clipped),
            search_id=search_id,
            started_at=started_clock.isoformat(),
            finished_at=finished_clock.isoformat(),
            duration_ms=round((time.perf_counter() - started_perf) * 1000, 2),
            provider_stats=self._last_provider_stats,
        )
        self.store.record_search_run(
            search_id,
            spec=spec_to_json(spec),
            started_at=result.started_at or started_clock.isoformat(),
            finished_at=result.finished_at or finished_clock.isoformat(),
            duration_ms=result.duration_ms,
            considered=result.considered,
            result_count=len(result.options),
            seatmaps_fetched=result.seatmaps_fetched,
            complete=result.complete,
            provider_stats=result.provider_stats,
            errors=result.provider_errors,
            clipped=result.clipped,
            user_id=user_id,
        )
        return result

    # ------------------------------------------------------------------
    def _provider_for(self, chain: str) -> Provider | None:
        return next((p for p in self.providers if p.chain == chain), None)

    def _persist(self, options: list[Option]) -> None:
        observations = []
        for option in options:
            s = option.screening
            self.store.put_work(s.work)
            self.store.upsert_screening(
                s.screening_id,
                canonical_id=s.canonical_screening_id,
                work_id=s.work.work_id,
                venue_id=s.venue_id,
                chain=s.chain,
                starts_at_utc=s.starts_at_utc,
                presentation=s.presentation.describe(),
                availability=s.availability.value,
                deeplink=s.deeplink,
            )
            presentation_payload = {
                "projection": s.presentation.projection.value,
                "brand": s.presentation.brand.value,
                "aspect": s.presentation.aspect,
                "attributes": sorted(attribute.value for attribute in s.presentation.attrs),
                "label": s.presentation.describe(),
                "raw": s.presentation.raw,
                "screening_id": s.screening_id,
                "canonical_screening_id": s.canonical_screening_id,
                "work_id": s.work.work_id,
                "title": s.work.title,
                "starts_at_utc": s.starts_at_utc.isoformat(),
            }
            subject_key = json.dumps(
                {
                    key: presentation_payload[key]
                    for key in ("projection", "brand", "aspect", "attributes")
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            sources = s.sources or (f"{s.chain}:screening",)
            for source in sources:
                observations.append({
                    "venue_id": s.venue_id,
                    "kind": "screening_presentation",
                    "subject_key": subject_key,
                    "payload": presentation_payload,
                    "source": source,
                    "source_url": s.deeplink,
                    "evidence_scope": "screening",
                    "confidence": 1.0,
                })
        self.store.record_venue_observations(observations)

    def booking_link(self, option_id: str) -> str | None:
        """The final step. The system stops here, deliberately.

        No cart automation, no purchase, no stored payment - the user opens
        this and finishes it themselves.
        """
        screening_id = option_id.split(Option.SEAT_SEPARATOR)[0]
        row = self.store._conn.execute(
            "SELECT deeplink FROM screenings WHERE screening_id=?", (screening_id,)
        ).fetchone()
        return row["deeplink"] if row else None


def _matches_query(screening: Screening, query: str) -> bool:
    from ..identity.normalize import match_key

    want, have = match_key(query), match_key(screening.work.title)
    return want in have or have in want


def _screening_richness(screening: Screening) -> tuple:
    """Prefer the duplicate with the best downstream enrichment handles."""
    return (
        screening.screen_id is not None,
        screening.deeplink is not None,
        screening.seats_available is not None,
        screening.presentation.projection.value != "unknown",
        screening.presentation.brand.value != "none",
        screening.availability is not Availability.UNKNOWN,
        len(screening.sources),
        screening.chain,
    )


def _merged_availability(screenings: list[Screening]) -> Availability:
    """Use the most actionable claim when equivalent sources disagree."""
    values = {screening.availability for screening in screenings}
    for candidate in (
        Availability.SELLABLE,
        Availability.ALMOST_FULL,
        Availability.SOLD_OUT,
        Availability.UNKNOWN,
    ):
        if candidate in values:
            return candidate
    return Availability.UNKNOWN
