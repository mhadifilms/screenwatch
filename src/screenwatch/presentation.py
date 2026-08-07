"""Turning what a source says into a Presentation.

Three paths, because venues describe themselves in three different ways:

  1. `classify_token` - chains emit machine tokens (`imax70mm`,
     `dolbycinemaatamcprime`). A table, and unknown tokens raise.

  2. `classify_text` - independents put the format in prose: "THE THIRD MAN
     in 35mm", "70mm presentation", "nitrate print". Verified live on
     filmforum.org, whose JSON-LD startDate fields are empty but whose event
     titles carry the format. No token table can ever cover this, so it is
     pattern matching over free text, with a confidence score.

  3. `refine` - a verified venue metadata overlay. Fills in what the listing hides
     only when the overlay has explicit provenance.
     AMC Lincoln Square 13 is a 1.43:1 house but advertises only "IMAX with
     Laser at AMC"; the Coolidge's 70mm is a different screen from its
     digital rooms. Neither fact is in any listing.
"""

from __future__ import annotations

import json
import pathlib
import re

from .models import Attribute, Brand, Presentation, Projection

_DATA = pathlib.Path(__file__).resolve().parent / "data"


class UnknownFormatError(ValueError):
    """A chain token we have never seen. Never guess - alert and add a case."""


# ---------------------------------------------------------------- chains ---

def _p(projection=Projection.UNKNOWN, brand=Brand.NONE, aspect=None, attrs=()):
    return Presentation(projection=projection, brand=brand, aspect=aspect,
                        attrs=frozenset(attrs))


_AMC_TOKENS: dict[str, Presentation] = {
    "imax70mm":           _p(Projection.FILM_70MM_15PERF, Brand.IMAX),
    "imax70mmfilm":       _p(Projection.FILM_70MM_15PERF, Brand.IMAX),
    "imaxwithlaseratamc": _p(Projection.DIGITAL_LASER, Brand.IMAX),
    "imaxwithlaser":      _p(Projection.DIGITAL_LASER, Brand.IMAX),
    "imaxlaseratamc":     _p(Projection.DIGITAL_LASER, Brand.IMAX),
    "imaxatamc":          _p(Projection.DIGITAL, Brand.IMAX),
    "imax":               _p(Projection.DIGITAL, Brand.IMAX),
    "imax3d":             _p(Projection.DIGITAL, Brand.IMAX, attrs=[Attribute.THREE_D]),
    "imaxdigital":        _p(Projection.DIGITAL, Brand.IMAX),

    "dolbycinemaatamcprime": _p(Projection.DIGITAL_LASER, Brand.DOLBY_CINEMA,
                                attrs=[Attribute.ATMOS, Attribute.RECLINERS]),
    "dolbycinema":        _p(Projection.DIGITAL_LASER, Brand.DOLBY_CINEMA,
                             attrs=[Attribute.ATMOS]),
    "primeatamc":         _p(Projection.DIGITAL, Brand.PLF),
    "bigd":               _p(Projection.DIGITAL, Brand.PLF),
    "rpx":                _p(Projection.DIGITAL, Brand.PLF),
    "xd":                 _p(Projection.DIGITAL, Brand.PLF),
    "ultrascreendlx":     _p(Projection.DIGITAL, Brand.PLF),

    "laseratamc":         _p(Projection.DIGITAL_LASER),
    "70mm":               _p(Projection.FILM_70MM),
    "35mm":               _p(Projection.FILM_35MM),
    "digital":            _p(Projection.DIGITAL),

    "screenx":            _p(Projection.DIGITAL, Brand.SCREENX),
    "4dx":                _p(Projection.DIGITAL, Brand.FOURDX),
    "dbox":               _p(Projection.DIGITAL, Brand.DBOX),

    "reald3d":            _p(attrs=[Attribute.THREE_D]),
    "3d":                 _p(attrs=[Attribute.THREE_D]),
    "dolbyatmos":         _p(attrs=[Attribute.ATMOS]),
    "opencaption":        _p(attrs=[Attribute.OPEN_CAPTION]),
    "closedcaption":      _p(attrs=[Attribute.CLOSED_CAPTION]),
    "audiodescription":   _p(attrs=[Attribute.AUDIO_DESCRIPTION]),
    "japaneseenglishsubtitle": _p(attrs=[Attribute.SUBTITLED]),
    "spanishsubtitles":   _p(attrs=[Attribute.SUBTITLED]),
    "englishsubtitles":   _p(attrs=[Attribute.SUBTITLED]),
    "sensoryfriendly":    _p(attrs=[Attribute.SENSORY_FRIENDLY]),
    "reservedseating":    _p(attrs=[Attribute.RESERVED_SEATING]),
    "amcsignaturerecliners": _p(attrs=[Attribute.RECLINERS]),
}

