# Development and contribution

## Environment

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev,api]'
```

Install the optional browser extra and Chromium only when working on the
browser-backed sources:

```bash
uv pip install -e '.[browser]'
playwright install chromium
```

`TMDB_API_KEY` enables the higher-confidence catalog resolver. The project
still runs without it using the local alias catalog.

## Validation

Run the full offline suite before handing off a change:

```bash
python -m pytest -q
ruff check src tests
node --check src/screenwatch/ui/app.js
python -m compileall -q src
git diff --check
```

For seating optimizer work, also run the reproducible oracle and runtime
benchmark:

```bash
.venv/bin/python tools/benchmark_seating_optimizer.py
```

It compares the structured large-room search with exhaustive enumeration on
seeded random small rooms, then reports cold runtime, selected shapes, robust
utility, and certificate method for parties from 1 through 20. A benchmark
with nonzero oracle regret is not automatically a bug—the algorithms optimize
an NP-hard model—but it must be investigated and recorded rather than hidden.

Fixtures are intentionally checked in so parser and service tests do not
depend on a live cinema site. A fixture can prove that a parser handles the
captured source; it cannot prove that the source still looks that way today.
When a fixture is stale, update the capture and its provenance rather than
loosening the parser until it accepts an empty response.

## Project boundaries

The architecture has one default service wiring in
`src/screenwatch/service/defaults.py`. The HTTP API, MCP server, local app, and
scheduler should all use that wiring. A new provider belongs in
`src/screenwatch/providers/` and must expose the same normalized `Screening`
and `Auditorium` contracts.

The useful layering is:

```text
adapters → providers → identity/presentation/seating → ranking
                                      ↓
                              service + SQLite
                                      ↓
                         API / MCP / UI / scheduler
```

Adapters parse source-specific markup. Providers decide how to fetch and scope
it. The service layer owns identity, persistence, watch state, and explanations.
Transports should remain thin.

Seat acquisition is replayable and does not accept a ranking `Option`. Every
provider builds a durable `SeatProbe` during showtime discovery and implements
`fetch(probe, transport) -> SeatCapture`. The capture carries the normalized
room plus the unmodified source body. Search may reconcile a matching stored
static topology with the current response; it must never reuse a past
availability state as if it were live.

`SearchService` remains title-led and budgeted. `HarvestService` is venue- or
city-led, enumerates every title over its requested horizon, persists every
probe before attempting it, and is the only path that claims exhaustive
auditorium collection.

## Adding or changing metadata

### Venue and room evidence

Do not add a static hardware guess to make a venue look complete. Provider
discovery belongs in an adapter/provider and must persist the source URL,
observed timestamp, and directory scope. A screening format belongs in a
`screening_presentation` observation. A seat map or auditorium shape belongs in
a `room_observation`. Keep the evidence scoped to the source event; do not
promote it to a building-wide hardware fact without a separate, reviewable
claim workflow. See [Data trust and provenance](data-trust.md).

Tests for new evidence must cover source identity, source URL, freshness, scope,
and the negative case where missing evidence remains unknown.

### Independent venues

`src/screenwatch/data/independent_venues.json` is a routing/parser override
registry, not the independent-venue census. National discovery comes from the
OpenStreetMap adapter. Keep ticketing platform and markup fields aligned with
the actual listing strategy. For example, a Vista-backed venue should not
retain an old Elevent label in a second metadata file. The directory merge
test exists to catch this class of drift.

When a provider adds a fast-path cap, it must also implement the
`SearchSpec.exhaustive` path so a caller can request the complete configured
scope. A source-side limit or failure must remain visible in provider stats;
removing a local cap must not turn an upstream refusal into an empty result.

### Provider behavior

Do not return an empty list when a source is blocked, malformed, or incomplete.
Raise or record a named provider error so a caller can distinguish “no
showtimes” from “the source was not readable.” If a provider cap is applied,
record what was skipped in `clipped`.

Seat failures use one of the concrete collector categories in
`seating/model.py`. General admission, throttling, browser requirements,
blocks, parser drift, ambiguous cross-provider matches, and missing durable
context must not be collapsed into a single retry policy.

For live changes, configure and run the self-hosted canaries described in
[Live provider canaries](live-canaries.md). Never update a parser from a
screenshot alone: retain the raw response and turn it into a narrow fixture
with provenance.

### Tests

Add parser tests against a fixture, service tests for identity and provenance,
and at least one transport assertion when a new field is public. Test the
negative case for any inference: an unknown or unverified input must remain
unknown rather than becoming a confident-looking claim.
