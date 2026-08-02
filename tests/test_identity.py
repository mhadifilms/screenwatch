"""Identity is the base grouping everything else keys on, so its failure modes
get tested harder than its happy path. The one that matters most is the silent
merge: two different films collapsing into one Work with nothing downstream
able to notice.
"""

from __future__ import annotations

import pytest

from screenwatch.identity.normalize import ProductKind, analyze, match_key
from screenwatch.identity.resolve import (
    AliasCatalog,
    Candidate,
    NullCatalog,
    WorkResolver,
)
from screenwatch.identity.work import Method, WorkRef
from screenwatch.models import Attribute, Projection

# The four products AMC's sitemap carried for one film on 2026-08-02.
AMC_ODYSSEY = [
    ("76238", "The Odyssey"),
    ("80679", "The Odyssey"),
    ("83988", "The Odyssey Sensory Friendly Screening"),
    ("84080", "The Odyssey Private Theatre Rental"),
]


class FakeCatalog:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, int | None]] = []

    def search(self, title: str, year: int | None = None) -> list[Candidate]:
        self.calls.append((title, year))
        key = match_key(title)
        return [c for c in self.candidates if key in c.match_keys()]


ODYSSEY = Candidate(title="The Odyssey", year=2026, tmdb_id=1, runtime_min=160)
THIRD_MAN_1949 = Candidate(title="The Third Man", year=1949, tmdb_id=2, runtime_min=104)
THIRD_MAN_2026 = Candidate(title="The Third Man", year=2026, tmdb_id=3, runtime_min=98)


class TestVariantStripping:
    def test_collapses_amcs_four_products_to_one_film(self):
        keys = {analyze(title).match_key for _, title in AMC_ODYSSEY}
        assert keys == {"odyssey"}

    def test_variant_becomes_an_attribute_not_a_different_film(self):
        a = analyze("The Odyssey Sensory Friendly Screening")
        assert a.clean == "The Odyssey"
        assert Attribute.SENSORY_FRIENDLY in a.attrs

    def test_private_rental_is_not_a_bookable_screening(self):
        """It shares the film's identity but you cannot buy a seat at it, so
        it must never appear as an option."""
        a = analyze("The Odyssey Private Theatre Rental")
        assert a.match_key == "odyssey"
        assert a.kind is ProductKind.RENTAL
        assert not a.is_bookable

    def test_ordinary_screening_is_bookable(self):
        assert analyze("The Odyssey").is_bookable
        assert analyze("The Odyssey").kind is ProductKind.FEATURE

    def test_format_words_leave_the_title_and_become_presentation(self):
        a = analyze("THE THIRD MAN in 35mm")
        assert a.clean.lower() == "the third man"
        assert a.presentation.projection is Projection.FILM_35MM

    def test_shot_on_guard_survives_into_title_analysis(self):
        """The origination guard lives in the shared classifier; identity
        inherits it rather than reimplementing it."""
        a = analyze("Paris, Texas — shot on 35mm by Robby Müller")
        assert a.presentation.projection is not Projection.FILM_35MM

    def test_accents_and_apostrophes_normalize(self):
        assert match_key("Amélie") == match_key("Amelie")
        assert match_key("Sherman's March") == match_key("Shermans March")

    def test_leading_article_is_ignored_for_matching_but_kept_for_display(self):
        a = analyze("The Odyssey")
        assert a.match_key == "odyssey"
        assert a.clean == "The Odyssey"

    def test_title_that_is_entirely_variant_noise_does_not_become_empty(self):
        a = analyze("Open Caption")
        assert a.clean


