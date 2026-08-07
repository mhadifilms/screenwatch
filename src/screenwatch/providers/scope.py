"""Honest reporting of what a provider did not look at.

Every provider has a fast path: so many venues, so many days ahead. The fast
path is useful for interactive work - a seven-day search across 402 Regal
theatres is thousands of page loads - but an uncounted cap is a lie. The
exhaustive path explicitly removes Screenwatch's local caps for callers that
need the full configured scope. A search that read three of a user's eleven
nearby Cinemarks and found nothing reports exactly what a search that read all
eleven and found nothing reports, and the two are not the same answer.

So a provider that clips says so, and `SearchResult.clipped` carries it up to
whoever asked. The cap stays; the silence goes.
"""

from __future__ import annotations

WATCH_MAX_DAYS = 31


class ScopeReporting:
    """Mixin: record and expose what a fast-path cap left out.

    `screenings()` calls `_reset_scope()` on entry and `_clip*` when a cap
    bites; the service reads `clipped` afterwards. Per-call state rather than
    accumulated, so a second search does not inherit the first's excuses.
    """

    _clipped: tuple[str, ...] = ()
    _errors: tuple[str, ...] = ()

    def _reset_scope(self) -> None:
        self._clipped = ()
        self._errors = ()

    def _note_clip(self, message: str) -> None:
        self._clipped = (*self._clipped, f"{self.chain}: {message}")

    def _note_error(self, message: str) -> None:
        """Record a partial-source failure without aborting other providers."""
        formatted = f"{self.chain}: {message}"
        if formatted not in self._errors:
            self._errors = (*self._errors, formatted)

    def _clip_venues(self, venues: list, *, exhaustive: bool = False) -> list:
        """Apply `max_venues`, recording the ones dropped."""
        cap = getattr(self, "max_venues", None)
        if exhaustive:
            cap = None
        if cap is None or len(venues) <= cap:
            return list(venues)
        dropped = venues[cap:]
        self._note_clip(
            f"read {cap} of {len(venues)} matching venues; skipped "
            + ", ".join(v.name for v in dropped[:4])
            + (f" and {len(dropped) - 4} more" if len(dropped) > 4 else "")
        )
        return list(venues[:cap])

    def _clip_days(
        self, days: list, *, cap: int | None = None, exhaustive: bool = False
    ) -> list:
        """Apply `max_days`, recording the tail dropped."""
        if not exhaustive:
            cap = getattr(self, "max_days", None) if cap is None else cap
        else:
            cap = None
        if cap is None or len(days) <= cap:
            return list(days)
        self._note_clip(
            f"read {cap} of {len(days)} days in the window; "
            f"nothing later than {days[cap - 1].isoformat()} was checked"
        )
        return list(days[:cap])

    @property
    def clipped(self) -> tuple[str, ...]:
        return self._clipped

    @property
    def errors(self) -> tuple[str, ...]:
        return self._errors
