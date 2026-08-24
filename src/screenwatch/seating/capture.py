"""Durable inputs and outputs for seat-map collection.

`Option` is a ranking result.  It is intentionally absent from this module:
seat acquisition must be replayable after a process restart without first
reconstructing a user's search session.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .model import Auditorium, Seat


@dataclass(frozen=True)
class SeatProbe:
    source: str
    venue_id: str
    source_venue_id: str | None
    showtime_id: str
    booking_url: str | None
    starts_at_local: datetime | None
    title: str | None
    source_screen_id: str | None
    metadata: dict[str, object] = field(default_factory=dict)
    ticketing_platform: str | None = None

    @property
    def probe_id(self) -> str:
        identity = json.dumps(
            [self.source, self.source_venue_id or self.venue_id, self.showtime_id],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return f"probe_{hashlib.sha256(identity).hexdigest()[:24]}"

    def to_dict(self) -> dict[str, object]:
        return {
            "probe_id": self.probe_id,
            "source": self.source,
            "venue_id": self.venue_id,
            "source_venue_id": self.source_venue_id,
            "showtime_id": self.showtime_id,
            "booking_url": self.booking_url,
            "starts_at_local": (
                self.starts_at_local.isoformat() if self.starts_at_local else None
            ),
            "title": self.title,
            "source_screen_id": self.source_screen_id,
            "metadata": self.metadata,
            "ticketing_platform": self.ticketing_platform,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> SeatProbe:
        starts = payload.get("starts_at_local")
        return cls(
            source=str(payload.get("source") or "unknown"),
            venue_id=str(payload.get("venue_id") or ""),
            source_venue_id=(
                str(payload["source_venue_id"])
                if payload.get("source_venue_id") is not None else None
            ),
            showtime_id=str(payload.get("showtime_id") or ""),
            booking_url=(
                str(payload["booking_url"])
                if payload.get("booking_url") is not None else None
            ),
            starts_at_local=(datetime.fromisoformat(str(starts)) if starts else None),
            title=str(payload["title"]) if payload.get("title") is not None else None,
            source_screen_id=(
                str(payload["source_screen_id"])
                if payload.get("source_screen_id") is not None else None
            ),
            metadata=dict(payload.get("metadata") or {}),
            ticketing_platform=(
                str(payload["ticketing_platform"])
                if payload.get("ticketing_platform") is not None else None
            ),
        )


@dataclass(frozen=True)
class SeatCapture:
    probe: SeatProbe
    auditorium: Auditorium
    source_layout_id: str | None = None
    raw_payload: bytes = b""
    raw_content_type: str = "application/octet-stream"
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_url: str | None = None
    status_code: int | None = None


def seat_static_payload(seat: Seat) -> dict[str, object]:
    """The physical part of a seat, excluding time-varying availability."""
    return {
        "row_label": seat.row_label,
        "row_index": seat.row_index,
        "col_label": seat.col_label,
        "col_index": seat.col_index,
        "kind": seat.kind.value,
        "x": seat.x,
        "y": seat.y,
        "aisle_adjacent": seat.aisle_adjacent,
        "module_id": seat.module_id,
        "module_position": seat.module_position,
        "module_size": seat.module_size,
        "module_required": seat.module_required,
    }


def static_layout_payload(auditorium: Auditorium) -> dict[str, object]:
    """Canonical room geometry used for versioning and deduplication."""
    seats = sorted(
        (seat_static_payload(seat) for seat in auditorium.seats),
        key=lambda item: (
            int(item["row_index"]), int(item["col_index"]),
            str(item["row_label"]), str(item["col_label"]),
        ),
    )
    return {
        "geometry_confidence": auditorium.geometry_confidence,
        "row_lengths": list(auditorium.row_lengths),
        "seats": seats,
    }


def layout_fingerprint(auditorium: Auditorium) -> str:
    payload = json.dumps(
        static_layout_payload(auditorium),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def availability_payload(auditorium: Auditorium) -> dict[str, object]:
    """Live state kept separately from the static layout."""
    return {
        "available": auditorium.available,
        "capacity": auditorium.capacity,
        "seats": [
            {
                "row_label": seat.row_label,
                "col_label": seat.col_label,
                "row_index": seat.row_index,
                "col_index": seat.col_index,
                "status": seat.status.value,
            }
            for seat in sorted(
                auditorium.seats,
                key=lambda item: (item.row_index, item.col_index),
            )
        ],
    }
