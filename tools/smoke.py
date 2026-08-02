"""Live end-to-end check: fetch -> parsers -> resolver -> ranked report.

Not a test. Tests replay fixtures; this proves the fixtures still describe
reality. Run it before an on-sale you care about.

    python tools/smoke.py                       # default chain venue + Film Forum
    python tools/smoke.py amc-metreon-16 san-francisco 2026-08-02

Exits non-zero if any fact needs review, so it works as a cron canary.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from screenwatch.adapters.amc.showtimes import AmcShowtimesDom, AmcShowtimesRsc  # noqa: E402
from screenwatch.adapters.amc.sitemap import AmcSitemap, Tripwire  # noqa: E402
from screenwatch.adapters.base import ParseError  # noqa: E402
from screenwatch.adapters.generic.jsonld import (  # noqa: E402
    IncompleteStructuredData,
    JsonLdScreenings,
)
from screenwatch.models import Brand, Preference, PresentationSpec, Projection  # noqa: E402
from screenwatch.resolver import Resolver  # noqa: E402
from screenwatch.transport import Transport  # noqa: E402

# What this run is looking for. Ranking is the user's, not the library's -
# note it spans a chain premium format and a rep-house film print with no
# special-casing anywhere in the pipeline.
WANTED = Preference([
    PresentationSpec(projection=Projection.FILM_70MM_15PERF, brand=Brand.IMAX,
                     label="IMAX 70mm"),
    PresentationSpec(brand=Brand.IMAX, aspect="1.43", label="IMAX GT (1.43)"),
    PresentationSpec(projection=Projection.FILM_35MM_NITRATE, label="nitrate print"),
    PresentationSpec(projection=Projection.FILM_70MM, label="70mm"),
    PresentationSpec(projection=Projection.FILM_35MM, label="35mm"),
    PresentationSpec(brand=Brand.DOLBY_CINEMA, label="Dolby Cinema"),
])


def chain_venue(transport: Transport, venue_id: str, market: str, date: str, now):
    print(f"\ntier 1  chain venue {venue_id} {date}")
    rsc_adapter = AmcShowtimesRsc()
    html = rsc_adapter.fetch(transport, venue_id=venue_id, market=market, date=date)
    print(f"        fetched {len(html):,}b")

    rsc = rsc_adapter.parse(html, observed_at=now, strict=False)
    dom = AmcShowtimesDom().parse(html, venue_id=venue_id, observed_at=now, strict=False)
    print(f"        rsc={len(rsc)} dom={len(dom)}")
    return rsc + dom


def independent_venue(transport: Transport, venue_id: str, url: str, now):
    print(f"\ntier 0  independent {venue_id} ({url})")
    adapter = JsonLdScreenings(venue_id, url=url)
    try:
        obs = adapter.parse(adapter.fetch(transport), observed_at=now, strict=False)
        print(f"        {len(obs)} screenings from schema.org markup")
        return obs
    except IncompleteStructuredData as exc:
        print(f"        INCOMPLETE: {exc.found} node(s), missing {exc.missing}")
        print("        -> needs a per-venue HTML fallback; NOT a dark venue")
        return []
    except ParseError as exc:
        print(f"        no usable markup: {exc}")
        return []


def main() -> int:
    venue_id = sys.argv[1] if len(sys.argv) > 1 else "amc-lincoln-square-13"
    market = sys.argv[2] if len(sys.argv) > 2 else "new-york-city"
    date = sys.argv[3] if len(sys.argv) > 3 else datetime.now().strftime("%Y-%m-%d")

    now = datetime.now(timezone.utc)
    transport = Transport()

    print("tier 0  sitemap tripwire")
    sitemap = AmcSitemap()
    entries = sitemap.parse(sitemap.fetch(transport, which="movies"))
    fired = Tripwire(title_contains="The Odyssey").check(entries, now=now)
    print(f"        {len(entries):,} movie entries; {len(fired) or 'no'} change(s)")

    observations = chain_venue(transport, venue_id, market, date, now)
    observations += independent_venue(
        transport, "filmforum", "https://filmforum.org/", now
    )

    facts = Resolver().resolve(observations, now=now)
    review = [f for f in facts if f.needs_review]
    print(
        f"\n{len(facts)} facts, {sum(f.corroborated for f in facts)} corroborated, "
        f"{len(review)} need review"
    )

    ranked = sorted(
        ((WANTED.rank(f.presentation), f) for f in facts),
        key=lambda p: (p[0] if p[0] is not None else 99, p[1].key.starts_at_utc),
    )
    matches = [(r, f) for r, f in ranked if r is not None]

    print(f"\nwanted presentations ({len(matches)} of {len(facts)}):")
    for rank, f in matches:
        label = WANTED.ranked[rank].label
        flag = "  !!" if f.needs_review else ""
        print(
            f"  #{rank} {label:16} {f.key.starts_at_utc:%Y-%m-%d %H:%MZ} "
            f"{f.availability.value:12} {f.presentation.describe():34} "
            f"{f.key.venue_id}{flag}"
        )

    for f in review:
        print(f"\nREVIEW {f.key}: agreement={f.agreement:.2f} conflicts={f.conflicts}")

    return 1 if review else 0


if __name__ == "__main__":
    raise SystemExit(main())
