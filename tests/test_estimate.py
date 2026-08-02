"""Group-feasibility estimation from seat counts.

Per-seat occupancy is reachable on exactly one source (AMC). Everywhere else
the seat map sits behind a booking hold, which this project will not create.
Without estimation, phase B would be inert for every other chain — and inert
precisely at a near-sellout, which is when ranking matters most.

The properties that have to hold: monotonic in the things that obviously
matter, never more confident than a real grid, and biased pessimistic.
"""

from __future__ import annotations

import pytest

from screenwatch.seating.estimate import (
    CLUSTERING_PENALTY,
    estimate,
    run_probability,
    seat_components,
)
from screenwatch.seating.groups import Cohesion


def room(available, capacity=100, rows=10, party=4):
    return estimate(party_size=party, available=available, capacity=capacity,
                    rows=rows, row_lengths=[capacity // rows] * rows)


class TestBounds:
    def test_empty_room_is_certain(self):
        assert room(100).together_probability == 1.0

    def test_fewer_seats_than_the_party_is_impossible(self):
        f = room(3, party=4)
        assert not f.can_fit_at_all and f.together_probability == 0.0

    def test_exactly_enough_seats_can_still_be_scattered(self):
        """Four free seats in a 100-seat room almost certainly are not
        adjacent, and saying otherwise is the error that ruins an evening."""
        f = room(4, party=4)
        assert f.can_fit_at_all
        assert f.together_probability < 0.2

    def test_a_solo_viewer_only_needs_one_seat(self):
        assert room(1, party=1).together_probability == 1.0

    def test_probabilities_stay_in_range(self):
        for available in range(0, 101, 7):
            assert 0.0 <= room(available).together_probability <= 1.0

    def test_zero_capacity_does_not_divide_by_zero(self):
        f = estimate(party_size=2, available=0, capacity=0, rows=0)
        assert f.together_probability == 0.0


class TestMonotonicity:
    def test_more_free_seats_never_lowers_the_odds(self):
        values = [room(a).together_probability for a in range(4, 101, 4)]
        assert values == sorted(values)

    def test_a_bigger_party_is_never_easier(self):
        base = dict(available=40, capacity=100, rows=10, row_lengths=[10] * 10)
        values = [estimate(party_size=n, **base).together_probability for n in range(1, 9)]
        assert values == sorted(values, reverse=True)

    def test_wider_rows_help(self):
        """Twenty seats spread over 5 long rows beats 10 short ones."""
        wide = estimate(party_size=4, available=30, capacity=100, rows=5,
                        row_lengths=[20] * 5)
        narrow = estimate(party_size=4, available=30, capacity=100, rows=10,
                          row_lengths=[10] * 10)
        assert wide.together_probability > narrow.together_probability


class TestPessimism:
    def test_the_estimate_is_biased_low(self):
        """Real bookings cluster, so the gaps left are more fragmented than a
        uniform scatter. Being pleasantly surprised is the acceptable error."""
        assert CLUSTERING_PENALTY < 1.0
        penalised = room(50).together_probability
        naive = 1.0 - (1.0 - run_probability(10, 5.0, 4)) ** 10
        assert penalised < naive

    def test_run_probability_is_zero_when_the_row_is_too_short(self):
        assert run_probability(3, 3, 4) == 0.0

    def test_a_full_row_is_certain(self):
        assert run_probability(10, 10, 4) == 1.0


class TestComponents:
    def test_an_estimate_never_outscores_a_confirmed_grid(self):
        """A guess must not beat a fact. Confirmed contiguous seating scores
        1.0 for cohesion; the best possible estimate must stay below that."""
        best = seat_components(room(100), 4)
        assert best["group_cohesion"] < 1.0
        assert best["party_fit"] < 1.0

    def test_impossible_seating_scores_near_zero(self):
        scores = seat_components(room(2, party=4), 4)
        assert scores["party_fit"] < 0.2 and scores["group_cohesion"] < 0.2

    def test_missing_data_is_neutral_not_punished(self):
        """No information is different from bad information; an option we
        simply have not measured must not sink below one we have."""
        unknown = seat_components(None, 4)
        bad = seat_components(room(2, party=4), 4)
        assert unknown["party_fit"] > bad["party_fit"]

    def test_emptier_rooms_score_better_on_seat_quality(self):
        """A near-empty room means free choice of where to sit."""
        assert seat_components(room(95), 4)["seat_quality"] > \
               seat_components(room(20), 4)["seat_quality"]


class TestNarrative:
    def test_describes_a_likely_outcome_in_words(self):
        assert "almost certainly" in room(100).describe()
        assert "unlikely" in room(6, party=4).describe()

    def test_says_plainly_when_the_party_cannot_fit(self):
        assert "cannot fit" in room(2, party=4).describe()

    def test_likely_cohesion_degrades_with_occupancy(self):
        assert room(100).likely_cohesion is Cohesion.CONTIGUOUS
        assert room(2, party=4).likely_cohesion is Cohesion.SCATTERED

    def test_solo_is_reported_as_solo(self):
        assert room(20, party=1).likely_cohesion is Cohesion.SOLO

    def test_occupancy_is_reported(self):
        assert room(25, capacity=100).occupancy == pytest.approx(0.75)
