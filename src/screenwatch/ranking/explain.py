"""Turning scores back into sentences.

The motivating request was not for a number, it was for a chain of reasoning:
*"4 in a row isn't there, but the 12:45 has 2 and 2 in row 3 — lowkey better."*
A ranker that emits 0.732 has not answered that.

So every option carries `reasons` (why it is good) and `tradeoffs` (what it
costs you), and the top option additionally carries a pairwise comparison
against the runner-up naming the components that actually decided it. All of
it is derived from the same component scores that produced the ordering, so
the explanation cannot drift from the maths.
"""

from __future__ import annotations

from ..seating.groups import Cohesion
from .candidate import Option
from .spec import SearchSpec

# Only mention a component in a comparison if it moved the needle.
SIGNIFICANT = 0.08

_LABELS = {
    "format_fit": "format",
    "time_fit": "showtime",
    "lateness": "how late it starts",
    "distance_fit": "travel",
    "membership_fit": "covered by your pass",
    "availability_prior": "availability",
    "venue_affinity": "venue",
    "group_cohesion": "sitting together",
    "seat_quality": "seat position",
    "party_fit": "fitting your whole party",
}


def _minutes_between(a: Option, b: Option) -> int:
    delta = a.screening.starts_at_utc - b.screening.starts_at_utc
    return round(delta.total_seconds() / 60)


def describe_seats(option: Option, spec: SearchSpec) -> str | None:
    group = option.seats
    if group is None:
        if option.seat_data == "estimated" and option.feasibility is not None:
            # No seat grid, but exact counts and the room's shape - so the
            # honest thing is a probability, not a shrug.
            return option.feasibility.describe()
        if option.seat_data == "count_only":
            return f"{option.auditorium.available} seats left (no seat map)"
        if option.seat_data == "unavailable":
            return "seat map not available for this venue"
        return None

    if group.cohesion is Cohesion.SOLO:
        return f"1 seat, {group.labels}"
    if group.cohesion is Cohesion.CONTIGUOUS:
        return f"all {group.size} together, {group.labels}"
    if group.cohesion is Cohesion.ACROSS_AISLE:
        return f"all {group.size} in a row but split by an aisle, {group.labels}"

    shape = "+".join(str(len(p)) for p in group.parts)
    where = {
        Cohesion.STACKED: "in adjacent rows, lined up",
        Cohesion.ADJACENT_ROWS: "in adjacent rows",
        Cohesion.NEARBY_ROWS: "within a couple of rows",
        Cohesion.SAME_ROW_SEPARATED: "in the same row but apart",
        Cohesion.SCATTERED: "scattered",
    }[group.cohesion]
    return f"{shape} {where}, {group.labels}"


def reasons_for(option: Option, spec: SearchSpec) -> tuple[str, ...]:
    out: list[str] = []
    c = option.components
    s = option.screening

    if spec.presentations is not None:
        rank = spec.presentations.rank(s.presentation)
        if rank == 0:
            out.append(f"your top format ({s.presentation.describe()})")
        elif rank is not None and rank <= 2:
            out.append(f"{s.presentation.describe()}, #{rank + 1} on your list")

    if option.seat_data == "estimated" and option.feasibility is not None:
        chance = option.feasibility.together_probability
        if chance >= 0.75 and spec.party_size > 1:
            out.append(f"room to seat all {spec.party_size} together (~{int(chance*100)}%)")
        elif option.feasibility.occupancy < 0.4:
            out.append(f"{option.feasibility.available} seats free")

    if option.seats is not None:
        if option.seats.complete and option.seats.cohesion.is_together:
            out.append(f"seats all {spec.party_size} of you together")
        elif option.seats.complete:
            out.append(f"seats all {spec.party_size} of you")
        if c.get("seat_quality", 0) >= 0.75:
            out.append("good position in the room")

    if c.get("membership_fit", 0) >= 1.0 and spec.memberships:
        out.append("covered by your pass")
    if s.distance_km is not None and s.distance_km <= 8:
        out.append(f"{s.distance_km:.0f} km away")

    return tuple(out)


