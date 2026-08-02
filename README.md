# screenwatch

Ranked cinema showtime search, as an **HTTP API and an MCP server**. Every
format, every venue type — chains and independents. It takes you to a
ready-to-book link and stops.

It does not answer "what's playing". It answers *"given who I am, where I am,
how many of us there are, and what I care about — which specific bookable
option should I take, and why."*

The scenario it is built around, which is also the headline acceptance test:

> 9pm, day after launch, four people, near sellout. The 3D show at 12:45am has
> two pairs in adjacent rows; the 11:30pm has second-row seats that don't fit
> four. The later, worse-format one is the right answer, and the system has to
> say so — and say why.

```
1. mon 12:45am · digital laser / 3D · amc-metreon-16 · 4 as 2+2 at C 4+5, D 4+5  [0.85]
   seats: 2+2 in adjacent rows, lined up, C 4+5, D 4+5
   for: seats all 4 of you; good position in the room; 5 km away
   against: you rank 3D below your first choice; party splits 2+2; late start

2. sun 11:30pm · digital laser · amc-metreon-16 · 3 together at B 4+5+6  [0.66]
   for: your top format
   against: only seats 3 of 4; poor seats — near the front

Ranked first on fitting your whole party, seat position, despite being worse on format.
```

## Architecture

```
adapters/     raw reads, two corroborating parsers per source where possible
providers/    per-chain seam: fetch + corroborate + resolve identity
service/defaults.py  the one place the provider list lives — three entry
              points used to build their own and drifted
identity/     canonical film identity across every venue's product ids
seating/      normalized auditoriums, seat quality, group assembly, renderers
ranking/      two-phase scorer + explanation
service/      orchestration, SQLite, watches
api/ mcp/     thin transports over the same service objects
```

### Two-phase ranking

Seat maps cost **one guarded request per showtime** — measured, not assumed —
so you cannot fetch them for every candidate.

* **Phase A** ranks every screening on free data: format fit, showtime,
  lateness, distance, pass coverage, availability.
* **Phase B** buys seat grids for the top `max_seatmap_fetches` only, then
  re-ranks on what you'd actually get: seat quality, and whether your party
  can sit together.

The unit being ranked is an **Option** — a screening *plus a concrete seat
assignment*. Four scattered singles and four seats together are the same
screening record and completely different products.

Venues with no reachable seat data still rank, flagged `seat_data:
unavailable`, scored on neutral seat values rather than sunk.

### Presentation is structured, not ranked

`Projection` × `Brand` × `Attribute`, orthogonal. A single premium-format enum
cannot express a nitrate print at a rep house, and cannot answer "is Dolby
better than IMAX Laser" — that is a preference, not a fact. Ranking is a
user-supplied ordered list of matchers:

```python
Preference([
    PresentationSpec(projection=FILM_70MM_15PERF, brand=IMAX, label="IMAX 70mm"),
    PresentationSpec(brand=IMAX, aspect="1.43",    label="IMAX GT"),
    PresentationSpec(projection=FILM_35MM_NITRATE, label="nitrate"),
    PresentationSpec(projection=FILM_35MM,         label="any 35mm"),
])
```

Chain premium formats and rep-house film prints rank in one list, no
special-casing anywhere in the pipeline.

### Identity

Chains list **products**, not films. AMC's sitemap carried four entries for one
film: `the-odyssey-76238`, `-80679`, `-sensory-friendly-screening-83988`,
`-private-theatre-rental-84080`. All four collapse to one `Work`; the variant
becomes an `Attribute`; the private rental is marked unbookable so it never
surfaces as an option.

Release year comes from the catalogue, never the screening date — a 2026
screening of THE THIRD MAN is a 1949 film. Fuzzy title match alone **never**
merges two works: a wrong merge is silent and poisons everything downstream.

### Seat normalization

Every seat carries `x` (−1..1 lateral) and `y` (0..1 depth), computed from the
venue's own layout. One scoring function and one renderer therefore work for a
500-seat IMAX and a 40-seat microcinema. Lateral position is normalized *per
row*, so a short centred front row stays centred.

