"""Capture raw fixtures from live sources. Run manually; never in CI.

Writes to tests/fixtures/<source>/<slug>.<ext> plus a .meta.json recording
when and from what URL it came, so replay tests can report fixture age.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from screenwatch.transport import Transport  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

TARGETS = [
    ("amc", "sitemap-index", "https://www.amctheatres.com/sitemap.xml", "xml"),
    ("amc", "sitemap-movies", "https://www.amctheatres.com/sitemaps/sitemap-movies.xml", "xml"),
    ("amc", "sitemap-theatres", "https://www.amctheatres.com/sitemaps/sitemap-theatres.xml", "xml"),
    (
        "amc",
        "theatre-lincoln-square",
        "https://www.amctheatres.com/movie-theatres/new-york-city/amc-lincoln-square-13",
        "html",
    ),
    (
        "amc",
        "theatre-metreon",
        "https://www.amctheatres.com/movie-theatres/san-francisco/amc-metreon-16",
        "html",
    ),
]


def capture(source: str, slug: str, url: str, ext: str, transport: Transport) -> None:
    out_dir = FIXTURES / source
    out_dir.mkdir(parents=True, exist_ok=True)
    resp = transport.get(url)
    (out_dir / f"{slug}.{ext}").write_text(resp.text, encoding="utf-8")
    (out_dir / f"{slug}.meta.json").write_text(
        json.dumps(
            {
                "url": url,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "status": resp.status_code,
                "bytes": len(resp.text),
                "queue_traversed": resp.queue_traversed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  {slug:28} {resp.status_code} {len(resp.text):>9,}b queue={resp.queue_traversed}")


def main() -> None:
    wanted = sys.argv[1:]
    transport = Transport()
    print("capturing fixtures...")
    for source, slug, url, ext in TARGETS:
        if wanted and slug not in wanted:
            continue
        try:
            capture(source, slug, url, ext, transport)
        except Exception as exc:  # noqa: BLE001 - capture tool, report and continue
            print(f"  {slug:28} FAILED {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
