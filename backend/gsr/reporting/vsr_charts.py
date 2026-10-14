"""Server-side SVG charts of the PDF report's VSR interview section.

The report's other charts are PNG captures of the screens' ECharts charts; these two
are drawn here from the stored moments and their signals, in the style of the
Interviews screen's previews, so the section does not depend on the browser capture. Colours are the design tokens'
values (``static/gsr/css/tokens.css``) written out, because WeasyPrint does not
resolve CSS variables inside SVG.
"""

from __future__ import annotations

from typing import Any, Sequence
from xml.sax.saxutils import escape

import numpy as np
from django.utils.formats import number_format

from ..templatetags.gsr_tags import seconds_mmss

FONT = "'Plex Sans Latin', 'Plex Sans Ext', 'DejaVu Sans', sans-serif"
INK, INK_2, INK_3 = "#14212b", "#4a5a68", "#617080"
LINE, LINE_2, SUNKEN = "#e3e7ec", "#cfd6dd", "#f1f3f6"
ACCENT, ACCENT_WEAK = "#0b6e74", "#e5f1f1"
AVERAGE_COLOR = INK_2  # the students' average has no recording colour
HIGH_COLOR, LOW_COLOR = "#b2182b", "#2166ac"  # the reserved High / Low colours (services/results.py)

TICK_STEPS_MINUTES = (1, 2, 5, 10, 15, 20, 30, 60)
MAX_POINTS = 160  # per series in a moment chart


def lesson_ticks(start: float, end: float, max_ticks: int = 8) -> list[dict[str, Any]]:
    """Whole-minute ticks of the lesson timeline (relative seconds), as % positions."""
    span = end - start
    if span <= 0:
        return []
    step = next((step * 60 for step in TICK_STEPS_MINUTES if span / (step * 60) <= max_ticks), 3600)
    first = -(-start // step) * step
    ticks = []
    seconds = first
    while seconds <= end:
        ticks.append({"seconds": seconds, "pct": f"{100.0 * (seconds - start) / span:.2f}"})
        seconds += step
    return ticks


def _text(x: float, y: float, content: str, *, size: float = 7.0, fill: str = INK_3, anchor: str = "start",
          weight: str = "400") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}">{escape(content)}</text>')


def _svg(width: int, height: int, body: Sequence[str], label: str) -> str:
    return (f'<svg class="vsr-chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-label="{escape(label, {chr(34): "&quot;"})}">'
            + "".join(body) + "</svg>")


def lesson_timeline_svg(moments: Sequence[dict[str, Any]], start: float, end: float, label: str) -> str:
    """The lesson with its moments as numbered bars (primary solid, reserve outlined), on a minute axis."""
    width, height, left, right, top = 680, 54, 8, 8, 6
    span = end - start
    track_w = width - left - right
    body = [f'<rect x="{left}" y="{top}" width="{track_w}" height="26" rx="3" fill="{SUNKEN}" stroke="{LINE}" '
            'stroke-width="0.5"/>']

    def x_of(seconds: float) -> float:
        return left + track_w * min(max((seconds - start) / span, 0.0), 1.0) if span > 0 else float(left)

    for tick in lesson_ticks(start, end):
        x = x_of(tick["seconds"])
        body.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top + 26}" stroke="{LINE_2}" stroke-width="0.5"/>')
        body.append(_text(x, top + 38, seconds_mmss(tick["seconds"]), anchor="middle"))
    for moment in moments:
        x0, x1 = x_of(moment["start_seconds"]), x_of(moment["end_seconds"])
        width_px = max(x1 - x0, 3.0)
        primary = moment["status"] == "primary"
        body.append(
            f'<rect x="{x0:.1f}" y="{top + 3}" width="{width_px:.1f}" height="20" rx="2" '
            f'fill="{ACCENT if primary else ACCENT_WEAK}" stroke="{ACCENT}" stroke-width="1"/>'
        )
        body.append(_text(x0 + width_px / 2, top + 17, str(moment["number"]), size=8.0,
                          fill="#ffffff" if primary else ACCENT, anchor="middle", weight="600"))
    return _svg(width, height, body, label)


def _decimate(count: int, limit: int) -> np.ndarray:
    return np.arange(0, count, max(1, int(np.ceil(count / limit))))


