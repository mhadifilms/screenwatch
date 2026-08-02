from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from screenwatch.adapters.amc.showtimes import AmcShowtimesDom, AmcShowtimesRsc
from screenwatch.models import (
    Attribute,
    Availability,
    Brand,
    FactKey,
    Observation,
    Presentation,
    Projection,
)
from screenwatch.resolver import Resolver

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
START = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)

IMAX_FILM = Presentation(Projection.FILM_70MM_15PERF, Brand.IMAX, "1.43")
IMAX_LASER = Presentation(Projection.DIGITAL_LASER, Brand.IMAX, "1.43")
PLAIN_70 = Presentation(Projection.FILM_70MM)


def obs(source, presentation=IMAX_FILM, *, tier=1, avail=Availability.SELLABLE,
        offset=timedelta(0), age=timedelta(0), confidence=1.0,
        venue="amc-lincoln-square-13", movie="76238"):
    return Observation(
        key=FactKey(venue, movie, START + offset),
        source=source, tier=tier, observed_at=NOW - age,
        presentation=presentation, availability=avail,
        external_id="1", deeplink="https://x/1", confidence=confidence,
    )


class TestCorroboration:
    def test_two_agreeing_sources_produce_one_corroborated_fact(self):
        facts = Resolver().resolve(
            [obs("amc:showtimes-rsc"), obs("amc:showtimes-dom")], now=NOW
        )
        assert len(facts) == 1
        f = facts[0]
        assert f.corroborated and f.agreement == 1.0 and not f.needs_review
        assert f.sources == ("amc:showtimes-dom", "amc:showtimes-rsc")

    def test_single_source_is_reported_but_not_corroborated(self):
        [f] = Resolver().resolve([obs("amc:showtimes-rsc")], now=NOW)
        assert not f.corroborated
        assert f.agreement == 1.0 and not f.needs_review

    def test_minute_skew_still_corroborates(self):
        """Surfaces that round display time must not split into two screenings."""
        facts = Resolver().resolve(
            [obs("a"), obs("b", offset=timedelta(seconds=45))], now=NOW
        )
        assert len(facts) == 1 and facts[0].corroborated

    def test_genuinely_different_showtimes_stay_separate(self):
        facts = Resolver().resolve(
            [obs("a"), obs("a", offset=timedelta(hours=3))], now=NOW
        )
        assert len(facts) == 2


class TestPresentationVoting:
    def test_attributes_are_additive_not_conflicting(self):
        """One surface listing open-caption and another not is two views of
        one screening, not a disagreement."""
        a = obs("a", IMAX_FILM.with_(attrs=frozenset({Attribute.OPEN_CAPTION})))
        b = obs("b", IMAX_FILM.with_(attrs=frozenset({Attribute.ATMOS})))
        [f] = Resolver().resolve([a, b], now=NOW)
        assert not f.conflicts and f.agreement == 1.0
        assert f.presentation.attrs == {Attribute.OPEN_CAPTION, Attribute.ATMOS}

    def test_core_disagreement_is_recorded_not_smoothed(self):
        [f] = Resolver().resolve([obs("a", IMAX_FILM), obs("b", IMAX_LASER)], now=NOW)
        assert f.needs_review
        assert f.conflicts and f.agreement < 1.0

    def test_aspect_disagreement_counts_as_conflict(self):
        """1.43 vs 1.90 is the difference between worth-a-flight and not."""
        gt = Presentation(Projection.DIGITAL_LASER, Brand.IMAX, "1.43")
        std = Presentation(Projection.DIGITAL_LASER, Brand.IMAX, "1.90")
        [f] = Resolver().resolve([obs("a", gt), obs("b", std)], now=NOW)
        assert f.conflicts

    def test_health_weighting_lets_a_drifting_source_lose(self):
        pair = [obs("good", IMAX_FILM), obs("drifted", PLAIN_70)]
        assert Resolver().resolve(pair, now=NOW)[0].agreement == pytest.approx(0.5)

        [f] = Resolver(health={"drifted": 0.1}).resolve(pair, now=NOW)
        assert f.presentation.projection is Projection.FILM_70MM_15PERF
        assert f.agreement > 0.9

    def test_low_confidence_prose_loses_to_a_machine_token(self):
        """A rep-house title guess must not overrule a chain's own token."""
        [f] = Resolver().resolve(
            [obs("amc:showtimes-rsc", IMAX_FILM, confidence=1.0),
             obs("jsonld:somewhere", PLAIN_70, tier=0, confidence=0.35)],
            now=NOW,
        )
        assert f.presentation.projection is Projection.FILM_70MM_15PERF

    def test_higher_tier_breaks_a_two_way_tie(self):
        [f] = Resolver().resolve(
            [obs("cheap", IMAX_LASER, tier=0), obs("guarded", IMAX_FILM, tier=3)],
            now=NOW,
        )
        assert f.presentation.projection is Projection.FILM_70MM_15PERF


class TestAvailability:
    def test_takes_the_most_pessimistic_claim(self):
        """A wasted browser launch during an on-sale costs more than a missed
        poll, so disagreement resolves toward 'do not bother'."""
        [f] = Resolver().resolve(
            [obs("a", avail=Availability.SELLABLE), obs("b", avail=Availability.SOLD_OUT)],
            now=NOW,
        )
        assert f.availability is Availability.SOLD_OUT

    def test_unknown_never_outvotes_a_real_reading(self):
        [f] = Resolver().resolve(
            [obs("a", avail=Availability.UNKNOWN), obs("b", avail=Availability.SELLABLE)],
            now=NOW,
        )
        assert f.availability is Availability.SELLABLE


class TestFreshness:
    def test_stale_observations_are_dropped(self):
        assert Resolver(ttl=timedelta(minutes=30)).resolve(
            [obs("old", age=timedelta(hours=2))], now=NOW
        ) == []

    def test_a_stale_source_cannot_keep_a_dead_screening_alive(self):
        facts = Resolver(ttl=timedelta(minutes=30)).resolve(
            [obs("fresh", IMAX_FILM), obs("stale", PLAIN_70, age=timedelta(days=1))],
            now=NOW,
        )
        assert len(facts) == 1
        assert facts[0].presentation.projection is Projection.FILM_70MM_15PERF
        assert not facts[0].conflicts


class TestEndToEnd:
    def test_real_fixture_resolves_to_fully_corroborated_facts(self, amc_showtimes_html):
        rsc = AmcShowtimesRsc().parse(amc_showtimes_html, observed_at=NOW)
        dom = AmcShowtimesDom().parse(amc_showtimes_html, observed_at=NOW)

        facts = Resolver().resolve(rsc + dom, now=NOW)
        assert len(facts) == 58
        assert all(f.corroborated for f in facts)
        assert not [f for f in facts if f.needs_review]

        imax70 = [f for f in facts
                  if f.presentation.projection is Projection.FILM_70MM_15PERF]
        assert len(imax70) == 1
        assert imax70[0].availability is Availability.SOLD_OUT
        assert imax70[0].presentation.aspect == "1.43"
        assert imax70[0].deeplink.startswith("https://www.amctheatres.com/showtimes/")
