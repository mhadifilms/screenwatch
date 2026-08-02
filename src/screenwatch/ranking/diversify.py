"""Stop near-identical options from monopolising the top slots.

A live search for Spider-Man in Manhattan returned, as its top three, the same
film in the same Dolby house at the same venue at 9:00am, 12:30pm and 4:00pm -
all scoring 0.93, all offering the same two seats. The ranking was correct and
the output was useless: three slots spent telling you one thing.

So the sorted list is re-ordered, never filtered. Every option stays present
and keeps its score; duplicates are simply pushed down so the visible top
carries distinct choices. Filtering would be wrong - if you genuinely want the
4pm showing at that one venue, it must still be findable.
"""

from __future__ import annotations

from collections import defaultdict

from .candidate import Option


def group_key(option: Option) -> tuple:
    """What counts as "the same kind of option"."""
    p = option.screening.presentation
    return (option.screening.venue_id, p.projection, p.brand, p.aspect)


def diversify(options: list[Option], *, per_group: int = 2) -> list[Option]:
    """Re-order so no venue-and-format repeats more than `per_group` times
    before every other distinct option has had a turn.

    Round-robin rather than a hard cap: with `per_group=2`, positions fill
    with each group's best two, then each group's next two, and so on. A
    search where one venue genuinely holds every good option still surfaces
    them, just after the alternatives have been seen.
    """
    if per_group <= 0 or len(options) < 2:
        return options

    buckets: dict[tuple, list[Option]] = defaultdict(list)
    order: list[tuple] = []
    for option in options:                       # input is already sorted
        key = group_key(option)
        if key not in buckets:
            order.append(key)
        buckets[key].append(option)

    out: list[Option] = []
    while any(buckets[k] for k in order):
        for key in order:
            take, buckets[key] = buckets[key][:per_group], buckets[key][per_group:]
            out.extend(take)
    return out
