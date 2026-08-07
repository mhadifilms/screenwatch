# Data trust and provenance

This document is the contract for interpreting Screenwatch output. The goal
is not to make every number look complete; it is to make the evidence level of
every number legible.

## Current audit

Audited on 2026-08-06:

| Check | Result | Risk |
| --- | ---: | --- |
| Hardware overlay records | 7 | Not national coverage |
| Rows with `source: seed-unverified` | 7 / 7 | Candidate metadata, not evidence |
| Rows with a non-null `verified_at` | 0 / 7 | No row is eligible for hardware inference |
| Hardware rows used to infer a missing live aspect | 0 | Runtime boundary is enforced |
| Independent venue registry entries | 7 | Curated list, not a census |

The file is packaged at
`src/screenwatch/data/venue_hardware.json`. There is no root-level
`data/venue_hardware.json` in this repository. The file's `source` and
`verified_at` fields are intentional and should not be “cleaned up” by
replacing them with a vague `hardware` label.

## What the seed file means

The seven rows are hypotheses about notable screens, formats, coordinates, and
venue metadata. They are useful as a work queue and as a place to preserve
candidate knowledge, but they are not a verified hardware database.

For an unverified row, Screenwatch may:

- show the candidate record in venue detail so the gap is visible;
- retain it as a possible future enrichment target;
- use the approximate directory information as a clearly labeled fallback
  while a provider has not yet supplied fresher metadata.

Screenwatch must not:

- fill a live screening's missing aspect ratio from that row;
- infer that a listing is 70mm, IMAX GT, Dolby, or any other format merely
  because the venue owns a candidate-capability row;
- treat the seven rows as evidence that other venues do not have that format;
- report the seven rows as national theater or room coverage.

The runtime exposes this boundary through `hardware_provenance()` and the
public `hardware` object. A current seed response looks like:

```json
{
  "status": "unverified",
  "source": "seed-unverified",
  "verified_at": null,
  "usable_for_inference": false,
  "recorded": true
}
```

The aggregate is included in `GET /v1/analytics/overview` and
`get_data_overview`:

```json
{
  "path": "src/screenwatch/data/venue_hardware.json",
  "status": "seed-unverified",
  "records": 7,
  "verified_records": 0,
  "unverified_records": 7
}
```

## Evidence levels

| Level | Meaning | Example |
| --- | --- | --- |
| `observed` | A configured source returned this value at a timestamp | A provider returned a showtime and its ticket URL |
| `verified` | Curated metadata has a named source and verification timestamp | A venue's official technical page confirms a room profile |
| `estimated` | A deterministic estimate is derived from observed values | C360 contiguous-seat probability from sold count and room shape |
| `availability` | The source exposes a sellable/sold-out state but no seat detail | Alamo session state |
| `unverified` | Candidate metadata has not passed the verification contract | The seven hardware rows |
| `unknown` | No evidence is available | A venue with no observed seat surface |

“Unknown” is not “no.” A missing IMAX capability means Screenwatch has not
verified one, not that the theater lacks IMAX.

## Verification workflow

To promote a hardware row, capture evidence that is specific to the venue and
the claim. A useful verification entry should include:

1. the exact venue and room/screen identifier;
2. the claim's scope—for example, “15/70 projector in auditorium 1,” not
   merely “this chain has IMAX”;
3. a named source URL or an attached source capture;
4. the date checked and, where relevant, the date the source says the hardware
   was installed or changed;
5. what remains unknown or could have changed.

Then update the row's `source` to a meaningful source label, set
`verified_at` to an ISO date, and add the evidence note. Do not mark a row
verified because a chain's marketing label happens to match the candidate
format. “IMAX with Laser” does not establish 1.43:1, and a venue's ownership
of a 70mm projector does not prove that a specific showtime is a 70mm print.

The runtime rule is deliberately mechanical:

```python
usable_for_inference = bool(verified_at) and source not in {
    None, "", "seed-unverified"
}
```

This makes verification auditable and prevents a future refactor from turning
an old seed into an invisible authority.

## Other coverage boundaries

The independent registry at
`src/screenwatch/data/independent_venues.json` is also curated. Its job is to
make selected art houses visible to the generic listing adapters; it is not a
claim that the US independent market has been enumerated.

The local evidence cube reports only what configured providers actually
observed. Seat totals use the latest snapshot for a known screening. Missing
seat maps, missing venues, and provider clipping remain missing or are
reported as warnings; they are not coerced to zero.

