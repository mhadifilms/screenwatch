"""Accessible, self-contained auditorium renderers.

The SVG renderer is the canonical visual surface for seat inventory.  It uses
only normalized auditorium data and embeds its styling, labels, metadata, and
legend, so exactly the same artifact can be shown by the browser app, returned
from HTTP, saved to disk, or emitted by an MCP client.

The coordinate system intentionally follows source row/column indices instead
of packing seats into a rectangle.  Missing columns remain aisles and skipped
rows remain cross-aisles, which lets small repertory rooms, recliner houses,
and very large premium auditoriums share one renderer without losing topology.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from html import escape
from typing import Literal

from .model import Auditorium, Seat, SeatKind, SeatStatus

MAX_GRID_WIDTH = 44

GLYPHS = {
    SeatStatus.AVAILABLE: "·",
    SeatStatus.SOLD: "×",
    SeatStatus.HELD: "◦",
    SeatStatus.UNAVAILABLE: " ",
}
PICK_GLYPH = "▮"


@dataclass(frozen=True)
class Legend:
    lines: tuple[str, ...] = (
        "· free   × sold   ◦ held   ▮ recommended",
    )


@dataclass(frozen=True)
class SvgOptions:
    """Portable visual options shared by HTTP and MCP callers."""

    theme: Literal["dark", "light"] = "dark"
    show_legend: bool = True
    show_labels: bool = True
    title: str | None = None


_THEMES = {
    "dark": {
        "bg": "#0B0D12",
        "panel": "#11151C",
        "text": "#F5F4F1",
        "muted": "#858C98",
        "line": "#242A33",
        "screen": "#E5E7EB",
        "screen_glow": "#C5D5FF",
        "available": "#171C24",
        "available_edge": "#8D98A8",
        "sold": "#262A31",
        "sold_edge": "#30353E",
        "held": "#5C4729",
        "held_edge": "#B78A4A",
        "unavailable": "#14171C",
        "unavailable_edge": "#20242B",
        # Kept stable for clients that key a preview swatch from the original
        # renderer's documented recommendation color.
        "selected": "#f0883e",
        "selected_edge": "#FFB58C",
        "accessible": "#64C7B5",
        "companion": "#8AD4C7",
    },
    "light": {
        "bg": "#F7F8FC",
        "panel": "#FFFFFF",
        "text": "#182033",
        "muted": "#65708A",
        "line": "#D8DEEA",
        "screen": "#5B77C8",
        "screen_glow": "#89A5F5",
        "available": "#FFFFFF",
        "available_edge": "#8390A9",
        "sold": "#B9C1D0",
        "sold_edge": "#98A3B7",
        "held": "#ECD39E",
        "held_edge": "#AC7A29",
        "unavailable": "#E4E7EE",
        "unavailable_edge": "#C8CEDA",
        "selected": "#EF7138",
        "selected_edge": "#C94F19",
        "accessible": "#1676B8",
        "companion": "#3C82B4",
    },
}


def _bucket(seats: list[Seat], width: int) -> list[list[Seat]]:
    """Split a row into at most ``width`` column buckets, preserving order."""
    if len(seats) <= width:
        return [[seat] for seat in seats]
    per = len(seats) / width
    buckets: list[list[Seat]] = [[] for _ in range(width)]
    for index, seat in enumerate(seats):
        buckets[min(int(index / per), width - 1)].append(seat)
    return buckets


def _bucket_glyph(bucket: list[Seat], picked: set[str]) -> str:
    if not bucket:
        return " "
    if any(seat.id in picked for seat in bucket):
        return PICK_GLYPH
    # Worst-status-wins: never let a downsampled bucket look more available
    # than it is.
    for status in (SeatStatus.UNAVAILABLE, SeatStatus.SOLD, SeatStatus.HELD):
        if all(seat.status is status or seat.kind is SeatKind.BLOCKED for seat in bucket):
            return GLYPHS[status]
    if any(seat.is_open for seat in bucket):
        return GLYPHS[SeatStatus.AVAILABLE]
    return GLYPHS[SeatStatus.SOLD]


def to_unicode_grid(
    auditorium: Auditorium,
    picked: set[str] | None = None,
    *,
    width: int = MAX_GRID_WIDTH,
    show_legend: bool = True,
) -> str:
    """Return a compact text seat map for text-only MCP clients."""
    picked = picked or set()
    rows = auditorium.rows()
    if not rows:
        available = auditorium.available
        return (
            f"[no seat map for {auditorium.venue_id}]\n"
            + (f"{available} seats reported available" if available else "availability unknown")
        )

    label_width = max(len(row[0].row_label) for row in rows)
    body_width = min(max(len(row) for row in rows), width)

    out = [
        " " * (label_width + 1) + "┌" + "─" * body_width + "┐",
        " " * (label_width + 1) + "│" + "SCREEN".center(body_width) + "│",
        " " * (label_width + 1) + "└" + "─" * body_width + "┘",
    ]
    for row in rows:
        glyphs = "".join(_bucket_glyph(bucket, picked) for bucket in _bucket(row, width))
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


def _layout(auditorium: Auditorium) -> dict[str, int]:
    seats = [seat for seat in auditorium.seats if seat.kind is not SeatKind.BLOCKED]
    if not seats:
        return {"min_col": 0, "max_col": 0, "min_row": 0, "max_row": 0}
    return {
        "min_col": min(seat.col_index for seat in seats),
        "max_col": max(seat.col_index for seat in seats),
        "min_row": min(seat.row_index for seat in seats),
        "max_row": max(seat.row_index for seat in seats),
    }


def seatmap_document(
    auditorium: Auditorium,
    picked: set[str] | None = None,
    *,
    options: SvgOptions | None = None,
    include_svg: bool = True,
) -> dict:
    """Return the renderer's stable machine-readable contract.

    Consumers that want to add interactions can use the seat records; clients
    that only need a finished visual can display ``svg`` unchanged.
    """
    picked = picked or set()
    options = options or SvgOptions()
    known_ids = {seat.id for seat in auditorium.seats if seat.kind.is_bookable}
    picked = picked & known_ids
    status_counts = Counter(seat.status.value for seat in auditorium.seats if seat.kind.is_bookable)
    kind_counts = Counter(seat.kind.value for seat in auditorium.seats if seat.kind.is_bookable)
    bounds = _layout(auditorium)
    rows = auditorium.rows()
    document = {
        "version": 1,
        "auditorium": {
            "venue_id": auditorium.venue_id,
            "screen_id": auditorium.screen_id,
            "name": auditorium.name,
            "geometry_confidence": auditorium.geometry_confidence,
        },
        "summary": {
            "capacity": auditorium.capacity,
            "available": auditorium.available,
            "occupancy": round(auditorium.occupancy, 4),
            "selected": len(picked),
            "rows": auditorium.row_count,
            "statuses": dict(sorted(status_counts.items())),
            "kinds": dict(sorted(kind_counts.items())),
        },
        "layout": {
            "columns": max(0, bounds["max_col"] - bounds["min_col"] + 1),
            "row_span": max(0, bounds["max_row"] - bounds["min_row"] + 1),
            "bounds": bounds,
        },
        "legend": [
            {"key": "available", "label": "Available"},
            {"key": "sold", "label": "Taken"},
            {"key": "held", "label": "Temporarily held"},
            {"key": "selected", "label": "Recommended"},
            {"key": "wheelchair", "label": "Wheelchair space"},
            {"key": "companion", "label": "Companion seat"},
        ],
        "rows": [
            {
                "label": row[0].row_label,
                "index": row[0].row_index,
                "seat_ids": [seat.id for seat in row if seat.kind.is_bookable],
            }
            for row in rows
        ],
        "seats": [
            {
                "id": seat.id,
                "row_label": seat.row_label,
                "row_index": seat.row_index,
                "col_label": seat.col_label,
                "col_index": seat.col_index,
                "status": seat.status.value,
                "kind": seat.kind.value,
                "selected": seat.id in picked,
                "aisle_adjacent": seat.aisle_adjacent,
                "module_id": seat.module_id,
                "module_position": seat.module_position,
                "module_size": seat.module_size,
                "module_required": seat.module_required,
                "x": seat.x,
                "y": seat.y,
            }
            for seat in auditorium.seats
            if seat.kind.is_bookable
        ],
    }
    if include_svg:
        document["svg"] = to_svg(auditorium, picked, options=options)
    return document


def _no_map_svg(auditorium: Auditorium, options: SvgOptions) -> str:
    colors = _THEMES[options.theme]
    title = escape(options.title or auditorium.name or "Seat map unavailable")
    message = (
        f"{auditorium.available} seats reported available"
        if auditorium.available else "Live seat positions are not published"
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 220" '
        'role="img" aria-labelledby="seatmap-title seatmap-desc" '
        'preserveAspectRatio="xMidYMid meet">'
        f'<title id="seatmap-title">{title}</title>'
        f'<desc id="seatmap-desc">{escape(message)}</desc>'
        f'<rect width="640" height="220" rx="24" fill="{colors["bg"]}"/>'
        f'<path d="M150 61 Q320 20 490 61" fill="none" stroke="{colors["screen"]}" '
        'stroke-width="7" stroke-linecap="round"/>'
        f'<text x="320" y="118" text-anchor="middle" fill="{colors["text"]}" '
        'font-family="Inter,system-ui,sans-serif" font-size="20" font-weight="650">'
        'Seat positions unavailable</text>'
        f'<text x="320" y="151" text-anchor="middle" fill="{colors["muted"]}" '
        f'font-family="Inter,system-ui,sans-serif" font-size="13">{escape(message)}</text>'
        '</svg>'
    )


def _seat_label(seat: Seat, selected: bool) -> str:
    status = {
        SeatStatus.AVAILABLE: "available",
        SeatStatus.SOLD: "taken",
        SeatStatus.HELD: "temporarily held",
        SeatStatus.UNAVAILABLE: "unavailable",
    }[seat.status]
    kind = seat.kind.value.replace("_", " ")
    recommendation = ", recommended for your party" if selected else ""
    return f"Seat {seat.id}, {kind}, {status}{recommendation}"


def to_svg(
    auditorium: Auditorium,
    picked: set[str] | None = None,
    *,
    seat_px: int = 18,
    gap_px: int = 6,
    options: SvgOptions | None = None,
) -> str:
    """Render a responsive, self-contained, accessible auditorium SVG.

    Seat and row source coordinates are preserved. Every seat group carries
    semantic ``data-*`` attributes and an accessible label, while the visual
    itself contains no script or external references.
    """
    picked = picked or set()
    options = options or SvgOptions()
    colors = _THEMES[options.theme]
    rows = auditorium.rows()
    if not rows:
        return _no_map_svg(auditorium, options)

    seat_px = max(10, min(int(seat_px), 32))
    gap_px = max(2, min(int(gap_px), 16))
    pitch = seat_px + gap_px
    bounds = _layout(auditorium)
    col_span = bounds["max_col"] - bounds["min_col"] + 1
    row_span = bounds["max_row"] - bounds["min_row"] + 1
    label_gutter = 28 if options.show_labels else 12
    right_gutter = label_gutter
    top = 88
    legend_height = 52 if options.show_legend else 18
    # The floor gives small rooms a deliberate composition, while source
    # coordinates—not a packed grid—still determine the width of large rooms.
    map_width = max(col_span * pitch - gap_px, 420)
    width = label_gutter + map_width + right_gutter
    height = top + max(row_span * pitch - gap_px, seat_px) + legend_height
    map_left = label_gutter + (map_width - (col_span * pitch - gap_px)) / 2
    title_text = options.title or auditorium.name or f"Screen {auditorium.screen_id}"
    available_percent = (
        round((auditorium.available / auditorium.capacity) * 100)
        if auditorium.capacity else 0
    )

    css = f"""
      .label{{font:650 8px Inter,system-ui,sans-serif;fill:{colors['muted']};opacity:.8}}
      .seat{{transition:opacity .15s ease}} .seat-base{{stroke-width:1.15}}
      .available .seat-base{{fill:{colors['available']};stroke:{colors['available_edge']}}}
      .sold .seat-base{{fill:{colors['sold']};stroke:{colors['sold_edge']}}}
      .held .seat-base{{fill:{colors['held']};stroke:{colors['held_edge']}}}
      .unavailable .seat-base{{fill:{colors['unavailable']};stroke:{colors['unavailable_edge']}}}
      .selected .seat-base{{fill:{colors['selected']};stroke:{colors['selected_edge']};
        stroke-width:1.6}}
      .sold{{opacity:.58}}.unavailable{{opacity:.38}}
      .selected-ring{{fill:none;stroke:{colors['selected']};stroke-width:1.25;opacity:.36}}
      .seat-detail{{fill:none;stroke:currentColor;stroke-width:1;stroke-linecap:round;
        stroke-linejoin:round;opacity:.78}}
      .seat-cushion{{fill:none;stroke:currentColor;stroke-width:1.15;stroke-linecap:round}}
      .available{{color:{colors['available_edge']}}}.sold{{color:{colors['sold_edge']}}}
      .held{{color:{colors['held_edge']}}}.selected{{color:{colors['selected_edge']}}}
      .accessible-mark{{fill:none;stroke:{colors['accessible']};stroke-width:1.35;
        stroke-linecap:round;stroke-linejoin:round}}
      .companion-mark{{fill:{colors['companion']}}}
      .companion-letter{{font:700 5px Inter,system-ui,sans-serif;fill:{colors['bg']}}}
      .module-link{{fill:{colors['line']}}}
      .legend-text{{font:550 8px Inter,system-ui,sans-serif;fill:{colors['muted']}}}
      .heading{{font:680 11px Inter,system-ui,sans-serif;fill:{colors['text']};
        letter-spacing:-.15px}}
      .subheading{{font:500 8px Inter,system-ui,sans-serif;fill:{colors['muted']}}}
      .screen-label{{font:650 7px Inter,system-ui,sans-serif;fill:{colors['muted']};
        letter-spacing:2.2px}}
    """

    venue_title = escape(title_text)
    description = (
        f"{auditorium.row_count} rows, {auditorium.capacity} seats, "
        f"{auditorium.available} available. Recommended seats are highlighted."
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height:g}" '
        'role="img" aria-labelledby="seatmap-title seatmap-desc" '
        'preserveAspectRatio="xMidYMid meet" data-seatmap-version="1">',
        f'<title id="seatmap-title">{venue_title}</title>',
        f'<desc id="seatmap-desc">{escape(description)}</desc>',
        "<defs>",
        f'<filter id="screen-glow" x="-30%" y="-200%" width="160%" height="500%">'
        f'<feGaussianBlur stdDeviation="5" flood-color="{colors["screen_glow"]}"/></filter>',
        '<linearGradient id="screen-line" x1="0" x2="1">'
        f'<stop stop-color="{colors["screen"]}" stop-opacity=".1"/>'
        f'<stop offset=".5" stop-color="{colors["screen"]}"/>'
        f'<stop offset="1" stop-color="{colors["screen"]}" stop-opacity=".1"/>'
        '</linearGradient>',
        f"<style>{css}</style>",
        "</defs>",
        f'<rect x=".5" y=".5" width="{width - 1:g}" height="{height - 1:g}" rx="13" '
        f'fill="{colors["bg"]}" stroke="{colors["line"]}"/>',
        f'<text class="heading" x="{width / 2:g}" y="18" text-anchor="middle">{venue_title}</text>',
        f'<text class="subheading" x="{width / 2:g}" y="32" text-anchor="middle">'
        f'{auditorium.available} open · {available_percent}% available · '
        f'{auditorium.row_count} rows</text>',
        f'<path d="M{width * .16:g} 61 Q{width / 2:g} 44 {width * .84:g} 61" '
        f'fill="none" stroke="{colors["screen_glow"]}" stroke-width="8" opacity=".1" '
        'filter="url(#screen-glow)"/>',
        f'<path d="M{width * .16:g} 61 Q{width / 2:g} 44 {width * .84:g} 61" '
        'fill="none" stroke="url(#screen-line)" stroke-width="2.5" stroke-linecap="round"/>',
        f'<text class="screen-label" x="{width / 2:g}" y="76" '
        'text-anchor="middle">SCREEN</text>',
    ]

    # Physical modules receive a quiet connector behind their component seats.
    modules: dict[str, list[Seat]] = {}
    for seat in auditorium.seats:
        if seat.module_id and seat.kind is not SeatKind.BLOCKED:
            modules.setdefault(seat.module_id, []).append(seat)
    for module in modules.values():
        module = sorted(module, key=lambda seat: seat.col_index)
        if len(module) < 2 or len({seat.row_index for seat in module}) != 1:
            continue
        first, last = module[0], module[-1]
        x = map_left + (first.col_index - bounds["min_col"]) * pitch - 2
        y = top + (first.row_index - bounds["min_row"]) * pitch + seat_px * .28
        connector_width = (last.col_index - first.col_index) * pitch + seat_px + 4
        parts.append(
            f'<rect class="module-link" x="{x:g}" y="{y:g}" width="{connector_width:g}" '
            f'height="{seat_px * .56:g}" rx="{seat_px * .25:g}" '
            f'data-module-id="{escape(first.module_id or "", quote=True)}"/>'
        )

    for row in rows:
        row_y = top + (row[0].row_index - bounds["min_row"]) * pitch
        if options.show_labels:
            row_label = escape(row[0].row_label)
            parts.extend([
                f'<text class="label" x="{label_gutter - 8:g}" y="{row_y + seat_px * .57:g}" '
                f'text-anchor="end">{row_label}</text>',
                f'<text class="label" x="{width - right_gutter + 9:g}" '
                f'y="{row_y + seat_px * .57:g}">{row_label}</text>',
            ])
        for seat in row:
            if seat.kind is SeatKind.BLOCKED:
                continue
            x = map_left + (seat.col_index - bounds["min_col"]) * pitch
            y = row_y
            selected = seat.id in picked
            classes = f"seat {seat.status.value}{' selected' if selected else ''} {seat.kind.value}"
            seat_id = escape(seat.id, quote=True)
            label = escape(_seat_label(seat, selected), quote=True)
            rx = seat_px * (.3 if seat.kind in {SeatKind.RECLINER, SeatKind.LOVESEAT} else .22)
            if selected:
                parts.append(
                    f'<rect class="selected-ring" x="{x - 2.25:g}" y="{y - 2.25:g}" '
                    f'width="{seat_px + 4.5:g}" height="{seat_px * .86 + 4.5:g}" '
                    f'rx="{rx + 2.25:g}"/>'
                )
            parts.append(
                f'<g class="{classes}" role="img" aria-label="{label}" tabindex="-1" '
                f'data-seat-id="{seat_id}" data-status="{seat.status.value}" '
                f'data-kind="{seat.kind.value}" data-selected="{str(selected).lower()}">'
                f'<title>{escape(_seat_label(seat, selected))}</title>'
            )
            if seat.kind is SeatKind.WHEELCHAIR:
                parts.append(
                    f'<rect class="seat-base" x="{x:g}" y="{y:g}" width="{seat_px:g}" '
                    f'height="{seat_px * .78:g}" rx="{seat_px * .22:g}"/>'
                    f'<circle class="accessible-mark" cx="{x + seat_px * .43:g}" '
                    f'cy="{y + seat_px * .19:g}" r="{seat_px * .075:g}"/>'
                    f'<path class="accessible-mark" d="M{x + seat_px * .42:g} '
                    f'{y + seat_px * .31:g}v{seat_px * .23:g}h{seat_px * .22:g}'
                    f'M{x + seat_px * .42:g} {y + seat_px * .42:g}'
                    f'l{-seat_px * .14:g} {seat_px * .23:g}'
                    f'M{x + seat_px * .31:g} {y + seat_px * .46:g}'
                    f'a{seat_px * .2:g} {seat_px * .2:g} 0 1 0 '
                    f'{seat_px * .33:g} {seat_px * .18:g}"/>'
                )
            else:
                parts.append(
                    f'<rect class="seat-base" x="{x + seat_px * .08:g}" y="{y:g}" '
                    f'width="{seat_px * .84:g}" height="{seat_px * .58:g}" rx="{rx:g}"/>'
                    f'<path class="seat-cushion" d="M{x + seat_px * .17:g} {y + seat_px * .5:g}'
                    f'v{seat_px * .13:g}q0 {seat_px * .11:g} {seat_px * .11:g} '
                    f'{seat_px * .11:g}h{seat_px * .44:g}q{seat_px * .11:g} 0 '
                    f'{seat_px * .11:g} {-seat_px * .11:g}v{-seat_px * .13:g}"/>'
                    f'<path class="seat-detail" d="M{x + seat_px * .12:g} {y + seat_px * .45:g}'
                    f'v{seat_px * .18:g}M{x + seat_px * .88:g} {y + seat_px * .45:g}'
                    f'v{seat_px * .18:g}" opacity=".55"/>'
                )
            if seat.status is SeatStatus.HELD:
                parts.append(
                    f'<circle cx="{x + seat_px / 2:g}" cy="{y + seat_px * .29:g}" '
                    f'r="{seat_px * .105:g}" fill="none" stroke="currentColor" '
                    'stroke-width="1.1"/>'
                )
            if seat.kind is SeatKind.COMPANION:
                parts.append(
                    f'<circle class="companion-mark" cx="{x + seat_px * .79:g}" '
                    f'cy="{y + seat_px * .12:g}" r="{max(1.8, seat_px * .13):g}"/>'
                    f'<text class="companion-letter" x="{x + seat_px * .79:g}" '
                    f'y="{y + seat_px * .205:g}" text-anchor="middle">C</text>'
                )
            elif seat.kind is SeatKind.RECLINER:
                parts.append(
                    f'<path class="seat-detail" d="M{x + seat_px * .3:g} {y + seat_px * .78:g}'
                    f'h{seat_px * .4:g}M{x + seat_px * .69:g} {y + seat_px * .63:g}'
                    f'l{seat_px * .13:g} {seat_px * .15:g}"/>'
                )
            elif seat.kind is SeatKind.LOVESEAT:
                parts.append(
                    f'<path class="seat-detail" d="M{x + seat_px * .5:g} {y + seat_px * .08:g}'
                    f'v{seat_px * .58:g}" opacity=".28"/>'
                )
            parts.append("</g>")

    if options.show_legend:
        legend_y = height - 24
        legend_items = [
            (colors["available"], colors["available_edge"], "Open"),
            (colors["sold"], colors["sold_edge"], "Taken"),
            (colors["held"], colors["held_edge"], "Held"),
            (colors["selected"], colors["selected_edge"], "Your seats"),
            ("none", colors["accessible"], "Accessible"),
        ]
        item_width = min(86, width / len(legend_items))
        legend_left = (width - item_width * len(legend_items)) / 2
        for index, (fill, stroke, label) in enumerate(legend_items):
            center = legend_left + item_width * index + 7
            parts.extend([
                f'<rect x="{center:g}" y="{legend_y - 6:g}" width="8" height="7" '
                f'rx="2" fill="{fill}" stroke="{stroke}" stroke-width="1"/>',
                f'<text class="legend-text" x="{center + 13:g}" y="{legend_y:g}">{label}</text>',
            ])

    parts.append("</svg>")
    return "".join(parts)


def build_auditorium(
    venue_id: str,
    screen_id: str,
    layout: list[str],
    *,
    row_labels: str | None = None,
) -> Auditorium:
    """Build an auditorium from an ASCII sketch for tests and API examples.

    ``.`` free, ``×``/``x`` sold, ``o`` held, a space is a structural gap,
    ``#`` blocked, ``w`` wheelchair, ``c`` companion, ``r`` recliner, and
    ``l`` loveseat.
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
    for row_index, line in enumerate(layout):
        for col_index, character in enumerate(line):
            if character == " ":
                continue
            status, kind = codes.get(character.lower(), codes["."])
            seats.append(
                Seat(
                    row_label=labels[row_index % len(labels)],
                    row_index=row_index,
                    col_label=str(col_index + 1),
                    col_index=col_index,
                    status=status,
                    kind=kind,
                )
            )
    return Auditorium(
        venue_id=venue_id,
        screen_id=screen_id,
        seats=normalize_geometry(mark_aisles(infer_modules(seats))),
    )
