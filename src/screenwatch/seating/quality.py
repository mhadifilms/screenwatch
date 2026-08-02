"""Scale-free seat scoring.

Everything here reads `x`/`y` only, never row counts or seat numbers, so a
score means the same thing in a 500-seat IMAX and a 40-seat microcinema.
That is the whole point of normalizing geometry first: one tuning, all rooms.

Defaults put the sweet spot at 60% of the way back and on the centreline,
with a soft falloff. Venues with unusually steep or shallow rakes override
`ideal_depth` in `data/venue_hardware.json`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..presentation import venue as venue_info
from .model import Auditorium, Seat

DEFAULT_IDEAL_DEPTH = 0.60
DEPTH_SIGMA = 0.24
LATERAL_SIGMA = 0.55


@dataclass(frozen=True)
class QualityModel:
    ideal_depth: float = DEFAULT_IDEAL_DEPTH
    depth_sigma: float = DEPTH_SIGMA
    lateral_sigma: float = LATERAL_SIGMA
    avoid_front_rows: int = 2
    front_row_penalty: float = 0.55      # multiplier, not a subtraction
    aisle_penalty: float = 0.0           # set >0 only if the user dislikes aisles
    max_lateral: float = 1.0

    @classmethod
    def for_venue(cls, venue_id: str | None, **overrides) -> "QualityModel":
        """Per-venue tuning from the hardware oracle, then caller overrides."""
        base: dict[str, float] = {}
        if venue_id and (info := venue_info(venue_id)):
            if (depth := info.get("ideal_depth")) is not None:
                base["ideal_depth"] = float(depth)
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**base)

    # ------------------------------------------------------------------
    def score(self, seat: Seat, *, row_count: int | None = None) -> float:
        """0..1 desirability for a single seat.

        Multiplicative rather than additive: a front-row seat dead centre is
        still a front-row seat, and adding a centred bonus to it would let it
        outrank a genuinely good seat slightly off axis.
        """
        depth = math.exp(-(((seat.y - self.ideal_depth) / self.depth_sigma) ** 2) / 2)
        lateral = math.exp(-((seat.x / self.lateral_sigma) ** 2) / 2)

        value = depth * lateral

        if abs(seat.x) > self.max_lateral:
            value *= 0.25

        # Front-row penalty is expressed in normalized depth so it survives
        # any auditorium size; row_count converts the user's "first N rows".
        if row_count and self.avoid_front_rows:
            cutoff = (self.avoid_front_rows - 0.5) / max(row_count - 1, 1)
            if seat.y <= cutoff:
                value *= self.front_row_penalty

        if self.aisle_penalty and seat.aisle_adjacent:
            value *= 1.0 - self.aisle_penalty

        return round(max(0.0, min(1.0, value)), 4)

    def score_all(self, auditorium: Auditorium) -> dict[str, float]:
        rows = auditorium.row_count
        return {s.id: self.score(s, row_count=rows) for s in auditorium.seats}

    def describe(self, seat: Seat, *, row_count: int | None = None) -> str:
        """Human phrasing for rationale strings, derived from geometry only."""
        depth = (
            "front" if seat.y < 0.25
            else "back" if seat.y > 0.8
            else "middle"
        )
        lateral = "centre" if abs(seat.x) < 0.25 else ("left" if seat.x < 0 else "right")
        return f"{depth}-{lateral} (row {seat.row_label})"
