"""Pydantic inputs for the MCP tools.

Worth the duplication with `service/serde.py`: these generate the JSON schema
an MCP client actually reads, and the field descriptions are the only
documentation a model gets about what a spec can express. A `dict` parameter
would validate nothing and describe nothing.

`.to_dict()` produces exactly the shape `spec_from_dict` expects, and the
round trip is asserted in tests so the two cannot drift.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkInput(BaseModel):
    query: str | None = Field(None, description="Free-text title, e.g. 'example feature'")
    work_id: str | None = Field(None, description="Internal id, e.g. 'tmdb:438631'")
    tmdb_id: int | None = None

    @model_validator(mode="after")
    def has_identity(self):
        if not any((self.query and self.query.strip(), self.work_id, self.tmdb_id)):
            raise ValueError("work needs query, work_id, or tmdb_id")
        return self


class OriginInput(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class LocationInput(BaseModel):
    origin: OriginInput | None = Field(None, description="Where to measure distance from")
    radius_km: float = Field(40.0, gt=0, le=10_000, description="Search radius around origin")
    city: str | None = None
    allow: list[str] = Field(
        default_factory=list,
        description="Venue ids always included, radius or not — use for a venue "
                    "worth travelling to (a 70mm house, say)",
    )
    deny: list[str] = Field(default_factory=list)
    chains: list[str] = Field(
        default_factory=list,
        description=(
            "Restrict discovery to exhibitor ids such as amc, regal, cinemark, "
            "alamo, independent"
        ),
    )
    venue_types: list[str] = Field(
        default_factory=list,
        description=(
            "Restrict venues to directory types such as multiplex, art_house, "
            "dine_in, drive_in"
        ),
    )


class DateWindowInput(BaseModel):
    start: str = Field(description="YYYY-MM-DD")
    end: str = Field(description="YYYY-MM-DD")

    @model_validator(mode="after")
    def valid_order(self):
        try:
            start = date.fromisoformat(self.start)
            end = date.fromisoformat(self.end)
        except ValueError as exc:
            raise ValueError("date window values must be YYYY-MM-DD") from exc
        if end < start:
            raise ValueError("date window end must not precede start")
        return self


class TimeWindowInput(BaseModel):
    start: str = Field("00:00", description="Local time, HH:MM")
    end: str = Field(
        "23:59",
        description="Local time, HH:MM. An end BEFORE the start wraps past "
                    "midnight — '18:00'..'02:00' is how 'tonight' includes a "
                    "12:45am show.",
    )
    weekdays: list[int] | None = Field(None, description="0=Monday. None = every day")

    @field_validator("start", "end")
    @classmethod
    def valid_clock(cls, value: str) -> str:
        try:
            hour, minute = (int(part) for part in value.split(":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("times must be HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("times must be valid 24-hour HH:MM values")
        return value

    @field_validator("weekdays")
    @classmethod
    def valid_weekdays(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(day < 0 or day > 6 for day in value):
            raise ValueError("weekdays must use 0=Monday through 6=Sunday")
        return value


class PresentationInput(BaseModel):
    """One entry in the ranked format wishlist. Unset fields are wildcards."""

    projection: Literal[
        "digital", "digital_xenon", "digital_laser", "film_16mm", "film_35mm",
        "film_35mm_nitrate", "film_70mm", "film_70mm_15perf",
    ] | None = Field(
        None,
        description="film_70mm_15perf is IMAX film; film_70mm is standard 5-perf",
    )
    brand: Literal["none", "imax", "dolby_cinema", "plf", "screenx", "4dx", "dbox"] | None = None
    aspect: str | None = Field(None, description="Screen aspect, e.g. '1.43' for IMAX GT")
    requires: list[str] = Field(default_factory=list, description="e.g. ['open_caption']")
    excludes: list[str] = Field(default_factory=list, description="e.g. ['3d']")
    label: str = ""


class RelationshipInput(BaseModel):
    a: int = Field(..., ge=0, description="Zero-based first party member")
    b: int = Field(..., ge=0, description="Zero-based second party member")
    weight: float = Field(1.0, gt=0, le=10)
    must_adjacent: bool = False


class SeatingInput(BaseModel):
    together: bool = Field(
        True,
        description="Prefer a contiguous group; false allows independent seat ranking",
    )
    allow_split: bool = Field(True, description="Accept a split rather than nothing")
    avoid_front_rows: int = Field(
        2, ge=0, le=20, description="Treat the first N rows as a last resort"
    )
    ideal_depth: float | None = Field(
        None, ge=0, le=1,
        description="0=front, 1=back. Default is an adaptive middle area; set to override",
    )
    max_lateral: float = Field(1.0, ge=0, le=1, description="0=centre only, 1=anywhere")
    require: list[str] = Field(default_factory=list)
    avoid_aisle: bool = False
    wheelchair_spaces: int = Field(0, ge=0)
    companion_seats: int = Field(0, ge=0)
    party_kind: Literal["generic", "date", "friends", "coworkers", "family"] = "generic"
    relationships: list[RelationshipInput] = Field(
        default_factory=list,
        description="Who should sit together; member numbers are zero-based",
    )
    max_rows: int | None = Field(None, ge=1, le=8)
    avoid_strangers: bool = True
    prefer_aisle: bool = False


class BudgetInput(BaseModel):
    max_total_usd: float | None = Field(None, ge=0)
    max_per_ticket_usd: float | None = Field(None, ge=0)


class SearchSpecInput(BaseModel):
    work: WorkInput
    party_size: int = Field(1, ge=1, le=100, description="How many tickets")
    location: LocationInput = Field(default_factory=LocationInput)
    date_window: DateWindowInput | None = Field(None, description="Defaults to today..+7")
    time_windows: list[TimeWindowInput] = Field(
        default_factory=list, description="Empty means any time"
    )
    presentations: list[PresentationInput] = Field(
        default_factory=list,
        description="Ranked best-first. Empty means no format preference.",
    )
    strict_presentations: bool = Field(
        False,
        description="Treat `presentations` as a hard filter instead of a "
                    "ranking. Off for searches - with everything sold out a "
                    "lesser format still beats not going. Watches turn it on "
                    "automatically, since an alert for the wrong format is a "
                    "wrong answer rather than a partial one.",
    )
    memberships: list[
        Literal["amc_alist", "regal_unlimited", "cinemark_movie_club", "alamo_season_pass"]
    ] = Field(default_factory=list, description="Subscriptions you hold — boosts covered venues")
    seating: SeatingInput = Field(default_factory=SeatingInput)
    budget: BudgetInput = Field(default_factory=BudgetInput)
    weights: dict[str, float] = Field(
        default_factory=dict,
        description="Override ranking weights, e.g. {'format_fit': 3.0} to care "
                    "more about format than sitting together",
    )
    include_sold_out: bool = False
    release_radar: bool = Field(
        False,
        description=(
            "Also watch the low-cost AMC movie sitemap for the title entering "
            "or changing in the catalog before showtimes exist"
        ),
    )
    max_seatmap_fetches: int = Field(
        10, ge=0, le=100, description="Seat maps cost one guarded request each; this caps them"
    )

    diversify_per_group: int = Field(
        2, ge=0, le=20, description="How many options from the same venue+format may run "
                       "before other choices get a turn. 0 disables re-ordering."
    )
    coverage: Literal["auto", "nearby", "exhaustive"] = Field(
        "auto",
        description=(
            "Source coverage policy. auto exhaustively checks a city, radius, "
            "chain, or explicit venue scope; nearby is the fast bounded mode; "
            "exhaustive removes venue/day caps for a national or long-horizon crawl."
        ),
    )

    @field_validator("weights")
    @classmethod
    def valid_weights(cls, weights: dict[str, float]) -> dict[str, float]:
        allowed = {
            "format_fit", "time_fit", "lateness", "distance_fit",
            "membership_fit", "availability_prior", "venue_affinity",
            "group_cohesion", "seat_quality", "party_fit",
        }
        unknown = set(weights) - allowed
        if unknown:
            raise ValueError(f"unknown ranking weights: {', '.join(sorted(unknown))}")
        if any(not math.isfinite(value) or value < 0 for value in weights.values()):
            raise ValueError("ranking weights must be finite and non-negative")
        return weights

    @model_validator(mode="after")
    def valid_party_constraints(self):
        seating = self.seating
        accessible = seating.wheelchair_spaces + seating.companion_seats
        if accessible > self.party_size:
            raise ValueError("accessible seats cannot exceed party_size")
        if seating.avoid_aisle and seating.prefer_aisle:
            raise ValueError("avoid_aisle and prefer_aisle are mutually exclusive")
        for relationship in seating.relationships:
            if relationship.a == relationship.b:
                raise ValueError("a relationship must connect two different people")
            if max(relationship.a, relationship.b) >= self.party_size:
                raise ValueError("relationship member index exceeds party_size")
        return self

    def to_dict(self) -> dict:
        data = self.model_dump(exclude_none=False)
        data["work"] = {k: v for k, v in data["work"].items() if v is not None}
        if data["location"].get("origin") is None:
            data["location"]["origin"] = None
        return data
