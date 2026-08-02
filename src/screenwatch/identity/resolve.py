"""Binding venue product ids to canonical Works.

The catalogue is injected rather than imported, so the resolver runs fully
offline in tests and degrades to a local alias table when no TMDB key is
configured. That matters: identity is on the critical path for every search,
and an outage in a third-party catalogue must not take the whole system down.

The governing rule, and the reason for the confidence plumbing: **never merge
two films on a fuzzy title match alone.** A wrong merge is silent and
poisons everything downstream - two different films become one Work, and no
later stage has any signal that it happened. Title plus year, or title plus
runtime, or it stays unresolved and a human looks at it.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .normalize import ProductKind, TitleAnalysis, analyze, match_key
from .work import Method, TitleLink, Work

_DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
_DEFAULT_OVERRIDES = _DATA / "title_overrides.json"

RUNTIME_TOLERANCE_MIN = 3
YEAR_TOLERANCE = 1


@dataclass(frozen=True)
class Candidate:
    """A catalogue hit, before we decide whether to believe it."""

    title: str
    year: int | None
    tmdb_id: int | None = None
    runtime_min: int | None = None
    original_title: str | None = None
    popularity: float = 0.0

    @property
    def key(self) -> str:
        return match_key(self.title)

    def match_keys(self) -> set[str]:
        """Every normalised title this candidate can be matched on.

        Named `keys()` once, which made `key in candidate.keys()` read - to a
        human and to a linter alike - as a dict membership test. It is not a
        mapping.
        """
        out = {self.key}
        if self.original_title:
            out.add(match_key(self.original_title))
        return out


class Catalog(Protocol):
    def search(self, title: str, year: int | None = None) -> list[Candidate]: ...


class NullCatalog:
    """No external catalogue. Everything falls to the alias table."""

    def search(self, title: str, year: int | None = None) -> list[Candidate]:
        return []


class AliasCatalog:
    """Local, curated, and always consulted before the network.

    `data/title_overrides.json` maps a match key to a Work. It is how you fix
    a bad resolution permanently, and how the system keeps working for films
    a catalogue has not indexed yet - which is common for festival titles and
    rep-house programming.
    """

    def __init__(self, path: pathlib.Path | None = _DEFAULT_OVERRIDES) -> None:
        # `path=None` means "no local table at all", which tests need; omitting
        # the argument means "the shipped table". Defaulting to None would
        # conflate the two and silently load real data into unit tests.
        self.path = path
        self._by_key: dict[str, Candidate] = {}
        self._pinned: dict[tuple[str, str], str] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        blob = json.loads(self.path.read_text(encoding="utf-8"))
        for entry in blob.get("works", []):
            candidate = Candidate(
                title=entry["title"],
                year=entry.get("year"),
                tmdb_id=entry.get("tmdb_id"),
                runtime_min=entry.get("runtime_min"),
            )
            for key in {match_key(entry["title"])} | {
                match_key(a) for a in entry.get("aliases", [])
            }:
                self._by_key[key] = candidate
        # Hard pins: (source, source_movie_id) -> work_id. The escape hatch
        # for a product the heuristics get wrong every time.
        for pin in blob.get("pins", []):
            self._pinned[(pin["source"], str(pin["source_movie_id"]))] = pin["work_id"]

    def search(self, title: str, year: int | None = None) -> list[Candidate]:
        hit = self._by_key.get(match_key(title))
        return [hit] if hit else []

    def pinned_work_id(self, source: str, source_movie_id: str) -> str | None:
        return self._pinned.get((source, str(source_movie_id)))


def work_id_for(candidate: Candidate) -> str:
    if candidate.tmdb_id:
        return f"tmdb:{candidate.tmdb_id}"
    key = match_key(candidate.title)
    return f"local:{key}-{candidate.year}" if candidate.year else f"local:{key}"


def _to_work(candidate: Candidate) -> Work:
    return Work(
        work_id=work_id_for(candidate),
        title=candidate.title,
        year=candidate.year,
        tmdb_id=candidate.tmdb_id,
        runtime_min=candidate.runtime_min,
        original_title=candidate.original_title,
    )


@dataclass
class Resolution:
    link: TitleLink
    work: Work | None
    analysis: TitleAnalysis
    candidates: tuple[Candidate, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1 and self.link.confidence < 0.75


class WorkResolver:
    """Resolves (source, product id, raw title) -> Work.

    Caching is by (source, source_movie_id) because that pair is what a venue
    reuses; the raw title occasionally changes wording mid-run without the
    product changing at all.

    Two tiers. The dict is per-process and free. The store, when given one,
    survives restarts - which is the tier that matters, because resolution
    calls out to TMDB and a monitor that restarts hourly would otherwise
    re-ask for every title it has ever seen. `title_links` existed for exactly
    this and nothing ever wrote to it.
    """

    def __init__(
        self,
        catalog: Catalog | None = None,
        aliases: AliasCatalog | None = None,
        *,
        cache: dict[tuple[str, str], Resolution] | None = None,
        store=None,
    ) -> None:
        self.catalog = catalog or NullCatalog()
        self.aliases = aliases if aliases is not None else AliasCatalog()
        self._cache: dict[tuple[str, str], Resolution] = cache if cache is not None else {}
        self.store = store

    # ------------------------------------------------------------------
    def resolve(
        self,
        source: str,
        source_movie_id: str,
        raw_title: str,
        *,
        hint_year: int | None = None,
        hint_runtime_min: int | None = None,
    ) -> Resolution:
        cache_key = (source, str(source_movie_id))
        if (hit := self._cache.get(cache_key)) is not None:
            return hit
        if (hit := self._from_store(source, source_movie_id, raw_title)) is not None:
            self._cache[cache_key] = hit
            return hit

        analysis = analyze(raw_title)

        pinned = self.aliases.pinned_work_id(source, str(source_movie_id))
        if pinned:
            resolution = self._pinned_resolution(
                source, source_movie_id, raw_title, analysis, pinned
            )
            self._remember(cache_key, resolution)
            return resolution

        # A local alias hit is curated data, not a guess, so it is taken as
        # authoritative rather than re-judged by the heuristics below. An
        # alias entry exists precisely because someone decided the heuristics
        # were getting this title wrong.
        if alias_hits := self.aliases.search(analysis.clean):
            candidates = alias_hits
            work, method, confidence = _to_work(alias_hits[0]), Method.ALIAS, 0.95
        else:
            candidates = self._candidates(analysis, hint_year)
            work, method, confidence = self._choose(
                analysis, candidates, hint_year, hint_runtime_min
            )

        resolution = Resolution(
            link=TitleLink(
                source=source,
                source_movie_id=str(source_movie_id),
                raw_title=raw_title,
                work_id=work.work_id if work else None,
                method=method,
                confidence=confidence,
                attrs=analysis.attrs,
                kind=analysis.kind,
                linked_at=datetime.now(UTC),
            ),
            work=work,
            analysis=analysis,
            candidates=tuple(candidates),
        )
        self._remember(cache_key, resolution)
        return resolution

    # ------------------------------------------------------------------
    def _remember(self, cache_key: tuple[str, str], resolution: Resolution) -> None:
        self._cache[cache_key] = resolution
        if self.store is None:
            return
        try:
            if resolution.work is not None:
                self.store.put_work(resolution.work)
            self.store.put_link(resolution.link)
        except Exception:                                       # noqa: BLE001
            # A cache is an optimisation. A locked or full database must not
            # turn a working search into a failing one.
            pass

    def _from_store(
        self, source: str, source_movie_id: str, raw_title: str
    ) -> Resolution | None:
        """A prior resolution of this exact product, if one was persisted.

        The raw title is re-analysed rather than stored-and-trusted: it is
        cheap, it is pure, and the variant suffixes it extracts are the part
        most likely to have been improved since the row was written.
        """
        if self.store is None:
            return None
        try:
            row = self.store.get_link(source, str(source_movie_id))
        except Exception:                                       # noqa: BLE001
            return None
        if not row:
            return None
        work = self.store.get_work(row["work_id"]) if row["work_id"] else None
        if row["work_id"] and work is None:
            return None                    # link outlived its work; resolve again
        return Resolution(
            link=TitleLink(
                source=source,
                source_movie_id=str(source_movie_id),
                raw_title=raw_title,
                work_id=row["work_id"],
                method=Method(row["method"]),
                confidence=row["confidence"],
                attrs=analyze(raw_title).attrs,
                kind=ProductKind(row["kind"]),
                linked_at=datetime.now(UTC),
            ),
            work=work,
            analysis=analyze(raw_title),
        )

    # ------------------------------------------------------------------
    def _pinned_resolution(self, source, source_movie_id, raw_title, analysis, work_id):
        return Resolution(
            link=TitleLink(
                source=source,
                source_movie_id=str(source_movie_id),
                raw_title=raw_title,
                work_id=work_id,
                method=Method.OVERRIDE,
                confidence=1.0,
                attrs=analysis.attrs,
                kind=analysis.kind,
                linked_at=datetime.now(UTC),
            ),
            work=Work(work_id=work_id, title=analysis.clean),
            analysis=analysis,
        )

    def _candidates(self, analysis: TitleAnalysis, hint_year: int | None) -> list[Candidate]:
        try:
            return list(self.catalog.search(analysis.clean, hint_year))
        except Exception:                                       # noqa: BLE001
            # A catalogue outage degrades identity; it must not fail a search.
            return []

    def _choose(
        self,
        analysis: TitleAnalysis,
        candidates: Iterable[Candidate],
        hint_year: int | None,
        hint_runtime: int | None,
    ) -> tuple[Work | None, Method, float]:
        candidates = list(candidates)
        if not candidates:
            return self._local_work(analysis), Method.UNRESOLVED, 0.3

        key = analysis.match_key
        exact = [c for c in candidates if key in c.match_keys()]

        if not exact:
            # Fuzzy containment is a hint, never a merge. Requires a year to
            # corroborate, and even then tops out below the review threshold.
            near = [c for c in candidates if key and (key in c.key or c.key in key)]
            if len(near) == 1 and hint_year and near[0].year == hint_year:
                return _to_work(near[0]), Method.TMDB_EXACT, 0.7
            return self._local_work(analysis), Method.UNRESOLVED, 0.3

        if len(exact) == 1:
            only = exact[0]
            if hint_year and only.year and abs(only.year - hint_year) <= YEAR_TOLERANCE:
                return _to_work(only), Method.TMDB_EXACT, 0.95
            if (
                hint_runtime
                and only.runtime_min
                and abs(only.runtime_min - hint_runtime) <= RUNTIME_TOLERANCE_MIN
            ):
                return _to_work(only), Method.TMDB_RUNTIME, 0.9
            return _to_work(only), Method.TMDB_EXACT, 0.8

        # Several films share this title - the re-release case. Disambiguate
        # on year, then runtime; refuse to guess if neither separates them.
        if hint_year:
            by_year = [c for c in exact if c.year and abs(c.year - hint_year) <= YEAR_TOLERANCE]
            if len(by_year) == 1:
                return _to_work(by_year[0]), Method.TMDB_EXACT, 0.9
        if hint_runtime:
            by_runtime = [
                c for c in exact
                if c.runtime_min and abs(c.runtime_min - hint_runtime) <= RUNTIME_TOLERANCE_MIN
            ]
            if len(by_runtime) == 1:
                return _to_work(by_runtime[0]), Method.TMDB_RUNTIME, 0.85

        best = max(exact, key=lambda c: (c.popularity, c.year or 0))
        return _to_work(best), Method.TMDB_EXACT, 0.5

    @staticmethod
    def _local_work(analysis: TitleAnalysis) -> Work | None:
        """Unresolved titles still get a stable local Work.

        Grouping by cleaned title is better than dropping the screening: the
        four AMC Odyssey products still collapse together even with no
        catalogue at all, they just carry low confidence.
        """
        if not analysis.match_key:
            return None
        return Work(work_id=f"local:{analysis.match_key}", title=analysis.clean)
