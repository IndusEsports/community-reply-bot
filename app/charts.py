"""Small inline-SVG chart builders for /analytics — no JS, no client fetch,
server-rendered straight into Jinja templates. Colors follow the dataviz skill's
validated reference palette (dark-mode steps), not ad-hoc choices:
  - status roles (good/warning/serious/critical) for the daily volume chart,
    since sent/held/failed/ignored are states, not arbitrary categories
  - the fixed 8-slot categorical order for sentiment (identity, not magnitude)
  - a single flat hue for the top-categories ranking (magnitude only, one series)
"""
from __future__ import annotations

STATUS_COLORS = {
    "sent": "#0ca30c",     # good
    "held": "#fab219",     # warning
    "failed": "#d03b3b",   # critical
    "ignored": "#898781",  # muted/neutral — not a status role, just "nothing happened"
}

CATEGORICAL_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

INK_PRIMARY = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRIDLINE = "#2c2c2a"


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def daily_volume_chart(rows: list[dict], width: int = 640, height: int = 220) -> str:
    """rows: [{"day": "2026-01-01", "sent": 3, "held": 1, "failed": 0, "ignored": 5}, ...]"""
    if not rows:
        return '<p class="empty">Not enough data yet.</p>'

    pad_left, pad_bottom, pad_top = 32, 24, 12
    plot_w = width - pad_left - 12
    plot_h = height - pad_bottom - pad_top
    keys = ["sent", "held", "failed", "ignored"]
    totals = [sum(r.get(k, 0) for k in keys) for r in rows]
    max_total = max(totals) or 1
    bar_w = plot_w / len(rows) * 0.6
    gap = plot_w / len(rows)

    bars = []
    for i, row in enumerate(rows):
        x = pad_left + i * gap + (gap - bar_w) / 2
        y_cursor = pad_top + plot_h
        for k in keys:
            v = row.get(k, 0)
            if not v:
                continue
            seg_h = (v / max_total) * plot_h
            y = y_cursor - seg_h
            title = f"{row['day']} · {k}: {v}"
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(seg_h - 1, 0):.1f}" '
                f'fill="{STATUS_COLORS[k]}" rx="2"><title>{_esc(title)}</title></rect>'
            )
            y_cursor = y - 2  # 2px surface gap between stacked segments

    # x-axis labels: first, middle, last day only (avoid label collision)
    label_idxs = sorted({0, len(rows) // 2, len(rows) - 1})
    labels = []
    for i in label_idxs:
        x = pad_left + i * gap + gap / 2
        labels.append(
            f'<text x="{x:.1f}" y="{height - 6}" fill="{INK_MUTED}" font-size="10" text-anchor="middle">{rows[i]["day"][5:]}</text>'
        )

    baseline = f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{width - 12}" y2="{pad_top + plot_h}" stroke="{GRIDLINE}" stroke-width="1"/>'

    legend_items = []
    for i, k in enumerate(keys):
        lx = pad_left + i * 90
        legend_items.append(
            f'<rect x="{lx}" y="0" width="10" height="10" rx="2" fill="{STATUS_COLORS[k]}"/>'
            f'<text x="{lx + 14}" y="9" fill="{INK_SECONDARY}" font-size="11">{k}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height + 20}" width="100%" style="max-width:{width}px" role="img" aria-label="Daily volume">'
        f'<g transform="translate(0,20)">{baseline}{"".join(bars)}{"".join(labels)}</g>'
        f'<g>{"".join(legend_items)}</g>'
        f"</svg>"
    )


def categorical_bar_list(rows: list[tuple[str, int]], width: int = 420) -> str:
    """rows: [(label, count), ...], already sorted by whatever order you want shown."""
    if not rows:
        return '<p class="empty">Not enough data yet.</p>'
    rows = rows[:8]  # fixed 8-slot categorical order — fold anything past this into "Other" upstream
    max_n = max(n for _, n in rows) or 1
    row_h = 26
    label_w = 110
    bar_area = width - label_w - 40
    height = row_h * len(rows)

    parts = []
    for i, (label, n) in enumerate(rows):
        y = i * row_h
        bar_w = (n / max_n) * bar_area
        color = CATEGORICAL_DARK[i % len(CATEGORICAL_DARK)]
        parts.append(
            f'<text x="0" y="{y + row_h / 2 + 4}" fill="{INK_SECONDARY}" font-size="12">{_esc(label)}</text>'
            f'<rect x="{label_w}" y="{y + 4}" width="{bar_w:.1f}" height="{row_h - 10}" rx="3" fill="{color}">'
            f"<title>{_esc(label)}: {n}</title></rect>"
            f'<text x="{label_w + bar_w + 6:.1f}" y="{y + row_h / 2 + 4}" fill="{INK_PRIMARY}" font-size="12">{n}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" role="img" aria-label="Category breakdown">'
        f"{''.join(parts)}</svg>"
    )


def magnitude_bar_list(rows: list[tuple[str, int]], width: int = 420) -> str:
    """Single-hue ranked bars (e.g. top categories) — one series, no categorical
    identity needed, just magnitude + a direct label."""
    if not rows:
        return '<p class="empty">Not enough data yet.</p>'
    max_n = max(n for _, n in rows) or 1
    row_h = 24
    label_w = 130
    bar_area = width - label_w - 40
    height = row_h * len(rows)
    color = CATEGORICAL_DARK[0]

    parts = []
    for i, (label, n) in enumerate(rows):
        y = i * row_h
        bar_w = (n / max_n) * bar_area
        parts.append(
            f'<text x="0" y="{y + row_h / 2 + 4}" fill="{INK_SECONDARY}" font-size="12">{_esc(label)}</text>'
            f'<rect x="{label_w}" y="{y + 4}" width="{bar_w:.1f}" height="{row_h - 10}" rx="3" fill="{color}">'
            f"<title>{_esc(label)}: {n}</title></rect>"
            f'<text x="{label_w + bar_w + 6:.1f}" y="{y + row_h / 2 + 4}" fill="{INK_PRIMARY}" font-size="12">{n}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" role="img" aria-label="Top categories">'
        f"{''.join(parts)}</svg>"
    )