class TestResolution:
    def test_all_four_products_link_to_one_work(self):
        resolver = WorkResolver(FakeCatalog([ODYSSEY]), AliasCatalog(path=None))
        ids = {
            resolver.resolve("amc", pid, title, hint_year=2026).link.work_id
            for pid, title in AMC_ODYSSEY
        }
        assert ids == {"tmdb:1"}

    def test_rental_links_to_the_work_but_is_flagged_unbookable(self):
        resolver = WorkResolver(FakeCatalog([ODYSSEY]), AliasCatalog(path=None))
        r = resolver.resolve("amc", "84080", "The Odyssey Private Theatre Rental")
        assert r.link.work_id == "tmdb:1"
        assert r.link.kind is ProductKind.RENTAL

    def test_rerelease_resolves_to_release_year_not_screening_year(self):
        """A 2026 screening of a 1949 film is a 1949 film. Getting this
        backwards makes every catalogue lookup miss."""
        resolver = WorkResolver(
            FakeCatalog([THIRD_MAN_1949, THIRD_MAN_2026]), AliasCatalog(path=None)
        )
        r = resolver.resolve("filmforum", "ff-1", "THE THIRD MAN in 35mm", hint_year=1949)
        assert r.work.year == 1949 and r.work.tmdb_id == 2

    def test_same_title_different_films_are_not_merged_without_evidence(self):
        """Two real films share this title. With nothing to separate them the
        resolver must report low confidence rather than pick one silently."""
        resolver = WorkResolver(
            FakeCatalog([THIRD_MAN_1949, THIRD_MAN_2026]), AliasCatalog(path=None)
        )
        r = resolver.resolve("v", "1", "The Third Man")
        assert r.link.needs_review and r.ambiguous

    def test_runtime_disambiguates_when_year_is_unknown(self):
        resolver = WorkResolver(
            FakeCatalog([THIRD_MAN_1949, THIRD_MAN_2026]), AliasCatalog(path=None)
        )
        r = resolver.resolve("v", "1", "The Third Man", hint_runtime_min=104)
        assert r.work.tmdb_id == 2
        assert r.link.method is Method.TMDB_RUNTIME

    def test_fuzzy_title_alone_never_merges(self):
        """Containment is a hint. Without a corroborating year it must not
        produce a link - a wrong merge is silent and poisons everything."""
        resolver = WorkResolver(FakeCatalog([ODYSSEY]), AliasCatalog(path=None))
        r = resolver.resolve("v", "1", "A Writer's Odyssey")
        assert r.link.method is Method.UNRESOLVED
        assert r.link.work_id != "tmdb:1"


class TestDegradedOperation:
    def test_works_with_no_catalogue_at_all(self):
        """No TMDB key: products still group by cleaned title, just with low
        confidence. Better than dropping every screening."""
        resolver = WorkResolver(NullCatalog(), AliasCatalog(path=None))
        ids = {
            resolver.resolve("amc", pid, title).link.work_id
            for pid, title in AMC_ODYSSEY
        }
        assert ids == {"local:odyssey"}
        assert resolver.resolve("amc", "76238", "The Odyssey").link.needs_review

    def test_catalogue_outage_does_not_raise(self):
        class Broken:
            def search(self, title, year=None):
                raise ConnectionError("catalogue down")

        r = WorkResolver(Broken(), AliasCatalog(path=None)).resolve("v", "1", "The Odyssey")
        assert r.link.method is Method.UNRESOLVED and r.work is not None

    def test_alias_table_is_consulted_before_the_network(self):
        catalog = FakeCatalog([ODYSSEY])
        aliases = AliasCatalog()  # ships The Third Man
        r = WorkResolver(catalog, aliases).resolve("v", "1", "Carol Reed's The Third Man")
        assert r.work.year == 1949
        assert catalog.calls == [], "alias hit must short-circuit the network"

    def test_resolution_is_cached_per_product_id(self):
        catalog = FakeCatalog([ODYSSEY])
        resolver = WorkResolver(catalog, AliasCatalog(path=None))
        resolver.resolve("amc", "76238", "The Odyssey")
        resolver.resolve("amc", "76238", "The Odyssey")
        assert len(catalog.calls) == 1


class TestWorkRef:
    def test_requires_at_least_one_identifier(self):
        with pytest.raises(ValueError):
            WorkRef()

    @pytest.mark.parametrize(
        "kw", [{"query": "dune"}, {"work_id": "tmdb:1"}, {"tmdb_id": 1}]
    )
    def test_accepts_any_single_identifier(self, kw):
        assert WorkRef(**kw)


