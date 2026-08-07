# Screenwatch

Screenwatch is a local-first US theater intelligence system: a browser app,
HTTP API, MCP server, and durable notification engine over theater chains and
selected independent cinemas.

It is built to answer a practical question:

> Given a title, a place, a party size, a format preference, and a time window,
> which specific bookable screening is best—and what evidence supports that
> answer?

The system ranks a screening together with the seats that are actually
available when a seat surface exists. It explains tradeoffs, preserves source
and freshness metadata, and stops at a booking link. It never creates a hold,
adds a ticket to a cart, or stores payment information.

## Read this first: current data truth

Screenwatch is useful today, but it is not a complete national theater
registry. The product deliberately distinguishes live observations, estimates,
curated metadata, and unknowns.

| Dataset or surface | Current state | How to interpret it |
| --- | --- | --- |
| `src/screenwatch/data/venue_hardware.json` | 7 rows; **0 verified**; every row is `source: seed-unverified` | Candidate hardware metadata only. It is exposed with provenance and is not used to infer a live screening's missing format geometry. |
| `src/screenwatch/data/independent_venues.json` | 7 curated venues | A transparent starting list, not a census of independent cinemas. |
| Chain and ticketing adapters | Live source reads, bounded by provider caps | A result means “observed in the sources and scope reported by this run,” not “every US theater was checked.” |
| Local SQLite evidence store | Durable observations from searches and watches | Missing data is unknown; it is never silently converted to zero. |

The exact provenance summary is available from `GET /v1/analytics/overview`,
`get_data_overview`, and every venue detail record. See
[Data trust and provenance](docs/data-trust.md) before using hardware or
coverage numbers in an analysis.

## What works

- Ranked showtime search across AMC, Alamo Drafthouse, Regal, Cinemark, Apple
  Cinemas/C360, and configured independent venues.
- Two-phase ranking: inexpensive showtime ranking first, then real seat-map
  retrieval for the highest-value candidates.
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
- Venue filtering by city, radius, chain, venue type, and explicit venue id;
  local inventory analytics; provider health; observed room profiles; and
  durable alert history.
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
    "work": {"query": "Dune: Part Three"},
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

Search responses include `complete`, `clipped`, `provider_errors`, provider
timings, source handles, seat-data type, and an explanation for each option.
If a provider cap or source failure means the system did not inspect
everything in scope, the response says so.

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
| `get_data_overview` | Inspect source coverage, freshness, hardware provenance, and alert backlog |
| `get_inventory_analytics` | Group observed evidence by chain, venue, type, city, format, or availability |
| `list_venues` / `get_venue` | Explore venues, seat surfaces, capabilities, and provenance |
| `refresh_venues` | Discover bounded provider venue metadata into SQLite |
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

Current provider behavior, measured source quirks, seat surfaces, and scope
caps are documented in [Source coverage](docs/source-coverage.md).

The biggest current limitations are:

- The hardware overlay is seven unverified candidate rows, not a nationwide
  screen-by-screen inventory. Unknown is the correct answer for every other
  venue until evidence is added.
- Independent venue coverage is intentionally curated rather than discovered
  from a national registry.
- Alamo exposes reserved-seat availability but no public seat grid or count;
  it cannot produce exact seat choices.
- C360 exposes a sold count and room shape, so contiguous-seat availability is
  estimated rather than confirmed.
- Regal seat enrichment requires Chromium and a browser-rendered public page.
- TMDB identity enrichment requires `TMDB_API_KEY`; without it, the resolver
  uses a lower-confidence local alias catalog.
- Provider caps and source outages are normal operating conditions and are
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
strategy, fixture policy, provider workflow, and the rules for adding verified
metadata.

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
