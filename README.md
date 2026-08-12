# Screenwatch

Screenwatch is a local-first US theater intelligence system: a browser app,
HTTP API, MCP server, and durable notification engine over theater chains and
the long tail of independent cinemas.

It is built to answer a practical question:

> Given a title, a place, a party size, a format preference, and a time window,
> which specific bookable screening is best—and what evidence supports that
> answer?

The system ranks a screening together with the seats that are actually
available when a seat surface exists. It explains tradeoffs, preserves source
and freshness metadata, and stops at a booking link. It never creates a hold,
adds a ticket to a cart, or stores payment information.

## Read this first: data truth

Screenwatch builds its venue graph from source discovery and records every
claim with provenance. Official exhibitor directories cover the major chains;
OpenStreetMap cinema records provide a national independent-cinema directory,
while each venue's showtime availability remains an explicit source-backed
observation. The product deliberately distinguishes live observations,
estimates, curated routing configuration, and unknowns.

| Dataset or surface | Current state | How to interpret it |
| --- | --- | --- |
| Official chain directories | AMC national theatre sitemap, Regal national directory, Cinemark sitemap, C360 locations, Alamo market schedule | Venue existence, routing, geography, and freshness are source-linked; each provider reports its own coverage and limits. |
| OpenStreetMap `amenity=cinema` directory | National independent-venue discovery | Source-linked venue identity and geography; a mapped venue is not a promise that its website publishes showtimes. |
| `src/screenwatch/data/independent_venues.json` | Curated routing/parser overrides | Special handling for known ticketing platforms and HTML quirks; this is configuration, not a hardware claim. |
| Local SQLite evidence store | Directory, screening-presentation, and room/seat observations | Every observation has a source, URL where available, timestamp, scope, and confidence. Missing data remains unknown. |
| Permanent venue hardware claims | 0 shipped | The system does not turn a static guess file into a fact. Use timestamped observed capabilities and room profiles instead. |

The exact coverage and evidence summary is available from
`GET /v1/analytics/overview`, `GET /v1/evidence/overview`,
`get_data_overview`, `get_venue_evidence`, and every venue detail record. See
[Data trust and provenance](docs/data-trust.md) before treating an observation
as a permanent room fact.

## What works

- Ranked showtime search across AMC, Alamo Drafthouse, Regal, Cinemark, Apple
  Cinemas/C360, and configured independent venues.
- Two-phase ranking: inexpensive showtime ranking first, then real seat-map
  retrieval for the highest-value candidates.
- Certified party-seat optimization: exhaustive proofs on tractable maps,
  bounded robust/Pareto search on large maps, uncertainty-aware fairness,
  relationship and module constraints, and meaningfully different alternatives.
- Exact per-seat grids for AMC, Cinemark, and browser-rendered Regal; exact
  sold counts plus room shape for C360; availability-only surfaces where that
  is all the source publishes.
- Structured presentation matching for IMAX 70mm, standard 70mm, 35mm,
  nitrate, laser, Dolby Cinema, aspect ratio, 3D, captions, subtitles, and
  other attributes.
- Durable watches for new screenings, sold-out-to-available transitions, seat
  returns, newly appearing seat maps, and party-sized groups becoming
  possible.
- A low-cost AMC catalog signal for release radar. Catalog presence is clearly
  labeled as an early signal, not proof that tickets are on sale.
- National source discovery plus venue filtering by city, radius, chain, venue
  type, and explicit venue id; local inventory analytics; provider health;
  source-linked observed room profiles; and durable alert history.
- The same service objects power the browser app, HTTP API, MCP server, and
  scheduler so those surfaces do not drift.

## Start locally

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/). Chromium is
optional; install the browser extra when you want Regal seat enrichment or the
browser-only independent sources.

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev,api]'

# Optional browser-backed sources
uv pip install -e '.[browser]'
playwright install chromium
```

Start the local app:

```bash
screenwatch --port 8787
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). The app is local-only by
default and includes search, venue graph, inventory pulse, watch creation,
alert history, and links to the generated API docs.

The HTTP API can also run without the browser UI:

```bash
uvicorn screenwatch.api.app:default_app --factory --port 8787
```

