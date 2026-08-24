"""Seat maps from Fandango, for the chains whose own pages refuse us.

Regal's seat plan lives on a page that renders after hydration, so reading it
needs a browser (see `sources/regal.py`), and Cloudflare increasingly answers
that browser with a firewall block rather than a solvable challenge: the page
comes back `Attention Required` / `Sorry, you have been blocked`. `browser.py`
is right that retrying a block only deepens it, which leaves the seat grid
genuinely unreachable from Regal itself.

Fandango sells the same Regal seats and publishes the same map through plain
JSON, with no challenge and no browser at all:

    GET /napi/theaterMovieShowtimes/<tmsId>?chainCode=<code>&startDate=<date>
    GET /napi/seatMap/<showtimeHashCode>

Two things make them answer. A session, which is one ordinary GET of the theater
page to collect cookies, and the `x-requested-with: XMLHttpRequest` header that
Fandango's own front end sends; without either, both endpoints reply
`FORBIDDEN / Session expired or invalid token`. That is the whole protocol.

It is a better source than the one it backs up. The payload carries seat kinds
including wheelchair spaces and their companion seats, per-seat availability,
and the room's real pixel geometry, which is more than Regal's own rendered page
gives up.

Nothing here holds a seat, selects anything, or signs in. It reads the same
public map a person sees before deciding where to sit.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from ..model import (
    AmbiguousShowtimeMatch,
    Auditorium,
    BlockedBySource,
    ParserDrift,
    PermanentNoSeatMap,
    RateLimited,
    Seat,
    SeatDataUnavailable,
    SeatKind,
    SeatStatus,
    TransientSourceFailure,
    mark_aisles,
    normalize_geometry,
)

FANDANGO = "https://www.fandango.com"

# Fandango's own code for each chain whose seats it sells.
CHAIN_CODES = {
    "regal": "REGL",
    "amc": "AMC",
    "cinemark": "CNMK",
}

# Their seat types, in our vocabulary. Anything unrecognised stays a plain seat
# rather than becoming a guess about what somebody is buying.
_KIND = {
    "standard": SeatKind.STANDARD,
    "recliner": SeatKind.RECLINER,
    "loveseat": SeatKind.LOVESEAT,
    "sofa": SeatKind.LOVESEAT,
    "wheelchair": SeatKind.WHEELCHAIR,
    "companion": SeatKind.COMPANION,
}

# Their per-seat status, read off the payload's own arithmetic rather than
# guessed at. In a live map: A = 110, R = 3, O = 39, with
# `totalAvailableSeatCount` = 110 and `totalSeatCount` = 113. So A is exactly
# what is available, A + R is the sellable inventory, which makes R *sold*
# rather than held, and O is excluded from the count altogether: space that is
# not a seat, not a seat somebody bought.
_STATUS = {
    "A": SeatStatus.AVAILABLE,
    "R": SeatStatus.SOLD,
    "O": SeatStatus.UNAVAILABLE,
}

# A showing is matched across two sites by its start time; nothing else is
# reliable. Fifteen minutes is far wider than any clock skew and far narrower
# than the gap between two showings of one film on one screen.
MATCH_WINDOW_S = 120


def _fold(text: str) -> str:
    """Reduce a title or theater name to something comparable across sites."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = folded.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", folded)


def _trigram_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    def grams(s: str) -> set[str]:
        return {s[i : i + 3] for i in range(max(len(s) - 2, 1))}
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / max(1, min(len(ga), len(gb)))


@dataclass(frozen=True)
class FandangoTheater:
    path: str          # /regal-hacienda-crossings-...-AAOPK/theater-page
    slug: str
    tms_id: str        # AAOPK


@dataclass(frozen=True)
class FandangoShowtime:
    hash_code: str
    showtime_id: str
    title: str
    starts_at_local: datetime | None
    reserved_seating: bool
    variant: str = ""
    amenities: tuple[str, ...] = ()

    @property
    def presentation_text(self) -> str:
        return " ".join((self.variant, *self.amenities)).strip()


