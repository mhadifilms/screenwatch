"""Turn one seat recommendation into a coordinated checkout runway.

Screenwatch never presses the final purchase button.  It can still remove the
most dangerous ambiguity from a large-party drop: how many transactions are
required, which browser/profile owns each transaction, and which exact seats
belong in each cart.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from ..ranking.candidate import Option
from ..seating.model import Seat


@dataclass(frozen=True)
class CheckoutLane:
    lane: int
    wave: int
    profile: str
    ticket_count: int
    seat_ids: tuple[str, ...] = ()


def balanced_transaction_sizes(party_size: int, transaction_limit: int) -> tuple[int, ...]:
    """Return the fewest legal transactions, balanced to reduce lane risk.

    A 15-person party with a ten-ticket cap becomes 8+7 rather than 10+5.
    Balanced carts are easier to place around a central seam and neither lane
    becomes an obviously disposable tail if availability changes mid-checkout.
    """
    if party_size < 1:
        raise ValueError("party_size must be at least 1")
    if transaction_limit < 1:
        raise ValueError("transaction_limit must be at least 1")
    count = ceil(party_size / transaction_limit)
    small, remainder = divmod(party_size, count)
    return tuple(small + (1 if index < remainder else 0) for index in range(count))


def _ordered_seats(option: Option) -> tuple[Seat, ...]:
    if option.seats is None:
        return ()
    parts = option.seats.parts or (option.seats.seats,)
    return tuple(
        seat
        for part in parts
        for seat in sorted(part, key=lambda item: (item.row_index, item.col_index))
    )


def _seat_blocks(
    seats: tuple[Seat, ...], sizes: tuple[int, ...], limit: int
) -> tuple[tuple[Seat, ...], ...]:
    """Split in physical order without cutting a required seat module."""
    blocks: list[tuple[Seat, ...]] = []
    cursor = 0
    for size in sizes[:-1]:
        boundary = cursor + size
        if boundary < len(seats):
            left, right = seats[boundary - 1], seats[boundary]
            if left.module_required and left.module_id == right.module_id:
                module_start = boundary - 1
                while module_start > cursor and seats[module_start - 1].module_id == left.module_id:
                    module_start -= 1
                boundary = module_start
        if boundary <= cursor or boundary - cursor > limit:
            raise ValueError("transaction limit would split a required seat module")
        blocks.append(seats[cursor:boundary])
        cursor = boundary
    blocks.append(seats[cursor:])
    if any(len(block) > limit for block in blocks):
        raise ValueError("transaction limit would split a required seat module")
    return tuple(blocks)


def _seat_label(seats: tuple[Seat, ...]) -> str | None:
    if not seats:
        return None
    rows: dict[str, list[str]] = {}
    for seat in seats:
        rows.setdefault(seat.row_label, []).append(seat.col_label)
    return ", ".join(f"{row} {'+'.join(columns)}" for row, columns in rows.items())


def build_booking_runway(
    option: Option,
    *,
    party_size: int,
    transaction_limit: int = 10,
    parallel_checkouts: int = 2,
) -> dict:
    """Build a user-controlled, multi-profile checkout plan for an option."""
    if parallel_checkouts < 1:
        raise ValueError("parallel_checkouts must be at least 1")
    if (
        option.seats is not None
        and option.seats.requested
        and party_size != option.seats.requested
    ):
        raise ValueError("party_size must match the search that produced this option")
    sizes = balanced_transaction_sizes(party_size, transaction_limit)
    seats = _ordered_seats(option)
    exact_assignment = len(seats) == party_size
    blocks = _seat_blocks(seats, sizes, transaction_limit) if exact_assignment else ()
    member_by_seat = {
        seat.id: member
        for seat, member in zip(
            option.seats.seats if option.seats else (),
            option.seats.assignment if option.seats else (),
            strict=False,
        )
    }
    lanes: list[CheckoutLane] = []
    listing = next(
        (
            item for item in option.screening.source_listings
            if item.availability.is_buyable and item.deeplink
        ),
        None,
    )
    booking_link = listing.deeplink if listing else option.screening.deeplink
    for index, size in enumerate(sizes):
        assigned = blocks[index] if exact_assignment else ()
        lanes.append(CheckoutLane(
            lane=index + 1,
            wave=(index // parallel_checkouts) + 1,
            profile=f"Checkout lane {chr(65 + (index % parallel_checkouts))}",
            ticket_count=len(assigned) if exact_assignment else size,
            seat_ids=tuple(seat.id for seat in assigned),
        ))

    waves = max(lane.wave for lane in lanes)
    if option.seats is not None and not option.seats.complete:
        readiness = "blocked"
        warning = "The current recommendation cannot seat the whole party."
    elif booking_link is None:
        readiness = "blocked"
        warning = "This source has no checkout link yet."
    elif exact_assignment:
        readiness = "ready"
        warning = None
    else:
        readiness = "provisional"
        warning = "Ticket counts are planned, but exact seats are not available from this source."

    lane_payload = []
    for lane, assigned in zip(lanes, blocks or ((),) * len(lanes), strict=True):
        lane_payload.append({
            "lane": lane.lane,
            "wave": lane.wave,
            "profile": lane.profile,
            "ticket_count": lane.ticket_count,
            "seat_ids": list(lane.seat_ids),
            "seat_label": _seat_label(tuple(assigned)),
            "members": [member_by_seat[seat.id] for seat in assigned if seat.id in member_by_seat],
            "booking_link": booking_link,
        })

    return {
        "readiness": readiness,
        "warning": warning,
        "party_size": party_size,
        "transaction_limit": transaction_limit,
        "parallel_checkouts": parallel_checkouts,
        "transactions": len(lanes),
        "waves": waves,
        "split": [lane.ticket_count for lane in lanes],
        "exact_seat_assignment": exact_assignment,
        "lanes": lane_payload,
        "option": {
            "option_id": option.option_id,
            "venue": option.screening.venue_name,
            "starts_at_local": option.screening.starts_at_local.isoformat(),
            "presentation": option.screening.presentation.describe(),
            "sources": list(option.screening.sources),
            "checkout_source": listing.source if listing else option.screening.chain,
        },
        "handoff": (
            "Open each lane in a separately signed-in browser, profile, or device. "
            "Coordinate carts, "
            "confirm the seam between seat blocks, then submit purchases together."
        ),
    }
