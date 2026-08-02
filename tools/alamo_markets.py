"""Re-derive Alamo's market list.

Alamo publishes no market index, and the obvious `/markets` paths reject GET,
so the slug list in `adapters/alamo/schedule.py` was found by probing. This
re-runs that probe so the list can be refreshed and, importantly, so a market
that has *stopped* resolving is visible instead of silently disappearing.

    python tools/alamo_markets.py            # probe the known list + guesses
    python tools/alamo_markets.py --known    # verify only the shipped list
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from curl_cffi import requests  # noqa: E402

from screenwatch.adapters.alamo.schedule import KNOWN_MARKETS, SCHEDULE  # noqa: E402

# Slugs worth trying beyond the confirmed list. Alamo's naming is inconsistent
# ("nyc" and "dfw" but "los-angeles" and "san-antonio"), so both styles are
# probed for each city.
GUESSES = (
    "houston", "phoenix", "el-paso", "lubbock", "kansas-city", "new-orleans",
    "washington", "dc", "seattle", "portland", "las-vegas", "nashville",
    "atlanta", "miami", "philadelphia", "baltimore", "detroit", "minneapolis",
    "milwaukee", "columbus", "cleveland", "pittsburgh", "charlotte", "tucson",
    "san-francisco", "bay-area", "brooklyn", "staten-island", "woodbury",
)


def probe(session, slug: str) -> tuple[str, int, int] | None:
    try:
        response = session.get(SCHEDULE.format(market=slug), timeout=15)
        if response.status_code != 200:
            return None
        data = response.json()["data"]
        market = data["market"][0]
        return market["name"], len(market.get("cinemas") or []), len(data.get("sessions") or [])
    except Exception:                                          # noqa: BLE001
        return None


def main() -> int:
    known_only = "--known" in sys.argv
    slugs = list(KNOWN_MARKETS) if known_only else sorted({*KNOWN_MARKETS, *GUESSES})

    session = requests.Session(impersonate="chrome131")
    found, missing = [], []

    for slug in slugs:
        result = probe(session, slug)
        if result is None:
            (missing if slug in KNOWN_MARKETS else []).append(slug)
            continue
        name, cinemas, sessions = result
        found.append(slug)
        new = "" if slug in KNOWN_MARKETS else "  NEW"
        print(f"  {slug:20} {name:30} cinemas={cinemas:<3} sessions={sessions}{new}")

    print(f"\n{len(found)} markets, {len(found)} resolving")
    if missing:
        # A market that used to resolve and no longer does is the signal that
        # matters - it means coverage silently shrank.
        print(f"STOPPED RESOLVING (was in KNOWN_MARKETS): {', '.join(missing)}")
        return 1
    if new_slugs := [s for s in found if s not in KNOWN_MARKETS]:
        print(f"Add to KNOWN_MARKETS: {', '.join(new_slugs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