# Tokens that are real but carry no presentation meaning. Listing them is what
# lets a genuinely *new* token raise instead of being silently dropped.
_AMC_IGNORED = frozenset({
    "amcartisanfilms", "amcclubrockers", "amcindependent", "fanevent",
    "specialevent", "amcscreenunseen", "discountmatinee", "amcstubsalist",
})

_TOKEN_TABLES = {"amc": (_AMC_TOKENS, _AMC_IGNORED)}

_NORMALIZE = re.compile(r"[^a-z0-9]+")


def normalize_token(raw: str) -> str:
    return _NORMALIZE.sub("", raw.lower())


def register_chain(chain: str, tokens: dict[str, Presentation],
                   ignored: frozenset[str] = frozenset()) -> None:
    """Add a chain's token table. Chains are data, not code."""
    _TOKEN_TABLES[chain] = ({normalize_token(k): v for k, v in tokens.items()}, ignored)


def assume_digital(p: Presentation) -> Presentation:
    """Fill an unstated projection with DIGITAL.

    Chains only label projection when it is unusual: AMC tags `imax70mm` but
    files a 3D showing as just `reald3d`, and Regal tags `2D` but leaves it
    implicit on premium brands. Those come through with UNKNOWN projection and
    render as bare "3D" or "unspecified", which is unreadable in a ranked list.

    Safe in the only direction that matters: it can never invent a *film*
    print. Wrongly claiming 70mm sends someone across a city for a DCP;
    wrongly saying "digital" about a digital screening says nothing at all.
    """
    if p.projection is not Projection.UNKNOWN:
        return p
    return p.with_(projection=Projection.DIGITAL)


def known_tokens(chain: str) -> frozenset[str]:
    tokens, ignored = _TOKEN_TABLES[chain]
    return frozenset(tokens) | ignored


# Substring probes for tokens we have never seen. Chain tokens are
# concatenated words with the separators stripped ("imaxwithlaseratamc"), so
# the word-boundary patterns used for prose cannot read them at all. Ordered
# most specific first.
_FUZZY_PROJECTION: list[tuple[str, Projection]] = [
    ("imax70mm", Projection.FILM_70MM_15PERF),
    ("nitrate", Projection.FILM_35MM_NITRATE),
    ("70mm", Projection.FILM_70MM),
    ("35mm", Projection.FILM_35MM),
    ("16mm", Projection.FILM_16MM),
    ("laser", Projection.DIGITAL_LASER),
]
_FUZZY_BRAND: list[tuple[str, Brand]] = [
    ("dolbycinema", Brand.DOLBY_CINEMA),
    ("imax", Brand.IMAX),
    ("screenx", Brand.SCREENX),
    ("4dx", Brand.FOURDX),
    ("dbox", Brand.DBOX),
]


def classify_token_fuzzy(raw_token: str) -> Presentation:
    """Best effort on an unrecognized chain token.

    Used only when the caller opted out of strict mode - a live poll should
    survive AMC inventing `imaxwithlaserxt` overnight, while CI over captured
    fixtures still fails so somebody adds the real table entry.
    """
    token = normalize_token(raw_token)
    projection = next((p for s, p in _FUZZY_PROJECTION if s in token), Projection.UNKNOWN)
    brand = next((b for s, b in _FUZZY_BRAND if s in token), Brand.NONE)
    return Presentation(projection=projection, brand=brand, raw=raw_token)


def classify_token(chain: str, raw_token: str, venue_id: str | None = None) -> Presentation:
    if chain not in _TOKEN_TABLES:
        raise UnknownFormatError(f"no token table for chain {chain!r}")
    tokens, ignored = _TOKEN_TABLES[chain]

    token = normalize_token(raw_token)
    hit = tokens.get(token)
    if hit is None:
        if token in ignored:
            return Presentation(raw=raw_token)
        raise UnknownFormatError(
            f"unknown {chain} format token {raw_token!r} (normalized {token!r}); "
            f"add it to the {chain} token table or its ignore set"
        )

    out = hit.with_(raw=raw_token)
    return refine(out, venue_id) if venue_id else out


# --------------------------------------------------------- free text ------

# Order matters: the most specific pattern must win. "IMAX 70mm" is not
# "70mm", and "35mm nitrate" is not "35mm".
_PROJECTION_PATTERNS: list[tuple[re.Pattern[str], Projection]] = [
    (re.compile(r"\b(?:imax\s*(?:70\s*mm|film)|15\s*/\s*70)\b", re.IGNORECASE), Projection.FILM_70MM_15PERF),
    (re.compile(r"\bnitrate\b", re.IGNORECASE), Projection.FILM_35MM_NITRATE),
    (re.compile(r"\b70\s*mm\b", re.IGNORECASE), Projection.FILM_70MM),
    (re.compile(r"\b35\s*mm\b", re.IGNORECASE), Projection.FILM_35MM),
    (re.compile(r"\b16\s*mm\b", re.IGNORECASE), Projection.FILM_16MM),
    (re.compile(r"\blaser\b", re.IGNORECASE), Projection.DIGITAL_LASER),
    (re.compile(r"\b(?:dcp|digital)\b", re.IGNORECASE), Projection.DIGITAL),
]

