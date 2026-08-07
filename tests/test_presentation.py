"""The classifier is where a wrong answer costs you a ticket, so it gets the
most adversarial tests: chain tokens, rep-house prose, and the venue oracle.
"""

from __future__ import annotations

import pytest

from screenwatch import presentation as pres
from screenwatch.models import (
    Attribute,
    Brand,
    Preference,
    Presentation,
    PresentationSpec,
    Projection,
)


class TestChainTokens:
    def test_imax_film_is_not_standard_film(self):
        """Both appear for the same film, same venue, same day. Verified live
        2026-08-02 at AMC Lincoln Square 13."""
        imax = pres.classify_token("amc", "imax70mm", "amc-lincoln-square-13")
        plain = pres.classify_token("amc", "70mm", "amc-lincoln-square-13")
        assert imax.projection is Projection.FILM_70MM_15PERF
        assert imax.brand is Brand.IMAX
        assert plain.projection is Projection.FILM_70MM
        assert plain.brand is Brand.NONE

    def test_laser_at_amc_is_not_imax(self):
        """'Laser at AMC' is AMC's non-IMAX laser brand. 40 of 58 screenings
        in the fixture carry it; misreading it as IMAX would drown the alert."""
        p = pres.classify_token("amc", "laseratamc", "amc-lincoln-square-13")
        assert p.brand is Brand.NONE
        assert p.projection is Projection.DIGITAL_LASER

    def test_dolby_cinema_carries_its_attributes(self):
        p = pres.classify_token("amc", "dolbycinemaatamcprime")
        assert p.brand is Brand.DOLBY_CINEMA
        assert Attribute.ATMOS in p.attrs

    def test_unknown_token_raises(self):
        with pytest.raises(pres.UnknownFormatError, match="unknown amc format token"):
            pres.classify_token("amc", "imax-with-laser-gt-ultra-2029")

    def test_ignored_token_is_not_an_error_but_carries_nothing(self):
        p = pres.classify_token("amc", "amcartisanfilms")
        assert p.projection is Projection.UNKNOWN and p.brand is Brand.NONE

    def test_chains_are_data_not_code(self):
        pres.register_chain(
            "testchain",
            {"nitrate-print": Presentation(projection=Projection.FILM_35MM_NITRATE)},
        )
        p = pres.classify_token("testchain", "nitrate-print")
        assert p.projection is Projection.FILM_35MM_NITRATE