class FandangoSeatSource:
    """Fandango's public seat map, as an `Auditorium`.

    Stateless apart from a small theater-lookup cache. The session cookies live
    on the `Transport` it is handed, which is the right place for them: one
    long-lived session per source is the same rule the rest of this project
    follows.
    """

    def __init__(self) -> None:
        self._theaters: dict[str, FandangoTheater | None] = {}

    # ------------------------------------------------------------------ lookup
    def find_theater(self, transport, name: str) -> FandangoTheater | None:
        """The Fandango theater whose name best matches, or None.

        Their search results are HTML and the only part worth reading is the
        theater link, which carries the slug and the id together. Note the id is
        upper-case in the href while the rest of the slug is not: matching only
        lower-case characters finds nothing at all.
        """
        key = _fold(name)
        if key in self._theaters:
            return self._theaters[key]

        response = transport.get(f"{FANDANGO}/search?q={quote(name)}")
        best: FandangoTheater | None = None
        best_score = 0.0
        wanted = _fold(name)
        for match in re.finditer(
            r'href="(/([A-Za-z0-9-]+)/theater-page)"', response.text or ""
        ):
            path, slug = match.group(1), match.group(2)
            tms = slug.rsplit("-", 1)[-1]
            if len(tms) < 4:
                continue
            score = _trigram_overlap(_fold(slug), wanted)
            if score > best_score:
                best_score = score
                best = FandangoTheater(path=path, slug=slug, tms_id=tms.upper())

        # A weak match is worse than none: it would hand back a different
        # theater's auditorium, which is indistinguishable from a correct answer.
        if best_score < 0.4:
            best = None
        self._theaters[key] = best
        return best

    def showtimes(
        self, transport, theater: FandangoTheater, day: date, chain: str
    ) -> list[FandangoShowtime]:
        code = CHAIN_CODES.get(chain, "")
        url = (
            f"{FANDANGO}/napi/theaterMovieShowtimes/{quote(theater.tms_id)}"
            f"?chainCode={quote(code)}&startDate={day.isoformat()}&isdesktop=true"
        )
        payload = self._napi(transport, url, theater)
        view = (payload or {}).get("viewModel") or {}

        out: list[FandangoShowtime] = []
        for movie in view.get("movies") or []:
            title = movie.get("title") or ""
            for variant in movie.get("variants") or []:
                for group in variant.get("amenityGroups") or []:
                    reserved = bool(group.get("hasReservedSeating"))
                    variant_name = _metadata_label(variant)
                    amenities = _metadata_items(group.get("amenities"))
                    if not amenities:
                        amenities = _metadata_items(group.get("amenityNames"))
                    for showtime in group.get("showtimes") or []:
                        hash_code = showtime.get("showtimeHashCode")
                        if not hash_code:
                            continue
                        out.append(
                            FandangoShowtime(
                                hash_code=str(hash_code),
                                showtime_id=str(showtime.get("id") or ""),
                                title=title,
                                starts_at_local=_parse_ticketing_date(
                                    showtime.get("ticketingDate")
                                ),
                                reserved_seating=reserved,
                                variant=variant_name,
                                amenities=tuple(dict.fromkeys([
                                    *amenities,
                                    *_metadata_items(showtime.get("amenities")),
                                ])),
                            )
                        )
        return out

    def seat_map(
        self, transport, theater: FandangoTheater, hash_code: str
    ) -> dict[str, Any]:
        url = f"{FANDANGO}/napi/seatMap/{quote(hash_code)}"
        payload = self._napi(transport, url, theater)
        if not isinstance(payload, dict) or not payload.get("seats"):
            raise PermanentNoSeatMap(
                "Fandango has no seat map for this showing (general admission?)"
            )
        return payload

    # ------------------------------------------------------------------ session
    def _napi(self, transport, url: str, theater: FandangoTheater) -> Any:
        """A `napi` GET, with the session and headers those endpoints require."""
        import json

        # One ordinary page view earns the cookies. The transport keeps them, so
        # this is cheap after the first call and is what makes `napi` answer.
        transport.get(f"{FANDANGO}{theater.path}")

        response = transport.get(
            url,
            headers={
                "accept": "*/*",
                "x-requested-with": "XMLHttpRequest",
                "referer": f"{FANDANGO}{theater.path}",
            },
        )
        if response.status_code >= 400:
            message = f"Fandango returned HTTP {response.status_code} for {url}"
            if response.status_code == 429:
                failure = RateLimited(message)
            elif response.status_code in {401, 403}:
                failure = BlockedBySource(message)
            elif response.status_code >= 500:
                failure = TransientSourceFailure(message)
            else:
                failure = SeatDataUnavailable(message)
            raise failure.with_capture(
                response.text,
                content_type=getattr(response, "headers", {}).get(
                    "content-type", "text/plain"
                ),
                source_url=url,
                status_code=response.status_code,
            )
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            failure = ParserDrift("Fandango returned a non-JSON body")
            raise failure.with_capture(
                response.text,
                content_type=getattr(response, "headers", {}).get(
                    "content-type", "text/plain"
                ),
                source_url=url,
                status_code=response.status_code,
            ) from exc
        if isinstance(payload, dict) and payload.get("error"):
            failure = BlockedBySource(
                f"Fandango refused: {payload.get('errorMessage') or payload['error']}"
            )
            raise failure.with_capture(
                response.text,
                content_type=getattr(response, "headers", {}).get(
                    "content-type", "application/json"
                ),
                source_url=url,
                status_code=response.status_code,
            )
        return payload

    # -------------------------------------------------------------------- parse
    @staticmethod
    def parse(payload: dict[str, Any], *, venue_id: str, screen_id: str = "") -> Auditorium:
        """Fandango's seat map as an `Auditorium`.

        Row and column *indices* come from Fandango's own grid, which is dense
        and starts at one. The *labels* come from the seat id: `A13` is row A,
        seat 13, and that is what the ticket prints and therefore what somebody
        reads off it. Falling back to the grid numbers when an id is not shaped
        that way keeps a strange room usable rather than dropping it.
        """
        raw = payload.get("seats") or []
        if not raw:
            raise PermanentNoSeatMap("Fandango seat map contained no seats")

        rows = sorted({int(s.get("row") or 0) for s in raw})
        cols = sorted({int(s.get("column") or 0) for s in raw})
        row_index = {row: i for i, row in enumerate(rows)}
        col_index = {col: i for i, col in enumerate(cols)}

        seats: list[Seat] = []
        for entry in raw:
            seat_id = str(entry.get("id") or "").strip()
            label = re.match(r"^([A-Za-z]{1,2})\s*(\d{1,3})$", seat_id)
            row = int(entry.get("row") or 0)
            col = int(entry.get("column") or 0)

            status_code = str(entry.get("status") or "").upper()
            kind = _KIND.get(
                str(entry.get("type") or "").lower(), SeatKind.STANDARD
            )
            if status_code == "O":
                # Geometry padding/space. Keep it to preserve the provider's
                # coordinate system, but never count or render it as a seat.
                kind = SeatKind.BLOCKED

            seats.append(
                Seat(
                    row_label=(label.group(1).upper() if label else str(row)),
                    row_index=row_index.get(row, 0),
                    col_label=(label.group(2) if label else str(col)),
                    col_index=col_index.get(col, 0),
                    status=_STATUS.get(status_code, SeatStatus.UNAVAILABLE),
                    kind=kind,
                )
            )

        auditorium_id = payload.get("auditoriumId")
        parsed_capacity = sum(1 for seat in seats if seat.kind.is_bookable)
        room = Auditorium(
            venue_id=venue_id,
            screen_id=screen_id or (str(auditorium_id) if auditorium_id else ""),
            # `infer_modules` is deliberately not applied: Fandango states the
            # kind of every seat, so pairing adjacent ones into a module would
            # be inventing topology over a source that already reported it.
            seats=normalize_geometry(mark_aisles(seats)),
            geometry_confidence=1.0,
            name=f"Auditorium {auditorium_id}" if auditorium_id else None,
            reported_capacity=(
                int(payload["totalSeatCount"])
                if payload.get("totalSeatCount") is not None else None
            ),
        )
        reported_capacity = payload.get("totalSeatCount")
        if reported_capacity is not None and parsed_capacity != int(reported_capacity):
            raise ParserDrift(
                "Fandango seat-map capacity mismatch: "
                f"parsed {parsed_capacity}, source reported {reported_capacity}",
                context={
                    "parsed_capacity": parsed_capacity,
                    "reported_capacity": int(reported_capacity),
                    "auditorium_id": auditorium_id,
                },
            )
        return room


