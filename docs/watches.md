# Release and ticket watches

Watches are durable searches with state. They are designed for events such as:

> Tell me when a new IMAX 70mm screening appears for Dune: Part Three in the
> Bay Area, and tell me when a party-sized seat group becomes possible.

## Watch lifecycle

```text
create → seed current state → poll source → compare state → persist hit
                                      ↓
                              deliver / acknowledge
```

Creation is seeded by default. A seeded watch records what is already visible
and stays quiet about it; it alerts on later changes. Set `seed: false` when a
one-shot monitor should report current inventory too.

Every hit is persisted before delivery. Polling is non-destructive by default:
reading alerts does not acknowledge them. A client acknowledges only after it
has handled the alert. Webhook delivery has its own retry ledger, so consuming
the local notification queue does not erase a webhook attempt.

## What can trigger a hit

Depending on the search specification, a watch can report:

- a genuinely new matching screening;
- a sold-out screening becoming sellable;
- seats returning to a previously known showing;
- a seat map becoming available;
- a contiguous group for the requested party becoming possible;
- a change in availability, seat evidence, or presentation state;
- a release-radar catalog signal changing.

The payload includes the alert type, previous and current state, source
handles, inventory status, seat evidence, recommended seats when available,
priority, and a booking link. The booking link is the final action Screenwatch
takes.

## Example HTTP watch

```bash
curl -s http://127.0.0.1:8787/v1/watches \
  -H 'content-type: application/json' \
  -d '{
    "label": "Dune 3 IMAX 70mm Bay Area",
    "cadence_s": 300,
    "seed": true,
    "spec": {
      "work": {"query": "Dune: Part Three"},
      "party_size": 4,
      "location": {
        "city": "San Francisco",
        "radius_km": 80,
        "venue_types": ["multiplex", "independent"]
      },
      "date_window": {"start": "2026-12-01", "end": "2027-01-31"},
      "presentations": [
        {
          "projection": "film_70mm_15perf",
          "brand": "imax",
          "label": "IMAX 70mm"
        }
      ],
      "strict_presentations": true,
      "coverage": "exhaustive",
      "include_sold_out": true,
      "release_radar": true
    }
  }'
```

The date range above is an example contract, not a claim about a distributor's
release schedule. Set it to the dates that matter to the user. For open-ended
ticket watches with `include_sold_out: true`, the service uses a rolling
31-day horizon so returned tickets remain watchable without an artificial
one-week cutoff.

Use `coverage: "exhaustive"` for a release watch when missing one venue would
matter. The setting is serialized with the watch, so every poll uses the same
coverage contract. Watch health exposes upstream errors and clipped scope;
exhaustive removes Screenwatch's local caps but cannot make a blocked or
website-less source publish inventory.

For a known high-value venue, add its id to `location.allow`. This is useful
when the trip is intentional, but it does not turn one observed screening into
proof that every room at the venue can project the requested format.

## Release radar is not ticket radar

`release_radar: true` adds a cheap AMC movie-sitemap check. It can say that a
title has entered or changed in the catalog before showtimes exist. It cannot
prove that tickets are on sale. The normal screening portion of the watch must
still produce a matching, source-backed showtime before a ticket-drop alert is
valid.

The HTTP response from `/v1/releases/signals` and the MCP
`find_release_signals` tool both expose `ticket_sale_proven: false` for this
reason.

## Cadence and scheduler

Run the scheduler as a separate local process for always-on monitoring:

```bash
screenwatch-scheduler
```

The scheduler uses a tiered cadence: faster near the target date or after a
hit, slower for distant windows, jittered to avoid synchronized requests, and
retired after the watch window passes. A provider error is recorded on the
watch and does not stop unrelated watches.

Watch health is visible through `last_success`, `last_error`, `last_warning`,
and `error_count`. A quiet watch with a dead provider should therefore look
different from a quiet watch that successfully found nothing.
