"""Exhaustive auditorium-map collection, separate from user ranking."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ..identity.work import WorkRef
from ..ranking.candidate import Option, Screening
from ..ranking.spec import DateWindow, LocationSpec, SearchSpec
from ..seating.capture import SeatCapture, SeatProbe
from ..seating.model import PermanentNoSeatMap, SeatDataUnavailable
from .search import SearchService


@dataclass(frozen=True)
class HarvestResult:
    run_id: str
    venue_id: str | None
    city: str | None
    date_start: str
    date_end: str
    showtimes_discovered: int
    probes_saved: int
    probes_attempted: int
    maps_captured: int
    screens_discovered: int
    failure_counts: dict[str, int]
    provider_errors: tuple[str, ...]
    clipped: tuple[str, ...]
    completion_status: str
    duration_ms: float

    @property
    def complete(self) -> bool:
        return self.completion_status == "complete"

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "venue_id": self.venue_id,
            "city": self.city,
            "date_start": self.date_start,
            "date_end": self.date_end,
            "showtimes_discovered": self.showtimes_discovered,
            "probes_saved": self.probes_saved,
            "probes_attempted": self.probes_attempted,
            "maps_captured": self.maps_captured,
            "screens_discovered": self.screens_discovered,
            "failure_counts": self.failure_counts,
            "provider_errors": list(self.provider_errors),
            "clipped": list(self.clipped),
            "completion_status": self.completion_status,
            "complete": self.complete,
            "duration_ms": self.duration_ms,
        }


class HarvestService:
    """Enumerate screenings and collect rooms without a seat-map budget."""

    def __init__(self, search: SearchService) -> None:
        self.search = search
        self.store = search.store

    def harvest(
        self,
        *,
        venue_id: str | None = None,
        city: str | None = None,
        days: int = 45,
        today: date | None = None,
        max_maps: int | None = None,
        max_attempts: int | None = None,
    ) -> HarvestResult:
        if bool(venue_id) == bool(city):
            raise ValueError("provide exactly one of venue_id or city")
        if days < 1 or days > 90:
            raise ValueError("days must be between 1 and 90")

        started = time.perf_counter()
        target_venue = self.search.directory.get(venue_id) if venue_id else None
        start = today or (
            target_venue.today()
            if target_venue is not None
            else datetime.now(UTC).astimezone().date()
        )
        end = start + timedelta(days=days - 1)
        run_id = f"harvest_{uuid.uuid4().hex[:12]}"
        self.store.create_harvest_run(
            run_id,
            venue_id=venue_id,
            city=city,
            date_start=start.isoformat(),
            date_end=end.isoformat(),
        )
        spec = SearchSpec(
            # `gather` deliberately does not filter by work. A non-empty
            # sentinel satisfies WorkRef without turning harvesting title-led.
            work=WorkRef(query="__screenwatch_harvest_all_titles__"),
            location=LocationSpec(
                allow=frozenset({venue_id}) if venue_id else frozenset(),
                city=city,
            ),
            date_window=DateWindow(start, end),
            include_sold_out=True,
            max_seatmap_fetches=0,
            coverage="exhaustive",
        )
        screenings, provider_errors, clipped = self.search.gather(spec)
        screenings = [
            screening for screening in screenings
            if start <= screening.starts_at_local.date() <= end
            and (not venue_id or screening.venue_id == venue_id)
        ]

        by_probe: dict[str, tuple[SeatProbe, Screening]] = {}
        for screening in screenings:
            venue = self.search.directory.get(screening.venue_id)
            probe = screening.durable_seat_probe(
                ticketing_platform=(venue.ticketing_platform if venue else None)
            )
            self.store.put_seat_probe(probe)
            by_probe.setdefault(probe.probe_id, (probe, screening))

        ordered = sorted(by_probe.values(), key=_probe_priority, reverse=True)
        attempted = 0
        captured = 0
        auditorium_ids: set[str] = set()
        failure_counts: dict[str, int] = {}
        unresolved_failure = False
        canary_limited = False
        covered_rooms: set[tuple[str, str, str]] = set()

        for probe, screening in ordered:
            if max_maps is not None and captured >= max_maps:
                canary_limited = True
                break
            if max_attempts is not None and attempted >= max_attempts:
                canary_limited = True
                break
            room_key = (
                probe.source,
                probe.source_venue_id or probe.venue_id,
                probe.source_screen_id or "",
            )
            if probe.source_screen_id and room_key in covered_rooms:
                self.store.mark_seat_probe_status(probe.probe_id, "covered")
                continue
            attempted += 1
            provider = self.search._provider_for(screening.chain)
            if provider is None:
                exc = SeatDataUnavailable(f"no provider for {screening.chain}")
                failure = self.store.record_harvest_failure(run_id, probe, exc)
                code = str(failure["code"])
                failure_counts[code] = failure_counts.get(code, 0) + 1
                unresolved_failure = True
                continue
            try:
                provider_fetch = getattr(provider, "fetch", None)
                if provider_fetch is not None:
                    result = provider_fetch(probe, self.search.transport)
                else:
                    result = provider.fetch_seats(
                        Option(screening=screening), self.search.transport
                    )
                capture = (
                    result
                    if isinstance(result, SeatCapture)
                    else SeatCapture(
                        probe=probe,
                        auditorium=result,
                        source_url=probe.booking_url,
                    )
                )
                capture = self.store.reuse_stored_geometry(capture)
                persisted = self.store.put_seat_capture(
                    capture, screening_id=screening.screening_id
                )
            except Exception as exc:  # noqa: BLE001 - normalized below
                failure = self.store.record_harvest_failure(run_id, probe, exc)
                code = str(failure["code"])
                failure_counts[code] = failure_counts.get(code, 0) + 1
                if not isinstance(exc, PermanentNoSeatMap):
                    unresolved_failure = True
                continue
            captured += 1
            auditorium_ids.add(str(persisted["auditorium_id"]))
            if probe.source_screen_id:
                covered_rooms.add(room_key)

        if provider_errors or clipped or unresolved_failure or canary_limited:
            completion_status = "partial"
        else:
            completion_status = "complete"
        self.store.finish_harvest_run(
            run_id,
            showtimes_discovered=len(screenings),
            probes_attempted=attempted,
            maps_captured=captured,
            screens_discovered=len(auditorium_ids),
            failure_counts=failure_counts,
            completion_status=completion_status,
        )
        return HarvestResult(
            run_id=run_id,
            venue_id=venue_id,
            city=city,
            date_start=start.isoformat(),
            date_end=end.isoformat(),
            showtimes_discovered=len(screenings),
            probes_saved=len(by_probe),
            probes_attempted=attempted,
            maps_captured=captured,
            screens_discovered=len(auditorium_ids),
            failure_counts=failure_counts,
            provider_errors=tuple(provider_errors),
            clipped=(
                *clipped,
                *(
                    ("live canary stopped after its configured map/attempt limit",)
                    if canary_limited else ()
                ),
            ),
            completion_status=completion_status,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def rooms(self, venue_id: str) -> list[dict[str, object]]:
        return self.store.rooms(venue_id)


def _probe_priority(item: tuple[SeatProbe, Screening]) -> tuple[float, float, float]:
    """Unsold, far-future showtimes are likeliest to expose complete layouts."""
    _probe, screening = item
    if screening.seats_available is not None and screening.seats_capacity:
        open_ratio = screening.seats_available / screening.seats_capacity
    elif screening.seats_sold == 0:
        open_ratio = 1.0
    elif screening.availability.value == "sellable":
        open_ratio = 0.75
    else:
        open_ratio = 0.0
    return (
        open_ratio,
        screening.starts_at_local.timestamp(),
        1.0 if screening.screen_id else 0.0,
    )
