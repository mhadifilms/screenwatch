"""Seating tests.

The load-bearing property is scale invariance: the same score must mean the
same thing in a 500-seat IMAX and a 40-seat microcinema, because the ranker
compares options across venues of wildly different sizes.
"""

from __future__ import annotations

import pytest

from screenwatch.seating.groups import Cohesion, find_groups
from screenwatch.seating.model import (
    Auditorium,
    SeatKind,
    normalize_geometry,
)
from screenwatch.seating.quality import QualityModel
from screenwatch.seating.render import build_auditorium, to_svg, to_unicode_grid


def grid(rows: int, cols: int, venue="v", screen="1") -> Auditorium:
    return build_auditorium(venue, screen, ["." * cols for _ in range(rows)])


class TestGeometryNormalization:
    def test_depth_spans_zero_to_one(self):
        a = grid(10, 8)
        assert min(s.y for s in a.seats) == 0.0
        assert max(s.y for s in a.seats) == 1.0

    def test_lateral_is_centred(self):
        a = grid(3, 9)
        row = a.rows()[0]
        assert row[0].x == -1.0 and row[-1].x == 1.0
        assert row[len(row) // 2].x == 0.0

    def test_rows_of_different_widths_each_centre_independently(self):
        """Normalizing against the widest row would push a short, centred
        front row off axis."""
        a = build_auditorium("v", "1", ["...", "........."])
        short, long = a.rows()
        assert short[1].x == pytest.approx(0.0)
        assert long[4].x == pytest.approx(0.0)

    def test_blocked_seats_do_not_shift_the_centreline(self):
        a = build_auditorium("v", "1", ["#.....#"])
        bookable = [s for s in a.rows()[0] if s.kind.is_bookable]
        assert bookable[len(bookable) // 2].x == pytest.approx(0.0)

    def test_single_row_does_not_divide_by_zero(self):
        assert all(s.y == 0.0 for s in grid(1, 5).seats)


class TestScaleInvariance:
    @pytest.mark.parametrize("rows,cols", [(6, 8), (20, 30), (48, 64)])
    def test_best_seat_lands_in_the_same_normalized_place(self, rows, cols):
        """A 48x64 IMAX and a 6x8 microcinema must agree on where 'good' is."""
        a = grid(rows, cols)
        model = QualityModel()
        best = max(a.seats, key=lambda s: model.score(s, row_count=a.row_count))
        # Within one row/seat of the ideal. An even column count has no exact
        # centre seat, so compare against the finest offset the room allows.
        assert abs(best.y - model.ideal_depth) <= 1 / (rows - 1) + 0.01
        assert abs(best.x) == pytest.approx(min(abs(s.x) for s in a.seats))

    def test_scores_are_bounded(self):
        a = grid(30, 40)
        model = QualityModel()
        assert all(0.0 <= model.score(s, row_count=30) <= 1.0 for s in a.seats)

    def test_front_row_is_penalized_at_any_size(self):
        for rows in (6, 40):
            a = grid(rows, 10)
            model = QualityModel()
            front = [s for s in a.seats if s.row_index == 0]
            mid = [s for s in a.seats if abs(s.y - 0.6) < 0.06]
            assert max(model.score(s, row_count=rows) for s in front) < max(
                model.score(s, row_count=rows) for s in mid
            )


class TestAdaptiveMiddleArea:
    def test_default_uses_a_layout_aware_middle_band(self):
        auditorium = grid(8, 10)
        model = QualityModel().for_auditorium(auditorium)

        assert model.middle_band == pytest.approx((0.2857, 0.7143), abs=0.0001)
        middle = [
            row[len(row) // 2] for row in auditorium.rows()
            if model.middle_band[0] <= row[0].y <= model.middle_band[1]
        ]
        outside = [
            row[len(row) // 2] for row in auditorium.rows()
            if row[0].y < model.middle_band[0] or row[0].y > model.middle_band[1]
        ]
        assert len({seat.y for seat in middle}) > 1
        assert min(model.score(seat, row_count=auditorium.row_count) for seat in middle) > max(
            model.score(seat, row_count=auditorium.row_count) for seat in outside
        )

        [best] = find_groups(auditorium, 1)[:1]
        assert model.middle_band[0] <= best.seats[0].y <= model.middle_band[1]

    def test_skipped_rows_choose_the_observed_physical_middle(self):
        auditorium = build_auditorium(
            "v", "1", ["......", "......", "", "", "......", "......", "......"]
        )
        model = QualityModel().for_auditorium(auditorium)

        # The gap remains in y, so the first row after the cross-aisle is the
        # closest observed middle instead of being treated as row three.
        assert model.middle_band == pytest.approx((0.6667, 0.6667), abs=0.0001)
        [best] = find_groups(auditorium, 1)[:1]
        assert best.seats[0].row_index == 4

    def test_tiny_rooms_do_not_penalize_every_seat_as_front(self):
        auditorium = grid(2, 6)
        model = QualityModel().for_auditorium(auditorium)
        centres = [row[len(row) // 2] for row in auditorium.rows()]

        assert model.score(centres[0], row_count=auditorium.row_count) == pytest.approx(
            model.score(centres[1], row_count=auditorium.row_count)
        )


class TestGroupFinding:
    def test_prefers_contiguous_seating(self):
        a = build_auditorium("v", "1", ["××....××", "××××××××"])
        [best, *_] = find_groups(a, 4)
        assert best.cohesion is Cohesion.CONTIGUOUS
        assert best.complete and best.size == 4

    def test_falls_back_to_a_balanced_split(self):
        """No four-in-a-row anywhere, but two pairs exist. This is the
        motivating case: the system must find 2+2 rather than give up."""
        a = build_auditorium("v", "1", [
            "..××××××..",   # A: a free pair at each end, sold in between
            "..××××××..",   # B: same
        ])
        [best, *_] = find_groups(a, 4)
        assert best.complete
        assert best.cohesion in (Cohesion.STACKED, Cohesion.ADJACENT_ROWS,
                                 Cohesion.SAME_ROW_SEPARATED)
        assert tuple(len(p) for p in best.parts) == (2, 2)

    def test_stacked_beats_offset_for_the_same_seat_quality(self):
        assert Cohesion.STACKED.score > Cohesion.ADJACENT_ROWS.score
        assert Cohesion.CONTIGUOUS.score > Cohesion.STACKED.score

    def test_solo_is_a_distinct_member_from_contiguous(self):
        """They score the same; they are not the same thing. Using the score
        as the enum value silently aliased them."""
        assert Cohesion.SOLO is not Cohesion.CONTIGUOUS
        assert Cohesion.SOLO.score == Cohesion.CONTIGUOUS.score
        assert len(set(Cohesion)) == 8

    def test_balanced_split_preferred_over_stranding_one_person(self):
        a = build_auditorium("v", "1", ["..××××××..", "..××××××.."])
        best = find_groups(a, 4)[0]
        assert sorted(len(p) for p in best.parts) == [2, 2]

    def test_aisle_break_is_distinguished_from_true_contiguity(self):
        a = build_auditorium("v", "1", ["..  .."])
        [best, *_] = find_groups(a, 4)
        assert best.cohesion is Cohesion.ACROSS_AISLE

    def test_party_of_one_is_solo_not_scattered(self):
        [best, *_] = find_groups(grid(5, 5), 1)
        assert best.cohesion is Cohesion.SOLO and best.size == 1

    def test_sold_out_house_yields_nothing(self):
        a = build_auditorium("v", "1", ["××××", "××××"])
        assert find_groups(a, 2) == []

    def test_incomplete_group_is_flagged_not_silently_shrunk(self):
        a = build_auditorium("v", "1", ["×.××", "××××"])
        [only] = find_groups(a, 4)
        assert not only.complete and only.size == 1

    def test_allow_split_false_returns_nothing_rather_than_a_split(self):
        a = build_auditorium("v", "1", ["..××××××..", "..××××××.."])
        assert find_groups(a, 4, allow_split=False) == []

    def test_quality_ranks_middle_over_front_among_contiguous_options(self):
        a = grid(8, 10)
        best = find_groups(a, 3)[0]
        assert best.seats[0].y > 0.3


class TestGraphOptimizerScenarios:
    def test_date_profile_does_not_make_larger_parties_impossible(self):
        from screenwatch.seating.groups import PartyKind, SeatRequest

        best = find_groups(
            grid(3, 10), SeatRequest(3, party_kind=PartyKind.DATE), limit=1
        )[0]
        assert best.complete

    def test_fifteen_people_are_not_forced_into_one_edge_heavy_row(self):
        from screenwatch.seating.groups import PartyKind, SeatRequest

        room = grid(10, 24)
        best = find_groups(
            room, SeatRequest(15, party_kind=PartyKind.FRIENDS), limit=1
        )[0]
        assert best.complete
        assert 2 <= len({seat.row_index for seat in best.seats}) <= 4
        assert max(len(part) for part in best.parts) < 15
        assert best.fairness > 0 and best.reasons

    def test_twenty_people_get_a_complete_compact_multirow_block(self):
        room = grid(10, 24)
        best = find_groups(room, 20, limit=1)[0]
        rows = sorted({seat.row_index for seat in best.seats})
        assert best.complete and len(rows) <= 4
        assert rows == list(range(rows[0], rows[-1] + 1))
        assert best.cohesion in (Cohesion.STACKED, Cohesion.ADJACENT_ROWS)

    def test_three_people_respect_loveseat_module_topology(self):
        from screenwatch.seating.groups import PartyKind, SeatRequest

        room = build_auditorium("v", "1", ["××llll××", "××llll××"])
        best = find_groups(
            room, SeatRequest(3, party_kind=PartyKind.FRIENDS), limit=1
        )[0]
        modules: dict[str, int] = {}
        for seat in best.seats:
            if seat.module_id:
                modules[seat.module_id] = modules.get(seat.module_id, 0) + 1
        assert best.complete and 2 in modules.values()
        assert len(best.parts) <= 2

    def test_explicit_family_bonds_drive_person_to_seat_assignment(self):
        from screenwatch.seating.groups import PartyBond, PartyKind, SeatRequest

        room = grid(6, 10)
        request = SeatRequest(
            5,
            party_kind=PartyKind.FAMILY,
            bonds=(PartyBond(0, 1, must_adjacent=True), PartyBond(2, 3, must_adjacent=True)),
        )
        best = find_groups(room, request, limit=1)[0]
        position = {person: best.seats[i] for i, person in enumerate(best.assignment)}
        for a, b in ((0, 1), (2, 3)):
            assert position[a].row_index == position[b].row_index
            assert abs(position[a].col_index - position[b].col_index) == 1

    def test_fragmented_twenty_person_room_can_use_more_than_four_components(self):
        room = build_auditorium("v", "1", ["....××....××....××...."] * 8)
        best = find_groups(room, 20, limit=1)[0]
        assert best.complete
        assert 5 <= len(best.parts) <= 6


class TestCertifiedRobustOptimizer:
    def test_small_room_is_globally_certified_against_every_subset(self):
        room = grid(3, 5)
        best = find_groups(room, 2, limit=1)[0]

        assert best.certificate is not None
        assert best.certificate.proven_optimal
        assert best.certificate.method == "exhaustive-global-enumeration"
        assert best.certificate.combinations_considered == 105  # 15 choose 2
        assert best.certificate.optimality_gap_upper_bound == 0.0
        assert best.pareto_optimal

    def test_large_room_never_pretends_the_bounded_search_is_exact(self):
        best = find_groups(grid(10, 24), 15, limit=1)[0]

        assert best.certificate is not None
        assert not best.certificate.proven_optimal
        assert best.certificate.method == "anytime-structured-search"
        assert 0.0 <= best.certificate.optimality_gap_upper_bound < 0.3

    def test_custom_large_relationship_assignment_is_not_overcertified(self):
        from screenwatch.seating.groups import PartyBond, SeatRequest

        room = grid(2, 4)
        request = SeatRequest(7, bonds=(PartyBond(0, 6, weight=1.0),))
        best = find_groups(room, request, limit=1)[0]

        assert best.certificate.method == "exhaustive-global-enumeration"
        assert not best.assignment_proven_optimal
        assert not best.certificate.proven_optimal
        assert "locally optimized" in best.certificate.scope

    def test_geometry_uncertainty_lowers_the_guaranteed_floor(self):
        exact_room = grid(8, 9)
        inferred_room = Auditorium(
            exact_room.venue_id,
            exact_room.screen_id,
            exact_room.seats,
            geometry_confidence=0.2,
        )

        exact = find_groups(exact_room, 1, limit=1)[0]
        inferred = find_groups(inferred_room, 1, limit=1)[0]
        assert exact.robustness == exact.quality
        assert inferred.robustness < inferred.quality
        assert inferred.certificate.geometry_confidence == 0.2

    def test_alternatives_are_distinct_pareto_safe_arrangements(self):
        groups = find_groups(grid(5, 7), 3, limit=4)
        seat_sets = [{seat.id for seat in group.seats} for group in groups]

        assert len(groups) == 4
        assert groups[0].pareto_optimal
        assert groups[0].certificate.proven_optimal
        assert all(not group.certificate.proven_optimal for group in groups[1:])
        assert len({frozenset(seats) for seats in seat_sets}) == 4
        assert any(
            len(seat_sets[0] & alternative) <= 1
            for alternative in seat_sets[1:]
        )

    def test_score_interval_collapses_for_exact_geometry(self):
        room = grid(6, 7)
        seat = room.rows()[3][3]
        model = QualityModel().for_auditorium(room)
        point = model.score(seat, row_count=room.row_count)

        assert model.score_interval(
            seat, row_count=room.row_count, geometry_confidence=1.0
        ) == (point, point)

    def test_solver_is_invariant_to_input_seat_order(self):
        room = build_auditorium("v", "1", ["..×....", ".......", "....×.."])
        reordered = Auditorium(
            room.venue_id,
            room.screen_id,
            tuple(reversed(room.seats)),
            geometry_confidence=room.geometry_confidence,
        )

        original = find_groups(room, 3, limit=1)[0]
        permuted = find_groups(reordered, 3, limit=1)[0]
        assert {seat.id for seat in original.seats} == {seat.id for seat in permuted.seats}
        assert original.objective == permuted.objective

    def test_unavailable_geometry_cannot_change_the_exact_winner(self):
        room = build_auditorium("v", "1", [".....", "..×..", "....."])
        baseline = find_groups(room, 2, limit=1)[0]
        # The sold seat is already part of the geometry but never part of the
        # feasible set; moving it to the end of the source feed is immaterial.
        sold = tuple(seat for seat in room.seats if not seat.is_open)
        open_seats = tuple(seat for seat in room.seats if seat.is_open)
        reordered = Auditorium("v", "1", open_seats + sold)
        changed = find_groups(reordered, 2, limit=1)[0]

        assert {seat.id for seat in baseline.seats} == {seat.id for seat in changed.seats}

    def test_identical_optimization_is_served_from_a_bounded_cache(self):
        from screenwatch.seating.groups import clear_seating_cache, seating_cache_info

        room = grid(6, 10)
        clear_seating_cache()
        first = find_groups(room, 8, limit=2)
        after_first = seating_cache_info()
        second = find_groups(room, 8, limit=2)
        after_second = seating_cache_info()

        assert first == second
        assert after_first.misses == 1 and after_first.hits == 0
        assert after_second.misses == 1 and after_second.hits == 1
        assert after_second.currsize <= after_second.maxsize == 128

    def test_availability_change_invalidates_the_optimizer_cache_key(self):
        from dataclasses import replace

        from screenwatch.seating.groups import clear_seating_cache, seating_cache_info
        from screenwatch.seating.model import SeatStatus

        room = grid(4, 7)
        clear_seating_cache()
        find_groups(room, 3, limit=1)
        changed = Auditorium(
            room.venue_id,
            room.screen_id,
            (replace(room.seats[0], status=SeatStatus.SOLD), *room.seats[1:]),
        )
        find_groups(changed, 3, limit=1)

        assert seating_cache_info().misses == 2


class TestRendering:
    def test_unicode_grid_has_screen_and_legend(self):
        out = to_unicode_grid(grid(5, 10))
        assert "SCREEN" in out and "free" in out

    def test_wide_house_is_downsampled_to_fit(self):
        out = to_unicode_grid(grid(30, 90), width=40)
        body = [l for l in out.splitlines() if l and l[0].isalpha()]
        assert all(len(l) <= 44 for l in body)
        assert "downsampled" in out

    def test_downsampling_never_makes_a_full_row_look_free(self):
        """Worst-status-wins: one free seat among sold ones in a bucket must
        not render the bucket as available."""
        a = build_auditorium("v", "1", ["×" * 89 + "."])
        out = to_unicode_grid(a, width=10)
        assert out.splitlines()[3].count("·") <= 1

    def test_tiny_house_renders_without_padding_artifacts(self):
        out = to_unicode_grid(grid(3, 4))
        assert "A ····" in out or "A ····" in out.replace("  ", " ")

    def test_picked_seats_are_highlighted(self):
        a = grid(4, 6)
        picked = {s.id for s in find_groups(a, 2)[0].seats}
        assert "▮" in to_unicode_grid(a, picked)

    def test_no_seatmap_renders_a_useful_message_not_an_empty_box(self):
        bare = Auditorium("v", "1", (), geometry_confidence=0.0, reported_available=42)
        out = to_unicode_grid(bare)
        assert "no seat map" in out and "42" in out

    def test_svg_is_self_contained(self):
        svg = to_svg(grid(10, 12))
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "http://" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert "viewBox" in svg

    def test_svg_handles_both_extremes(self):
        for rows, cols in [(3, 4), (40, 60)]:
            svg = to_svg(grid(rows, cols))
            assert svg.count("<rect") >= rows * cols

    def test_svg_marks_picked_seats_distinctly(self):
        a = grid(5, 5)
        svg = to_svg(a, {"A1"})
        assert "#f0883e" in svg


class TestDegradedSeatData:
    def test_count_only_auditorium_still_reports_availability(self):
        bare = Auditorium("v", "1", (), geometry_confidence=0.0, reported_available=12)
        assert not bare.has_grid and bare.available == 12

    def test_find_groups_on_a_gridless_auditorium_returns_nothing(self):
        bare = Auditorium("v", "1", (), geometry_confidence=0.0, reported_available=12)
        assert find_groups(bare, 2) == []


class TestAccessibility:
    def test_wheelchair_and_companion_seats_are_typed(self):
        a = build_auditorium("v", "1", ["wc..", "...."])
        kinds = {s.kind for s in a.seats}
        assert SeatKind.WHEELCHAIR in kinds and SeatKind.COMPANION in kinds

    def test_blocked_seats_are_not_bookable(self):
        a = build_auditorium("v", "1", ["#..#"])
        assert a.capacity == 2


def test_normalize_geometry_on_empty_input():
    assert normalize_geometry([]) == ()


class TestSeatPreferencesAreHonoured:
    """`SeatingPrefs` used to be collected and then ignored.

    `find_groups` was called with the party size and `allow_split` only, so a
    party asking for accessible seating got ordinary seats, a party asking for
    recliners got whatever was free, and a party that said it did not mind
    splitting up was still scored as though it did.
    """

    def room(self, layout):
        from screenwatch.seating.render import build_auditorium

        return build_auditorium("v", "1", layout)

    # -- accessible seats are not general stock ------------------------
    def test_wheelchair_seats_are_not_handed_to_a_party_that_did_not_ask(self):
        from screenwatch.seating.groups import SeatRequest, find_groups

        # The only run of three includes the wheelchair bay and its companion.
        room = self.room(["××wc.××"])
        groups = find_groups(room, SeatRequest(party_size=3))
        for group in groups:
            kinds = {s.kind for s in group.seats}
            assert SeatKind.WHEELCHAIR not in kinds
            assert SeatKind.COMPANION not in kinds

    def test_a_wheelchair_bay_breaks_adjacency_rather_than_being_stepped_over(self):
        from screenwatch.seating.groups import SeatRequest, find_groups

        # Two free standard seats, a wheelchair bay between them. That is not
        # a pair of neighbouring seats.
        room = self.room([".w.", "..."])
        groups = find_groups(room, SeatRequest(party_size=2))
        assert groups
        assert all(g.seats[0].row_index == 1 for g in groups)

    def test_a_party_needing_a_space_gets_the_space_and_a_companion_beside_it(self):
        from screenwatch.seating.groups import Cohesion, SeatRequest, find_groups

        room = self.room(["××××××", "wc...."])
        [group] = find_groups(
            room, SeatRequest(party_size=2, wheelchair_spaces=1, companion_seats=1)
        )
        assert {s.kind for s in group.seats} == {
            SeatKind.WHEELCHAIR, SeatKind.COMPANION
        }
        assert group.complete
        assert group.cohesion is Cohesion.CONTIGUOUS

    def test_the_rest_of_the_party_is_seated_near_the_space_not_across_the_room(self):
        from screenwatch.seating.groups import SeatRequest, find_groups

        room = self.room(["......", "......", "......", "wc...."])
        [group] = find_groups(
            room,
            SeatRequest(party_size=4, wheelchair_spaces=1, companion_seats=1),
        )
        assert group.size == 4
        # The two general seats sit in, or right beside, the accessible row.
        general = [s for s in group.seats if s.kind is SeatKind.STANDARD]
        assert all(abs(s.row_index - 3) <= 1 for s in general)

    def test_no_free_space_means_no_group_rather_than_ordinary_seats(self):
        from screenwatch.seating.groups import SeatRequest, find_groups

        room = self.room(["......", "......"])          # no accessible seating
        assert find_groups(
            room, SeatRequest(party_size=2, wheelchair_spaces=1, companion_seats=1)
        ) == []

    def test_accessible_allocation_honours_max_rows_and_no_split(self):
        from screenwatch.seating.groups import SeatRequest, find_groups

        room = self.room(["wc××××", "×××××", "...××"])
        base = dict(party_size=4, wheelchair_spaces=1, companion_seats=1)
        assert find_groups(room, SeatRequest(**base))
        assert find_groups(room, SeatRequest(**base, max_rows=1)) == []
        assert find_groups(room, SeatRequest(**base, allow_split=False)) == []

    # -- recliners ------------------------------------------------------
    def test_requiring_recliners_excludes_standard_seats(self):
        from screenwatch.seating.groups import RECLINING_KINDS, SeatRequest, find_groups

        room = self.room(["..rr..", "......"])
        groups = find_groups(room, SeatRequest(party_size=2, kinds=RECLINING_KINDS))
        assert groups
        assert all(s.kind in RECLINING_KINDS for g in groups for s in g.seats)

    # -- together=False --------------------------------------------------
    def test_a_party_happy_to_split_takes_the_best_seats_in_the_room(self):
        from screenwatch.seating.groups import SeatRequest, find_groups

        # Two together at the very front, or two excellent singles mid-house.
        room = self.room(["..××××", "×.××.×", "××××××"])
        [apart] = find_groups(room, SeatRequest(party_size=2, together=False))
        assert apart.size == 2
        together = find_groups(room, SeatRequest(party_size=2, together=True))
        assert together[0].seats[0].row_index == 0        # forced to the front row
        assert apart.quality >= together[0].quality
