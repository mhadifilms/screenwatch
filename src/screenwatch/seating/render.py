"""Rendering auditoriums, normalized across wildly different room sizes.

Both renderers read `x`/`y` and row/column order only, so a 500-seat IMAX and
a 40-seat microcinema come out legible with the same code. The IMAX case is
the one that forces the design: at 40 seats per row a character grid is wider
than any terminal, so wide rooms are downsampled by column with the *worst*
status in each bucket winning - a bucket containing one free seat among three
sold ones must not read as free.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Auditorium, Seat, SeatKind, SeatStatus

MAX_GRID_WIDTH = 44

GLYPHS = {
    SeatStatus.AVAILABLE: "·",
    SeatStatus.SOLD: "×",
    SeatStatus.HELD: "◦",
    SeatStatus.UNAVAILABLE: " ",
}
PICK_GLYPH = "▮"
ACCESSIBLE_GLYPH = "♿"


@dataclass(frozen=True)
class Legend:
    lines: tuple[str, ...] = (
        "· free   × sold   ◦ held   ▮ your seats",
    )


def _bucket(seats: list[Seat], width: int) -> list[list[Seat]]:
    """Split a row into at most `width` column buckets, preserving order."""
    if len(seats) <= width:
        return [[s] for s in seats]
    per = len(seats) / width
    buckets: list[list[Seat]] = [[] for _ in range(width)]
    for i, seat in enumerate(seats):
        buckets[min(int(i / per), width - 1)].append(seat)
    return buckets


def _bucket_glyph(bucket: list[Seat], picked: set[str]) -> str:
    if not bucket:
        return " "
    if any(s.id in picked for s in bucket):
        return PICK_GLYPH
    # Worst-status-wins: never let a downsampled bucket look more available
    # than it is.
    for status in (SeatStatus.UNAVAILABLE, SeatStatus.SOLD, SeatStatus.HELD):
        if all(s.status is status or s.kind is SeatKind.BLOCKED for s in bucket):
            return GLYPHS[status]
    if any(s.is_open for s in bucket):
        return GLYPHS[SeatStatus.AVAILABLE]
    return GLYPHS[SeatStatus.SOLD]


def to_unicode_grid(
    auditorium: Auditorium,
    picked: set[str] | None = None,
    *,
    width: int = MAX_GRID_WIDTH,
    show_legend: bool = True,
) -> str:
    """Compact text seat map. The default rendering for MCP clients."""
    picked = picked or set()
    rows = auditorium.rows()
    if not rows:
        available = auditorium.available
        return (
            f"[no seat map for {auditorium.venue_id}]\n"
            + (f"{available} seats reported available" if available else "availability unknown")
        )

    label_width = max(len(r[0].row_label) for r in rows)
    body_width = min(max(len(r) for r in rows), width)

    out = [
        " " * (label_width + 1) + "┌" + "─" * body_width + "┐",
        " " * (label_width + 1) + "│" + "SCREEN".center(body_width) + "│",
        " " * (label_width + 1) + "└" + "─" * body_width + "┘",
    ]
    for row in rows:
        glyphs = "".join(_bucket_glyph(b, picked) for b in _bucket(row, width))
        out.append(f"{row[0].row_label:>{label_width}} {glyphs}")

    if show_legend:
        out.append("")
        out.extend(Legend().lines)
        out.append(
            f"{auditorium.available}/{auditorium.capacity} free"
            + (f"  ·  {int(auditorium.occupancy * 100)}% full" if auditorium.capacity else "")
        )
        if len(max(rows, key=len)) > width:
            out.append(f"(downsampled to {width} columns)")
    return "\n".join(out)


def to_svg(
    auditorium: Auditorium,
    picked: set[str] | None = None,
    *,
    seat_px: int = 14,
    gap_px: int = 3,
) -> str:
    """Self-contained SVG. No external refs, so it embeds anywhere.

    Uses a viewBox and no fixed pixel size, so the same markup scales from a
    thumbnail in a chat client to a full-page view.
    """
    picked = picked or set()
    rows = auditorium.rows()
    if not rows:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 60" role="img">'
            '<title>No seat map available</title>'
            '<text x="120" y="34" text-anchor="middle" font-family="sans-serif" '
            f'font-size="12" fill="#888">no seat map · {auditorium.available} free</text></svg>'
        )

    pitch = seat_px + gap_px
    cols = max(len(r) for r in rows)
    w = cols * pitch + gap_px
    h = len(rows) * pitch + 52

    fill = {
        SeatStatus.AVAILABLE: "#3fb950",
        SeatStatus.SOLD: "#30363d",
        SeatStatus.HELD: "#9e6a03",
        SeatStatus.UNAVAILABLE: "#161b22",
    }

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img">',
        f'<title>{auditorium.venue_id} screen {auditorium.screen_id} — '
        f'{auditorium.available} of {auditorium.capacity} free</title>',
        f'<rect x="{gap_px}" y="6" width="{w - 2 * gap_px}" height="14" rx="7" fill="#58a6ff"/>',
        f'<text x="{w / 2}" y="17" text-anchor="middle" font-family="sans-serif" '
        f'font-size="9" fill="#0d1117">SCREEN</text>',
    ]

    for r, row in enumerate(rows):
        y = 32 + r * pitch
        for seat in row:
            if seat.kind is SeatKind.BLOCKED:
                continue
            x = gap_px + seat.col_index * pitch
            colour = "#f0883e" if seat.id in picked else fill[seat.status]
            stroke = ' stroke="#ffffff" stroke-width="1.5"' if seat.id in picked else ""
            parts.append(
                f'<rect x="{x}" y="{y}" width="{seat_px}" height="{seat_px}" rx="3" '
                f'fill="{colour}"{stroke}><title>{seat.id}</title></rect>'
            )
        parts.append(
            f'<text x="1" y="{y + seat_px - 3}" font-family="sans-serif" font-size="8" '
            f'fill="#8b949e">{row[0].row_label}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def build_auditorium(
    venue_id: str,
    screen_id: str,
    layout: list[str],
    *,
    row_labels: str | None = None,
) -> Auditorium:
    """Build an Auditorium from an ASCII sketch. For tests and fixtures.

        build_auditorium("v", "1", [
            "..××..",
            "......",
        ])

    `.` free, `×`/`x` sold, `o` held, ` ` structural gap, `#` blocked,
    `w` wheelchair space, `c` companion seat, `r` recliner, `l` loveseat - all free.
    """
    from .model import Seat, infer_modules, mark_aisles, normalize_geometry

    codes = {
        ".": (SeatStatus.AVAILABLE, SeatKind.STANDARD),
        "x": (SeatStatus.SOLD, SeatKind.STANDARD),
        "×": (SeatStatus.SOLD, SeatKind.STANDARD),
        "o": (SeatStatus.HELD, SeatKind.STANDARD),
        "◦": (SeatStatus.HELD, SeatKind.STANDARD),
        "#": (SeatStatus.UNAVAILABLE, SeatKind.BLOCKED),
        "w": (SeatStatus.AVAILABLE, SeatKind.WHEELCHAIR),
        "c": (SeatStatus.AVAILABLE, SeatKind.COMPANION),
        "r": (SeatStatus.AVAILABLE, SeatKind.RECLINER),
        "l": (SeatStatus.AVAILABLE, SeatKind.LOVESEAT),
    }
    labels = row_labels or "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    seats: list[Seat] = []
    for r, line in enumerate(layout):
        for c, ch in enumerate(line):
            if ch == " ":
                continue          # a real gap: aisle or missing seat
            status, kind = codes[ch.lower()] if ch.lower() in codes else codes["."]
            seats.append(
                Seat(
                    row_label=labels[r % len(labels)],
                    row_index=r,
                    col_label=str(c + 1),
                    col_index=c,
                    status=status,
                    kind=kind,
                )
            )
    return Auditorium(
        venue_id=venue_id,
        screen_id=screen_id,
        seats=normalize_geometry(mark_aisles(infer_modules(seats))),
    )