_BRAND_PATTERNS: list[tuple[re.Pattern[str], Brand]] = [
    (re.compile(r"\bdolby\s+cinema\b", re.IGNORECASE), Brand.DOLBY_CINEMA),
    (re.compile(r"\bimax\b", re.IGNORECASE), Brand.IMAX),
    (re.compile(r"\bscreen\s*x\b", re.IGNORECASE), Brand.SCREENX),
    (re.compile(r"\b4dx\b", re.IGNORECASE), Brand.FOURDX),
    (re.compile(r"\bd-?box\b", re.IGNORECASE), Brand.DBOX),
    (re.compile(r"\b(?:rpx|big\s*d|ultrascreen|grand\s+screen|prime\s+at\s+amc)\b", re.IGNORECASE), Brand.PLF),
]

_ATTR_PATTERNS: list[tuple[re.Pattern[str], Attribute]] = [
    (re.compile(r"\b3-?d\b", re.IGNORECASE), Attribute.THREE_D),
    (re.compile(r"\batmos\b", re.IGNORECASE), Attribute.ATMOS),
    (re.compile(r"\bhfr\b|high\s+frame\s+rate", re.IGNORECASE), Attribute.HFR),
    (re.compile(r"\bOC\b|\bopen[- ]caption", re.IGNORECASE), Attribute.OPEN_CAPTION),
    (re.compile(r"\bCC\b|\bclosed[- ]caption", re.IGNORECASE), Attribute.CLOSED_CAPTION),
    (re.compile(r"\baudio\s+descri", re.IGNORECASE), Attribute.AUDIO_DESCRIPTION),
    (re.compile(r"\bsubtitle", re.IGNORECASE), Attribute.SUBTITLED),
    (re.compile(r"\bdubbed\b", re.IGNORECASE), Attribute.DUBBED),
    (re.compile(r"sensory[- ]friendly", re.IGNORECASE), Attribute.SENSORY_FRIENDLY),
    (re.compile(r"\brestor(?:ed|ation)\b", re.IGNORECASE), Attribute.RESTORATION),
    (re.compile(r"\barchival\b|\barchive\s+print\b", re.IGNORECASE), Attribute.ARCHIVAL_PRINT),
    (re.compile(r"\bq\s*&\s*a\b|\bq\s*and\s*a\b", re.IGNORECASE), Attribute.Q_AND_A),
    (re.compile(r"\bintroduc(?:ed|tion)\b", re.IGNORECASE), Attribute.INTRODUCTION),
    (re.compile(r"\bdouble\s+(?:feature|bill)\b", re.IGNORECASE), Attribute.DOUBLE_FEATURE),
]

# "shot on 35mm" / "filmed in 70mm" describe the negative, not the print in
# the projector. A rep house saying "restored from the original 35mm camera
# negative" is very often screening a DCP.
_ORIGINATION = re.compile(
    r"\b(?:shot|filmed|photographed|originated|captured)\s+(?:on|in)\s*$"
    r"|\b(?:camera\s+)?negative\b\s*$"
    r"|\boriginal\s*$",
    re.IGNORECASE,
)


def classify_text(text: str, venue_id: str | None = None) -> tuple[Presentation, float]:
    """Extract a Presentation from prose. Returns (presentation, confidence).

    Confidence is deliberately blunt - it exists so the resolver can weight a
    prose guess below a machine token, not to be calibrated.
    """
    if not text:
        return Presentation(), 0.0

    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"[ \s]+", " ", clean).strip()

    projection = Projection.UNKNOWN
    confidence = 0.0
    for pattern, value in _PROJECTION_PATTERNS:
        m = pattern.search(clean)
        if not m:
            continue
        if _ORIGINATION.search(clean[max(0, m.start() - 40):m.start()]):
            continue  # describes the negative, not the print being screened
        projection = value
        confidence = 0.9 if value.is_film else 0.6
        break

    brand = Brand.NONE
    for pattern, value in _BRAND_PATTERNS:
        if pattern.search(clean):
            brand = value
            confidence = max(confidence, 0.8)
            break

    attrs = {value for pattern, value in _ATTR_PATTERNS if pattern.search(clean)}
    if attrs and confidence == 0.0:
        confidence = 0.4

    out = Presentation(projection=projection, brand=brand,
                       attrs=frozenset(attrs), raw=clean[:200])
    return (refine(out, venue_id) if venue_id else out), confidence