class TestFreeText:
    """Rep houses put the format in prose. Verified against filmforum.org,
    whose event title is literally 'THE THIRD MAN in 35mm'."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("THE THIRD MAN in 35mm", Projection.FILM_35MM),
            ("Lawrence of Arabia — 70mm presentation", Projection.FILM_70MM),
            ("IMAX 70mm", Projection.FILM_70MM_15PERF),
            ("Presented in 15/70", Projection.FILM_70MM_15PERF),
            ("A rare NITRATE print", Projection.FILM_35MM_NITRATE),
            ("Screening on 16mm", Projection.FILM_16MM),
            ("New 4K DCP", Projection.DIGITAL),
        ],
    )
    def test_extracts_projection_from_prose(self, text, expected):
        p, confidence = pres.classify_text(text)
        assert p.projection is expected
        assert confidence > 0

    def test_specific_beats_general(self):
        """'IMAX 70mm' must not be read as plain 70mm, and 'nitrate' must not
        be read as ordinary 35mm."""
        assert pres.classify_text("IMAX 70mm")[0].projection is Projection.FILM_70MM_15PERF
        assert pres.classify_text("35mm nitrate print")[0].projection is Projection.FILM_35MM_NITRATE

    @pytest.mark.parametrize(
        "text",
        [
            "Shot on 35mm by Robby Müller",
            "Restored from the original 35mm camera negative",
            "filmed in 70mm and presented here in a new digital restoration",
        ],
    )
    def test_origination_language_is_not_a_projection_claim(self, text):
        """The most dangerous false positive: a synopsis describing what the
        film was *shot* on, when a DCP is what will actually be projected."""
        p, _ = pres.classify_text(text)
        assert p.projection is not Projection.FILM_35MM
        assert p.projection is not Projection.FILM_70MM

    def test_extracts_attributes(self):
        p, _ = pres.classify_text("SHERMAN'S MARCH (OC) — with Q&A, new restoration")
        assert Attribute.OPEN_CAPTION in p.attrs
        assert Attribute.Q_AND_A in p.attrs
        assert Attribute.RESTORATION in p.attrs

    def test_empty_text_is_not_a_guess(self):
        p, confidence = pres.classify_text("")
        assert p.projection is Projection.UNKNOWN and confidence == 0.0


class TestVenueMetadata:
    def test_missing_venue_hardware_does_not_fill_live_aspect(self):
        """A live token stays honest when no source-backed room claim exists."""
        gt = pres.classify_token("amc", "imaxwithlaseratamc", "amc-metreon-16")
        std = pres.classify_token("amc", "imaxwithlaseratamc", "amc-empire-25")
        assert gt.aspect is None
        assert std.aspect is None

    def test_no_packaged_hardware_overlay_is_loaded(self):
        assert not hasattr(pres, "_VENUES")
        assert pres.venue_capabilities("coolidge-corner") == []

    def test_hardware_summary_reports_observations_only(self):
        summary = pres.hardware_dataset_summary()
        assert summary["status"] == "observations-only"
        assert summary["records"] == 0
        assert summary["verified_records"] == 0

    def test_ambiguous_venue_is_left_alone(self):
        """Lincoln Square has both a 15/70 IMAX screen and a plain 70mm screen.
        With no brand to disambiguate, guessing would be worse than nothing."""
        p, _ = pres.classify_text("70mm", "amc-lincoln-square-13")
        assert p.projection is Projection.FILM_70MM
        assert p.aspect is None

    def test_never_contradicts_an_explicit_claim(self):
        """A venue observation cannot overrule what a source explicitly says."""
        p = pres.classify_token("amc", "imax70mm", "amc-empire-25")
        assert p.projection is Projection.FILM_70MM_15PERF

    def test_unknown_venue_is_a_no_op(self):
        p = pres.classify_token("amc", "imaxwithlaseratamc", "amc-not-in-table-1")
        assert p.aspect is None
        assert p.brand is Brand.IMAX

    def test_independent_capabilities_are_not_static_claims(self):
        assert pres.venue_capabilities("coolidge-corner") == []


class TestPreference:
    """Ranking is the user's, not the library's - the whole reason the format
    model is structured rather than a single ordered enum."""

    def test_ranks_in_declared_order(self):
        prefs = Preference([
            PresentationSpec(projection=Projection.FILM_70MM_15PERF, brand=Brand.IMAX,
                             label="IMAX 70mm"),
            PresentationSpec(brand=Brand.IMAX, aspect="1.43", label="IMAX GT laser"),
            PresentationSpec(brand=Brand.DOLBY_CINEMA, label="Dolby"),
        ])
        imax70 = pres.classify_token("amc", "imax70mm", "amc-lincoln-square-13")
        gt = Presentation(Projection.DIGITAL_LASER, Brand.IMAX, "1.43")
        dolby = pres.classify_token("amc", "dolbycinemaatamcprime")

        assert prefs.rank(imax70) == 0
        assert prefs.rank(gt) == 1
        assert prefs.rank(dolby) == 2
        assert prefs.rank(pres.classify_token("amc", "laseratamc")) is None

    def test_a_repertory_wish_needs_no_chain_concept(self):
        """'any 35mm anywhere' is expressible; a chain-format enum could not."""
        prefs = Preference([PresentationSpec(projection=Projection.FILM_35MM)])
        rep, _ = pres.classify_text("THE THIRD MAN in 35mm", "filmforum")
        assert prefs.wants(rep)

    def test_excludes_filter_out_unwanted_attributes(self):
        spec = PresentationSpec(
            brand=Brand.IMAX, excludes=frozenset({Attribute.THREE_D})
        )
        assert spec.matches(pres.classify_token("amc", "imax"))
        assert not spec.matches(pres.classify_token("amc", "imax3d"))

    def test_requires_matches_accessibility_needs(self):
        spec = PresentationSpec(requires=frozenset({Attribute.OPEN_CAPTION}))
        oc, _ = pres.classify_text("SHERMAN'S MARCH (OC)")
        assert spec.matches(oc)
        assert not spec.matches(pres.classify_token("amc", "laseratamc"))


class TestAssumeDigital:
    """Chains only label projection when it is unusual, so 3D and premium-brand
    showings arrive with none and render as bare "3D" or "unspecified"."""

    def test_fills_an_unstated_projection(self):
        p = pres.classify_token("amc", "reald3d")
        assert p.projection is Projection.UNKNOWN
        assert pres.assume_digital(p).projection is Projection.DIGITAL

    def test_never_overwrites_a_stated_one(self):
        for token in ("imax70mm", "70mm", "laseratamc"):
            p = pres.classify_token("amc", token)
            assert pres.assume_digital(p).projection is p.projection

    def test_can_never_invent_a_film_print(self):
        """The only dangerous direction. A wrong 'digital' says nothing; a
        wrong '70mm' sends someone across a city for a DCP."""
        assert not pres.assume_digital(Presentation()).projection.is_film

    def test_preserves_brand_and_attributes(self):
        p = pres.assume_digital(pres.classify_token("amc", "reald3d"))
        assert Attribute.THREE_D in p.attrs
