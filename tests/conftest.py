from __future__ import annotations

import json
import pathlib
import sys
from datetime import UTC, datetime

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# Fixtures are snapshots of a live site. Past this age they stop being
# evidence that the parsers still work and become evidence of what the site
# used to look like. The suite warns rather than fails so CI stays green
# offline, but the warning is the prompt to re-run tools/capture.py.
STALE_AFTER_DAYS = 30


def load(source: str, name: str) -> str:
    return (FIXTURES / source / name).read_text(encoding="utf-8")


def meta(source: str, slug: str) -> dict:
    path = FIXTURES / source / f"{slug}.meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.fixture(scope="session")
def now() -> datetime:
    return datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def amc_showtimes_html() -> str:
    return load("amc", "showtimes-lincoln-square-2026-08-02.html")


@pytest.fixture(scope="session")
def amc_sitemap_movies() -> str:
    return load("amc", "sitemap-movies.xml")


@pytest.fixture(scope="session")
def amc_sitemap_theatres() -> str:
    return load("amc", "sitemap-theatres.xml")


@pytest.fixture(scope="session")
def filmforum_home() -> str:
    return load("independent", "filmforum-home.html")


def pytest_collection_modifyitems(session, config, items):
    """Surface fixture staleness once per run."""
    for slug in ("showtimes-lincoln-square-2026-08-02",):
        info = meta("amc", slug)
        if not (captured := info.get("captured_at")):
            continue
        age = (datetime.now(UTC) - datetime.fromisoformat(captured)).days
        if age > STALE_AFTER_DAYS:
            config.issue_config_time_warning(
                UserWarning(
                    f"fixture {slug} is {age}d old (>{STALE_AFTER_DAYS}d). "
                    "Green tests no longer prove the live parsers work. "
                    "Run: python tools/capture.py"
                ),
                stacklevel=1,
            )
