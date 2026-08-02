"""Core domain types.

Two ideas carry the design.

*Observations, not facts.* Adapters emit "source S claimed X at time T".
Facts are what the resolver concludes after cross-checking independent
surfaces. Disagreement is data, not noise.

*Presentation is structured, not ranked.* An earlier draft had a single
IMAX-tiered enum, which cannot express a 35mm nitrate print at a rep house
and cannot answer "is Dolby Cinema better than IMAX with Laser" - because
that is a viewer's preference, not a property of the screening. So a
screening carries an orthogonal descriptor (what light hits the screen, what
brand is on the door, what else is true about it), and ranking is supplied by
the user's config as an ordered list of matchers.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta


class Projection(enum.Enum):
    """What is physically putting the image on the screen."""

    UNKNOWN = "unknown"
    DIGITAL = "digital"                    # unspecified digital
    DIGITAL_XENON = "digital_xenon"
    DIGITAL_LASER = "digital_laser"
    FILM_16MM = "film_16mm"
    FILM_35MM = "film_35mm"
    FILM_35MM_NITRATE = "film_35mm_nitrate"
    FILM_70MM = "film_70mm"                # standard 5-perf
    FILM_70MM_15PERF = "film_70mm_15perf"  # IMAX film

    @property
    def is_film(self) -> bool:
        return self.value.startswith("film_")


class Brand(enum.Enum):
    """Premium-format brand on the door. Orthogonal to projection."""

    NONE = "none"
    IMAX = "imax"
    DOLBY_CINEMA = "dolby_cinema"
    PLF = "plf"          # chain large format: RPX, XD, Prime, BigD, UltraScreen…
    SCREENX = "screenx"
    FOURDX = "4dx"
    DBOX = "dbox"


class Attribute(enum.Enum):
    THREE_D = "3d"
    ATMOS = "atmos"
    HFR = "hfr"
    OPEN_CAPTION = "open_caption"
    CLOSED_CAPTION = "closed_caption"
    AUDIO_DESCRIPTION = "audio_description"
    SUBTITLED = "subtitled"
    DUBBED = "dubbed"
    SENSORY_FRIENDLY = "sensory_friendly"
    RESERVED_SEATING = "reserved_seating"
    RECLINERS = "recliners"
    ARCHIVAL_PRINT = "archival_print"
    RESTORATION = "restoration"
    Q_AND_A = "q_and_a"
    INTRODUCTION = "introduction"
    DOUBLE_FEATURE = "double_feature"


@dataclass(frozen=True)
class Presentation:
    """How a screening is being shown.

    `raw` keeps whatever the source actually said. The structured fields are
    lossy by construction, and `raw` is what you re-read when a
    classification turns out to be wrong.
    """

    projection: Projection = Projection.UNKNOWN
    brand: Brand = Brand.NONE
    aspect: str | None = None            # screen aspect, e.g. "1.43"
    attrs: frozenset[Attribute] = field(default_factory=frozenset)
    raw: str = ""

    # Attributes that change what you are buying, as opposed to metadata.
    # These appear in the human description; the rest stay in `attrs`.
    SALIENT = ("THREE_D", "ATMOS", "HFR", "OPEN_CAPTION", "SUBTITLED", "DUBBED",
               "SENSORY_FRIENDLY")

    def describe(self) -> str:
        bits = []
        if self.brand is not Brand.NONE:
            bits.append(self.brand.value.replace("_", " ").upper())
        if self.projection is not Projection.UNKNOWN:
            bits.append(self.projection.value.replace("film_", "").replace("_", " "))
        if self.aspect:
            bits.append(f"{self.aspect}:1")
        # Without this a 3D showing and a 2D one at the same venue render
        # identically, which makes any format tradeoff impossible to read.
        salient = [
            a.value.replace("_", " ").upper()
            for name in self.SALIENT
            for a in (Attribute[name],)
            if a in self.attrs
        ]
        return " / ".join(bits + salient) or "unspecified"

    def with_(self, **kw) -> "Presentation":
        return Presentation(
            projection=kw.get("projection", self.projection),
            brand=kw.get("brand", self.brand),
            aspect=kw.get("aspect", self.aspect),
            attrs=kw.get("attrs", self.attrs),
            raw=kw.get("raw", self.raw),
        )


@dataclass(frozen=True)
class PresentationSpec:
    """A matcher over Presentation. This is how users say what they want.

    Unset fields are wildcards, so `PresentationSpec(projection=FILM_35MM)`
    means "any 35mm screening anywhere" - the rep-house case a chain-format
    enum could never express.
    """

    projection: Projection | None = None
    brand: Brand | None = None
    aspect: str | None = None
    requires: frozenset[Attribute] = field(default_factory=frozenset)
    excludes: frozenset[Attribute] = field(default_factory=frozenset)
    label: str = ""

    def matches(self, p: Presentation) -> bool:
        if self.projection is not None and p.projection is not self.projection:
            return False
        if self.brand is not None and p.brand is not self.brand:
            return False
        if self.aspect is not None and p.aspect != self.aspect:
            return False
        if not self.requires <= p.attrs:
            return False
        return not (self.excludes & p.attrs)


@dataclass
class Preference:
    """Ordered wants. Index 0 is best; unmatched screenings rank None."""

    ranked: list[PresentationSpec]

    def rank(self, p: Presentation) -> int | None:
        for i, spec in enumerate(self.ranked):
            if spec.matches(p):
                return i
        return None

    def wants(self, p: Presentation) -> bool:
        return self.rank(p) is not None


class Availability(enum.Enum):
    UNKNOWN = "unknown"
    SELLABLE = "sellable"
    ALMOST_FULL = "almost_full"
    SOLD_OUT = "sold_out"

    @property
    def is_buyable(self) -> bool:
        return self in (Availability.SELLABLE, Availability.ALMOST_FULL)


@dataclass(frozen=True)
class FactKey:
    """Source-independent identity of a screening.

    Deliberately excludes anything chain-specific: two adapters looking at the
    same screening through different surfaces must produce an equal key, or
    the resolver can never corroborate them.
    """

    venue_id: str
    movie_id: str
    starts_at_utc: datetime

    def __str__(self) -> str:
        return f"{self.venue_id}|{self.movie_id}|{self.starts_at_utc.isoformat()}"

    def bucketed(self, tolerance: timedelta = timedelta(minutes=2)) -> str:
        """Key rounded to a tolerance window.

        Sources disagree by a minute or two (rounded display time vs true
        UTC). Without bucketing that reads as two separate screenings and
        corroboration silently never happens.
        """
        secs = int(self.starts_at_utc.timestamp())
        window = max(int(tolerance.total_seconds()), 1)
        return f"{self.venue_id}|{self.movie_id}|{secs // window}"


@dataclass
class Observation:
    """One source's claim about one screening at one moment."""

    key: FactKey
    source: str                     # "amc:showtimes-rsc", "jsonld:filmforum"
    tier: int                       # 0 = cheap/unguarded .. 3 = expensive/guarded
    observed_at: datetime

    presentation: Presentation
    availability: Availability = Availability.UNKNOWN
    title: str | None = None
    external_id: str | None = None
    deeplink: str | None = None
    price_hint_usd: float | None = None

    confidence: float = 1.0


@dataclass
class Fact:
    """Resolved view of a screening after cross-source corroboration."""

    key: FactKey
    presentation: Presentation
    availability: Availability
    sources: tuple[str, ...]
    agreement: float
    title: str | None = None
    external_id: str | None = None
    deeplink: str | None = None
    observed_at: datetime | None = None
    conflicts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def corroborated(self) -> bool:
        return len(self.sources) >= 2 and self.agreement >= 0.8

    @property
    def needs_review(self) -> bool:
        """Worth a low-priority ping even when we will not act on it.

        A single-source sighting during an on-sale is usually the fastest
        surface winning the race. A *disagreement* means an adapter drifted,
        and you want to know before the drop, not after.
        """
        return bool(self.conflicts) or self.agreement < 0.8
