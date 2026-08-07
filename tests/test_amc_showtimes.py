"""Replay tests over captured payloads.

The assertions that earn their keep are the ones about *disagreement*: that
the two parse paths see the same screenings, and that unknown format tokens
raise. Anything that merely asserts "parsing produced some rows" would stay
green through the failure modes that actually cost you tickets.
"""

from __future__ import annotations

import re

import pytest

from screenwatch import presentation as pres
from screenwatch.adapters.amc.showtimes import (
    AmcShowtimesDom,
    AmcShowtimesRsc,
    _split_aria,
)
from screenwatch.adapters.base import ParseError
from screenwatch.models import Availability, Brand, Projection
from screenwatch.rsc import FlightPayloadError, extract_flight


class TestAriaChain:
    def test_splits_venue_slug_containing_digits_and_hyphens(self):
        aria = (
            "the-odyssey-76238 "
            "the-odyssey-76238-amc-lincoln-square-13 "
            "the-odyssey-76238-amc-lincoln-square-13-imax70mm "
            "the-odyssey-76238-amc-lincoln-square-13-imax70mm-0"
        )
        assert _split_aria(aria) == (
            "76238", "amc-lincoln-square-13", "imax70mm", "the-odyssey"
        )

    def test_recovers_the_title_slug_not_just_the_id(self):
        """AMC's numeric id is a product id. Identity resolution needs a title,
        and without the slug every screening resolves to the string "76238"
        and matches no search query at all."""
        aria = ("blade-runner-2049-12345 "
                "blade-runner-2049-12345-amc-empire-25 "
                "blade-runner-2049-12345-amc-empire-25-imax")
        movie_id, venue_id, token, slug = _split_aria(aria)
        assert (movie_id, slug) == ("12345", "blade-runner-2049")

    @pytest.mark.parametrize("aria", ["", "only-one-token", "a b"])
    def test_raises_on_malformed_chain(self, aria):
        with pytest.raises(ParseError):
            _split_aria(aria)

    def test_raises_when_tokens_stop_nesting(self):
        with pytest.raises(ParseError, match="does not extend"):
            _split_aria("movie-1 unrelated-2 unrelated-2-imax")


class TestRscParser:
    def test_extracts_every_screening(self, amc_showtimes_html, now):
        obs = AmcShowtimesRsc().parse(amc_showtimes_html, observed_at=now)
        assert len(obs) == 58
        assert all(o.external_id and o.deeplink for o in obs)

    def test_finds_the_imax_film_screening(self, amc_showtimes_html, now):
        obs = AmcShowtimesRsc().parse(amc_showtimes_html, observed_at=now)
        imax70 = [
            o for o in obs
            if o.presentation.projection is Projection.FILM_70MM_15PERF
        ]
        assert len(imax70) == 1
        assert imax70[0].key.venue_id == "amc-lincoln-square-13"
        assert imax70[0].key.movie_id == "76238"
        assert imax70[0].presentation.brand is Brand.IMAX
        # The source does not publish aspect ratio, and the packaged hardware
        # overlay is intentionally unverified, so this stays unknown.
        assert imax70[0].presentation.aspect is None

    def test_separates_imax_film_from_standard_70mm(self, amc_showtimes_html, now):
        """The same film, the same venue, the same day, two different
        projectors. Conflating them is the bug this project exists to avoid."""
        obs = AmcShowtimesRsc().parse(amc_showtimes_html, observed_at=now)
        imax = [o for o in obs if o.presentation.projection is Projection.FILM_70MM_15PERF]
        plain = [o for o in obs if o.presentation.projection is Projection.FILM_70MM]
        assert len(imax) == 1 and len(plain) == 4
        assert {o.key.movie_id for o in imax} == {o.key.movie_id for o in plain}

    def test_availability_is_read_without_a_seatmap(self, amc_showtimes_html, now):
        obs = AmcShowtimesRsc().parse(amc_showtimes_html, observed_at=now)
        seen = {o.availability for o in obs}
        assert Availability.SOLD_OUT in seen
        assert Availability.ALMOST_FULL in seen
        assert Availability.SELLABLE in seen
        assert Availability.UNKNOWN not in seen

    def test_rejects_challenge_page_instead_of_reporting_empty(self):
        with pytest.raises(FlightPayloadError):
            extract_flight("<html><body>Queue-it interstitial</body></html>")

    def test_empty_result_raises_rather_than_looking_like_a_quiet_day(self):
        html = 'x<script>self.__next_f.push([1,"no showtimes here"])</script>'
        with pytest.raises(ParseError, match="no showtimes"):
            AmcShowtimesRsc().parse(html)


class TestDomParser:
    def test_extracts_every_screening_including_sold_out(self, amc_showtimes_html, now):
        """Sold-out times render as <button disabled> with no href and no id.
        An anchor-anchored parser silently drops them - and they are exactly
        the ones you watch for returned seats."""
        obs = AmcShowtimesDom().parse(amc_showtimes_html, observed_at=now)
        assert len(obs) == 58
        sold_out = [o for o in obs if o.availability is Availability.SOLD_OUT]
        assert len(sold_out) == 1
        assert sold_out[0].presentation.projection is Projection.FILM_70MM_15PERF

    def test_agrees_with_rsc_on_identity_and_presentation(self, amc_showtimes_html, now):
        """Two independent parse paths, one document. Divergence here means
        one of them drifted - which is the whole point of running both."""
        rsc = {o.external_id: o for o in AmcShowtimesRsc().parse(amc_showtimes_html, observed_at=now)}
        dom = {o.external_id: o for o in AmcShowtimesDom().parse(amc_showtimes_html, observed_at=now)}

        assert rsc.keys() == dom.keys()
        for sid, a in rsc.items():
            b = dom[sid]
            assert a.key == b.key, f"identity mismatch on {sid}"
            assert a.presentation == b.presentation, f"presentation mismatch on {sid}"
            assert a.availability is b.availability, f"availability mismatch on {sid}"


class TestFormatCoverage:
    def test_every_token_in_the_fixture_is_known(self, amc_showtimes_html):
        """The guard against silent drift: if AMC ships a new format token,
        this fails in CI on the next capture instead of at 00:01 on-sale."""
        payload = extract_flight(amc_showtimes_html)
        tokens = set()
        for m in re.finditer(r'"aria-describedby":"([^"]+)"', payload):
            try:
                tokens.add(_split_aria(m.group(1))[2])
            except ParseError:
                continue
        assert tokens, "no aria chains found - parser precondition broken"

        known = pres.known_tokens("amc")
        unknown = {t for t in tokens if pres.normalize_token(t) not in known}
        assert not unknown, f"unclassified format tokens: {sorted(unknown)}"

    def test_strict_false_degrades_instead_of_raising(self, amc_showtimes_html, now):
        """Runtime wants the poll to survive one novel token; CI does not."""
        html = amc_showtimes_html.replace("imax70mm", "imaxsomethingnew")
        with pytest.raises(pres.UnknownFormatError):
            AmcShowtimesRsc().parse(html, observed_at=now, strict=True)

        obs = AmcShowtimesRsc().parse(html, observed_at=now, strict=False)
        assert obs
        # The prose fallback still recovers the brand from a novel token.
        novel = [o for o in obs if o.presentation.raw == "imaxsomethingnew"]
        assert novel and novel[0].presentation.brand is Brand.IMAX
