"""Ticketing-platform routing for independent exhibitors.

An exhibitor name does not identify its reservation system. This module keeps
that distinction explicit and makes unsupported platform work visible through
typed collector failures instead of a generic "independent" dead end.
"""

from __future__ import annotations

import re
from typing import Protocol

from ...transport import Transport
from ..capture import SeatCapture, SeatProbe
from ..model import (
    BlockedBySource,
    BrowserRequired,
    MissingProviderContext,
    RateLimited,
    TransientSourceFailure,
)


class PlatformSeatSource(Protocol):
    platform: str

    def fetch(self, probe: SeatProbe, transport: Transport) -> SeatCapture: ...


_URL_PLATFORMS = (
    ("vista", re.compile(r"/ticketing/visselecttickets\.aspx", re.I)),
    ("agile", re.compile(r"agile(?:ticketing|tix)|ticketsearchcriteria\.aspx", re.I)),
    ("veezi", re.compile(r"(?:ticketing\.)?veezi\.com", re.I)),
    ("elevent", re.compile(r"(?:ticketing\.)?elevent\.co", re.I)),
    ("rts", re.compile(r"rts(?:-?solutions)?\.(?:com|net)|rtsapp", re.I)),
    ("fandango", re.compile(r"fandango\.com", re.I)),
)


def detect_ticketing_platform(url: str | None) -> str | None:
    for platform, pattern in _URL_PLATFORMS:
        if pattern.search(url or ""):
            return platform
    return None


class _BrowserPlatformSource:
    """Named boundary for platforms whose authoritative map is client-rendered.

    These adapters are deliberately explicit instead of attempting to parse a
    generic page as a room. A platform-specific capture fixture is required
    before geometry can be accepted as authoritative.
    """

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def fetch(self, probe: SeatProbe, transport: Transport) -> SeatCapture:
        if not probe.booking_url:
            raise MissingProviderContext(
                f"{self.platform} probe has no booking URL",
                context={"platform": self.platform},
            )
        if transport is None:
            raise BrowserRequired(
                f"{self.platform} reserved-seat selection is client-rendered",
                context={
                    "platform": self.platform,
                    "booking_url": probe.booking_url,
                },
            )
        try:
            response = transport.get(probe.booking_url)
        except Exception as exc:
            raise TransientSourceFailure(
                f"{self.platform} ticketing page failed: "
                f"{type(exc).__name__}: {exc}",
                context={"platform": self.platform},
            ) from exc

        content_type = response.headers.get("content-type", "text/html")
        context = {
            "platform": self.platform,
            "booking_url": probe.booking_url,
        }
        if response.status_code == 429:
            failure = RateLimited(f"{self.platform} returned HTTP 429", context=context)
        elif response.status_code >= 500:
            failure = TransientSourceFailure(
                f"{self.platform} returned HTTP {response.status_code}",
                context=context,
            )
        elif response.status_code >= 400 or _blocked(response.text):
            failure = BlockedBySource(
                f"{self.platform} blocked the public ticketing page",
                context=context,
            )
        elif self.platform == "vista" and _vista_order_gate(response.text):
            failure = MissingProviderContext(
                "Vista exposes the auditorium but gates seat geometry behind "
                "adding a ticket to an order; Screenwatch will not create that order",
                context={
                    **context,
                    "auditorium_hint": _vista_auditorium(response.text),
                    "boundary": "ticket_quantity_or_order_required",
                },
            )
        else:
            failure = BrowserRequired(
                f"{self.platform} did not expose authoritative seat geometry "
                "in the read-only response",
                context=context,
            )
        raise failure.with_capture(
            response.text,
            content_type=content_type,
            source_url=response.url,
            status_code=response.status_code,
        )


def _blocked(body: str) -> bool:
    lowered = (body or "").lower()
    return any(marker in lowered for marker in (
        "_incapsula_resource",
        "request unsuccessful",
        "incident id",
        "sorry, you have been blocked",
        "attention required",
    ))


def _vista_order_gate(body: str) -> bool:
    lowered = (body or "").lower()
    return (
        'id="select-tickets"' in lowered
        and 'id="txtenablemanualseatselection"' in lowered
        and 'id="ibtnordertickets"' in lowered
    )


def _vista_auditorium(body: str) -> str | None:
    match = re.search(
        r'class="[^"]*cinema-screen-name[^"]*"[^>]*>\s*([^<]+)',
        body or "",
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


class _CredentialedPlatformSource:
    def __init__(self, platform: str) -> None:
        self.platform = platform

    def fetch(self, probe: SeatProbe, transport: Transport) -> SeatCapture:
        raise MissingProviderContext(
            f"{self.platform} seat inventory requires venue-issued API context",
            context={
                "platform": self.platform,
                "booking_url": probe.booking_url,
            },
        )


class PlatformSeatRouter:
    def __init__(self, sources: list[PlatformSeatSource] | None = None) -> None:
        default_sources: list[PlatformSeatSource] = [
            _BrowserPlatformSource("vista"),
            _BrowserPlatformSource("agile"),
            _CredentialedPlatformSource("veezi"),
            _BrowserPlatformSource("elevent"),
            _BrowserPlatformSource("rts"),
        ]
        self._sources = {
            source.platform: source for source in (sources or default_sources)
        }

    def source_for(self, probe: SeatProbe) -> PlatformSeatSource:
        platform = (
            probe.ticketing_platform
            or detect_ticketing_platform(probe.booking_url)
        )
        if not platform:
            raise MissingProviderContext(
                "ticketing platform is unknown for this independent showing",
                context={"booking_url": probe.booking_url},
            )
        source = self._sources.get(platform)
        if source is None:
            raise MissingProviderContext(
                f"no seat source registered for ticketing platform {platform}",
                context={"platform": platform},
            )
        return source

    def fetch(self, probe: SeatProbe, transport: Transport) -> SeatCapture:
        return self.source_for(probe).fetch(probe, transport)
