"""Ranking tests, headed by the scenario that motivated the whole design.

Everything here is offline and deterministic: synthetic auditoriums, fixed
clock, injected seat fetcher. Ranking quality is a judgement encoded in
weights, and the only way to keep it honest as the weights move is to pin the
judgements as tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from screenwatch.identity.work import Work
from screenwatch.models import (
    Attribute,
    Availability,
    Brand,
    Preference,
    Presentation,
    PresentationSpec,
    Projection,
)
from screenwatch.ranking.candidate import Option, Screening
from screenwatch.ranking.coarse import coarse_rank, lateness, score_screening
from screenwatch.ranking.explain import annotate, compare, describe_seats, narrate
from screenwatch.ranking.fine import fine_rank
from screenwatch.ranking.spec import (
    Budget,
    DateWindow,
    GeoPoint,
    LocationSpec,
    Membership,
    SearchSpec,
    SeatingPrefs,
    TimeWindow,
    Weights,
)
from screenwatch.identity.work import WorkRef
from screenwatch.seating.model import Auditorium, SeatDataUnavailable
from screenwatch.seating.render import build_auditorium

SPIDER_MAN = Work(work_id="tmdb:900", title="Spider-Man: Brand New Day", year=2026)

FLAT = Presentation(Projection.DIGITAL_LASER, Brand.NONE, raw="laseratamc")
THREE_D = Presentation(
    Projection.DIGITAL_LASER, Brand.NONE, attrs=frozenset({Attribute.THREE_D}), raw="reald3d"
)
IMAX_FILM = Presentation(Projection.FILM_70MM_15PERF, Brand.IMAX, "1.43", raw="imax70mm")


def local(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 8, day, hour, minute)


def screening(
    sid: str,
    *,
    at: datetime,
    presentation: Presentation = FLAT,
    venue: str = "amc-metreon-16",
    chain: str = "amc",
    availability: Availability = Availability.ALMOST_FULL,
    distance_km: float = 5.0,
) -> Screening:
    return Screening(
        screening_id=sid,
        work=SPIDER_MAN,
        venue_id=venue,
        venue_name=venue,
        chain=chain,
        starts_at_utc=at.replace(tzinfo=timezone.utc) + timedelta(hours=7),
        starts_at_local=at,
        presentation=presentation,
        availability=availability,
        distance_km=distance_km,
        deeplink=f"https://example.test/{sid}",
    )


# --------------------------------------------------------------------------
class TestTheMotivatingScenario:
    """9pm, day after launch, four people, nearly everything sold out.

      A. 11:30pm, 2D — only second-row seats, and they do not seat four.
      B. 12:45am, 3D — third row, two pairs (2+2) in adjacent rows.

    B is later and in a format the user ranks lower, but it is the only one
    that actually seats the party. It must win, and it must say why.
    """

    @pytest.fixture
    def spec(self):
        return SearchSpec(
            work=WorkRef(query="spider-man"),
            party_size=4,
            date_window=DateWindow(local(2).date(), local(3).date()),
            time_windows=(TimeWindow(start=local(18).time(), end=local(2).time()),),
            presentations=Preference([
                PresentationSpec(excludes=frozenset({Attribute.THREE_D}), label="2D"),
                PresentationSpec(requires=frozenset({Attribute.THREE_D}), label="3D"),
            ]),
            location=LocationSpec(origin=GeoPoint(37.78, -122.40), radius_km=40),
        )

    @pytest.fixture
    def auditoriums(self):
        # A: second row only, and just three seats - cannot fit four.
        second_row_only = build_auditorium("amc-metreon-16", "a", [
            "××××××××××",
            "×××...××××",
            "××××××××××",
            "××××××××××",
            "××××××××××",
            "××××××××××",
        ])
        # B: two pairs, lined up in adjacent rows around the third row.
        two_pairs = build_auditorium("amc-metreon-16", "b", [
            "××××××××××",
            "××××××××××",
            "×××..×××××",
            "×××..×××××",
            "××××××××××",
            "××××××××××",
        ])
        return {"A": second_row_only, "B": two_pairs}

    @pytest.fixture
    def ranked(self, spec, auditoriums):
        options = coarse_rank(
            [
                screening("A", at=local(23, 30), presentation=FLAT),
                screening("B", at=local(0, 45, day=3), presentation=THREE_D),
            ],
            spec,
        )
        ranked = fine_rank(
            options, spec, lambda o: auditoriums[o.screening.screening_id]
        )
        return annotate(ranked, spec)

    def test_the_option_that_seats_everyone_wins(self, ranked):
        assert ranked[0].screening.screening_id == "B"
        assert ranked[0].can_seat_party is True

    def test_it_wins_despite_being_later_and_a_worse_format(self, ranked):
        winner, loser = ranked[0], ranked[1]
        assert winner.components["lateness"] < loser.components["lateness"]
        assert winner.components["format_fit"] < loser.components["format_fit"]
        assert winner.score > loser.score

    def test_the_loser_is_correctly_diagnosed_as_not_fitting_the_party(self, ranked):
        loser = ranked[1]
        assert loser.can_seat_party is False
        assert any("only seats 3 of 4" in t for t in loser.tradeoffs)

    def test_the_winner_names_the_split_honestly(self, ranked, spec):
        assert any("splits 2+2" in t for t in ranked[0].tradeoffs)
        assert "2+2" in describe_seats(ranked[0], spec)

    def test_the_comparison_reads_like_the_reasoning_a_person_would_give(self, ranked, spec):
        text = compare(ranked[0], ranked[1], spec)
        assert "fitting your whole party" in text or "sitting together" in text
        assert "despite" in text

    def test_narration_mentions_both_the_upside_and_the_cost(self, ranked, spec):
        text = narrate(ranked, spec)
        assert "for:" in text and "against:" in text
        assert "3d" in text.lower() or "split" in text.lower()


# --------------------------------------------------------------------------
class TestCoarsePhase:
    def base_spec(self, **kw):
        return SearchSpec(work=WorkRef(query="x"), **kw)

    def test_sold_out_is_excluded_unless_watching(self):
        shows = [screening("s", at=local(20), availability=Availability.SOLD_OUT)]
        assert coarse_rank(shows, self.base_spec()) == []
        assert coarse_rank(shows, self.base_spec(include_sold_out=True))

    def test_time_window_wrapping_past_midnight_admits_late_shows(self):
        """'Tonight after 6pm' has to include a 12:45am start, or the whole
        motivating scenario is filtered out before ranking begins."""
        spec = self.base_spec(
            time_windows=(TimeWindow(start=local(18).time(), end=local(2).time()),)
        )
        assert coarse_rank([screening("late", at=local(0, 45, day=3))], spec)
        assert coarse_rank([screening("morning", at=local(10))], spec) == []

    def test_preferred_format_outranks_others_all_else_equal(self):
        spec = self.base_spec(
            presentations=Preference([
                PresentationSpec(brand=Brand.IMAX, label="imax"),
                PresentationSpec(label="anything"),
            ])
        )
        ranked = coarse_rank(
            [screening("flat", at=local(20)),
             screening("imax", at=local(20), presentation=IMAX_FILM)],
            spec,
        )
        assert ranked[0].screening.screening_id == "imax"

    def test_unlisted_format_scores_low_but_is_not_dropped(self):
        """With everything sold out, a format you did not ask for still beats
        not going."""
        spec = self.base_spec(
            presentations=Preference([PresentationSpec(brand=Brand.IMAX, label="imax")])
        )
        [only] = coarse_rank([screening("flat", at=local(20))], spec)
        assert 0 < only.components["format_fit"] < 0.5

    def test_membership_boosts_but_never_filters(self):
        spec = self.base_spec(memberships=frozenset({Membership.AMC_ALIST}))
        ranked = coarse_rank(
            [screening("alamo", at=local(20), chain="alamo"),
             screening("amc", at=local(20), chain="amc")],
            spec,
        )
        assert [o.screening.screening_id for o in ranked] == ["amc", "alamo"]
        assert len(ranked) == 2

    def test_distance_decays_with_radius(self):
        spec = self.base_spec(
            location=LocationSpec(origin=GeoPoint(37.78, -122.40), radius_km=20)
        )
        near = score_screening(screening("n", at=local(20), distance_km=2), spec)
        far = score_screening(screening("f", at=local(20), distance_km=35), spec)
        assert near.components["distance_fit"] > far.components["distance_fit"]

    def test_budget_excludes_unaffordable_screenings(self):
        spec = self.base_spec(party_size=2, budget=Budget(max_total_usd=30))
        cheap = screening("cheap", at=local(20))
        pricey = screening("pricey", at=local(20))
        object.__setattr__(cheap, "price_hint_usd", 12.0)
        object.__setattr__(pricey, "price_hint_usd", 26.0)
        ids = [o.screening.screening_id for o in coarse_rank([cheap, pricey], spec)]
        assert ids == ["cheap"]

    @pytest.mark.parametrize(
        "hour,minute,expected_full", [(19, 0, True), (21, 30, True), (23, 30, False), (0, 45, False)]
    )
    def test_lateness_penalty_kicks_in_after_ten(self, hour, minute, expected_full):
        spec = self.base_spec()
        value = lateness(screening("s", at=local(hour, minute)), spec)
        assert (value == 1.0) is expected_full

    def test_lateness_orders_late_night_correctly(self):
        spec = self.base_spec()
        assert lateness(screening("a", at=local(23, 30)), spec) > lateness(
            screening("b", at=local(0, 45, day=3)), spec
        )


# --------------------------------------------------------------------------
class TestFinePhase:
    def spec(self, **kw):
        kw.setdefault("party_size", 2)
        return SearchSpec(work=WorkRef(query="x"), **kw)

    def test_only_the_budgeted_number_of_seat_maps_are_fetched(self):
        spec = self.spec(max_seatmap_fetches=2)
        options = coarse_rank(
            [screening(str(i), at=local(19 + i % 3)) for i in range(6)], spec
        )
        calls: list[str] = []

        def fetch(option):
            calls.append(option.screening.screening_id)
            return build_auditorium("v", "1", ["....", "...."])

        fine_rank(options, spec, fetch)
        assert len(calls) == 2

    def test_unfetched_options_are_kept_not_dropped(self):
        spec = self.spec(max_seatmap_fetches=1)
        options = coarse_rank([screening(str(i), at=local(20)) for i in range(4)], spec)
        ranked = fine_rank(
            options, spec, lambda o: build_auditorium("v", "1", ["....", "...."])
        )
        assert len(ranked) == 4
        assert sum(o.seat_data == "not_fetched" for o in ranked) == 3

    def test_a_venue_with_no_seat_data_still_ranks(self):
        """Graceful degradation: missing seat data must not sink an option
        below one we simply have not looked at."""
        spec = self.spec()

        def fetch(option):
            raise SeatDataUnavailable("venue does not expose seats")

        [only] = fine_rank(coarse_rank([screening("s", at=local(20))], spec), spec, fetch)
        assert only.seat_data == "unavailable"
        assert only.score > 0
        assert only.can_seat_party is None

    def test_count_only_auditorium_is_labelled(self):
        spec = self.spec()
        bare = Auditorium("v", "1", (), geometry_confidence=0.0, reported_available=9)
        [only] = fine_rank(
            coarse_rank([screening("s", at=local(20))], spec), spec, lambda o: bare
        )
        assert only.seat_data == "count_only"
        assert "9 seats left" in describe_seats(only, spec)

    def test_together_beats_apart_at_equal_screening_score(self):
        spec = self.spec(party_size=2)
        rooms = {
            "together": build_auditorium("v", "1", ["××××", "×..×"]),
            "apart": build_auditorium("v", "1", [".××.", "××××"]),
        }
        options = coarse_rank(
            [screening("together", at=local(20)), screening("apart", at=local(20))], spec
        )
        ranked = fine_rank(options, spec, lambda o: rooms[o.screening.screening_id])
        assert ranked[0].screening.screening_id == "together"

    def test_better_seats_break_a_tie_between_identical_showings(self):
        spec = self.spec(party_size=2)
        rooms = {
            "front": build_auditorium("v", "1", ["..××", "××××", "××××", "××××"]),
            "middle": build_auditorium("v", "1", ["××××", "××××", "..××", "××××"]),
        }
        options = coarse_rank(
            [screening("front", at=local(20)), screening("middle", at=local(20))], spec
        )
        ranked = fine_rank(options, spec, lambda o: rooms[o.screening.screening_id])
        assert ranked[0].screening.screening_id == "middle"

    def test_allow_split_false_reports_the_party_cannot_be_seated(self):
        spec = SearchSpec(
            work=WorkRef(query="x"),
            party_size=4,
            seating=SeatingPrefs(allow_split=False),
        )
        room = build_auditorium("v", "1", ["..××××××..", "..××××××.."])
        [only] = fine_rank(
            coarse_rank([screening("s", at=local(20))], spec), spec, lambda o: room
        )
        assert only.seats is None
        assert only.components["party_fit"] == 0.6


# --------------------------------------------------------------------------
class TestWeightsAreTunable:
    def test_caring_only_about_format_flips_the_motivating_result(self):
        """The defaults encode a judgement, not a law. A user who would
        rather sit apart than watch 3D must be able to say so."""
        spec = SearchSpec(
            work=WorkRef(query="x"),
            party_size=4,
            presentations=Preference([
                PresentationSpec(excludes=frozenset({Attribute.THREE_D}), label="2D"),
                PresentationSpec(requires=frozenset({Attribute.THREE_D}), label="3D"),
            ]),
            weights=Weights(format_fit=8.0, group_cohesion=0.1, party_fit=0.1,
                            seat_quality=0.1, lateness=0.1),
        )
        rooms = {
            "A": build_auditorium("v", "1", ["×××...××××"]),
            "B": build_auditorium("v", "1", ["×××..×××××", "×××..×××××"]),
        }
        options = coarse_rank(
            [screening("A", at=local(23, 30), presentation=FLAT),
             screening("B", at=local(0, 45, day=3), presentation=THREE_D)],
            spec,
        )
        ranked = fine_rank(options, spec, lambda o: rooms[o.screening.screening_id])
        assert ranked[0].screening.screening_id == "A"


def test_narrate_handles_no_results():
    assert "No screenings matched" in narrate([], SearchSpec(work=WorkRef(query="x")))


class TestDiversification:
    """A live search returned the same film, format and venue at three
    different times as its entire top three - correct ranking, useless output.
    """

    def options(self, specs):
        out = []
        for i, (venue, hour, fmt) in enumerate(specs):
            s = screening(f"s{i}", at=local(hour), venue=venue, presentation=fmt)
            out.append(Option(screening=s, score=1.0 - i * 0.001))
        return out

    def test_reorders_rather_than_filters(self):
        from screenwatch.ranking.diversify import diversify

        opts = self.options([("v1", 10, FLAT), ("v1", 13, FLAT), ("v1", 16, FLAT),
                             ("v2", 11, FLAT)])
        out = diversify(opts, per_group=2)
        assert len(out) == 4, "every option must survive - only the order changes"
        assert {o.screening.screening_id for o in out} == {"s0", "s1", "s2", "s3"}

    def test_other_venues_get_a_turn_before_a_third_repeat(self):
        from screenwatch.ranking.diversify import diversify

        opts = self.options([("v1", 10, FLAT), ("v1", 13, FLAT), ("v1", 16, FLAT),
                             ("v2", 11, FLAT)])
        ids = [o.screening.screening_id for o in diversify(opts, per_group=2)]
        assert ids.index("s3") < ids.index("s2")

    def test_formats_at_one_venue_are_distinct_groups(self):
        from screenwatch.ranking.diversify import diversify

        opts = self.options([("v1", 10, FLAT), ("v1", 13, FLAT),
                             ("v1", 16, THREE_D)])
        ids = [o.screening.screening_id for o in diversify(opts, per_group=2)]
        assert ids == ["s0", "s1", "s2"], "different formats need no separation"

    def test_zero_disables_it(self):
        from screenwatch.ranking.diversify import diversify

        opts = self.options([("v1", 10, FLAT), ("v1", 13, FLAT)])
        assert diversify(opts, per_group=0) == opts

    def test_best_option_stays_first(self):
        from screenwatch.ranking.diversify import diversify

        opts = self.options([("v1", 10, FLAT), ("v1", 13, FLAT), ("v2", 11, FLAT)])
        assert diversify(opts, per_group=1)[0].screening.screening_id == "s0"


class TestSeatBudgetCountsSuccesses:
    """A chain with no seat surface raises instantly. If those attempts spent
    the budget, one such option at the top would mean no seat map at all.
    """

    def spec(self, **kw):
        kw.setdefault("party_size", 2)
        return SearchSpec(work=WorkRef(query="x"), **kw)

    def test_unsupported_options_do_not_consume_the_budget(self):
        spec = self.spec(max_seatmap_fetches=1)
        options = coarse_rank(
            [screening("noseats", at=local(19), chain="regal"),
             screening("hasseats", at=local(20), chain="amc")],
            spec,
        )
        room = build_auditorium("v", "1", ["....", "...."])

        def fetch(option):
            if option.screening.chain == "regal":
                raise SeatDataUnavailable("no seat surface")
            return room

        ranked = fine_rank(options, spec, fetch)
        by_id = {o.screening.screening_id: o for o in ranked}
        assert by_id["noseats"].seat_data == "unavailable"
        assert by_id["hasseats"].seat_data == "grid"

    def test_budget_still_caps_successful_fetches(self):
        spec = self.spec(max_seatmap_fetches=2)
        options = coarse_rank(
            [screening(str(i), at=local(19 + i % 3)) for i in range(6)], spec
        )
        calls = []

        def fetch(option):
            calls.append(option.screening.screening_id)
            return build_auditorium("v", "1", ["....", "...."])

        ranked = fine_rank(options, spec, fetch)
        assert len(calls) == 2
        assert sum(o.seat_data == "grid" for o in ranked) == 2

    def test_attempts_are_capped_when_everything_is_unsupported(self):
        spec = self.spec(max_seatmap_fetches=2)
        options = coarse_rank(
            [screening(str(i), at=local(19 + i % 3)) for i in range(40)], spec
        )
        calls = []

        def fetch(option):
            calls.append(option.screening.screening_id)
            raise SeatDataUnavailable("none anywhere")

        fine_rank(options, spec, fetch)
        assert len(calls) <= 2 * 5, "attempt cap must stop a long unsupported tail"