def tradeoffs_for(option: Option, spec: SearchSpec, *, baseline: Option | None = None) -> tuple[str, ...]:
    out: list[str] = []
    c = option.components
    s = option.screening

    if spec.presentations is not None:
        rank = spec.presentations.rank(s.presentation)
        if rank is None:
            out.append(f"{s.presentation.describe()} — not on your format list")
        elif rank > 0:
            out.append(f"{s.presentation.describe()} — you rank this below your first choice")

    if option.seat_data == "estimated" and option.feasibility is not None:
        if not option.feasibility.can_fit_at_all:
            out.append(f"only {option.feasibility.available} seats left")
        elif option.feasibility.together_probability < 0.4 and spec.party_size > 1:
            out.append(
                f"sitting together looks unlikely "
                f"(~{int(option.feasibility.together_probability*100)}%)"
            )

    if option.seats is not None:
        if not option.seats.complete:
            out.append(
                f"only seats {option.seats.size} of {spec.party_size}"
            )
        elif not option.seats.cohesion.is_together:
            shape = "+".join(str(len(p)) for p in option.seats.parts)
            out.append(f"party splits {shape}")
        if c.get("seat_quality", 1.0) < 0.45:
            out.append("poor seats — near the front or far off centre")

    if c.get("lateness", 1.0) < 0.9:
        out.append(f"late start ({s.starts_at_local.strftime('%-I:%M%p').lower()})")
    if c.get("membership_fit", 1.0) < 1.0:
        out.append("not covered by your pass")
    if s.distance_km is not None and c.get("distance_fit", 1.0) < 0.5:
        out.append(f"{s.distance_km:.0f} km away")
    if s.availability.value == "almost_full":
        out.append("almost full — may go before you book")

    if baseline is not None and option is not baseline:
        delta = _minutes_between(option, baseline)
        if abs(delta) >= 20:
            out.append(
                f"{abs(delta)} min {'later' if delta > 0 else 'earlier'} than the next best"
            )
    return tuple(out)


def compare(winner: Option, runner_up: Option, spec: SearchSpec) -> str:
    """Why the top option beat the next one, in the user's own terms."""
    gains, losses = [], []
    for key, label in _LABELS.items():
        a, b = winner.components.get(key), runner_up.components.get(key)
        if a is None or b is None:
            continue
        diff = a - b
        if diff >= SIGNIFICANT:
            gains.append((diff * getattr(spec.weights, key, 1.0), label))
        elif diff <= -SIGNIFICANT:
            losses.append((-diff * getattr(spec.weights, key, 1.0), label))

    gains.sort(reverse=True)
    losses.sort(reverse=True)
    if not gains:
        return "Effectively tied; ordered by start time."

    won_on = ", ".join(label for _, label in gains[:2])
    text = f"Ranked first on {won_on}"
    if losses:
        text += f", despite being worse on {losses[0][1]}"
    return text + "."


def annotate(options: list[Option], spec: SearchSpec) -> list[Option]:
    """Attach reasons and tradeoffs to a ranked list, in place."""
    best = options[0] if options else None
    for option in options:
        option.reasons = reasons_for(option, spec)
        option.tradeoffs = tradeoffs_for(
            option, spec, baseline=best if option is not best else None
        )
    return options


def narrate(options: list[Option], spec: SearchSpec, *, limit: int = 3) -> str:
    """A short human-readable digest of the top options."""
    if not options:
        return "No screenings matched."

    lines = []
    for i, option in enumerate(options[:limit], 1):
        lines.append(f"{i}. {option.summary()}  [{option.score:.2f}]")
        if seats := describe_seats(option, spec):
            lines.append(f"   seats: {seats}")
        if option.reasons:
            lines.append(f"   for: {'; '.join(option.reasons)}")
        if option.tradeoffs:
            lines.append(f"   against: {'; '.join(option.tradeoffs)}")

    if len(options) >= 2:
        lines.append("")
        lines.append(compare(options[0], options[1], spec))
    return "\n".join(lines)