FastAPI's interactive schema is at
[http://127.0.0.1:8787/docs](http://127.0.0.1:8787/docs).

## First API search

```bash
curl -s http://127.0.0.1:8787/v1/search \
  -H 'content-type: application/json' \
  -d '{
    "work": {"query": "Example Feature"},
    "party_size": 4,
    "location": {"city": "San Francisco", "radius_km": 40},
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
    "include_sold_out": true
  }'
```

Search responses include `coverage`, `complete`, `clipped`, `provider_errors`,
provider timings, source handles, seat-data type, and an explanation for each
option. Use `coverage: "exhaustive"` for a national or long-horizon crawl;
use `coverage: "nearby"` for the fast bounded mode. `coverage: "auto"`
selects exhaustive behavior for an explicitly scoped city, radius, chain, or
venue search and keeps an unscoped query cheap.

In-memory search sessions retain renderable seat maps for 30 minutes and are
bounded to 32 entries. Their lightweight search audit remains in SQLite after
the renderable session expires. `/v1/health` reports live-session capacity and
the bounded seating-optimizer cache, which automatically misses whenever the
seat map or request changes.

## MCP

Run the stdio server from the activated environment:

```bash
claude mcp add screenwatch -- "$PWD/.venv/bin/python" -m screenwatch.mcp.server
```

The main tools are:

| Tool | Use it for |
| --- | --- |
| `resolve_title` | Canonicalize a title and inspect variants/bookability |
| `find_screenings` | Rank options with reasons, tradeoffs, and seat evidence |
| `get_seatmap` | Render a normalized seat map for a search option |
| `get_data_overview` | Inspect source coverage, freshness, evidence provenance, and alert backlog |
| `get_inventory_analytics` | Group observed evidence by chain, venue, type, city, format, or availability |
| `list_venues` / `get_venue` | Explore venues, seat surfaces, observed capabilities, and provenance |
| `get_venue_evidence` | Inspect timestamped room and presentation evidence for one venue |
| `refresh_venues` | Refresh official provider directories into SQLite |
| `create_watch` / `poll_watches` | Create and run durable monitors |
| `get_watch_history` | Audit alert state transitions and delivery status |
| `get_booking_link` | Get the final source URL; this is the hard stop |

Detailed request and response examples live in [API and MCP](docs/api.md) and
[Watches](docs/watches.md).

## Architecture

```text
provider adapters
      ↓
source observations + provenance
      ↓
canonical title / venue / screening identity
      ↓
normalized presentation + seat model
      ↓
phase-A ranking → phase-B seat enrichment → explanations
      ↓
SQLite evidence store
      ↓
HTTP API · MCP · local app · scheduler
```

The important boundary is that an observation is not automatically a fact.
The code keeps source claims, timestamps, confidence, conflicts, provider
limits, and seat-data quality visible all the way to the transports.

## Data coverage and limitations

Current provider behavior, source quirks, seat surfaces, and the difference
between fast and exhaustive coverage are documented in
[Source coverage](docs/source-coverage.md).

The important operating boundaries are:

- A source's presentation label is evidence about that screening. It is not a
  permanent room inventory. Permanent hardware claims are intentionally zero
  until a narrowly scoped source capture and verification workflow is added.
- Independent venue discovery is national and source-linked through
  OpenStreetMap; the curated JSON file only supplies parser and routing
  overrides for known venues.
- `coverage: "exhaustive"` removes Screenwatch's venue/day caps for the
  requested scope and makes national/long-horizon searches possible. Upstream
  outages, bot challenges, missing websites, and provider-side limits remain
  visible in `complete`, `clipped`, and `provider_errors` rather than being
  mistaken for no inventory.
- Alamo exposes reserved-seat availability but no public seat grid or count;
  it cannot produce exact seat choices.
- C360 exposes a sold count and room shape, so contiguous-seat availability is
  estimated rather than confirmed.
- Regal seat enrichment requires Chromium and a browser-rendered public page.
- TMDB identity enrichment requires `TMDB_API_KEY`; without it, the resolver
  uses a lower-confidence local alias catalog.
- Fast nearby mode and source outages are normal operating conditions and are
  surfaced as warnings or clipped scope instead of being hidden.

Nothing in the repository purchases tickets or bypasses a checkout flow.

## Development

```bash
python -m pytest -q
ruff check src tests
node --check src/screenwatch/ui/app.js
python -m compileall -q src
git diff --check
```

See [Development and contribution](docs/development.md) for the test
strategy, fixture policy, provider workflow, and the evidence contract.

## Repository map

```text
src/screenwatch/adapters/       source parsers and link extractors
src/screenwatch/providers/      chain and independent provider seams
src/screenwatch/identity/       title normalization and canonical works
src/screenwatch/seating/        seat geometry, estimates, rendering
src/screenwatch/ranking/        coarse/fine ranking and explanations
src/screenwatch/service/        orchestration, SQLite, watches, observatory
src/screenwatch/api/            FastAPI transport
src/screenwatch/mcp/            MCP transport and schemas
src/screenwatch/ui/             local browser app
src/screenwatch/data/           explicit, provenance-bearing seed/config data
tests/                          offline parser, service, API, and watch tests
docs/                           focused product, API, provenance, and dev docs
```

## License

No license has been declared yet. Treat the repository as “all rights
reserved” until a license is added.