## Measured facts about the live sources

| Source | Finding |
|---|---|
| amctheatres.com | Stock TLS → 403. Chrome impersonation → a Queue-it waiting-room interstitial, traversable via the `enqueuetoken` redirect. Sitemaps bypass both. |
| AMC seat maps | `graph.amctheatres.com` accepts unauthenticated POSTs **with introspection enabled**, so the query shape self-documented. `viewer.showtime(id: Int!).seatingLayout` returns a full grid. |
| Alamo Drafthouse | `drafthouse.com/s/mother/v2/schedule/market/<market>` — open JSON, no auth, 84 presentations / 1154 sessions per request. |
| cinemark.com | robots.txt explicitly disallows `/tickets/`, `/ticketseatmap/`, `/shoppingcart`. Sitemap and theatre pages are open; the Cloudflare challenge is intermittent. Coordinates live only inside a Bing static-map URL on the theatre page. |
| regmovies.com | Cloudflare challenge is **intermittent** — a bounded retry clears it. `graph.regmovies.com` is a catch-all rendering the homepage, whose `fullTheatreData` holds all 402 theatres with coordinates. |
| applecinemas.com (C360) | Cloudflare issues its cookies on the landing page, not the API: warm a session there and the JSON endpoints answer unauthenticated. Route shape is `Kiosk/{action}/{locationId}/{date}` — extra path segments silently return the Angular shell with HTTP 200. `totalAvailable` is always 0; `totalSeatsSold` is real, so remaining seats come from capacity minus sold. |
| filmforum.org | Publishes 56 `ScreeningEvent` nodes with `"startDate": ""`. Decorative markup; the adapter raises `IncompleteStructuredData` rather than looking like a dark venue. |

## Seat data: what is actually obtainable

Per-seat occupancy turned out to be reachable on **exactly one** source.
Everywhere else the seat map sits behind the booking flow, and getting it
means creating a hold — a write against someone else's ticketing system,
which this project does not do.

| Source | Seat data | Phase B can |
|---|---|---|
| **AMC** | full grid, per-seat status | pick actual seats, score position and cohesion |
| **Cinemark** | full grid, per-seat status | same |
| **Regal** | full grid via the booking API | same *(implemented from bundle evidence; unverified — see below)* |
| **C360** | exact sold count + auditorium shape | **estimate** whether the party can sit together |
| Alamo | sold-out flag only | availability ranking |
| Independents | none | availability ranking |

Every one of these is a **plain GET**. No login, no cart, and no hold — a hold
is created by *selecting* a seat, which nothing here does.

That would leave phase B useful for one chain and inert for the rest —
worthless precisely at a near-sellout, which is when ranking matters. So
`seating/estimate.py` computes the probability that N adjacent seats exist,
from the free-seat count and the room's row structure:

```
1. sun 9:15pm · digital · Apple Cinemas Brass Mill  [0.95]
   seats: 111 of 111 free — 4 together almost certainly available (~100%)
2. sun 10:00am · digital / 3D · Apple Cinemas Brass Mill  [0.93]
   seats: 150 of 161 free — 4 together almost certainly available (~100%)
```

Two properties the tests pin. It is **biased pessimistic** — real bookings
cluster, so the gaps they leave are more fragmented than a uniform scatter;
being pleasantly surprised is the acceptable error. And an estimate **never
outscores a confirmed grid**: the best possible estimate caps below 1.0, so a
verified contiguous block always wins a tie. A guess must not beat a fact.

## Seat maps

AMC's seat surface is live. The GraphQL schema is public and introspectable,
so the recon was one request rather than a reverse-engineering exercise. Quirks
the adapter has to absorb, each of which cost debugging:

* `id` is `Int!` despite the field returning an `ID`.
* The layout is a padded rectangle; `type: "NotASeat"` cells are aisles and
  walls. Dropping them leaves column gaps, which is exactly how aisles get
  detected.
