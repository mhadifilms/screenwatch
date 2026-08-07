"""The things being ranked.

A `Screening` is what a search finds. An `Option` is what a user actually
books - a screening *plus the specific seats they would get*. That distinction
is the reason the ranking works at all: a showing with four scattered singles
and a showing with four seats together are the same screening-level record and
completely different products.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..identity.show import canonical_show_id
from ..identity.work import Work
from ..models import Availability, Presentation
from ..seating.groups import SeatGroup
from ..seating.model import Auditorium


@dataclass(frozen=True)
class Screening:
    """One showing at one venue, before seats are considered."""

    screening_id: str            # stable: "<source>:<external_id>"
    work: Work
    venue_id: str
    venue_name: str
    chain: str
    starts_at_utc: datetime
    starts_at_local: datetime
    presentation: Presentation
    availability: Availability = Availability.UNKNOWN
    deeplink: str | None = None
    price_hint_usd: float | None = None
    distance_km: float | None = None
    screen_id: str | None = None
    sources: tuple[str, ...] = ()
    # Exact counts where the source publishes them (C360 does). Carried on the
    # screening so phase B does not re-request data already in hand.
    seats_available: int | None = None
    seats_capacity: int | None = None
    seats_sold: int | None = None

    @property
    def canonical_screening_id(self) -> str:
        """Identity of the real-world showing, independent of its provider.

        ``screening_id`` remains the provider-scoped handle because seat-map
        adapters and booking links need the original value.  Persistence and
        watches use this canonical id so a provider id changing does not look
        like a brand-new showing.
        """
        return canonical_show_id(
            work_id=self.work.work_id,
            venue_id=self.venue_id,
            starts_at_utc=self.starts_at_utc,
            presentation=self.presentation,
            screen_id=self.screen_id,
        )

    @property
    def bookable(self) -> bool:
        return self.availability.is_buyable


@dataclass
class Option:
    """A bookable choice: a screening plus a concrete seat assignment.

    `seats` is None when no seat grid was obtainable - the option is still
    rankable, just with less to go on, and `seat_data` records why.
    """

    screening: Screening
    score: float = 0.0
    coarse_score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    seats: SeatGroup | None = None
    auditorium: Auditorium | None = None
    feasibility: object | None = None  # seating.estimate.Feasibility, when estimated
    seat_data: str = "not_fetched"     # not_fetched | grid | estimated | count_only | unavailable
    tradeoffs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    # Separates the screening from its seat assignment inside an option id.
    #
    # `~` rather than `#`, which is what this used to be. Option ids travel in
    # URL paths - `/v1/seatmap/{option_id}` - and a browser or client truncates
    # at `#` before the request is even sent, so every seat map request arrived
    # asking for an option whose id had been silently cut in half. `~` is
    # unreserved in RFC 3986 and needs no escaping anywhere.
    SEAT_SEPARATOR = "~"

    @property
    def option_id(self) -> str:
        seats = "-".join(s.id for s in self.seats.seats) if self.seats else "noseats"
        return f"{self.screening.screening_id}{self.SEAT_SEPARATOR}{seats}"

    @property
    def can_sit_together_probability(self) -> float | None:
        """Certainty where a grid exists, estimate where only counts do."""
        if self.seats is not None:
            return 1.0 if self.seats.cohesion.is_together else 0.0
        if self.feasibility is not None:
            return self.feasibility.together_probability
        return None

    @property
    def seats_together(self) -> bool | None:
        if self.seats is None:
            return None
        return self.seats.cohesion.is_together

    @property
    def can_seat_party(self) -> bool | None:
        if self.seats is not None:
            return self.seats.complete
        if self.feasibility is not None:
            return self.feasibility.can_fit_at_all
        return None

    def summary(self) -> str:
        s = self.screening
        when = s.starts_at_local.strftime("%a %-I:%M%p").lower()
        bits = [when, s.presentation.describe(), s.venue_name]
        if self.seats:
            bits.append(self.seats.describe())
        return " · ".join(bits)
