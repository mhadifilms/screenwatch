# Data trust and provenance

This is the contract for interpreting Screenwatch output. The system stores
what a source said, when it said it, where it was observed, and how broad the
claim is. It does not silently convert a listing into a permanent theater
hardware fact.

## What is in the product

The venue graph is populated by provider discovery and persisted in SQLite.
The configured sources currently include:

- AMC's official national theater sitemap, including theatre id, slug, city,
  state, coordinates, and official URL;
- Regal's national directory payload;
- Cinemark's official sitemap, with coordinates hydrated only from official
  theater pages as they are visited;
- C360 location records and Alamo market schedules;
- OpenStreetMap's `amenity=cinema` directory for independent-cinema discovery;
- a clearly marked curated routing/parser registry for known independent
  ticketing sites.

The counts are intentionally dynamic. Query `/v1/analytics/overview` or
`/v1/evidence/overview` after a refresh rather than copying a stale count into
an analysis.

## Venue hardware is not a seed file

The old seven-row `venue_hardware.json` fixture has been removed from runtime
and from the repository. There are no packaged permanent hardware claims.
That is a deliberate production boundary: a guessed IMAX aspect ratio or a
remembered 70mm projector is not evidence that a particular screening uses it.

Instead, the store records two useful kinds of live evidence:

1. `screening_presentation` — a source reported a format, projection, brand,
   aspect, or accessibility attribute for one screening;
2. `room_observation` — a seat-map or auditorium endpoint returned a room
   identifier, capacity, geometry, and/or occupancy at a timestamp.

The API calls these “observed capabilities” and “observed rooms.” They are
useful for searches, watches, and analysis, but their scope remains visible.
An observation of `IMAX / digital_laser / 1.43` on one screening does not mean
every IMAX showing at that venue is 1.43.

## Observation record

Every evidence row has this shape internally and is summarized publicly:

```json
{
  "kind": "room_observation",
  "subject_key": "imax-1",
  "source": "amc:seat-map",
  "source_url": "https://www.amctheatres.com/...",
  "observed_at": "2026-08-06T20:15:00+00:00",
  "evidence_scope": "screening-seat-map",
  "confidence": 1.0
}
```

`source` identifies the parser or upstream surface. `source_url` is the URL a
person can inspect when the source exposes one. `observed_at` is when the local
process captured the value, not a claim about when the theater installed a
projector. `evidence_scope` prevents a room observation from being mistaken
for a building-wide inventory.

## Evidence levels

| Level | Meaning | Example |
| --- | --- | --- |
| `observed` | A configured source returned the value at a timestamp | A showtime feed says IMAX laser |
| `estimated` | A deterministic result derived from observed inputs | C360 party-fit probability from room shape and sold count |
| `availability` | The source exposes sellable/sold-out state without seats | An Alamo session status |
| `curated` | Human-maintained routing/configuration | An independent venue's ticketing URL |
| `unknown` | No usable evidence is available | No seat surface observed for a venue |

“Unknown” is not “no.” A missing capability means Screenwatch has not observed
one in the configured sources.

## Reading the API

`GET /v1/evidence/overview` reports counts by observation kind, distinct venues,
source count, and first/last observation timestamps. `GET
/v1/venues/{venue_id}/evidence` groups one venue's observations into
source-linked presentation claims and room rollups. The `rooms` entries expose
latest observed availability/capacity plus capacity ranges, source URLs, scope,
and freshness. `GET /v1/venues/{venue_id}` includes the same evidence summary
plus `observed_rooms` on the detailed response.

The compatibility-shaped `hardware` object is intentionally explicit:

```json
{
  "status": "observations-only",
  "permanent_claims": 0,
  "usable_for_inference": false
}
```

The `capabilities` array on a venue is therefore not a technical inventory. It
is a compact list of observed screening presentations. Each item includes its
observation count, sources, URLs, scope, and freshness through the `evidence`
object.

## Coverage rules

- A provider's directory count is not a showtime count.
- A showtime count is not a national census. `coverage: "exhaustive"` removes
  Screenwatch's local venue/day caps, but upstream errors, unmapped websites,
  bot challenges, and missing source surfaces are still carried in `complete`,
  `clipped`, and `provider_errors`.
- A seat count is not a seat grid. C360 can support an estimate; AMC,
  Cinemark, and rendered Regal sources can support a grid when the source
  returns one.
- A room observed once is not proof that every room was observed.
- A missing source URL lowers auditability; it does not make a value verified.
- Repeated observations are retained so freshness and change detection can be
  measured instead of guessed.

The local evidence cube only answers what configured providers actually
observed. It never coerces missing venues, room profiles, or seat maps to zero.

## Adding a future permanent hardware claim

If a future contributor adds a permanent hardware dataset, it must be a
separate, explicit evidence type—not a fallback mixed into directory rows. The
claim must include:

1. a stable venue and room/screen identifier;
2. a narrowly scoped claim, such as “15/70 projector in auditorium 1”;
3. an official source URL or attached capture;
4. capture date and, where available, the source's effective date;
5. a review/expiry policy for equipment that can change.

Until all of those exist, the claim remains `unknown` and live screening data
must stand on its own.
