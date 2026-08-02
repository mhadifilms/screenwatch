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

| Source | Seat data | Phase B can |
|---|---|---|
| **AMC** | full grid, per-seat status | pick actual seats, score position and cohesion |
| **Cinemark** | full grid, per-seat status | same |
| **C360** | exact sold count + auditorium shape | **estimate** whether the party can sit together |
| Regal | none reachable | availability ranking *(parser written, no live response — see below)* |
| Alamo | sold-out flag only — no count exists | availability ranking |
| Independents | none | availability ranking |

Nothing here logs in, adds to a cart, or creates a hold — a hold is created by
*selecting* a seat, which no code path does. AMC and C360 answer a plain GET.
Cinemark's seat page is now behind a Cloudflare challenge, so that one fetch
goes through the browser transport to clear it; it is still a read of a page
any visitor sees, and still nothing but a GET.

Cinemark's seat URL has to be **the one the theatre page wrote**, not one
rebuilt from the theater and showtime ids. Its link carries four parameters;
the two-parameter form is answered with a redirect to the homepage, which
parses to zero seats and is indistinguishable from a sellout.

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
| **Regal** | ✅ 402 theatres nationally | ✗ no reachable surface | `__NEXT_DATA__` blob; Cloudflare challenge is intermittent, cleared by retry |
| **Cinemark** | ✅ 307 theatres nationally | ✅ full grid | ASP.NET page; `data-json-model` joined to rendered showtime divs. Seat page needs the browser to clear a challenge |
| **Independents** | ✅ schema.org, Vista, Agile **or** own-site links | ✗ | 393 showtimes across IFC / Metrograph / Roxie / Coolidge / Music Box. Film Forum's markup is decorative and is reported as such |
| **C360 / Apple Cinemas** | ✅ 14 venues | ⚠ **counts + room shape** | Warm the session on the landing page, then an open JSON API |
| Elevent | ✗ | ✗ | Not started — Metrograph turned out to be Vista, which covers that tail instead |

Alamo is the cheapest source by a wide margin — one unauthenticated request
returns a whole market (NYC: 1152 sessions), and the payload carries its own
format taxonomy, so `unknown_formats()` verifies the token table against the
API's own vocabulary instead of waiting to meet a surprise in production.

## Use

```bash
uv venv && uv pip install -e '.[dev,api]'
python -m pytest                      # 566 tests, offline
ruff check src tests                  # ruleset pinned in pyproject.toml
```

MCP server (stdio):