def moment_chart_svg(window: dict[str, Any], names: Sequence[str], colors: Sequence[str | None], label: str,
                     number: int | None, high: float | None = None, low: float | None = None) -> str | None:
    """The moment preview of the Interviews step, drawn for the report.

    Both z-scored signals around the moment (``services/vsr.preview``), the moment shaded
    and numbered like an Analysis episode, and the Co-arousal High (``high``) or Low
    (``low``) threshold when the moment is about a shared state. None without signals.
    """
    seconds = np.asarray(window["seconds"], dtype=float)
    if len(seconds) < 2:
        return None
    keep = _decimate(len(seconds), MAX_POINTS)
    t = seconds[keep]
    series = [np.asarray(window["a"], dtype=float)[keep], np.asarray(window["b"], dtype=float)[keep]]
    finite = np.concatenate([values[np.isfinite(values)] for values in series])
    if not len(finite):
        return None
    lo, hi = float(window["low"]), float(window["high"])
    start, end = float(window["start"]), float(window["end"])
    marks = [value for value in (high, low) if value is not None]
    y_low, y_high = min([float(finite.min()), *marks]), max([float(finite.max()), *marks])
    pad = max((y_high - y_low) * 0.08, 0.15)
    y_min, y_max = y_low - pad, y_high + pad

    width, height, left, right, top, bottom = 340, 132, 26, 6, 22, 18
    plot_w, plot_h = width - left - right, height - top - bottom

    def x_of(value: float) -> float:
        return left + plot_w * (value - lo) / (hi - lo) if hi > lo else float(left)

    def y_of(value: float) -> float:
        return top + plot_h * (y_max - value) / (y_max - y_min)

    body = [f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#ffffff" stroke="{LINE}" '
            'stroke-width="0.5"/>']
    shade_x0, shade_x1 = x_of(max(start, lo)), x_of(min(end, hi))
    body.append(f'<rect x="{shade_x0:.1f}" y="{top}" width="{max(shade_x1 - shade_x0, 1.0):.1f}" height="{plot_h}" '
                f'fill="{INK}" fill-opacity="0.07" stroke="{INK}" stroke-opacity="0.28" stroke-width="0.6"/>')
    body.append(_text((shade_x0 + shade_x1) / 2, top + 9, str(number) if number else "\u00d7", size=8.0,
                      fill=INK_2, anchor="middle", weight="600"))
    if y_min < 0 < y_max:
        body.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y_of(0):.1f}" y2="{y_of(0):.1f}" stroke="{LINE_2}" '
                    'stroke-width="0.5"/>')
        body.append(_text(left - 3, y_of(0) + 2.5, "0", anchor="end"))
    for value, color in ((high, HIGH_COLOR), (low, LOW_COLOR)):
        if value is not None:
            body.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y_of(value):.1f}" y2="{y_of(value):.1f}" '
                        f'stroke="{color}" stroke-width="0.8" stroke-dasharray="3 2"/>')
    body.append(_text(left - 3, top + 6, number_format(y_max, 1), anchor="end"))
    body.append(_text(left - 3, top + plot_h, number_format(y_min, 1), anchor="end"))
    for index, values in enumerate(series):
        points = " ".join(f"{x_of(float(x)):.1f},{y_of(float(y)):.1f}" for x, y in zip(t, values) if np.isfinite(y))
        body.append(f'<polyline points="{points}" fill="none" stroke="{colors[index] or AVERAGE_COLOR}" '
                    'stroke-width="1.2" stroke-linejoin="round"/>')
    labelled = -1e9
    for value, anchor in ((lo, "start"), (start, "middle"), (end, "middle"), (hi, "end")):
        if lo <= value <= hi and x_of(value) - labelled >= 30:  # skip a label that would sit on the previous one
            body.append(_text(x_of(value), height - 5, seconds_mmss(value), anchor=anchor))
            labelled = x_of(value)
    cursor = float(left)
    for index, name in enumerate(names):
        color = colors[index] or AVERAGE_COLOR
        body.append(f'<line x1="{cursor:.1f}" x2="{cursor + 10:.1f}" y1="9" y2="9" stroke="{color}" stroke-width="1.8"/>')
        body.append(_text(cursor + 13, 11.5, name, fill=INK_2))
        cursor += 24 + 4.4 * len(name)
    return _svg(width, height, body, label)
