"""Canonical film identity.

A `Work` is one film. Every venue's product id links to exactly one Work, and
that link is the base grouping the rest of the system keys on - search,
ranking, and watches all operate on Works, never on a chain's own id.

`year` is the film's release year, never the screening date. A rep house
showing THE THIRD MAN in 2026 is showing a 1949 film, and getting that
backwards makes every catalogue lookup miss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..models import Attribute
from .normalize import ProductKind, match_key


@dataclass(frozen=True)
class Work:
    work_id: str                       # internal, stable across catalogue changes
    title: str
    year: int | None = None            # RELEASE year
    tmdb_id: int | None = None
    runtime_min: int | None = None
    original_title: str | None = None
    aliases: frozenset[str] = field(default_factory=frozenset)

    @property
    def key(self) -> str:
        return match_key(self.title)

    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass(frozen=True)
class WorkRef:
    """How a caller names a film: free text, an internal id, or a TMDB id."""

    query: str | None = None
    work_id: str | None = None
    tmdb_id: int | None = None

    def __post_init__(self) -> None:
        if not any((self.query, self.work_id, self.tmdb_id)):
            raise ValueError("WorkRef needs one of query, work_id, tmdb_id")


class Method(Enum):
    """How a link was established. Kept so bad links are auditable later."""

    OVERRIDE = "override"          # human-curated, always wins
    CACHED = "cached"
    TMDB_EXACT = "tmdb_exact"      # title + year both agree
    TMDB_RUNTIME = "tmdb_runtime"  # title agrees, runtime disambiguated
    ALIAS = "alias"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TitleLink:
    """A venue product id bound to a Work, with provenance and confidence."""

    source: str                    # "amc"
    source_movie_id: str           # "83988"
    raw_title: str
    work_id: str | None
    method: Method
    confidence: float
    attrs: frozenset[Attribute] = field(default_factory=frozenset)
    kind: ProductKind = ProductKind.UNKNOWN
    linked_at: datetime | None = None

    @property
    def resolved(self) -> bool:
        return self.work_id is not None and self.method is not Method.UNRESOLVED

    @property
    def needs_review(self) -> bool:
        """Low-confidence links are usable but should be visible to a human.

        Silently guessing wrong here is worse than most failures in this
        system: it merges two films, and every downstream ranking inherits
        the error without any signal that it happened.
        """
        return not self.resolved or self.confidence < 0.75
