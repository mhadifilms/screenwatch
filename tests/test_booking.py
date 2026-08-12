from datetime import UTC, datetime

import pytest

from screenwatch.identity.work import Work
from screenwatch.models import Availability, Brand, Presentation, Projection
from screenwatch.ranking.candidate import Option, Screening, SourceListing
from screenwatch.seating.groups import Cohesion, SeatGroup
from screenwatch.seating.model import Seat
from screenwatch.service.booking import balanced_transaction_sizes, build_booking_runway


@pytest.mark.parametrize(
    "party,limit,expected",
    [
        (15, 10, (8, 7)),
        (25, 10, (9, 8, 8)),
        (10, 10, (10,)),
        (4, 1, (1, 1, 1, 1)),
    ],
)
def test_balanced_transaction_sizes(party, limit, expected):
    assert balanced_transaction_sizes(party, limit) == expected


def test_large_party_runway_assigns_exact_seats_across_profiles():
    seats = tuple(
        Seat("H", 7, str(index), index, x=(index - 8) / 8, y=0.7)
        for index in range(1, 16)
    )
    screening = Screening(
        screening_id="chain:feature-drop",
        work=Work("tmdb:feature", "Example Feature", 2026),
        venue_id="grand-cinema",
        venue_name="Grand Cinema",
        chain="chain",
        starts_at_utc=datetime(2026, 12, 18, 4, tzinfo=UTC),
        starts_at_local=datetime(2026, 12, 17, 20),
        presentation=Presentation(Projection.FILM_70MM_15PERF, Brand.IMAX, "1.43"),
        availability=Availability.SELLABLE,
        deeplink="https://example.test/feature-drop",
        sources=("chain:showtimes", "aggregator:listings"),
    )
    option = Option(
        screening=screening,
        seats=SeatGroup(
            seats,
            Cohesion.CONTIGUOUS,
            quality=0.9,
            requested=15,
            assignment=tuple(range(15)),
        ),
        seat_data="grid",
    )

    runway = build_booking_runway(
        option, party_size=15, transaction_limit=10, parallel_checkouts=2
    )

    assert runway["readiness"] == "ready"
    assert runway["split"] == [8, 7]
    assert runway["waves"] == 1
    assert runway["lanes"][0]["profile"] == "Checkout lane A"
    assert runway["lanes"][0]["seat_label"] == "H 1+2+3+4+5+6+7+8"
    assert runway["lanes"][1]["seat_label"] == "H 9+10+11+12+13+14+15"
    assert runway["lanes"][0]["members"] == list(range(8))
    assert runway["option"]["sources"] == ["chain:showtimes", "aggregator:listings"]


def test_runway_without_seat_grid_is_honestly_provisional():
    screening = Screening(
        screening_id="amc:drop",
        work=Work("local:feature", "Example Feature"),
        venue_id="amc-metreon-16",
        venue_name="AMC Metreon 16",
        chain="amc",
        starts_at_utc=datetime(2026, 12, 18, 4, tzinfo=UTC),
        starts_at_local=datetime(2026, 12, 17, 20),
        presentation=Presentation(),
        deeplink="https://example.test/drop",
    )
    runway = build_booking_runway(Option(screening), party_size=15)
    assert runway["readiness"] == "provisional"
    assert runway["exact_seat_assignment"] is False
    assert all(lane["seat_ids"] == [] for lane in runway["lanes"])


def test_runway_uses_the_storefront_that_is_actually_sellable():
    screening = Screening(
        "chain:show", Work("local:feature", "Feature"), "venue", "Venue", "chain",
        datetime(2026, 12, 18, 4, tzinfo=UTC), datetime(2026, 12, 17, 20),
        Presentation(),
        listings=(
            SourceListing("chain:web", Availability.SOLD_OUT, "https://chain.test/show"),
            SourceListing(
                "aggregator:web", Availability.SELLABLE, "https://aggregator.test/show"
            ),
        ),
    )
    runway = build_booking_runway(Option(screening), party_size=2)
    assert runway["option"]["checkout_source"] == "aggregator:web"
    assert runway["lanes"][0]["booking_link"] == "https://aggregator.test/show"


def test_runway_rejects_a_party_size_changed_after_search():
    seats = tuple(Seat("A", 0, str(index), index) for index in range(4))
    screening = Screening(
        "chain:show", Work("local:feature", "Feature"), "venue", "Venue", "chain",
        datetime(2026, 12, 18, 4, tzinfo=UTC), datetime(2026, 12, 17, 20),
        Presentation(), deeplink="https://example.test/show",
    )
    option = Option(
        screening,
        seats=SeatGroup(seats, Cohesion.CONTIGUOUS, 0.8, requested=4),
    )
    with pytest.raises(ValueError, match="must match the search"):
        build_booking_runway(option, party_size=3)


def test_runway_does_not_split_a_required_seat_module():
    seats = tuple(
        Seat(
            "A", 0, str(index), index,
            module_id="pair" if index in {4, 5} else None,
            module_size=2 if index in {4, 5} else None,
            module_required=index in {4, 5},
        )
        for index in range(1, 9)
    )
    screening = Screening(
        "chain:show", Work("local:feature", "Feature"), "venue", "Venue", "chain",
        datetime(2026, 12, 18, 4, tzinfo=UTC), datetime(2026, 12, 17, 20),
        Presentation(), deeplink="https://example.test/show",
    )
    option = Option(
        screening,
        seats=SeatGroup(seats, Cohesion.CONTIGUOUS, 0.8, requested=8),
    )
    runway = build_booking_runway(option, party_size=8, transaction_limit=5)
    assert runway["split"] == [3, 5]
    assert runway["lanes"][1]["seat_ids"][:2] == ["A4", "A5"]
