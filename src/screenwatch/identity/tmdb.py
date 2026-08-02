"""TMDB catalogue client.

Implements the `Catalog` protocol so `WorkResolver` gets real film metadata -
release year, runtime, original title - instead of grouping by cleaned title
alone. That upgrade matters most across chains: AMC calls it product 76238,
Alamo calls it `spider-man-brand-new-day`, Regal calls it `HO00021207`, and
only a shared catalogue id makes those provably the same film rather than
three strings that happen to normalize alike.

Activates when `TMDB_API_KEY` (or `TMDB_READ_TOKEN`) is in the environment.
Without one, `from_env()` returns None and the resolver falls back to the
alias table, which is why identity degrades rather than breaks.

Results are cached in-process and negative results are cached too - a title
TMDB does not know (festival premieres, one-off rep programming) would
otherwise be re-queried on every single poll.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable

from curl_cffi import requests

from .resolve import Candidate

API = "https://api.themoviedb.org/3"
SEARCH = API + "/search/movie"
DETAIL = API + "/movie/{tmdb_id}"


class TmdbCatalog:
    def __init__(
        self,
        api_key: str | None = None,
        bearer: str | None = None,
        *,
        session: requests.Session | None = None,
        timeout: int = 12,
        min_interval_s: float = 0.06,     # TMDB allows ~50 req/s; stay well under
        fetch_runtime: bool = True,
    ) -> None:
        if not api_key and not bearer:
            raise ValueError("TmdbCatalog needs an api_key or a bearer token")
        self.api_key = api_key
        self.bearer = bearer
        self.timeout = timeout
        self.min_interval_s = min_interval_s
        self.fetch_runtime = fetch_runtime
        self._session = session or requests.Session(impersonate="chrome131")
        self._cache: dict[tuple[str, int | None], list[Candidate]] = {}
        self._runtimes: dict[int, int | None] = {}
        self._last_call = 0.0

    @classmethod
    def from_env(cls, **kw) -> TmdbCatalog | None:
        """Build from the environment, or None if no credentials are set.

        Returning None rather than raising is deliberate: a missing key is a
        degraded mode, not a misconfiguration to crash on.
        """
        key = os.environ.get("TMDB_API_KEY")
        token = os.environ.get("TMDB_READ_TOKEN")
        if not key and not token:
            return None
        return cls(api_key=key, bearer=token, **kw)

    # ------------------------------------------------------------------
    def _pace(self) -> None:
        delta = self._last_call + self.min_interval_s - time.monotonic()
        if delta > 0:
            time.sleep(delta)
        self._last_call = time.monotonic()

    def _get(self, url: str, params: dict) -> dict:
        headers = {"accept": "application/json"}
        if self.bearer:
            headers["authorization"] = f"Bearer {self.bearer}"
        else:
            params = {**params, "api_key": self.api_key}

        self._pace()
        response = self._session.get(url, params=params, headers=headers,
                                     timeout=self.timeout)
        if response.status_code != 200:
            raise RuntimeError(f"TMDB {response.status_code} for {url}")
        return response.json()

    # ------------------------------------------------------------------
    def search(self, title: str, year: int | None = None) -> list[Candidate]:
        key = (title.strip().lower(), year)
        if key in self._cache:
            return self._cache[key]

        params: dict[str, object] = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = year

        payload = self._get(SEARCH, params)
        candidates = list(self._to_candidates(payload.get("results") or []))

        # Runtime is not in search results but is the tiebreaker when two
        # films share a title and neither year is known - the re-release case.
        if self.fetch_runtime and len(candidates) > 1:
            candidates = [self._with_runtime(c) for c in candidates[:5]]

        # Negative results are cached too, or every poll re-asks about the
        # festival titles TMDB has never heard of.
        self._cache[key] = candidates
        return candidates

    def _to_candidates(self, results: Iterable[dict]) -> Iterable[Candidate]:
        for row in results:
            release = (row.get("release_date") or "")[:4]
            yield Candidate(
                title=row.get("title") or row.get("original_title") or "",
                year=int(release) if release.isdigit() else None,
                tmdb_id=row.get("id"),
                original_title=row.get("original_title"),
                popularity=float(row.get("popularity") or 0.0),
            )

    def _with_runtime(self, candidate: Candidate) -> Candidate:
        if candidate.tmdb_id is None:
            return candidate
        if candidate.tmdb_id not in self._runtimes:
            try:
                detail = self._get(DETAIL.format(tmdb_id=candidate.tmdb_id), {})
                self._runtimes[candidate.tmdb_id] = detail.get("runtime")
            except Exception:                                  # noqa: BLE001
                # A detail lookup failing must not lose the search hit; the
                # resolver simply has one less tiebreaker.
                self._runtimes[candidate.tmdb_id] = None
        return Candidate(
            title=candidate.title,
            year=candidate.year,
            tmdb_id=candidate.tmdb_id,
            runtime_min=self._runtimes[candidate.tmdb_id],
            original_title=candidate.original_title,
            popularity=candidate.popularity,
        )
