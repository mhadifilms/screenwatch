"""Search orchestration: adapters -> identity -> ranking -> explanation.

This is the only layer the API and MCP transports talk to, so both get
identical behaviour by construction.

Providers are injected. That keeps the service testable without a network and
means adding a chain never touches this file - a provider turns a SearchSpec
into `Screening`s however it likes, and everything downstream is uniform.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from ..identity.normalize import ProductKind
from ..identity.resolve import WorkResolver
from ..ranking.candidate import Option, Screening
from ..ranking.coarse import coarse_rank
from ..ranking.diversify import diversify
from ..ranking.explain import annotate, compare, narrate
from ..ranking.fine import fine_rank
from ..ranking.spec import SearchSpec
from ..seating.model import Auditorium, SeatDataUnavailable
from ..transport import Transport
from .store import Store
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
    # system is not limited to the hand-maintained seed table.
    # def discover(self, spec: SearchSpec) -> list[Venue]: ...


@dataclass
class SearchResult:
    options: list[Option]
    spec: SearchSpec
    considered: int = 0
    seatmaps_fetched: int = 0
    unresolved_titles: tuple[str, ...] = ()
    provider_errors: tuple[str, ...] = ()
    # What the providers' own caps left unread. Not an error - the caps are
    # deliberate - but the difference between "nothing is on" and "nothing is
    # on in the part we looked at", which the caller has to be able to see.
    clipped: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Did the search cover everything the spec asked for?"""
        return not self.clipped and not self.provider_errors

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
        for provider in providers:
            if getattr(provider, "store", "unset") is None:
                provider.store = store
        self.store = store or Store.memory()
        self.resolver = resolver or WorkResolver()
        self.directory = directory or VenueDirectory()
        self._transport = transport

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

        for provider in self.providers:
            if discover := getattr(provider, "discover", None):
                try:
                    self.directory.register(discover(spec))
                except Exception as exc:                       # noqa: BLE001
                    errors.append(f"{provider.chain} discovery: {type(exc).__name__}: {exc}")
            venues = self.directory.matching(spec.location, chain=provider.chain)
            if not venues:
                continue
            try:
                screenings.extend(provider.screenings(spec, venues, self.transport))
            except Exception as exc:                       # noqa: BLE001
                # One dead provider must not empty the whole search - but it
                # must be visible, because a silently missing chain looks
                # exactly like a chain with nothing on.
                errors.append(f"{provider.chain}: {type(exc).__name__}: {exc}")
            clipped.extend(getattr(provider, "clipped", ()))

        return screenings, errors, clipped

    def filter_by_work(self, screenings: list[Screening], spec: SearchSpec) -> list[Screening]:
        """Keep only screenings of the requested film, and only bookable ones."""
        ref = spec.work
        out = []
        for s in screenings:
            link = self.store.get_link(s.sources[0].split(":")[0] if s.sources else "unknown",
                                       s.work.work_id)
            if link and link["kind"] == ProductKind.RENTAL.value:
                continue
            if ref.work_id and s.work.work_id != ref.work_id:
                continue
            if ref.tmdb_id and s.work.tmdb_id != ref.tmdb_id:
                continue
            if ref.query and not _matches_query(s, ref.query):
                continue
            out.append(s)
        return out

    def search(self, spec: SearchSpec, *, today: date | None = None) -> SearchResult:
        today = today or datetime.now(UTC).date()
        screenings, errors, clipped = self.gather(spec)
        screenings = self.filter_by_work(screenings, spec)

        window = spec.window(today)
        screenings = [
            s for s in screenings if window.contains(s.starts_at_local.date())
        ]
        if spec.strict_presentations and spec.presentations is not None:
            screenings = [
                s for s in screenings if spec.presentations.wants(s.presentation)
            ]

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
                {"geometry_confidence": auditorium.geometry_confidence},
            )
            return auditorium

        options = fine_rank(options, spec, fetch)
        options = diversify(options, per_group=spec.diversify_per_group)
        annotate(options, spec)
        self._persist(options)

        return SearchResult(
            options=options,
            spec=spec,
            considered=len(screenings),
            seatmaps_fetched=fetched,
            unresolved_titles=tuple(
                sorted({s.work.title for s in screenings if s.work.work_id.startswith("local:")})
            ),
            provider_errors=tuple(errors),
            clipped=tuple(clipped),
        )

    # ------------------------------------------------------------------
    def _provider_for(self, chain: str) -> Provider | None:
        return next((p for p in self.providers if p.chain == chain), None)

    def _persist(self, options: list[Option]) -> None:
        for option in options:
            s = option.screening
            self.store.put_work(s.work)
            self.store.upsert_screening(
                s.screening_id,
                work_id=s.work.work_id,
                venue_id=s.venue_id,
                chain=s.chain,
                starts_at_utc=s.starts_at_utc,
                presentation=s.presentation.describe(),
                availability=s.availability.value,
                deeplink=s.deeplink,
            )

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
