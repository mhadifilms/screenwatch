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

## Adding or changing metadata

### Hardware overlay

The packaged hardware file is not a scratchpad for guesses. Every row must
carry:

- a stable `venue_id`;
- a narrowly scoped `screens` claim;
- `source`;
- `verified_at` (or an explicit null while unverified);
- a note that describes uncertainty and scope.

Until a row has a non-seed source and a verification date, it is displayed as
`unverified` and is excluded from presentation refinement and seat-model
calibration. Update the data-quality tests when intentionally promoting a
row. See [Data trust and provenance](data-trust.md).

### Independent venues

`src/screenwatch/data/independent_venues.json` is a curated registry. Keep
ticketing platform and markup fields aligned with the actual listing strategy.
For example, a Vista-backed venue should not retain an old Elevent label in a
second metadata file. The directory merge test exists to catch this class of
drift.

### Provider behavior

Do not return an empty list when a source is blocked, malformed, or incomplete.
Raise or record a named provider error so a caller can distinguish “no
showtimes” from “the source was not readable.” If a provider cap is applied,
record what was skipped in `clipped`.

### Tests

Add parser tests against a fixture, service tests for identity and provenance,
and at least one transport assertion when a new field is public. Test the
negative case for any inference: an unknown or unverified input must remain
unknown rather than becoming a confident-looking claim.

