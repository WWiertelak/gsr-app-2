"""Server-side PNGs of the report charts (matplotlib), drawn from the ``gsr/charts.py`` data.

The screens draw the same data with ECharts in the browser (``static/gsr/js/charts.js``);
the PDF converter runs no JavaScript, so the report draws its charts here with the
same colours, series and markers.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any, Callable

import numpy as np
from matplotlib import dates as mdates
from matplotlib.colors import LinearSegmentedColormap, to_rgba
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

logger = logging.getLogger(__name__)

AXIS_COLOR = "#8ea8bb"
LABEL_COLOR = "#486581"
GRID_COLOR = "#dce8f0"
EPISODE_AREA_COLOR = to_rgba("#ee9b00", 0.18)
SCIENTIFIC_PALETTE = (
    "#003049", "#0077b6", "#0a9396", "#94d2bd", "#ee9b00", "#bb3e03", "#9b2226", "#4c1d95", "#7c3aed", "#db2777",
    "#be123c", "#ea580c", "#ca8a04", "#65a30d", "#16a34a", "#059669", "#0891b2", "#2563eb", "#4338ca", "#6d28d9",
)
SYNC_CMAP = LinearSegmentedColormap.from_list("sync", ["#264653", "#8ecae6", "#f7fbff", "#f4a261", "#9b2226"])
STATE_CMAP = LinearSegmentedColormap.from_list("states", ["#0077b6", "#f7fbff", "#9b2226"])
DPI = 150
WIDE = (10.0, 4.4)


def _figure(size: tuple[float, float] = WIDE) -> Figure:
    figure = Figure(figsize=size, dpi=DPI, facecolor="white")
    figure.set_layout_engine("constrained")
    return figure


def _style(ax: Any) -> None:
    for spine in ax.spines.values():
        spine.set_color(AXIS_COLOR)
    ax.tick_params(colors=LABEL_COLOR, labelsize=8)
    ax.xaxis.label.set_color(LABEL_COLOR)
    ax.yaxis.label.set_color(LABEL_COLOR)
    ax.grid(True, color=GRID_COLOR, linewidth=0.7)
    ax.set_axisbelow(True)


def _legend(ax: Any, handles: list[Any] | None = None) -> None:
    handles = handles if handles is not None else ax.get_legend_handles_labels()[0]
    if handles:
        ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.01), ncol=min(len(handles), 6),
                  frameon=False, fontsize=8, labelcolor=LABEL_COLOR, borderaxespad=0)


def _png(figure: Figure) -> bytes:
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=DPI, facecolor="white")
    return buffer.getvalue()


def _episode_spans(ax: Any, episodes: list[dict[str, float]], numbered: bool) -> None:
    for index, episode in enumerate(episodes or []):
        ax.axvspan(episode["start"], episode["end"], color=EPISODE_AREA_COLOR, linewidth=0)
        if numbered:
            # Episode number only, alternating top/bottom so adjacent labels don't overlap.
            top = index % 2 == 0
            ax.text((episode["start"] + episode["end"]) / 2, 0.98 if top else 0.02, f"#{index + 1}",
                    transform=ax.get_xaxis_transform(), ha="center", va="top" if top else "bottom",
                    fontsize=7, color="#824c00")


def _time_value(value: Any) -> Any:
    return mdates.date2num(datetime.fromisoformat(value)) if isinstance(value, str) else value


def overlay(data: dict[str, Any], labels: dict[str, str]) -> Figure | None:
    series = [entry for entry in data.get("series") or [] if entry.get("points")]
    if not series:
        return None
    time_axis = data.get("axis") == "time"
    figure = _figure()
    ax = figure.add_subplot()
    _style(ax)
    for entry in series:
        xs = [_time_value(point[0]) if time_axis else point[0] for point in entry["points"]]
        ax.plot(xs, [point[1] for point in entry["points"]], color=entry["color"], linewidth=1.1,
                label=entry["alias"])
    if time_axis:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    else:
        ax.set_xlabel(labels.get("minutes", "min"))
        window = data.get("window") or {}
        for edge in (window.get("from"), window.get("to")):
            if isinstance(edge, (int, float)):
                ax.axvline(edge, color=LABEL_COLOR, linestyle="--", linewidth=0.9)
                ax.text(edge, 0.99, f" {labels.get('timeWindow', '')}", transform=ax.get_xaxis_transform(),
                        va="top", fontsize=7, color=LABEL_COLOR)
    ax.set_ylabel(labels.get("scaled") if data.get("unit") == "scaled" else labels.get("microsiemens", "µS"))
    _legend(ax)
    return figure


def sync_timeline(data: dict[str, Any], labels: dict[str, str]) -> Figure | None:
    centers = data.get("centers") or []
    if not centers:
        return None
    figure = _figure()
    ax = figure.add_subplot()
    _style(ax)
    for index, pair in enumerate(data.get("pairs") or []):
        ax.plot(centers, pair["values"], color=SCIENTIFIC_PALETTE[index % len(SCIENTIFIC_PALETTE)], linewidth=0.8,
                alpha=0.5, label=pair["name"])
    ax.plot(centers, data.get("groupMean") or [], color="#005f73", linewidth=2.4, label=labels.get("groupMean"))
    threshold = data.get("threshold")
    if isinstance(threshold, (int, float)):
        ax.axhline(threshold, color="#bb3e03", linestyle="--", linewidth=1)
        ax.text(1.0, threshold, f"{labels.get('threshold', '')} ", transform=ax.get_yaxis_transform(), ha="right",
                va="bottom", fontsize=7, color="#bb3e03")
    ax.set_ylim(-1, 1)
    ax.set_xlabel(labels.get("minutes", "min"))
    _episode_spans(ax, data.get("episodes") or [], numbered=False)

    counts_ax = ax.twinx()
    active_counts = data.get("activeCounts") or []
    counts = [active_counts[i] if i < len(active_counts) else 0 for i in range(len(centers))]
    counts_ax.step(centers, counts, where="mid", color="#ee9b00", linewidth=1.2, label=labels.get("activeCount"))
    counts_ax.set_ylim(0, max(data.get("aliasCount") or 0, 2))
    counts_ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    counts_ax.tick_params(colors="#ee9b00", labelsize=8)
    for spine in counts_ax.spines.values():
        spine.set_color(AXIS_COLOR)
    _legend(ax, ax.get_legend_handles_labels()[0] + counts_ax.get_legend_handles_labels()[0])
    return figure


def episode_signals(data: dict[str, Any], labels: dict[str, str]) -> Figure | None:
    series = [entry for entry in data.get("series") or [] if entry.get("points")]
    if not series:
        return None
    figure = _figure()
    ax = figure.add_subplot()
    _style(ax)
    for entry in series:
        ax.plot([point[0] for point in entry["points"]], [point[1] for point in entry["points"]],
                color=entry["color"], linewidth=1.0, label=entry["alias"])
    _episode_spans(ax, data.get("episodes") or [], numbered=True)
    ax.set_xlabel(labels.get("minutes", "min"))
    ax.set_ylabel(labels.get("scaled"))
    _legend(ax)
    return figure


def _colorbar(figure: Figure, image: Any, ax: Any) -> Any:
    colorbar = figure.colorbar(image, ax=ax, orientation="horizontal", fraction=0.05, pad=0.08, aspect=40)
    colorbar.ax.tick_params(colors=LABEL_COLOR, labelsize=7)
    colorbar.outline.set_edgecolor(AXIS_COLOR)
    return colorbar


def heatmap(data: dict[str, Any], labels: dict[str, str]) -> Figure | None:
    aliases = data.get("aliases") or []
    matrix = np.array([[np.nan if value is None else value for value in row] for row in data.get("matrix") or []],
                      dtype=float)
    if not aliases or matrix.size == 0:
        return None
    side = min(4.0 + 0.6 * len(aliases), 9.0)
    figure = _figure((side + 1.0, side))
    ax = figure.add_subplot()
    image = ax.imshow(matrix, cmap=SYNC_CMAP, vmin=-1, vmax=1)
    ax.set_xticks(range(len(aliases)), aliases, rotation=20, ha="right")
    ax.set_yticks(range(len(aliases)), aliases)
    ax.tick_params(colors=LABEL_COLOR, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(AXIS_COLOR)
    if len(aliases) <= 16:
        for row, values in enumerate(matrix):
            for column, value in enumerate(values):
                if not np.isnan(value):
                    ax.text(column, row, f"{value:.2f}", ha="center", va="center", fontsize=7, color="#13315c")
    _colorbar(figure, image, ax)
    return figure


def coarousal_states(data: dict[str, Any], labels: dict[str, str]) -> Figure | None:
    rows = data.get("rows") or []
    times = data.get("times") or []
    if not rows or not times:
        return None
    figure = _figure((10.0, min(2.2 + 0.32 * len(rows), 8.0)))
    ax = figure.add_subplot()
    states = np.array([[np.nan if value is None else value for value in row["states"]] for row in rows], dtype=float)
    image = ax.imshow(states, cmap=STATE_CMAP, vmin=-1, vmax=1, aspect="auto", interpolation="nearest")
    step = max(1, len(times) // 20)
    ticks = list(range(0, len(times), step))
    ax.set_xticks(ticks, [f"{times[i]:.0f}" for i in ticks])
    ax.set_yticks(range(len(rows)), [row["alias"] for row in rows])
    ax.set_xlabel(labels.get("timeMin", "min"))
    ax.xaxis.label.set_color(LABEL_COLOR)
    ax.tick_params(colors=LABEL_COLOR, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(AXIS_COLOR)
    colorbar = _colorbar(figure, image, ax)
    colorbar.set_ticks([-1, 0, 1], labels=[labels.get("low", "Low"), "0", labels.get("high", "High")])
    return figure


BUILDERS: dict[str, Callable[[dict[str, Any], dict[str, str]], Figure | None]] = {
    "overlay": overlay,
    "syncTimeline": sync_timeline,
    "episodeSignals": episode_signals,
    "heatmap": heatmap,
    "coarousalStates": coarousal_states,
}


def render_chart_images(report_charts: list[dict[str, Any]], labels: dict[str, str]) -> dict[str, bytes]:
    """PNG per chart key from ``charts.report_charts``; a chart without data or that fails to draw is left out."""
    images: dict[str, bytes] = {}
    for chart in report_charts:
        builder = BUILDERS.get(chart["kind"])
        if builder is None:
            continue
        try:
            figure = builder(chart["data"], labels)
            if figure is not None:
                images[chart["key"]] = _png(figure)
        except Exception:  # one broken chart must not take the whole report down
            logger.exception("Report chart %s could not be drawn", chart["key"])
    return images
