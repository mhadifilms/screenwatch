# HTTP API and MCP

The HTTP API and MCP server call the same service layer. If a behavior differs
between them, it is a bug rather than a separate product rule.

## Start the API

```bash
uvicorn screenwatch.api.app:default_app --factory --port 8787
```

The browser app uses the same process:

```bash
screenwatch --port 8787
```

FastAPI publishes OpenAPI at `/openapi.json` and interactive documentation at
`/docs`. The API is local-only by default. `X-User-Id` selects the isolated
watch, alert, and search-history namespace; omit it for the default `local`
user.

## Core HTTP routes

| Route | Purpose |
| --- | --- |
| `GET /v1/health` | Process and provider health |
| `GET /v1/analytics/overview` | Inventory, directory, provider surfaces, evidence coverage, and alert backlog |
| `GET /v1/evidence/overview` | Counts and freshness for locally captured source observations |
| `GET /v1/analytics/providers` | Persisted freshness, latency, clipping, and error health |
| `GET /v1/analytics/inventory?group_by=...` | Evidence cube grouped by `chain`, `venue`, `venue_type`, `city`, `format`, or `availability` |
| `GET /v1/venues` | Filter the local venue graph by chain, type, city, radius, or query |
| `GET /v1/venues/{venue_id}` | Venue detail, observed presentations, room profiles, and provenance |
| `GET /v1/venues/{venue_id}/evidence` | Source-linked evidence grouped by kind, scope, source, and freshness |
| `POST /v1/venues/refresh` | Run a full configured national-directory refresh without fetching film showtimes; returns `scope`, `full_refresh`, `complete`, per-provider counts, errors, clipping, and duration |
| `GET /v1/resolve?query=...` | Inspect title normalization and canonical identity |
| `GET /v1/releases/signals?query=...` | Read the AMC catalog tripwire; not ticket-sale proof |
| `POST /v1/search` | Run a ranked, source-backed screening search |
| `GET /v1/search/{search_id}` | Read a remembered or persisted search run |
| `GET /v1/search/{search_id}/seatmap/{option_id}` | Retrieve the structured seat document, SVG, and text grid for a search option |
| `GET /v1/search/{search_id}/seatmap/{option_id}.svg` | Display the self-contained live auditorium SVG |
| `POST /v1/seatmaps/render` | Render any supplied auditorium to structured JSON plus SVG, without a search session |
| `POST /v1/seatmaps/render.svg` | Render any supplied auditorium directly as `image/svg+xml` |
| `POST /v1/search/{search_id}/booking-runway/{option_id}` | Split a large party into capped, coordinated browser/profile checkout lanes |
| `GET /v1/booking-link/{option_id}` | Return the final source booking URL |
| `POST /v1/watches` | Create a durable monitor |
| `GET /v1/watches` / `GET /v1/watches/{watch_id}` | Inspect active monitors and health |
| `POST /v1/watches/{watch_id}/run` | Run one monitor immediately |
| `GET /v1/watches/{watch_id}/history` | Replay durable alert history |
| `GET /v1/notifications` | Read unacknowledged local alerts |
| `POST /v1/watches/poll` | Run due watches and read pending alerts |
| `POST /v1/watches/acknowledge` | Explicitly acknowledge handled alerts |

## Seat-map renderer

The renderer is a native, reusable service rather than a browser-only view.
Its SVG has a responsive `viewBox`, embedded styles, screen and row labels, a
legend, accessible names, and machine-readable `data-seat-id`, `data-status`,
and `data-kind` attributes. Source row and column coordinates remain intact,
so horizontal aisles, cross-aisles, short rows, and large premium rooms do not
collapse into a rectangular approximation. Standard seats, recliners,
loveseats/modules, wheelchair spaces, and companion seats each retain their
type. Available, taken, temporarily held, unavailable, and recommended seats
remain visually distinct.

For a quick generated room, send ASCII rows:

```bash
curl -s http://127.0.0.1:8787/v1/seatmaps/render \
  -H 'content-type: application/json' \
  -d '{
    "venue_id": "demo-house",
    "screen_id": "7",
    "name": "Demo House · Auditorium 7",
    "layout": [".. xx ..", "rr wwcll"],
    "picked": ["A1", "A2"],
    "theme": "dark"
  }'
```

The layout alphabet is `.` available, `x` taken, `o` held, a space for a
structural gap, `#` blocked, `w` wheelchair, `c` companion, `r` recliner, and
`l` loveseat. For a source-backed layout, send `seats` instead of `layout`;
each seat accepts row/column labels and indices, status, kind, aisle adjacency,
and physical-module metadata. Seat ids and row/column coordinate pairs must be
unique.

The JSON response contains `summary`, `layout`, `legend`, ordered `rows`, all
semantic `seats`, and the finished `svg`. Post the same body to the `.svg`
route for a direct image response. Search-scoped seat-map routes use the same
document and highlight the optimizer's recommendation. SVG routes and JSON
routes accept dark or light themes and optional legend and row labels.

## Search shape

The smallest useful request is:

```json
{
  "work": {"query": "Example Feature"},
  "party_size": 4,
  "seating": {
    "party_kind": "friends",
    "together": true,
    "allow_split": true,
    "avoid_strangers": true
  },
  "location": {
    "city": "San Francisco",
    "radius_km": 40
  },
  "presentations": [
    {
      "projection": "film_70mm_15perf",
      "brand": "imax",
      "label": "IMAX 70mm"
    },
    {
      "brand": "imax",
      "label": "Any IMAX"
    }
  ],
  "coverage": "exhaustive",
  "include_sold_out": true
}
```