# ------------------------------------------------ venue metadata overlay ---

def _load_venues() -> dict[str, dict]:
    path = _DATA / "venue_hardware.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["venues"]


_VENUES: dict[str, dict] = _load_venues()


def hardware_provenance(venue_id: str) -> dict:
    """Return the trust boundary for a curated hardware record.

    The shipped file is intentionally useful as a set of hypotheses while it
    is still explicitly marked ``seed-unverified``.  It must not silently turn
    a listing's missing aspect ratio or a seat-scoring prior into a fact.  A
    record becomes usable for inference only after both a non-seed source and
    ``verified_at`` are present.
    """
    info = _VENUES.get(venue_id)
    if info is None:
        return {
            "status": "unknown",
            "source": None,
            "verified_at": None,
            "usable_for_inference": False,
            "recorded": False,
            "note": "No curated hardware record is loaded for this venue.",
        }

    source = info.get("source")
    verified_at = info.get("verified_at")
    usable = bool(verified_at) and source not in {None, "", "seed-unverified"}
    return {
        "status": "verified" if usable else "unverified",
        "source": source,
        "verified_at": verified_at,
        "usable_for_inference": usable,
        "recorded": True,
        "note": (
            "Hardware metadata may be shown as a candidate, but it is not used "
            "to infer a screening's presentation until verified."
            if not usable else
            "Hardware metadata is eligible for narrowly scoped inference."
        ),
    }


def hardware_dataset_summary() -> dict:
    """Summarize the shipped hardware overlay without overstating coverage."""
    records = list(_VENUES.items())
    verified = [
        venue_id for venue_id, _info in records
        if hardware_provenance(venue_id)["usable_for_inference"]
    ]
    unverified = [
        venue_id for venue_id, _info in records
        if hardware_provenance(venue_id)["status"] == "unverified"
    ]
    return {
        "path": "src/screenwatch/data/venue_hardware.json",
        "schema_version": _load_venue_schema_version(),
        "status": "verified" if verified and not unverified else "seed-unverified",
        "records": len(records),
        "verified_records": len(verified),
        "unverified_records": len(unverified),
        "verified_venue_ids": verified,
        "unverified_venue_ids": unverified,
        "coverage_note": (
            "This is a seven-venue candidate overlay, not a national hardware "
            "registry. Missing venues and missing capabilities are unknown."
        ),
    }


def _load_venue_schema_version() -> int | None:
    path = _DATA / "venue_hardware.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("schema_version")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def venue(venue_id: str) -> dict | None:
    return _VENUES.get(venue_id)


def venue_capabilities(venue_id: str) -> list[Presentation]:
    """Candidate screen profiles from the overlay, including unverified rows.

    This low-level accessor preserves the raw candidate data for inspection.
    Callers making an inference should use :func:`trusted_venue_capabilities`.
    """
    info = _VENUES.get(venue_id)
    if not info:
        return []
    return [
        Presentation(
            projection=Projection[s["projection"]] if s.get("projection") else Projection.UNKNOWN,
            brand=Brand[s["brand"]] if s.get("brand") else Brand.NONE,
            aspect=s.get("aspect"),
        )
        for s in info.get("screens", [])
    ]


def trusted_venue_capabilities(venue_id: str) -> list[Presentation]:
    """Screen profiles eligible to refine a live screening presentation."""
    if not hardware_provenance(venue_id)["usable_for_inference"]:
        return []
    return venue_capabilities(venue_id)


def trusted_venue_info(venue_id: str) -> dict | None:
    """Return hardware metadata only when its provenance is explicit."""
    if not hardware_provenance(venue_id)["usable_for_inference"]:
        return None
    return venue(venue_id)


def refine(p: Presentation, venue_id: str | None) -> Presentation:
    """Fill in the *aspect* a listing omitted, using verified venue metadata.

    Deliberately narrow. It fills aspect and nothing else.

    An earlier version also inferred projection when the source said nothing,
    and the test suite immediately caught what that means in practice: an
    unrecognized token at a venue whose table lists a 70mm screen came back
    claiming 70mm. The table lists a venue's *notable* screens, never all of
    them, so "this venue owns a 70mm projector" is not evidence that this
    screening uses it. Inventing a film-print claim is the single worst error
    this system can make - it sends you across a city for a DCP.

    Aspect is safe because it is scoped by brand: if the venue has exactly one
    screen carrying the brand the listing already claimed, that screen's
    geometry is the screening's geometry.
    """
    if not venue_id or p.aspect:
        return p

    candidates = [
        c for c in trusted_venue_capabilities(venue_id)
        if c.brand is p.brand and c.aspect
    ]
    if p.brand is Brand.NONE or len({c.aspect for c in candidates}) != 1:
        return p
    return p.with_(aspect=candidates[0].aspect)
