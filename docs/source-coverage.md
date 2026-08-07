# Source coverage

Coverage is a set of source behaviors, not a claim that every upstream surface
is always reachable. Screenwatch has two local coverage modes: `nearby` is a
fast bounded search, while `exhaustive` removes Screenwatch's venue/day caps
for the requested scope. Bot protection, markup drift, missing websites,
upstream limits, and source failures remain part of the result and are
reported to callers.

## Current providers

| Provider | Directory discovery | Showtimes | Seat surface | Important boundary |
| --- | --- | --- | --- | --- |
| AMC | Official national theatre sitemap with ids, slugs, geography, and URLs | Sitemap and showtime surfaces | Exact GraphQL grid | Queue/interstitial traversal and source drift are guarded; seat maps are a read-only request |
| Alamo Drafthouse | Open market schedule JSON for configured markets | Open market schedule JSON | Availability only | Source exposes sellable/sold-out state but no public seat count or grid |
| Regal | Official national directory payload | Theatre/showtime pages | Exact browser-rendered grid | Chromium is required for seat enrichment; `nearby` caps reads, `exhaustive` traverses the requested directory scope |
| Cinemark | Official sitemap; page coordinates hydrate lazily | Sitemap and theatre pages | Exact seat grid | Seat page may require Chromium to clear a challenge; ticket routes are not guessed |
| Apple Cinemas / C360 | Official locations endpoint | Open JSON after session warm-up | Estimated | Sold count and room shape are available; contiguous seats remain an estimate |
| Independents | [OpenStreetMap `amenity=cinema` records](https://wiki.openstreetmap.org/wiki/Tag%3Aamenity%3Dcinema) queried through [Overpass](https://wiki.openstreetmap.org/wiki/Overpass_API/Language_Guide), plus curated routing overrides | Schema.org, Vista links, Agile links, or mapped own-site listings | Availability or unknown | OSM establishes a source-linked venue record; a venue without a mapped website remains directory-only |

Directory discovery is persisted separately from showtime search. A national
directory row proves that the provider reported a venue; it does not prove that
the venue has inventory in the requested date window. `GET /v1/venues/refresh`
and `refresh_venues` expose per-provider discovery counts, errors, clipping,
and elapsed time.

## What “complete” means

Search results include:

- `complete`: false when a provider errored or clipped its configured scope;
- `clipped`: human-readable descriptions of venues or days not read;
- `provider_errors`: named failures;
- `provider_stats`: per-provider counts and timings.

`complete: true` means the configured providers reported no error or clipping
for that run. In `exhaustive` mode it means Screenwatch completed its requested
source traversal; it still does not mean every US theater or every ticketing
surface was available to the internet at that moment.

## Choosing coverage

| Mode | Behavior | Best for |
| --- | --- | --- |
| `nearby` | Uses provider fast-path caps and cached/local geography | Interactive previews and low-cost polling |
| `auto` | Exhaustive for a city, radius, chain, venue type, or explicit venue scope; nearby for an unscoped request | Normal API, MCP, app, and watch requests |
| `exhaustive` | Removes Screenwatch venue/day caps and asks every configured source for the requested scope | Nationwide inventories, long-horizon release watches, and audits |

The source can still decline a request, require Chromium, expose no website, or
return incomplete markup. Those states are represented as source errors or
unknown observations, never as fabricated empty inventory.

## Measured source notes

- AMC sitemaps are useful when the main site is blocked by Queue-it; the seat
  surface is a public GraphQL read with the required query shape.
- Alamo's market endpoint carries a rich presentation vocabulary and many
  sessions in one request, but no seat grid.
- Cinemark's robots policy and ticket routes mean the adapter uses the theatre
  page's own seat URL rather than constructing a speculative URL.
- Regal's useful seat data is on the rendered public showtime page, not the
  blocked raw API route.
- C360's `totalAvailable` is not reliable in the observed payloads; the
  estimate uses observed sold count and room shape instead.
- Film Forum's `ScreeningEvent` markup is structurally valid but has empty
  `startDate` values, so it is surfaced as incomplete rather than silently
  interpreted as a dark venue.
- Independent sources can be behind browser interstitials or use ticket links
  whose structure carries more reliable date and screen information than CSS
  classes.

## Seat-data semantics

| Surface | Screenwatch can say |
| --- | --- |
| Confirmed grid | Which seats are currently available and how a party can sit |
| Exact count + shape | How likely a contiguous party is, conservatively estimated |
| Availability state | Sellable or sold out; no exact seat recommendation |
| No surface | Unknown; ranking falls back without inventing seats |

A confirmed grid always outranks an estimate. The system stops at a booking URL
and never selects a seat or creates a hold.