Presentation entries are ordered best-first. Unset fields are wildcards, so a
projection plus brand expresses “IMAX 70mm” while a brand alone expresses any
IMAX. Use `strict_presentations` when a fallback format must not be returned.

Location supports `city`, an `origin` latitude/longitude, `radius_km`, chain
filters, venue-type filters, explicit `allow` venue ids, and `deny` venue ids.
An explicitly allowed venue remains in scope even when it is outside the
radius—the API does not assume that a 70mm trip is accidental.

Seating `party_kind` may be `date`, `friends`, `family`, `coworkers`, or
`generic`. For a known party graph, `relationships` accepts zero-based member
pairs with `weight` and optional `must_adjacent`; for example
`{"a": 0, "b": 2, "weight": 1.0, "must_adjacent": true}`. The optimizer
uses those bonds when assigning people inside the selected geometric block.
`max_rows`, `avoid_strangers`, `prefer_aisle`, wheelchair spaces, and companion
seats further constrain or personalize the result. Accessibility requirements
change the feasible inventory rather than merely adding a ranking bonus.

`coverage` is `auto`, `nearby`, or `exhaustive`. `auto` is exhaustive whenever
the request has a city, origin/radius, chain, venue type, or explicit venue
scope; it stays nearby for an unscoped request. `nearby` is the low-cost
bounded mode. `exhaustive` removes Screenwatch's venue/day caps for the
requested scope and is the right choice for a national search or a long-lived
release watch.

## Reading a search response

The response includes:

- `options`: ranked screening-plus-seat products;
- `narration` and `comparison`: human-readable explanations;
- `coverage`: the effective source traversal mode (`nearby` or `exhaustive`);
- `complete`: whether provider errors or scope clipping changed the search;
- `clipped` and `provider_errors`: what was not read or failed;
- `provider_stats`: counts and timing per source;
- `seatmaps_fetched`: how much guarded seat enrichment was performed;
- `unresolved_titles`: title identity work that needs review.

An option's seat data is not all equivalent:

| `seat_data` | Meaning |
| --- | --- |
| `grid` | Confirmed per-seat availability and geometry |
| `estimated` | Derived from a source count and room shape |
| `availability` | Sellable/sold-out state without an exact count or grid |
| `unavailable` / `unknown` | No usable seat surface was returned |

Do not compare an estimated contiguous-seat result as if it were a confirmed
seat selection. The ranker intentionally caps estimates below confirmed
evidence.

For confirmed grids, `seats` includes `worst_person_utility`, `nash_welfare`,
`robust_preference_score`, `pareto_optimal`, and the physical `parts` of the
arrangement. `seat_alternatives` contains high-merit, structurally different
Pareto arrangements for the same screening rather than cosmetic one-seat
shifts. Each carries the same fields and proof contract. The primary seat
selection's `optimization` object reports:

- `proven_optimal: true` means every candidate combination in the declared
  hard-constraint space was evaluated and `optimality_gap_upper_bound` is zero;
- `proven_optimal: false` means the large-room anytime solver was used. Its
  method, evaluated-candidate count, geometry confidence, scope, and valid
  robust-utility gap bound remain visible rather than being presented as proof.

Optimality is relative to the known seat map, request, and ScreenWatch utility
model. It is not a claim that an approximate venue feed contains unobserved
screen dimensions, obstructions, or personal preferences.

Search inputs reject invalid coordinates and clock values, reversed date
windows, accessible-seat counts larger than the party, out-of-range
relationship members, contradictory aisle preferences, unknown or negative
ranking weights, and unreasonable fetch/diversification budgets before any
provider work begins.

For a large-party drop, post `party_size`, `transaction_limit`, and
`parallel_checkouts` to the booking-runway route. A party of 15 with a limit
of 10 becomes balanced 8+7 carts. When the option has an exact seat grid, each
lane receives its exact seat ids and labels; otherwise the response is marked
`provisional`. The route returns links for separate signed-in profiles and
never holds seats or submits a purchase.

## Inventory and provenance

```bash
curl -s 'http://127.0.0.1:8787/v1/analytics/inventory?group_by=venue_type'
curl -s 'http://127.0.0.1:8787/v1/venues?city=San%20Francisco&type=multiplex'
curl -s 'http://127.0.0.1:8787/v1/venues/amc-metreon-16'
```

Venue detail includes `evidence`, `capabilities`, and (on the detailed route)
`observed_rooms`. The evidence route also returns room rollups with the latest
observed capacity and availability. These are source-backed observations, not a
permanent room inventory. The compatibility `hardware` object reports
`status: "observations-only"`, `permanent_claims: 0`, and
`usable_for_inference: false` until a future narrowly scoped hardware claim
workflow exists. See [Data trust and provenance](data-trust.md).

## MCP setup and tools

```bash
claude mcp add screenwatch -- "$PWD/.venv/bin/python" -m screenwatch.mcp.server
```

The MCP server exposes the same major operations:

- `resolve_title`, `find_screenings`, `get_seatmap`, `render_seatmap`,
  `explain_ranking`;
- `get_data_overview`, `get_inventory_analytics`;
- `list_venues`, `get_venue`, `get_venue_evidence`, `refresh_venues`;
- `find_release_signals`;
- `create_watch`, `list_watches`, `cancel_watch`, `poll_watches`,
  `acknowledge_hits`, `get_watch_history`;
- `get_booking_link` as the final, non-purchasing step.

The Pydantic input descriptions in `src/screenwatch/mcp/schemas.py` are part
of the MCP contract. Update them whenever a search field's meaning changes.
`get_seatmap` returns `unicode`, `svg`, or structured `json` for a ranked
option. `render_seatmap` accepts the same ASCII layout alphabet without first
running a search, which is useful for agents that already possess a room map.