* Row numbers **skip cross-aisles** — Lincoln Square's IMAX has no rows 5, 11
  or 12 — so depth interpolates over row *values*, not ordinal position.
  Otherwise row 6 lands closer to the screen than it physically is.
* Seat names run right to left: "A15" sits at column 7, "A1" at column 22.
  Geometry uses `column`; `name` is only a label.
* `available` is the authority, not `seatStatus`, which is blank for padding
  and splits real availability across "Available" and "Unblocked".

**A sold-out showing returns no layout at all.** Watching one for returns has
to key off the showtime `status` flipping back, not off seat-level diffs.

## Coverage

| Source | Showtimes | Seats | Notes |
|---|---|---|---|
| **AMC** | ✅ two corroborating parsers | ✅ GraphQL | Queue-it traversal, sitemap tripwire |
| **Alamo Drafthouse** | ✅ 19 markets, 34 cinemas | ✗ not exposed | One open request per market; self-discovers venues and coordinates |
| **Regal** | ✅ 402 theatres nationally | ✗ not in payload | `__NEXT_DATA__` blob; Cloudflare challenge is intermittent, cleared by retry |
| **Cinemark** | ✅ 307 theatres nationally | ✗ **robots.txt disallows** | ASP.NET page; `data-json-model` joined to rendered showtime divs |
| **Independents** | ✅ schema.org **or** Vista ticket links | ✗ | Metrograph: 183 showtimes / 20 dates. Film Forum's markup is decorative and is reported as such |
| **C360 / Apple Cinemas** | ✅ 14 venues | ⚠ **counts + room shape** | Warm the session on the landing page, then an open JSON API |
| Elevent, Agile | ✗ | ✗ | Not started |

Alamo is the cheapest source by a wide margin — one unauthenticated request
returns a whole market (NYC: 1152 sessions), and the payload carries its own
format taxonomy, so `unknown_formats()` verifies the token table against the
API's own vocabulary instead of waiting to meet a surprise in production.

## Use

```bash
uv venv && uv pip install -e '.[dev,api]'
python -m pytest                      # 453 tests, offline
```

MCP server (stdio):

```bash
claude mcp add screenwatch -- /Users/livestream/screenwatch/.venv/bin/python \
    -m screenwatch.mcp.server
```

| Tool | Purpose |
|---|---|
| `resolve_title` | free text → canonical film, variant and bookability |
| `find_screenings` | ranked options with reasons and tradeoffs |
| `get_seatmap` | unicode grid or SVG, recommended seats highlighted |
| `explain_ranking` | pairwise component comparison |
| `create_watch` / `list_watches` / `cancel_watch` / `poll_watches` | monitors |
| `get_booking_link` | final URL — **hard stop** |

Always-on monitors:

```bash
screenwatch-scheduler          # or: python -m screenwatch.service.scheduler
```

HTTP:

```bash
uvicorn screenwatch.api.app:default_app --factory --port 8787
```

## robots.txt is advisory here

robots.txt is a convention for crawlers — software that walks a site on its own
initiative to build an index. This is not that: it fetches the pages a specific
user asked about, at roughly the volume that user would generate by clicking.

So enforcement is **off by default**. `robots.py` records what a crawler-mode
client would have skipped and lets the request through. `RobotsCache(enforce=
True)` restores blocking, and exists for the one genuinely crawler-shaped part
of the system — the scheduler, which polls on a timer with no human in the
loop.

## Independents: three strategies, no per-venue parsers

1. **schema.org `ScreeningEvent`** where it is real.
2. **Vista ticket links** — `visSelectTickets.aspx?cinemacode=&txtSessionId=`.
   Metrograph: 183 showtimes across 20 dates.
3. **Agile WebSales links** — `ticketsearchcriteria.aspx?evtinfo=`. These also
   carry a real sales state and the screen name. The Coolidge: 24 showtimes
   including **"The Odyssey in 70mm" on screen MH1** — the rep-house film-print
   case the whole presentation model exists for.

