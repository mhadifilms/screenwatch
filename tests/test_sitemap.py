from __future__ import annotations

from datetime import UTC, datetime

import pytest

from screenwatch.adapters.amc.sitemap import AmcSitemap, Tripwire

NOW = datetime(2026, 8, 2, tzinfo=UTC)


@pytest.fixture(scope="module")
def entries(amc_sitemap_movies):
    return AmcSitemap().parse(amc_sitemap_movies)


class TestParse:
    def test_parses_the_real_sitemap(self, entries):
        assert len(entries) > 100
        assert all(e.url.startswith("https://www.amctheatres.com/movies/") for e in entries)

    def test_separates_numeric_movie_id_from_slug(self, entries):
        with_id = [e for e in entries if e.movie_id]
        assert with_id, "expected AMC movie ids in slugs"
        e = with_id[0]
        assert e.movie_id.isdigit() and not e.slug.endswith(e.movie_id)

    def test_slug_only_entries_still_get_a_key(self, entries):
        """Some AMC movie URLs carry no numeric id; they must not collide."""
        keys = [e.key for e in entries]
        assert len(keys) == len(set(keys))

    def test_empty_sitemap_raises(self):
        with pytest.raises(ValueError, match="zero movie entries"):
            AmcSitemap().parse("<urlset></urlset>")

    def test_parses_the_national_theatre_directory(self, amc_sitemap_theatres):
        entries = AmcSitemap().parse_theatres(amc_sitemap_theatres)
        assert len(entries) > 100
        assert len({entry.venue_id for entry in entries}) == len(entries)
        assert all(entry.url.startswith("https://www.amctheatres.com/movie-theatres/")
                   for entry in entries)
        assert all(entry.theatre_id and entry.name and entry.city and entry.state
                   for entry in entries)
        assert sum(entry.latitude is not None and entry.longitude is not None
                   for entry in entries) / len(entries) > 0.99

    def test_theatre_entry_uses_official_slug_and_coordinates(self, amc_sitemap_theatres):
        entry = AmcSitemap().parse_theatres(amc_sitemap_theatres)[0]
        assert entry.venue_id == f"amc-{entry.slug}"
        assert entry.market
        assert entry.latitude is not None and entry.longitude is not None

    def test_empty_theatre_sitemap_raises(self):
        with pytest.raises(ValueError, match="zero theatre entries"):
            AmcSitemap().parse_theatres("<urlset></urlset>")

    def test_digest_is_order_insensitive(self):
        a = "<url><loc>https://x/movies/a-1</loc></url><url><loc>https://x/movies/b-2</loc></url>"
        b = "<url><loc>https://x/movies/b-2</loc></url><url><loc>https://x/movies/a-1</loc></url>"
        assert AmcSitemap.digest(a) == AmcSitemap.digest(b)

    def test_digest_changes_when_a_movie_appears(self):
        a = "<url><loc>https://x/movies/a-1</loc></url>"
        b = a + "<url><loc>https://x/movies/the-odyssey-76238</loc></url>"
        assert AmcSitemap.digest(a) != AmcSitemap.digest(b)


class TestTripwire:
    def test_fires_once_when_the_movie_first_appears(self):
        tw = Tripwire(title_contains="The Odyssey")
        xml = '<url><loc>https://x/movies/the-odyssey-76238</loc><lastmod>2026-08-01T00:00:00Z</lastmod></url>'
        entries = AmcSitemap().parse(xml)

        assert tw.check(entries, now=NOW), "should fire on first sighting"
        assert not tw.check(entries, now=NOW), "must not re-fire on unchanged input"

    def test_fires_again_when_lastmod_advances(self):
        tw = Tripwire(title_contains="The Odyssey")
        base = '<url><loc>https://x/movies/the-odyssey-76238</loc><lastmod>{}</lastmod></url>'
        tw.check(AmcSitemap().parse(base.format("2026-08-01T00:00:00Z")), now=NOW)

        reasons = tw.check(AmcSitemap().parse(base.format("2026-08-02T09:00:00Z")), now=NOW)
        assert reasons and "lastmod advanced" in reasons[0]

    def test_ignores_unrelated_movies(self):
        tw = Tripwire(title_contains="The Odyssey")
        xml = '<url><loc>https://x/movies/some-other-film-11111</loc></url>'
        assert tw.check(AmcSitemap().parse(xml), now=NOW) == []