def _metadata_label(payload: object) -> str:
    if not isinstance(payload, dict):
        return str(payload or "").strip()
    for key in ("name", "label", "displayName", "description", "format"):
        if payload.get(key):
            return str(payload[key]).strip()
    return ""


def _metadata_items(payload: object) -> list[str]:
    if payload is None:
        return []
    if not isinstance(payload, (list, tuple)):
        payload = [payload]
    return [label for item in payload if (label := _metadata_label(item))]


def _parse_ticketing_date(value: str | None) -> datetime | None:
    """`2026-08-21+11:30` is how Fandango writes a local start time."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).replace("+", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def pick_showtime(
    candidates: list[FandangoShowtime],
    *,
    title: str | None,
    starts_at_local: datetime | None,
    presentation: str | None = None,
    source_screen_id: str | None = None,
    auditorium_id_for: Callable[[FandangoShowtime], str | None] | None = None,
) -> FandangoShowtime | None:
    """Which Fandango showing is the one we already know about.

    Start time, title, and presentation metadata all participate. If multiple
    candidates remain equally plausible, the caller may supply a map resolver
    so their source auditorium ids can be compared. Ambiguity is otherwise a
    named failure, never an arbitrary first item.
    """
    if not candidates or starts_at_local is None:
        return None
    wanted_title = _fold(title or "")
    wanted_presentation = _fold(presentation or "")

    scored: list[tuple[float, FandangoShowtime]] = []
    for candidate in candidates:
        if candidate.starts_at_local is None:
            continue
        delta = abs((candidate.starts_at_local - starts_at_local).total_seconds())
        if delta > MATCH_WINDOW_S:
            continue
        score = 2.0 - (delta / MATCH_WINDOW_S)
        if wanted_title and candidate.title:
            score += _trigram_overlap(_fold(candidate.title), wanted_title)
        if wanted_presentation and candidate.presentation_text:
            score += _trigram_overlap(
                _fold(candidate.presentation_text), wanted_presentation
            )
        scored.append((score, candidate))

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0]
    tied = [candidate for score, candidate in scored if abs(score - best_score) < 1e-9]
    if len(tied) == 1:
        return tied[0]
    if source_screen_id and auditorium_id_for is not None:
        matching = [
            candidate for candidate in tied
            if auditorium_id_for(candidate) == str(source_screen_id)
        ]
        if len(matching) == 1:
            return matching[0]
    raise AmbiguousShowtimeMatch(
        "multiple Fandango showtimes match the same title, time, and presentation",
        context={
            "candidate_hashes": [candidate.hash_code for candidate in tied],
            "starts_at_local": starts_at_local.isoformat(),
            "title": title,
            "presentation": presentation,
            "source_screen_id": source_screen_id,
        },
    )
