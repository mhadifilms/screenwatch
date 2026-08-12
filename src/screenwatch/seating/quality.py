"""Scale-free seat scoring.

Everything here reads `x`/`y` only, never row counts or seat numbers, so a
score means the same thing in a 500-seat IMAX and a 40-seat microcinema.
That is the whole point of normalizing geometry first: one tuning, all rooms.

The default is a middle *area*, not one magic row. Once a layout is known,
`for_auditorium()` selects the rows in the central half of the room's
 normalized depth and gives that band a shallow preference for the exact
 centre. The band is made from the rows that actually exist, so gaps,
 cross-aisles, short rooms and irregular layouts do not need venue-specific
 row numbers. A caller can provide an explicit `ideal_depth` when it has
 independently reviewed room evidence; observed listings never calibrate the
 seat model automatically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from ..models import Attribute, Brand, Presentation
from ..presentation import trusted_venue_info
from .model import Auditorium, Seat

DEFAULT_IDEAL_DEPTH = 0.50
DEPTH_SIGMA = 0.24
LATERAL_SIGMA = 0.55
MIDDLE_DEPTH_START = 0.25
MIDDLE_DEPTH_END = 0.75


@dataclass(frozen=True)
class QualityModel:
    ideal_depth: float = DEFAULT_IDEAL_DEPTH
    depth_sigma: float = DEPTH_SIGMA
    lateral_sigma: float = LATERAL_SIGMA
    avoid_front_rows: int = 2
    front_row_penalty: float = 0.55      # multiplier, not a subtraction
    aisle_penalty: float = 0.0           # set >0 only if the user dislikes aisles
    max_lateral: float = 1.0
    adaptive_middle: bool = True
    middle_band: tuple[float, float] | None = None

    @classmethod
    def for_venue(cls, venue_id: str | None, **overrides) -> QualityModel:
        """Apply explicit caller tuning; observed listings never calibrate seats."""
        base: dict[str, object] = {}
        if (
            venue_id
            and (info := trusted_venue_info(venue_id))
            and (depth := info.get("ideal_depth")) is not None
        ):
            base["ideal_depth"] = float(depth)
            # A future explicit calibration may override the generic
            # middle-band prior. Observed screening evidence does not.
            base["adaptive_middle"] = False
        if overrides.get("ideal_depth") is not None and "adaptive_middle" not in overrides:
            base["adaptive_middle"] = False
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**base)

    # ------------------------------------------------------------------
    def for_auditorium(self, auditorium: Auditorium) -> QualityModel:
        """Adapt the depth prior to the rows present in one auditorium.

        `y` is already physical-ish normalized depth: skipped row numbers
        retain their gap. We therefore choose the actual rows falling in the
        central half of that depth, rather than assuming every room has the
        same number of evenly spaced rows. A one- or two-row room has no
        meaningful front/back choice, so depth is neutral and lateral
        position decides.
        """
        if not self.adaptive_middle:
            return self

        depths = sorted({
            seat.y for seat in auditorium.seats if seat.kind.is_bookable
        })
        if not depths:
            return replace(self, middle_band=None)
        if len(depths) <= 2:
            return replace(self, middle_band=(0.0, 1.0))

        middle = [
            depth for depth in depths
            if MIDDLE_DEPTH_START <= depth <= MIDDLE_DEPTH_END
        ]
        if not middle:
            # Sparse or unusual geometry: choose the observed row(s) closest
            # to the perceptual midpoint so the recommendation is never
            # forced to an arbitrary edge row.
            ordered = sorted(depths, key=lambda depth: abs(depth - 0.5))
            middle = ordered[:2] if len(ordered) > 3 else ordered[:1]
        return replace(self, middle_band=(min(middle), max(middle)))

    def for_presentation(self, presentation: Presentation) -> QualityModel:
        """Apply conservative format-specific comfort adjustments.

        These are broad priors, not invented venue calibration: 3D and
        panoramic formats are less forgiving off axis; IMAX/4DX deliberately
        support a wider immersion band. Explicit user/venue preferences still
        take precedence through the base model.
        """
        model = self
        if Attribute.THREE_D in presentation.attrs:
            model = replace(model, lateral_sigma=model.lateral_sigma * 0.82,
                            front_row_penalty=model.front_row_penalty * 0.85)
        if presentation.brand is Brand.SCREENX:
            model = replace(model, lateral_sigma=model.lateral_sigma * 0.78)
        if presentation.brand in (Brand.IMAX, Brand.FOURDX, Brand.DBOX):
            model = replace(model, depth_sigma=model.depth_sigma * 1.15)
        return model

    def _depth_score(self, y: float) -> float:
        sigma = max(abs(self.depth_sigma), 1e-6)
        if self.middle_band is not None:
            low, high = sorted(self.middle_band)
            if low == 0.0 and high == 1.0:
                return 1.0
            if low <= y <= high:
                # Keep the whole middle area desirable, with only a gentle
                # nudge toward the exact centre of the band.
                band_sigma = max((high - low) / 2, sigma / 2, 0.01)
                centre_bias = math.exp(-(((y - self.ideal_depth) / band_sigma) ** 2) / 2)
                return 0.95 + 0.05 * centre_bias
            distance = low - y if y < low else y - high
            return math.exp(-((distance / sigma) ** 2) / 2)

        return math.exp(-(((y - self.ideal_depth) / sigma) ** 2) / 2)

    def score(self, seat: Seat, *, row_count: int | None = None) -> float:
        """0..1 desirability for a single seat.

        Multiplicative rather than additive: a front-row seat dead centre is
        still a front-row seat, and adding a centred bonus to it would let it
        outrank a genuinely good seat slightly off axis.
        """
        depth = self._depth_score(seat.y)
        lateral = math.exp(-((seat.x / self.lateral_sigma) ** 2) / 2)

        value = depth * lateral

        if abs(seat.x) > self.max_lateral:
            value *= 0.25

        # Front-row penalty is expressed in normalized depth so it survives
        # any auditorium size; row_count converts the user's "first N rows".
        # In a tiny room there may be no non-front alternative. Penalizing
        # the first two rows of a two-row room would penalize every seat.
        if row_count and row_count > self.avoid_front_rows and self.avoid_front_rows:
            cutoff = (self.avoid_front_rows - 0.5) / max(row_count - 1, 1)
            if seat.y <= cutoff:
                value *= self.front_row_penalty

        if self.aisle_penalty and seat.aisle_adjacent:
            value *= 1.0 - self.aisle_penalty

        return round(max(0.0, min(1.0, value)), 4)

    def score_interval(
        self,
        seat: Seat,
        *,
        row_count: int | None = None,
        geometry_confidence: float = 1.0,
    ) -> tuple[float, float]:
        """Conservative desirability interval when seat geometry is uncertain.

        Venue feeds range from real diagram coordinates to row/seat labels that
        Screen Watch has normalized itself.  Treating those sources as equally
        precise creates false confidence around the centreline and the edge of
        the preferred depth band.  We therefore score a small uncertainty box
        around the reported position.  At confidence 1 this collapses to the
        ordinary point score; inferred geometry receives a wider, deliberately
        conservative interval.

        This is a distribution-free bound, not a claim that coordinate error is
        Gaussian.  It gives the group optimizer a stable worst-case value even
        when a source's drawing is approximate.
        """
        confidence = max(0.0, min(1.0, geometry_confidence))
        x_radius = 0.18 * (1.0 - confidence)
        y_radius = 0.12 * (1.0 - confidence)
        samples = {
            (
                max(-1.0, min(1.0, seat.x + dx)),
                max(0.0, min(1.0, seat.y + dy)),
            )
            for dx in (-x_radius, 0.0, x_radius)
            for dy in (-y_radius, 0.0, y_radius)
        }
        values = [
            self.score(replace(seat, x=x, y=y), row_count=row_count)
            for x, y in samples
        ]
        return round(min(values), 4), round(max(values), 4)

    def score_all(self, auditorium: Auditorium) -> dict[str, float]:
        model = self.for_auditorium(auditorium)
        rows = auditorium.row_count
        return {s.id: model.score(s, row_count=rows) for s in auditorium.seats}

    def describe(self, seat: Seat, *, row_count: int | None = None) -> str:
        """Human phrasing for rationale strings, derived from geometry only."""
        low, high = self.middle_band or (MIDDLE_DEPTH_START, MIDDLE_DEPTH_END)
        depth = (
            "front" if seat.y < low
            else "back" if seat.y > high
            else "middle"
        )
        lateral = "centre" if abs(seat.x) < 0.25 else ("left" if seat.x < 0 else "right")
        return f"{depth}-{lateral} (row {seat.row_label})"
