"""The generic independent-cinema adapter, tested against real captured markup.

The headline case is unhappy: filmforum.org publishes valid ScreeningEvent
records whose startDate is empty. Returning [] there would be indistinguishable
from a dark venue, so the adapter must raise something specific enough to route
to an HTML fallback.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from screenwatch.adapters.base import ParseError
from screenwatch.adapters.generic.jsonld import (
    IncompleteStructuredData,
    JsonLdScreenings,
    _walk,
    slugify,
)
from screenwatch.models import Attribute, Availability, Projection

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def page(payload: dict | list) -> str:
    return (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps(payload)
        + "</script></head><body></body></html>"
    )


def screening(**kw) -> dict:
    base = {
        "@type": "ScreeningEvent",
        "name": "THE THIRD MAN in 35mm",
        "startDate": "2026-08-02T19:30:00-04:00",
        "url": "https://filmforum.org/film/the-third-man-2026",
        "location": {"@type": "MovieTheater", "name": "Film Forum"},
    }
    base.update(kw)
    return base


class TestGraphWalking:
    def test_finds_events_nested_in_a_collection_page(self, filmforum_home):
        """Film Forum nests them CollectionPage -> ItemList -> ListItem -> item.
        A top-level @type check finds nothing at all."""
        obs = JsonLdScreenings("filmforum").parse(
            filmforum_home.replace('"startDate": ""', '"startDate": "2026-08-02T19:30:00-04:00"'),
            observed_at=NOW,
            strict=False,
        )
        assert obs, "expected nested ScreeningEvents to be discovered"

    def test_follows_at_graph(self):
        html = page({"@context": "http://schema.org", "@graph": [screening()]})
        assert len(JsonLdScreenings("v").parse(html, observed_at=NOW)) == 1

    def test_walk_terminates_on_self_reference(self):
        node: dict = {"@type": "ScreeningEvent"}
        node["self"] = node
        assert len(list(_walk(node))) == 1


class TestRealFilmForumMarkup:
    def test_decorative_markup_raises_instead_of_returning_empty(self, filmforum_home):
        """The actual captured page: 7 ScreeningEvent nodes, every startDate
        empty. This must never look like 'no screenings today'."""
        with pytest.raises(IncompleteStructuredData) as exc:
            JsonLdScreenings("filmforum").parse(filmforum_home, observed_at=NOW)

        assert exc.value.found >= 1
        assert exc.value.missing == "startDate"
        assert "decorative" in str(exc.value)
        assert "HTML fallback" in str(exc.value)

    def test_it_is_a_parse_error_so_a_poller_can_catch_one_type(self, filmforum_home):
        with pytest.raises(ParseError):
            JsonLdScreenings("filmforum").parse(filmforum_home, observed_at=NOW)


class TestParsing:
    def test_reads_format_from_the_event_title(self):
        [o] = JsonLdScreenings("filmforum").parse(page(screening()), observed_at=NOW)
        assert o.presentation.projection is Projection.FILM_35MM
        assert o.key.venue_id == "filmforum"
        assert o.title == "THE THIRD MAN in 35mm"

    def test_normalizes_offsets_to_utc(self):
        [o] = JsonLdScreenings("v").parse(page(screening()), observed_at=NOW)
        assert o.key.starts_at_utc == datetime(2026, 8, 2, 23, 30, tzinfo=UTC)

    def test_reads_availability_from_offers(self):
        sold_out = screening(offers={"@type": "Offer",
                                     "availability": "https://schema.org/SoldOut"})
        [o] = JsonLdScreenings("v").parse(page(sold_out), observed_at=NOW)
        assert o.availability is Availability.SOLD_OUT

    def test_prose_confidence_stays_below_a_machine_token(self):
        [o] = JsonLdScreenings("v").parse(page(screening()), observed_at=NOW)
        assert o.confidence < 1.0

    def test_picks_up_attributes_from_the_title(self):
        oc = screening(name="SHERMAN'S MARCH (OC)")
        [o] = JsonLdScreenings("v").parse(page(oc), observed_at=NOW)
        assert Attribute.OPEN_CAPTION in o.presentation.attrs


class TestFailureModes:
    def test_no_markup_at_all(self):
        with pytest.raises(ParseError, match="no ld\\+json blocks"):
            JsonLdScreenings("v").parse("<html></html>", observed_at=NOW)

    def test_markup_but_no_screenings(self):
        with pytest.raises(ParseError, match="no ScreeningEvent"):
            JsonLdScreenings("v").parse(
                page({"@type": "Restaurant", "name": "not a cinema"}), observed_at=NOW
            )

    def test_one_malformed_block_does_not_blind_the_others(self):
        html = (
            '<script type="application/ld+json">{ oops </script>'
            + page(screening())
        )
        assert len(JsonLdScreenings("v").parse(html, observed_at=NOW)) == 1

    def test_partial_dates_raise_in_strict_mode_only(self):
        html = page([screening(), screening(name="UNDATED", startDate="")])
        with pytest.raises(IncompleteStructuredData):
            JsonLdScreenings("v").parse(html, observed_at=NOW, strict=True)
        assert len(JsonLdScreenings("v").parse(html, observed_at=NOW, strict=False)) == 1


def test_slugify_is_stable_across_punctuation():
    assert slugify("SHERMAN'S MARCH (OC)") == slugify("shermans march oc")
