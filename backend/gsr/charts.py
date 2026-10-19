"""Chart data for the ECharts option builders in ``static/gsr/js/charts.js``.

Each chart on a page is ``<div data-chart="<kind>">`` plus its data in a
``json_script`` element; the browser builds the ECharts option from both. The
same data feeds the off-screen charts the Export screen captures for the PDF.
"""

from __future__ import annotations

import math
from typing import Any

from django.utils.translation import gettext as _

from .pipeline.common import clock_seconds_of_day, clock_start
from .services.display import MINMAX, ZSCORE
from .services.results import FALLBACK_SIGNAL_COLOR


def chart_labels() -> dict[str, str]:
    """Translated strings the chart builders show (series names, axis names, legends)."""
    return {
        "minutes": _("min"),
        "microsiemens": "µS",
        "scaled": _("scaled"),
        "zscore": "z",
        "zscoreScale": _("z-score"),
        "minmaxScale": _("min-max (0–1)"),
        "raw": _("raw"),
        "clean": _("clean"),
        "clipped": _("clipped (off scale)"),
        "timeWindow": _("Time window"),
        "high": _("High ↑"),
        "low": _("Low ↓"),
        "comove": _("Co-move ↔"),
        "episodes": _("Episodes"),
        "state": _("State"),
        "clock": _("Sensor clock"),
        "lesson": _("lesson"),
        "savePng": _("Save PNG"),
        "candidatePrimary": _("primary"),
        "candidateReserve": _("reserve"),
        "candidateOther": _("other candidate"),
        "candidateRejected": _("rejected"),
    }


def _points(entries: list[dict[str, Any]]) -> list[list[Any]]:
    return [[point["x"], point["y"]] for point in entries]


def preview_clock(preview: dict[str, Any]) -> float | None:
    """The recordings' clock at lesson time 0 as seconds after midnight: the charts' clock axis origin.

    The charts draw lesson time on the recordings' own clock and show the lesson time next to it in the tooltip.
    """
    return clock_seconds_of_day(clock_start(preview.get("alignment")))


def raw_signal(alias: str, color: str, points: list[list[Any]]) -> dict[str, Any]:
    return {"alias": alias, "color": color, "points": points}


def overlay(series: list[dict[str, Any]], *, window: dict[str, Any] | None = None, axis: str = "value",
            unit: str = "microsiemens", clock: float | None = None) -> dict[str, Any]:
    """``axis`` "time" takes clock timestamps; "value" takes lesson minutes, drawn on the clock from ``clock``."""
    return {"series": series, "window": window, "axis": axis, "unit": unit, "clock_start": clock}


def cleaning_charts(preview: dict[str, Any], colors: dict[str, str], config: dict[str, Any]) -> dict[str, Any]:
    window = {"from": config.get("time_range_from"), "to": config.get("time_range_to")}
    clock = preview_clock(preview)
    per_signal = []
    for entry in preview.get("datasets", []):
        color = colors.get(entry["alias"], FALLBACK_SIGNAL_COLOR)
        per_signal.append(
            {
                "alias": entry["alias"],
                "color": color,
                "raw": entry.get("synced", []),
                "clean": _points(entry.get("clean", [])),
                "window": window,
                "clock_start": clock,
            }
        )
    clean_overlay = overlay(
        [{"alias": item["alias"], "color": item["color"], "points": item["clean"]} for item in per_signal],
        window=window,
        clock=clock,
    )
    return {"per_signal": per_signal, "overlay": clean_overlay}


def raw_overlay_from_preview(preview: dict[str, Any], colors: dict[str, str]) -> dict[str, Any]:
    series = [
        {"alias": entry["alias"], "color": colors.get(entry["alias"], FALLBACK_SIGNAL_COLOR), "points": _points(entry["raw"])}
        for entry in preview.get("datasets", [])
    ]
    return overlay(series, axis="time")


def clean_overlay_from_preview(preview: dict[str, Any], colors: dict[str, str], config: dict[str, Any]) -> dict[str, Any]:
    series = [
        {"alias": entry["alias"], "color": colors.get(entry["alias"], FALLBACK_SIGNAL_COLOR), "points": _points(entry["clean"])}
        for entry in preview.get("datasets", [])
    ]
    return overlay(series, window={"from": config.get("time_range_from"), "to": config.get("time_range_to")},
                   clock=preview_clock(preview))


def _episodes(synchrony: dict[str, Any]) -> list[dict[str, float]]:
    return [{"start": episode["start_relative"], "end": episode["end_relative"]}
            for episode in synchrony.get("episodes", [])]


