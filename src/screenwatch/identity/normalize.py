"""Title normalization, and the variant-stripping that makes identity possible.

Chains do not list films. They list *products*. On 2026-08-02 AMC's sitemap
carried four entries for one film:

    the-odyssey-76238
    the-odyssey-80679
    the-odyssey-sensory-friendly-screening-83988
    the-odyssey-private-theatre-rental-84080

Treating those as four movies breaks every downstream grouping. Treating them
as one movie uncritically is also wrong: the private theatre rental is not a
screening you can buy a seat at, and the sensory-friendly one is a materially
different experience.

So a raw title decomposes into three things:
  * the film's actual title, used for catalogue lookup,
  * `Attribute`s the variant implies (sensory friendly, open caption, …),
  * a `ProductKind` saying whether this is even a bookable public screening.

Format words ("in 35mm") are handed to the existing presentation classifier
rather than reimplemented, so the "shot on 35mm" guard applies here too.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from ..models import Attribute, Presentation
from ..presentation import classify_text


class ProductKind(Enum):
    FEATURE = "feature"        # a normal public screening
    RENTAL = "rental"          # private hire - not a bookable seat
    EVENT = "event"            # opera/anime/concert/marathon - real, but not a film release
    UNKNOWN = "unknown"


# (pattern, attribute it implies, product kind it implies)
# Order matters only for readability; all are applied.
_VARIANTS: list[tuple[re.Pattern[str], Attribute | None, ProductKind | None]] = [
    (re.compile(r"\bprivate\s+(?:theatre|theater)\s+rental\b", re.IGNORECASE), None, ProductKind.RENTAL),
    (re.compile(r"\bprivate\s+(?:screening|watch\s*party)\b", re.IGNORECASE), None, ProductKind.RENTAL),
    (re.compile(r"\bsensory[\s-]friendly(?:\s+screening)?\b", re.IGNORECASE), Attribute.SENSORY_FRIENDLY, None),
    (re.compile(r"\bopen[\s-]caption(?:ed|s)?\b", re.IGNORECASE), Attribute.OPEN_CAPTION, None),
    (re.compile(r"\bclosed[\s-]caption(?:ed|s)?\b", re.IGNORECASE), Attribute.CLOSED_CAPTION, None),
    (re.compile(r"\baudio[\s-]descri(?:bed|ption)\b", re.IGNORECASE), Attribute.AUDIO_DESCRIPTION, None),
    (re.compile(r"\bdouble\s+(?:feature|bill)\b", re.IGNORECASE), Attribute.DOUBLE_FEATURE, None),
    (re.compile(r"\bwith\s+q\s*&\s*a\b|\bq\s*&\s*a\b", re.IGNORECASE), Attribute.Q_AND_A, None),
    (re.compile(r"\bintroduc(?:ed|tion)\s+by\b", re.IGNORECASE), Attribute.INTRODUCTION, None),
    (re.compile(r"\b(?:\d+k\s+)?restoration\b|\brestored\b", re.IGNORECASE), Attribute.RESTORATION, None),
    (re.compile(r"\bsubtitled\b|\bsubtitles?\b", re.IGNORECASE), Attribute.SUBTITLED, None),
    (re.compile(r"\bdubbed\b", re.IGNORECASE), Attribute.DUBBED, None),
    (re.compile(r"\bthe\s+met:\s*live\s+in\s+hd\b", re.IGNORECASE), None, ProductKind.EVENT),
    (re.compile(r"\bmarathon\b|\btriple\s+feature\b|\ball[\s-]nighter\b", re.IGNORECASE), None, ProductKind.EVENT),
    (re.compile(r"\bfan\s+event\b|\bearly\s+access\b|\badvance\s+screening\b", re.IGNORECASE), None, None),
    (re.compile(r"\bencore\b|\bre[\s-]?release\b|\banniversary\b", re.IGNORECASE), None, None),
]

# Noise that survives suffix stripping: trailing separators, empty parens,
# and the presentation words the classifier already consumed.
_FORMAT_WORDS = re.compile(
    r"\b(?:in\s+)?(?:imax\s*)?(?:15/70|70\s*mm|35\s*mm|16\s*mm|nitrate|dcp|4k|2k|3-?d|imax|"
    r"dolby\s+cinema|atmos|screenx|4dx|laser)\b",
    re.IGNORECASE,
)
_EDGE_JUNK = re.compile(r"^[\s\-–—:,·|()\[\]]+|[\s\-–—:,·|()\[\]]+$")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
_WS = re.compile(r"\s+")
_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_APOSTROPHE = re.compile(r"['‘’ʼ`]")


@dataclass(frozen=True)
class TitleAnalysis:
    raw: str
    clean: str                      # "The Odyssey" - for display and catalogue lookup
    match_key: str                  # "odyssey"    - for comparison only
    attrs: frozenset[Attribute]
    kind: ProductKind
    presentation: Presentation      # format words found in the title
    presentation_confidence: float

    @property
    def is_bookable(self) -> bool:
        """A private rental is a product, not a screening you can sit in."""
        return self.kind in (ProductKind.FEATURE, ProductKind.EVENT, ProductKind.UNKNOWN)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def match_key(title: str) -> str:
    """Comparison key. Lossy on purpose - never display this.

    Drops the leading article so "The Odyssey" and "Odyssey" collide, which is
    what we want for matching but catastrophic for display.
    """
    text = _APOSTROPHE.sub("", strip_accents(title).lower())
    text = _ARTICLE.sub("", text.strip())
    return _NON_ALNUM.sub("-", text).strip("-")


def analyze(raw_title: str) -> TitleAnalysis:
    """Decompose a venue's product title into film + attributes + kind."""
    working = _WS.sub(" ", (raw_title or "").replace("\xa0", " ")).strip()

    attrs: set[Attribute] = set()
    kind = ProductKind.UNKNOWN

    for pattern, attr, product_kind in _VARIANTS:
        if not pattern.search(working):
            continue
        if attr is not None:
            attrs.add(attr)
        if product_kind is not None and kind is ProductKind.UNKNOWN:
            kind = product_kind
        working = pattern.sub(" ", working)

    # Format words go to the shared classifier, which knows that "shot on
    # 35mm" describes the negative rather than the print.
    presentation, confidence = classify_text(raw_title)
    attrs |= presentation.attrs

    working = _FORMAT_WORDS.sub(" ", working)
    working = _EMPTY_BRACKETS.sub(" ", working)
    working = _EDGE_JUNK.sub("", _WS.sub(" ", working)).strip()

    # Everything was variant noise - fall back rather than emit an empty title.
    clean = working or _EDGE_JUNK.sub("", _WS.sub(" ", raw_title or "")).strip()

    if kind is ProductKind.UNKNOWN and clean:
        kind = ProductKind.FEATURE

    return TitleAnalysis(
        raw=raw_title,
        clean=clean,
        match_key=match_key(clean),
        attrs=frozenset(attrs),
        kind=kind,
        presentation=presentation,
        presentation_confidence=confidence,
    )


# Words a title keeps lowercase unless they lead it.
_MINOR_WORDS = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "or", "over", "the", "to", "up", "via", "with",
})


def from_slug(slug: str) -> str:
    """Recover a rough title from a URL slug: `the-odyssey` -> `The Odyssey`.

    Cased, because this is a *display* title. It used to return the words
    lowercased, which is right for matching and wrong for reading: AMC derives
    every title from its URL slug, so its options were listed as "spider man
    brand new day" while other chains showed "Spider-Man: Brand New Day".

    The punctuation is not recoverable - a slug spells both a hyphen and a
    colon as "-" - so this is a best effort, and any source with a real title
    should be preferred over it. `WorkResolver` does prefer one.

    Numeric ids must already be stripped by the adapter; a trailing number here
    is part of the title ("blade-runner-2049").
    """
    words = _WS.sub(" ", slug.replace("-", " ")).strip().split()
    return " ".join(
        word if i and word.lower() in _MINOR_WORDS else word[:1].upper() + word[1:]
        for i, word in enumerate(words)
    )
