"""schema.org ScreeningEvent - the venue-agnostic adapter.

This is the long-tail play. Independents have no chain API and no shared
backend, but a large fraction of them publish schema.org markup because SEO
demands it, and nobody bot-protects their own SEO. One adapter, many venues,
no reverse engineering.

Two things make real-world JSON-LD harder than the spec suggests:

*Events hide inside wrappers.* Film Forum nests them
CollectionPage -> mainEntity(ItemList) -> itemListElement[] -> item, so a
top-level `@type` check finds nothing. We walk the whole graph, including
`@graph`.

*Structured data is often decorative.* Verified live on filmforum.org
2026-08-02: 7 ScreeningEvent records, every one with `"startDate": ""`. The
markup exists to satisfy a validator, not to be read. An adapter that
returned an empty list there would look exactly like a venue with no
screenings, so incomplete markup raises IncompleteStructuredData naming what
was missing - a venue that needs an HTML fallback, not a venue that is dark.

Format comes from the event title via free-text classification, because that
is where rep houses put it ("THE THIRD MAN in 35mm").
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterator

from ...models import Availability, FactKey, Observation, Presentation
from ...presentation import classify_text
from ...transport import Transport
from ..base import ParseError

_LD_BLOCK = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_SCREENING_TYPES = {"ScreeningEvent", "Event", "TheaterEvent"}
_SLUG = re.compile(r"[^a-z0-9]+")
# Apostrophes join a word rather than separating it: "Sherman's" must slug to
# "shermans", not "sherman-s", or the same film keys differently across
# sources that punctuate differently.
_APOSTROPHE = re.compile(r"['‘’ʼ]")


class IncompleteStructuredData(ParseError):
    """Markup found, but not usable as showtimes.

    Deliberately distinct from "no markup" and from "no screenings". This is
    the common independent-cinema case and it needs a per-venue HTML fallback
    rather than a retry.
    """

    def __init__(self, message: str, *, found: int, missing: str) -> None:
        super().__init__(message)
        self.found = found
        self.missing = missing


def _walk(node, seen: set[int] | None = None) -> Iterator[dict]:
    """Yield every dict in a JSON-LD graph, following @graph and nesting."""
    seen = seen if seen is not None else set()
    if isinstance(node, dict):
        if id(node) in seen:
            return
        seen.add(id(node))
        yield node
        for value in node.values():
            yield from _walk(value, seen)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, seen)


def _types(node: dict) -> set[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return {raw}
    return set(raw) if isinstance(raw, list) else set()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def slugify(text: str) -> str:
    return _SLUG.sub("-", _APOSTROPHE.sub("", (text or "").lower())).strip("-")


def _parse_dt(value: str) -> datetime | None:
    """Parse a schema.org date-time. Naive values are left to the caller's tz."""
    if not value or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[: len(fmt) + 2], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _availability(node: dict) -> Availability:
    offers = node.get("offers")
    offers = offers if isinstance(offers, list) else [offers] if offers else []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        avail = str(offer.get("availability", "")).rsplit("/", 1)[-1].lower()
        if avail in ("soldout", "outofstock"):
            return Availability.SOLD_OUT
        if avail in ("instock", "limitedavailability", "preorder"):
            return (
                Availability.ALMOST_FULL
                if avail == "limitedavailability"
                else Availability.SELLABLE
            )
    return Availability.UNKNOWN


class JsonLdScreenings:
    """Works on any site publishing schema.org screening markup."""

    chain = "generic"
    tier = 0

    def __init__(self, venue_id: str, *, url: str | None = None) -> None:
        self.venue_id = venue_id
        self.url = url
        self.source = f"jsonld:{venue_id}"

    def fetch(self, transport: Transport, *, url: str | None = None) -> str:
        target = url or self.url
        if not target:
            raise ValueError("JsonLdScreenings needs a url")
        return transport.get(target).text

    def parse(self, raw: str, *, observed_at: datetime | None = None,
              strict: bool = True) -> list[Observation]:
        observed_at = observed_at or datetime.now(timezone.utc)

        blocks = _LD_BLOCK.findall(raw)
        if not blocks:
            raise ParseError(f"{self.source}: no ld+json blocks on the page")

        events: list[dict] = []
        for block in blocks:
            try:
                doc = json.loads(block)
            except json.JSONDecodeError:
                continue  # one malformed block must not blind us to the others
            events.extend(n for n in _walk(doc) if _types(n) & _SCREENING_TYPES)

        if not events:
            raise ParseError(
                f"{self.source}: {len(blocks)} ld+json block(s) but no "
                "ScreeningEvent/Event nodes"
            )

        out: list[Observation] = []
        undated = 0
        for node in events:
            starts_at = _parse_dt(str(node.get("startDate", "")))
            if starts_at is None:
                undated += 1
                continue

            title = _clean(str(node.get("name", "")))
            text = " ".join(
                filter(None, [title, _clean(str(node.get("description", "")))])
            )
            presentation, confidence = classify_text(text, self.venue_id)

            out.append(
                Observation(
                    key=FactKey(
                        venue_id=self.venue_id,
                        movie_id=slugify(title) or "unknown",
                        starts_at_utc=starts_at,
                    ),
                    source=self.source,
                    tier=self.tier,
                    observed_at=observed_at,
                    presentation=presentation or Presentation(),
                    availability=_availability(node),
                    title=title or None,
                    deeplink=node.get("url"),
                    # Prose beats nothing but loses to a machine token.
                    confidence=max(0.35, confidence),
                )
            )

        if not out:
            raise IncompleteStructuredData(
                f"{self.source}: found {len(events)} screening node(s) but none "
                f"carry a usable startDate ({undated} empty). The markup is "
                "decorative - this venue needs an HTML fallback parser, it is "
                "not a venue with no screenings.",
                found=len(events),
                missing="startDate",
            )

        if undated and strict:
            raise IncompleteStructuredData(
                f"{self.source}: {undated} of {len(events)} screening nodes have "
                f"no startDate; parsed {len(out)}. Partial markup - inspect "
                "before trusting the ones that did parse.",
                found=len(events),
                missing="startDate",
            )
        return out
