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
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from ..model import (
    Auditorium,
    Seat,
    SeatDataUnavailable,
    SeatKind,
    SeatStatus,
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
MATCH_WINDOW_S = 900


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
                            )
                        )
        return out

    def seat_map(
        self, transport, theater: FandangoTheater, hash_code: str
    ) -> dict[str, Any]:
        url = f"{FANDANGO}/napi/seatMap/{quote(hash_code)}"
        payload = self._napi(transport, url, theater)
        if not isinstance(payload, dict) or not payload.get("seats"):
            raise SeatDataUnavailable(
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
            raise SeatDataUnavailable(
                f"Fandango returned HTTP {response.status_code} for {url}"
            )
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            raise SeatDataUnavailable("Fandango returned a non-JSON body") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise SeatDataUnavailable(
                f"Fandango refused: {payload.get('errorMessage') or payload['error']}"
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
            raise SeatDataUnavailable("Fandango seat map contained no seats")

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

            seats.append(
                Seat(
                    row_label=(label.group(1).upper() if label else str(row)),
                    row_index=row_index.get(row, 0),
                    col_label=(label.group(2) if label else str(col)),
                    col_index=col_index.get(col, 0),
                    status=_STATUS.get(
                        str(entry.get("status") or "").upper(), SeatStatus.UNAVAILABLE
                    ),
                    kind=_KIND.get(
                        str(entry.get("type") or "").lower(), SeatKind.STANDARD
                    ),
                )
            )

        auditorium_id = payload.get("auditoriumId")
        return Auditorium(
            venue_id=venue_id,
            screen_id=screen_id or (str(auditorium_id) if auditorium_id else ""),
            # `infer_modules` is deliberately not applied: Fandango states the
            # kind of every seat, so pairing adjacent ones into a module would
            # be inventing topology over a source that already reported it.
            seats=normalize_geometry(mark_aisles(seats)),
            geometry_confidence=1.0,
            name=f"Auditorium {auditorium_id}" if auditorium_id else None,
        )


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
) -> FandangoShowtime | None:
    """Which Fandango showing is the one we already know about.

    Start time decides it, because no theater runs two showings of anything in
    the same minute; the title only breaks ties between screens showing the same
    film at once. Without a start time to match on this returns None rather than
    guessing: handing back the wrong auditorium is worse than handing back none,
    and it would be indistinguishable from a correct answer.
    """
    if not candidates or starts_at_local is None:
        return None
    wanted_title = _fold(title or "")

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
        scored.append((score, candidate))

    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]