Vista and Agile between them run a large majority of US art houses, so two
extractors cover the long tail without a line of venue-specific code.

Deliberately structural, not CSS-based: class names differ per venue and change
on redesign, but "the anchor text of a ticket link is the showtime, and the
nearest heading above it is the film" holds because it is how listings are
*shaped*.

One trap worth naming. Every listing page also renders a date **picker**, and
in isolation a picker entry is indistinguishable from a day heading — taking
the nearest preceding date dated the Coolidge's whole schedule to September.
Day groupings are block containers; navigation is anchors and table cells, and
`DATE_CONTAINER` encodes exactly that.

## Watches

`screenwatch-scheduler` runs the always-on loop, with tiered cadence: 45s when
the target date is today or a hit just landed, 5 min when it is imminent,
30 min when distant, and retired once the window has passed. Every sleep is
jittered, and one watch throwing never stops the others.

A watch seeds its seen-set at creation with everything already on sale, so
*"some 70mm already dropped — tell me about NEW Dune 3 70mm IMAX in the Bay
Area"* fires only on genuinely new screenings. Hits persist before delivery,
so a disconnected client or a failed webhook loses nothing.

## What is not built

Stated plainly, because a plausible-looking gap is worse than a named one.

* **C360 / Apple Cinemas.** The domain now serves a Cloudflare Turnstile
  challenge on every path. Reaching it needs a real browser with a persistent
  profile, which is a different mechanism than everything here uses.
* **Cinemark, Elevent, Agile.** Not started; each needs its own recon.
* **Alamo and Regal seat maps.** Both report reserved seating, so layouts
  exist, but neither serves them outside the booking flow. Alamo's
  `/schedule/session/{cinemaId}/{sessionId}` leaks a stack trace confirming
  the route but carries no seats; Regal's ticketing is the part Cloudflare
  guards hardest. Both fall back to availability-only ranking.
* **Regal seat maps are unverified.** `GET {booking_api}/api/GetSeatPlan?
  theatreCode=&sessionId=` was recovered from Regal's own bundle and the
  parser is written against the Vista schema it returns, but Cloudflare
  firewalled this IP off the booking hosts mid-recon (a hard
  `Attention Required`, not a solvable challenge), so no live response has
  been parsed. The provider routes through the browser transport, which is
  what clears a managed challenge when one is present.
* **Alamo seat maps** need the ticket-type step before the picker renders,
  which is the one place a request would start an order. Left alone.
* **Elevent / Agile** not built — Metrograph turned out to be Vista, not
  Elevent, and the Vista link extractor covers it and a large slice of the
  art-house tail instead.
* **TMDB catalogue client.** `WorkResolver` takes an injected `Catalog`; only
  `AliasCatalog` and `NullCatalog` ship. Identity works degraded without it —
  products still group by cleaned title, at low confidence.
* **Scheduler.** `run_due()` exists; nothing calls it on a timer yet.

## Entry points

All three go through `service.defaults.default_service()`, and a test asserts
they do. They previously built their own provider lists and drifted — the
scheduler was a chain behind, so monitors silently never saw it.

```bash
screenwatch-mcp                                     # stdio MCP
uvicorn screenwatch.api.app:default_app --factory   # localhost HTTP
screenwatch-scheduler                               # always-on monitors
```

## Before you rely on this

* `data/venue_hardware.json` is unverified seed data — every row says
  `source: seed-unverified`. Seven venues only.
* Fixtures expire; the suite warns past 30 days. Green tests on a stale
  fixture prove the parsers handle *last month's* site.
* Parsers raise rather than return empty. A truncated sitemap response
  surfaced exactly this during development — `ValueError: sitemap parsed to
  zero movie entries` instead of a silent quiet day. Callers must retry; the
  noise is the feature.
* Ranking weights encode a judgement, not a law. `Weights` is fully
  overridable per search, and a test pins the fact that raising `format_fit`
  flips the headline scenario.