class TestTmdbCatalog:
    """Exercised through a fake HTTP session: no network, no key required."""

    class FakeSession:
        def __init__(self, pages):
            self.pages = pages
            self.requests: list[tuple[str, dict]] = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.requests.append((url, params or {}))
            for fragment, payload in self.pages.items():
                if fragment in url:
                    return type("R", (), {"status_code": 200,
                                          "json": lambda self, p=payload: p})()
            return type("R", (), {"status_code": 404,
                                  "json": lambda self: {}})()

    SEARCH = {
        "results": [
            {"id": 2, "title": "The Third Man", "release_date": "1949-08-31",
             "original_title": "The Third Man", "popularity": 20.0},
            {"id": 3, "title": "The Third Man", "release_date": "2026-03-01",
             "original_title": "The Third Man", "popularity": 5.0},
        ]
    }

    def catalog(self, pages=None, **kw):
        from screenwatch.identity.tmdb import TmdbCatalog

        session = self.FakeSession(pages or {"search/movie": self.SEARCH})
        return TmdbCatalog(api_key="k", session=session, min_interval_s=0,
                           fetch_runtime=False, **kw), session

    def test_maps_results_to_candidates(self):
        catalog, _ = self.catalog()
        [a, b] = catalog.search("The Third Man")
        assert (a.tmdb_id, a.year) == (2, 1949)
        assert (b.tmdb_id, b.year) == (3, 2026)

    def test_year_is_passed_through_as_a_filter(self):
        catalog, session = self.catalog()
        catalog.search("The Third Man", 1949)
        assert session.requests[0][1]["year"] == 1949

    def test_results_are_cached(self):
        catalog, session = self.catalog()
        catalog.search("The Third Man")
        catalog.search("The Third Man")
        assert len(session.requests) == 1

    def test_misses_are_cached_too(self):
        """Festival and rep titles TMDB has never heard of would otherwise be
        re-queried on every poll."""
        catalog, session = self.catalog({"search/movie": {"results": []}})
        catalog.search("Some Rep House Premiere")
        catalog.search("Some Rep House Premiere")
        assert catalog.search("Some Rep House Premiere") == []
        assert len(session.requests) == 1

    def test_runtime_is_fetched_only_to_break_a_tie(self):
        from screenwatch.identity.tmdb import TmdbCatalog

        pages = {"search/movie": self.SEARCH, "/movie/": {"runtime": 104}}
        session = self.FakeSession(pages)
        catalog = TmdbCatalog(api_key="k", session=session, min_interval_s=0)
        results = catalog.search("The Third Man")
        assert any(c.runtime_min == 104 for c in results)
        assert any("/movie/" in url for url, _ in session.requests)

    def test_a_failed_detail_lookup_does_not_lose_the_search_hit(self):
        from screenwatch.identity.tmdb import TmdbCatalog

        session = self.FakeSession({"search/movie": self.SEARCH})  # detail 404s
        catalog = TmdbCatalog(api_key="k", session=session, min_interval_s=0)
        assert len(catalog.search("The Third Man")) == 2

    def test_missing_credentials_is_a_degraded_mode_not_a_crash(self, monkeypatch):
        from screenwatch.identity.tmdb import TmdbCatalog

        monkeypatch.delenv("TMDB_API_KEY", raising=False)
        monkeypatch.delenv("TMDB_READ_TOKEN", raising=False)
        assert TmdbCatalog.from_env() is None

    def test_constructing_without_any_credential_raises(self):
        from screenwatch.identity.tmdb import TmdbCatalog

        with pytest.raises(ValueError, match="api_key or a bearer"):
            TmdbCatalog()

    def test_resolver_uses_it_to_separate_same_title_films(self):
        """The payoff: two films share a title, and the catalogue plus a year
        hint picks the right one instead of leaving it unresolved."""
        catalog, _ = self.catalog()
        resolver = WorkResolver(catalog, AliasCatalog(path=None))
        r = resolver.resolve("regal", "HO001", "The Third Man", hint_year=1949)
        assert r.work.tmdb_id == 2 and r.link.confidence >= 0.9


class TestResolutionsPersist:
    """`title_links` existed, had tests, and nothing ever wrote to it.

    Resolution calls out to TMDB. A monitor that restarts hourly and keeps
    its cache only in a dict re-asks for every title it has ever seen.
    """

    def store(self):
        from screenwatch.service.store import Store

        return Store.memory()

    def resolver(self, store, catalog=None):
        from screenwatch.identity.resolve import WorkResolver

        return WorkResolver(catalog, store=store)

    def test_a_resolution_is_written_to_the_store(self):
        store = self.store()
        resolution = self.resolver(store).resolve("amc", "76238", "The Odyssey")
        row = store.get_link("amc", "76238")
        assert row is not None
        assert row["raw_title"] == "The Odyssey"
        assert row["work_id"] == resolution.link.work_id

    def test_a_fresh_resolver_reuses_the_stored_link(self):
        store = self.store()
        first = self.resolver(store).resolve("amc", "76238", "The Odyssey")

        class ExplodingCatalog:
            def search(self, *a, **k):
                raise AssertionError("catalogue must not be consulted again")

        second = self.resolver(store, ExplodingCatalog()).resolve(
            "amc", "76238", "The Odyssey"
        )
        assert second.link.work_id == first.link.work_id
        assert second.link.method == first.link.method

    def test_variant_suffixes_are_re_analysed_rather_than_trusted(self):
        """The cheap, pure part is recomputed; only the expensive lookup is
        cached. A title-parsing improvement then applies to old rows too."""
        store = self.store()
        self.resolver(store).resolve("amc", "84080",
                                     "The Odyssey - Private Theatre Rental")
        again = self.resolver(store).resolve(
            "amc", "84080", "The Odyssey - Private Theatre Rental"
        )
        assert not again.analysis.is_bookable

    def test_a_broken_store_does_not_break_resolution(self):
        class BrokenStore:
            def get_link(self, *a):
                raise RuntimeError("database is locked")

            def put_link(self, *a):
                raise RuntimeError("database is locked")

            def put_work(self, *a):
                raise RuntimeError("database is locked")

            def get_work(self, *a):
                raise RuntimeError("database is locked")

        resolved = self.resolver(BrokenStore()).resolve("amc", "1", "Sholay")
        assert resolved.analysis.clean == "Sholay"
