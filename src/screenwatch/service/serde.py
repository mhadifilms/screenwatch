"""SearchSpec <-> JSON.

Watches persist a spec and replay it days later, and both transports accept
one as input, so the wire format is load-bearing rather than a convenience.
It is deliberately hand-written: the round trip is asserted in tests, and a
spec that silently loses its time windows on reload would make a watch mean
something different from the search that created it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, time

from ..identity.work import WorkRef
from ..models import Attribute, Brand, Preference, PresentationSpec, Projection
from ..ranking.spec import (
    Budget,
    DateWindow,
    GeoPoint,
    LocationSpec,
    Membership,
    SearchSpec,
    SeatingPrefs,
    TimeWindow,
    Weights,
)


def _attrs(names) -> frozenset[Attribute]:
    return frozenset(Attribute(n) for n in names or ())


def location_from_dict(data: dict | None) -> LocationSpec:
    """Decode the reusable location fragment used by search and refresh APIs."""
    loc = data or {}
    origin = loc.get("origin")
    return LocationSpec(
        origin=GeoPoint(origin["lat"], origin["lon"]) if origin else None,
        radius_km=loc.get("radius_km", 40.0),
        city=loc.get("city"),
        allow=frozenset(loc.get("allow") or ()),
        deny=frozenset(loc.get("deny") or ()),
        chains=frozenset(loc.get("chains") or ()),
        venue_types=frozenset(loc.get("venue_types") or ()),
    )


def spec_to_dict(spec: SearchSpec) -> dict:
    return {
        "work": {k: v for k, v in
                 {"query": spec.work.query, "work_id": spec.work.work_id,
                  "tmdb_id": spec.work.tmdb_id}.items() if v is not None},
        "party_size": spec.party_size,
        "location": {
            "origin": ({"lat": spec.location.origin.lat, "lon": spec.location.origin.lon}
                       if spec.location.origin else None),
            "radius_km": spec.location.radius_km,
            "city": spec.location.city,
            "allow": sorted(spec.location.allow),
            "deny": sorted(spec.location.deny),
            "chains": sorted(spec.location.chains),
            "venue_types": sorted(spec.location.venue_types),
        },
        "date_window": ({"start": spec.date_window.start.isoformat(),
                         "end": spec.date_window.end.isoformat()}
                        if spec.date_window else None),
        "time_windows": [
            {"start": w.start.isoformat(timespec="minutes"),
             "end": w.end.isoformat(timespec="minutes"),
             "weekdays": sorted(w.weekdays) if w.weekdays is not None else None}
            for w in spec.time_windows
        ],
        "presentations": [
            {"projection": s.projection.value if s.projection else None,
             "brand": s.brand.value if s.brand else None,
             "aspect": s.aspect,
             "requires": sorted(a.value for a in s.requires),
             "excludes": sorted(a.value for a in s.excludes),
             "label": s.label}
            for s in (spec.presentations.ranked if spec.presentations else [])
        ],
        "strict_presentations": spec.strict_presentations,
        "memberships": sorted(m.value for m in spec.memberships),
        "seating": {
            "together": spec.seating.together,
            "allow_split": spec.seating.allow_split,
            "avoid_front_rows": spec.seating.avoid_front_rows,
            "ideal_depth": spec.seating.ideal_depth,
            "max_lateral": spec.seating.max_lateral,
            "require": sorted(a.value for a in spec.seating.require),
            "avoid_aisle": spec.seating.avoid_aisle,
            "wheelchair_spaces": spec.seating.wheelchair_spaces,
            "companion_seats": spec.seating.companion_seats,
        },
        "budget": {"max_total_usd": spec.budget.max_total_usd,
                   "max_per_ticket_usd": spec.budget.max_per_ticket_usd},
        "weights": spec.weights.as_dict(),
        "include_sold_out": spec.include_sold_out,
        "release_radar": spec.release_radar,
        "max_seatmap_fetches": spec.max_seatmap_fetches,
        "diversify_per_group": spec.diversify_per_group,
        "coverage": spec.coverage,
    }


def spec_from_dict(data: dict) -> SearchSpec:
    seating = data.get("seating") or {}
    budget = data.get("budget") or {}

    presentations = [
        PresentationSpec(
            projection=Projection(p["projection"]) if p.get("projection") else None,
            brand=Brand(p["brand"]) if p.get("brand") else None,
            aspect=p.get("aspect"),
            requires=_attrs(p.get("requires")),
            excludes=_attrs(p.get("excludes")),
            label=p.get("label", ""),
        )
        for p in data.get("presentations") or []
    ]

    dw = data.get("date_window")
    return SearchSpec(
        work=WorkRef(**(data.get("work") or {"query": ""})),
        party_size=data.get("party_size", 1),
        location=location_from_dict(data.get("location")),
        date_window=(
            DateWindow(date.fromisoformat(dw["start"]), date.fromisoformat(dw["end"]))
            if dw else None
        ),
        time_windows=tuple(
            TimeWindow(
                start=time.fromisoformat(w["start"]),
                end=time.fromisoformat(w["end"]),
                weekdays=frozenset(w["weekdays"]) if w.get("weekdays") is not None else None,
            )
            for w in data.get("time_windows") or []
        ),
        presentations=Preference(presentations) if presentations else None,
        strict_presentations=bool(data.get("strict_presentations", False)),
        memberships=frozenset(Membership(m) for m in data.get("memberships") or ()),
        seating=SeatingPrefs(
            together=seating.get("together", True),
            allow_split=seating.get("allow_split", True),
            avoid_front_rows=seating.get("avoid_front_rows", 2),
            ideal_depth=seating.get("ideal_depth"),
            max_lateral=seating.get("max_lateral", 1.0),
            require=_attrs(seating.get("require")),
            avoid_aisle=seating.get("avoid_aisle", False),
            wheelchair_spaces=seating.get("wheelchair_spaces", 0),
            companion_seats=seating.get("companion_seats", 0),
        ),
        budget=Budget(budget.get("max_total_usd"), budget.get("max_per_ticket_usd")),
        weights=Weights(**(data.get("weights") or {})),
        include_sold_out=data.get("include_sold_out", False),
        release_radar=data.get("release_radar", False),
        max_seatmap_fetches=data.get("max_seatmap_fetches", 10),
        diversify_per_group=data.get("diversify_per_group", 2),
        coverage=data.get("coverage", "auto"),
    )


def spec_to_json(spec: SearchSpec) -> str:
    return json.dumps(spec_to_dict(spec), sort_keys=True)


def spec_from_json(raw: str) -> SearchSpec:
    return spec_from_dict(json.loads(raw))


def local_offset(screening) -> str:
    """Return the source venue's wall-clock offset as ``+/-HH:MM``.

    Screening models intentionally keep local showtimes naive because every
    provider supplies them as venue wall-clock values. The UTC instant is the
    authoritative companion, so their difference gives clients an explicit
    offset without forcing them to guess from the browser's timezone.
    """
    utc = screening.starts_at_utc
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=UTC)
    delta = screening.starts_at_local - utc.astimezone(UTC).replace(tzinfo=None)
    minutes = round(delta.total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def option_to_dict(option, *, include_seatmap: bool = False) -> dict:
    """Wire shape for an Option. Shared by the API and MCP so the two agree."""
    from ..seating.render import to_unicode_grid

    s = option.screening
    out = {
        "option_id": option.option_id,
        "screening_id": s.screening_id,
        "canonical_screening_id": s.canonical_screening_id,
        "score": round(option.score, 4),
        "title": s.work.title,
        "work_id": s.work.work_id,
        "venue": {"id": s.venue_id, "name": s.venue_name, "chain": s.chain,
                  "distance_km": s.distance_km},
        "starts_at_local": s.starts_at_local.isoformat(),
        "starts_at_local_offset": local_offset(s),
        "starts_at_utc": s.starts_at_utc.isoformat(),
        "presentation": s.presentation.describe(),
        "presentation_raw": s.presentation.raw,
        "sources": list(s.sources),
        "screen_id": s.screen_id,
        "price_hint_usd": s.price_hint_usd,
        "availability": s.availability.value,
        "seats_available": s.seats_available,
        "seats_capacity": s.seats_capacity,
        "seats_sold": s.seats_sold,
        "seat_data": option.seat_data,
        "seats": (
            {
                "labels": option.seats.labels,
                "cohesion": option.seats.cohesion.value,
                "together": option.seats.cohesion.is_together,
                "complete": option.seats.complete,
                "count": option.seats.size,
                "quality": option.seats.quality,
            }
            if option.seats else None
        ),
        "seat_estimate": (
            {
                "available": option.feasibility.available,
                "capacity": option.feasibility.capacity,
                "together_probability": option.feasibility.together_probability,
                "can_fit": option.feasibility.can_fit_at_all,
                "summary": option.feasibility.describe(),
            }
            if getattr(option, "feasibility", None) is not None else None
        ),
        "components": {k: round(v, 4) for k, v in option.components.items()},
        "reasons": list(option.reasons),
        "tradeoffs": list(option.tradeoffs),
        "booking_link": s.deeplink,
    }
    if include_seatmap and option.auditorium is not None:
        picked = {x.id for x in option.seats.seats} if option.seats else set()
        out["seatmap"] = to_unicode_grid(option.auditorium, picked)
    return out