def timeline_span(synchrony: dict[str, Any]) -> list[float] | None:
    """Whole minutes around the synchronization windows: the x range of the episode mini bars."""
    centers = synchrony.get("windows", {}).get("centers", [])
    if not centers:
        return None
    low, high = math.floor(min(centers)), math.ceil(max(centers))
    return [low, high if high > low else low + 1]


def _zscore_points(entry: dict[str, Any]) -> list[list[Any]]:
    """A participant's analysis input as z-scores; a run stored before they were kept z-scores its 0–1 sample."""
    if "zscore" in entry:
        return _points(entry["zscore"])
    values = [point["y"] for point in entry["scaled"]]
    mean = sum(values) / len(values) if values else 0.0
    sd = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)) if values else 0.0
    return [[point["x"], (point["y"] - mean) / sd if sd > 1e-12 else 0.0] for point in entry["scaled"]]


def sync_signals(synchrony: dict[str, Any], preview: dict[str, Any], colors: dict[str, str], scale: str = ZSCORE,
                 config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The signals with the synchronization episodes marked, on the project's chart scale (services/display.py):
    z-scores with the run's High / Low thresholds (``config``) drawn, or the 0–1 min-max preview."""
    aliases = set(synchrony.get("aliases", []))
    series = [
        {"alias": entry["alias"], "color": colors.get(entry["alias"], FALLBACK_SIGNAL_COLOR),
         "points": _points(entry["scaled"]) if scale == MINMAX else _zscore_points(entry)}
        for entry in preview.get("datasets", [])
        if entry["alias"] in aliases
    ]
    zscores = scale != MINMAX
    return {
        "series": series,
        "episodes": _episodes(synchrony),
        "clock_start": preview_clock(preview),
        "scale": MINMAX if scale == MINMAX else ZSCORE,
        "high": (config or {}).get("contagion_high_z") if zscores else None,
        "low": (config or {}).get("contagion_low_z") if zscores else None,
    }


def sync_matrix(synchrony: dict[str, Any]) -> dict[str, Any]:
    return {"matrix": synchrony.get("sync_matrix", []), "aliases": synchrony.get("aliases", [])}


def episode_lanes(episodes: list[dict[str, Any]]) -> list[int]:
    """Track lane per episode (sorted by start): an episode that starts before the previous one in a lane ends
    moves to the next lane, so overlapping episodes stack instead of covering each other."""
    lane_ends: list[float] = []
    lanes = []
    for episode in episodes:
        lane = next((index for index, end in enumerate(lane_ends) if end <= episode["start_min"]), len(lane_ends))
        if lane == len(lane_ends):
            lane_ends.append(episode["end_min"])
        else:
            lane_ends[lane] = episode["end_min"]
        lanes.append(lane)
    return lanes


def coarousal_states(coarousal: dict[str, Any], clock: float | None = None) -> dict[str, Any]:
    """The state map and its episode track; ``clock`` is the clock axis origin (``preview_clock``)."""
    timeline = coarousal.get("state_timeline", {})
    episodes = sorted(coarousal.get("episodes", []), key=lambda episode: episode["start_min"])
    lanes = episode_lanes(episodes)
    return {
        "times": timeline.get("times_min", []),
        "rows": [{"alias": row["alias"], "states": row["states"]} for row in timeline.get("per_alias", [])],
        # The episode track above the state map, on the same minute axis.
        "episodes": [
            {"start": episode["start_min"], "end": episode["end_min"], "direction": episode["direction"],
             "lane": lane, "contributors": episode.get("contributors", [])}
            for episode, lane in zip(episodes, lanes)
        ],
        "lanes": max(lanes) + 1 if lanes else 0,
        "clock_start": clock,
    }


def report_charts(payload: dict[str, Any], colors: dict[str, str], config: dict[str, Any],
                  scale: str = ZSCORE) -> list[dict[str, Any]]:
    """The charts the PDF report embeds, keyed as in ``services.exports.CHART_KEYS``; ``scale`` is the chart scale."""
    preview = payload.get("preview", {})
    synchrony = payload.get("synchrony", {})
    coarousal = payload.get("coarousal", {})
    charts = [
        {"key": "raw_overlay", "kind": "overlay", "data": raw_overlay_from_preview(preview, colors)},
        {"key": "clean_overlay", "kind": "overlay", "data": clean_overlay_from_preview(preview, colors, config)},
    ]
    if synchrony:
        charts += [
            {"key": "sync_signals", "kind": "episodeSignals",
             "data": sync_signals(synchrony, preview, colors, scale, config)},
            {"key": "sync_matrix", "kind": "heatmap", "data": sync_matrix(synchrony)},
        ]
    if coarousal and coarousal.get("state_timeline", {}).get("per_alias"):
        charts.append({"key": "coarousal_states", "kind": "coarousalStates",
                       "data": coarousal_states(coarousal, preview_clock(preview))})
    return charts
