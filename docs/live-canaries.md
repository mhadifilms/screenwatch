# Live provider canaries

Offline fixtures protect parser behavior. The scheduled live workflow protects
the current read paths and runs only on a self-hosted runner labeled
`screenwatch-live`, avoiding shared data-center IPs.

Set the repository variable `SCREENWATCH_CANARY_CONFIG` to JSON with current,
explicit venue targets. Use one venue each for AMC, Cinemark, Regal/Fandango,
Vista, and Agile. A target that should yield an exact map uses `expect: "map"`:

```json
{
  "days": 7,
  "targets": [
    {
      "name": "amc",
      "venue_id": "amc-metreon-16",
      "expect": "map"
    },
    {
      "name": "regal-fandango",
      "venue_id": "replace-with-current-regal-id",
      "expect": "map",
      "allowed_capture_sources": ["regal", "fandango"]
    },
    {
      "name": "vista",
      "venue_id": "replace-with-current-vista-venue-id",
      "expect": "map"
    }
  ]
}
```

`typed_failure` is available only for an explicitly accepted source boundary,
such as a general-admission venue that permanently has no map. Do not use it
to make a broken exact-map canary green.

Each run retains the SQLite evidence database, raw response bodies, structured
failure records, redacted response headers, browser screenshot, page body, and
a redacted HAR when Chromium was involved. The browser profile is temporary
and is never uploaded. The workflow does not select seats, create holds, sign
in, or enter checkout.
