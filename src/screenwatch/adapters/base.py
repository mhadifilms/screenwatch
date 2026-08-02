"""Adapter contract.

Fetching and parsing are deliberately separate methods. Every parser must be
callable on a stored fixture with no network, which is what makes the replay
test suite possible - and the replay suite is the only thing that catches
silent schema drift before an on-sale does.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Observation
from ..transport import Transport


@runtime_checkable
class Adapter(Protocol):
    source: str
    tier: int          # 0 = unguarded/cheap, 3 = guarded/expensive
    chain: str

    def parse(self, raw: str, **ctx) -> list[Observation]:
        """Pure. No network, no clock beyond the injected observed_at."""

    def fetch(self, transport: Transport, **ctx) -> str:
        """Retrieve raw text for parse(). The only method allowed to do I/O."""


class ParseError(ValueError):
    """Raised when a payload no longer matches the expected shape.

    Always prefer raising over returning an empty list. An empty list looks
    exactly like a quiet Tuesday; an exception pages you.
    """
