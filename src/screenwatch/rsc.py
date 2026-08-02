"""Next.js App Router RSC flight-payload extraction.

An App Router page ships its data as a sequence of

    self.__next_f.push([1,"<JS string literal>"])

calls whose decoded strings concatenate into one flight payload. That payload
is a far better read surface than the rendered DOM: it is the same data the
client hydrates from, it has no presentational noise, and it survives CSS and
markup redesigns that would break selectors.

We decode the string literals with a real JSON decoder rather than unescaping
by hand, because hand-unescaping mangles legitimately backslashed content in
movie titles and breaks on \\u sequences.
"""

from __future__ import annotations

import json
import re

_PUSH = re.compile(r"self\.__next_f\.push\(\s*\[\s*\d+\s*,\s*")
_DECODER = json.JSONDecoder()


class FlightPayloadError(ValueError):
    pass


def extract_flight(html: str) -> str:
    """Concatenate every RSC chunk in the document into one payload string."""
    chunks: list[str] = []
    for m in _PUSH.finditer(html):
        idx = m.end()
        if idx >= len(html) or html[idx] != '"':
            continue  # push([0]) bootstrap and similar non-string chunks
        try:
            decoded, _ = _DECODER.raw_decode(html, idx)
        except ValueError:
            continue
        if isinstance(decoded, str):
            chunks.append(decoded)

    if not chunks:
        raise FlightPayloadError(
            "no RSC flight chunks found - the page is not App Router, or it is a "
            "challenge/interstitial page rather than content"
        )
    return "".join(chunks)


def iter_json_objects(payload: str, anchor: str):
    """Yield every JSON object in `payload` that starts with `anchor`.

    Uses raw_decode from each anchor hit so nested braces and strings are
    handled by the JSON parser rather than by brace counting.
    """
    start = 0
    while (i := payload.find(anchor, start)) != -1:
        try:
            obj, end = _DECODER.raw_decode(payload, i)
        except ValueError:
            start = i + 1
            continue
        if isinstance(obj, dict):
            yield obj
            start = end
        else:
            start = i + 1
