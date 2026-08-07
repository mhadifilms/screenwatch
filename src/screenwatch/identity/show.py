"""Canonical identity for a real-world cinema showing.

Provider showtime ids are handles into a chain's system, not identities.  AMC,
Regal, Cinemark, and independent ticketing systems can all assign different
ids to the same showing, and some of them recycle ids between runs.

This module deliberately keeps the provider id out of the canonical key.  The
provider id remains an alias for seat-map and booking lookups; the stable key
is built from facts about the showing that survive a provider changing its
backend.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ..models import Attribute, Presentation

_SPACE = re.compile(r"\s+")


def _text(value: object | None) -> str:
    """Normalize a free-form identity component without losing its meaning."""
    return _SPACE.sub(" ", str(value or "").strip()).casefold()


def _aspect(value: str | None) -> str:
    value = _text(value)
    if value.endswith(":1"):
        value = value[:-2]
    return value


def _start_minute(value: datetime) -> str:
    """Serialize an instant at the precision ticketing sites publish."""
    if value.tzinfo is None:
        raise ValueError("show start must be timezone-aware")
    return value.astimezone(UTC).replace(second=0, microsecond=0).isoformat()


def presentation_fingerprint(presentation: Presentation) -> dict:
    """Return the source-independent portion of a presentation descriptor.

    Raw labels are intentionally excluded.  For example, ``IMAX with Laser``
    and ``Laser at AMC`` can describe the same structured presentation.
    Salient attributes stay in the fingerprint because 2D, 3D, and captioned
    versions at the same time are different products.
    """
    salient = {
        Attribute.THREE_D,
        Attribute.HFR,
        Attribute.OPEN_CAPTION,
        Attribute.SUBTITLED,
        Attribute.DUBBED,
        Attribute.SENSORY_FRIENDLY,
    }
    return {
        "projection": presentation.projection.value,
        "brand": presentation.brand.value,
        "aspect": _aspect(presentation.aspect),
        "attrs": sorted(a.value for a in presentation.attrs if a in salient),
    }


@dataclass(frozen=True)
class ShowIdentity:
    """Stable identity plus the material used to derive it."""

    work_id: str
    venue_id: str
    starts_at_utc: datetime
    presentation: Presentation
    screen_id: str | None = None

    @property
    def material(self) -> str:
        parts = {
            "work": _text(self.work_id),
            "venue": _text(self.venue_id),
            "start": _start_minute(self.starts_at_utc),
            "presentation": presentation_fingerprint(self.presentation),
            "screen": _text(self.screen_id),
        }
        return json.dumps(parts, sort_keys=True, separators=(",", ":"))

    @property
    def canonical_id(self) -> str:
        # 32 hex characters gives ample collision resistance while keeping
        # ids usable in URLs, SQLite keys, and alert payloads.
        digest = hashlib.sha256(self.material.encode("utf-8")).hexdigest()[:32]
        return f"show:{digest}"


def show_identity(
    *,
    work_id: str,
    venue_id: str,
    starts_at_utc: datetime,
    presentation: Presentation,
    screen_id: str | None = None,
) -> ShowIdentity:
    return ShowIdentity(
        work_id=work_id,
        venue_id=venue_id,
        starts_at_utc=starts_at_utc,
        presentation=presentation,
        screen_id=screen_id,
    )


def canonical_show_id(
    *,
    work_id: str,
    venue_id: str,
    starts_at_utc: datetime,
    presentation: Presentation,
    screen_id: str | None = None,
) -> str:
    """Convenience wrapper used by the domain model."""
    return show_identity(
        work_id=work_id,
        venue_id=venue_id,
        starts_at_utc=starts_at_utc,
        presentation=presentation,
        screen_id=screen_id,
    ).canonical_id