```bash
claude mcp add screenwatch -- "$PWD/.venv/bin/python" -m screenwatch.mcp.server
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
   case the whole presentation model exists for. IFC Center: 140 more.

4. **Own-site listings** — venues on no shared platform at all. What they
   still share is a link whose text is a time. Roxie: 34. Music Box: 12.

**393 showtimes across five art houses, no venue-specific code.**

Two rules keep the generic case safe, and the first alone does not. The time
must be **a link**, because page copy is full of times and matching bare text
would drag all of it in. But links carry prose too: `<a href="/visit">Box
office open 7:00 PM daily</a>` passed the link rule and was emitted as a
screening of whatever film sat above it, with `/visit` as the booking URL. So
the link's text must also be *essentially just the time* — a closed whitelist
allows the call-to-action words that really do appear beside one ("11:00am BUY
TICKETS") and nothing else.

Music Box sits behind a Sucuri interstitial that serves 1.3KB of obfuscated
JavaScript to a plain client, so it is marked `fetch: browser` in the config
and read through Chromium. It also puts the day in a `visually-hidden` span
beside each time — accessibility markup again, and the most reliable date
signal on the page for the same reason AMC's aria chains are.

Getting there meant generalising twice. Two Agile venues ship two different
shapes: the Coolidge wraps each link in a `sales-state--` div with the time in
a nested span; IFC emits a bare anchor whose own text is the time, under a
plain `<h3>` with no title class, grouped by a written "Sun Aug 2" heading
rather than an ISO attribute. Requiring the first shape found all 140 of IFC's
links and threw every one away.

Deliberately structural, not CSS-based: class names differ per venue and change
on redesign, but "the anchor text of a ticket link is the showtime, and the
nearest heading above it is the film" holds because it is how listings are
*shaped*.

Dates are the awkward part, and three signals all mean the same thing: an ISO
date on a block container, a *written* day on one (`<div id="day_Sun_Aug_2">`,
Metrograph's only signal), and a heading naming the day. The closest one above
the link wins, because "which day is this listed under" is a question about
proximity rather than markup style; ISO breaks ties, being the only form that
cannot be ambiguous about the year.

Two traps, both of which produced silently wrong dates rather than errors.
Every listing page also renders a date **picker**, and in isolation a picker
entry is indistinguishable from a day heading — taking the nearest preceding
date dated the Coolidge's whole schedule to September. Day groupings are block
containers; navigation is anchors and table cells, and the patterns encode
exactly that. And venues break the day number out for styling —
`<h5>Sun Aug <span class="day-number">2</span></h5>` — so a contiguous-text
pattern reads "Sun Aug" and discards it. Inner markup is stripped first.

The mirror image of that bites the *title* lookup: those same day headings sit
between a film and its showtime links, which is exactly where a proximity
search looks. Headings that are page furniture or day labels are skipped, or
Metrograph's 114 films collapse to seven dates wearing film names.

All of this lives in `adapters/listing_common.py`, and it lives there because
it did not used to. Vista kept private copies of the date, time and title
helpers; the copies drifted, and deleting the body of the shared
`nearest_date_before` passed the entire suite. Consolidating the three
extractors onto one implementation is what surfaced both Metrograph faults.

## Time zones

Every relative search window is anchored on **the venue's** date, not UTC's.
5pm in San Francisco is already tomorrow in UTC, so a UTC anchor started the
window a day late for the last hours of a venue's evening and stamped undated
showtimes a day forward — a "tonight" search returned nothing at exactly the
hour someone would run it. Alamo anchors per cinema rather than per market,
since a market can straddle zones.

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

* **Regal seat maps.** `GET {booking_api}/api/GetSeatPlan?theatreCode=&
  sessionId=` was recovered from Regal's own bundle, and the parser is written
  against the Vista schema that endpoint returns, but **no live response has
  ever been parsed**.

  The obstacle moved while this was being built, which is worth recording
  because the earlier diagnosis was wrong. It used to be a Cloudflare 403 on
  `/api/*`, and was described here as a path rule. It is not one now: the same
  URL returns **200 with the 784KB Next.js app shell**. Every path on
  `webbooking.regmovies.com` does — it is client-side routing, so an unknown
  route is indistinguishable from a known one from outside, and the endpoint
  cannot be confirmed by probing. The parser rejects HTML rather than
  pretending, and Regal falls back to availability-only ranking.

  Bundle recon to find the real route needs a browser session on the booking
  flow, and plain Playwright is Cloudflare-blocked on regmovies (curl_cffi's
  TLS impersonation is what works for the showtime pages, and it cannot run
  JavaScript). That is the open thread.
* **Regal booking links go to the theatre page, not the showing.** There is no
  per-performance page — `/showtimes/{performance_id}` 404s — so the deeplink
  is the dated theatre page, which lists the showing among that day's others.
  One click further from checkout than the other chains.
* **Alamo has no seat or count surface.** It reports reserved seating, so
  layouts exist, but nothing serves them outside the booking flow.
* **Alamo's absence of a seat surface is measured, not assumed.** Probed five
  ways:
  `/session/{id}/seats` (wants a cinema id), `/schedule/session/{cinemaId}/
  {sessionId}` (works, carries no seats), the `/tickets/{slug}/{id}` URL
  (redirects to the theatre page), and `/schedule/venue/{slug}` (same fields
  as the market feed). Availability is `ONSALE` / `SOLDOUT` and nothing finer,
  so there is not even a count to estimate from.
* **Elevent** not built — Metrograph turned out to be Vista, not Elevent, and
  the Vista link extractor covers it and a large slice of the art-house tail
  instead. Agile *is* built; it is what reads the Coolidge and IFC.
* **TMDB catalogue needs a key.** `TmdbCatalog.from_env()` ships and is wired
  into `default_resolver`, but without `TMDB_API_KEY` it degrades to
  `AliasCatalog`: products still group by cleaned title, at lower confidence.
  Resolutions persist to SQLite either way, so a restart does not re-ask.

## Entry points

All three go through `service.defaults.default_service()`, and a test asserts
they do. They previously built their own provider lists and drifted — the
scheduler was a chain behind, so monitors silently never saw it.

```bash
screenwatch-mcp                                     # stdio MCP
uvicorn screenwatch.api.app:default_app --factory   # localhost HTTP
screenwatch-scheduler                               # always-on monitors
```

## Scope caps are reported

Providers cap how many venues and days they read. The caps are real — a
seven-day sweep of 402 Regal theatres is thousands of page loads — but a
silent cap is a lie: a search that read three of eleven nearby theatres and
found nothing reports exactly what a search that read all eleven and found
nothing reports.

So `SearchResult` carries `clipped` and a `complete` flag, and both surface on
the API and MCP responses:

```
complete=False
  clipped: regal: read 4 of 12 matching venues; skipped Regal Battery Park,
           Regal Secaucus Showplace, Regal Concourse, Regal Atlas Park and 4 more
```

The independent provider is uncapped by default: its venue list is curated by
hand rather than crawled, so every entry is one someone asked for.

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
