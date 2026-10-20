"""Export files of the last analysis run: CSV tables, a reproducibility JSON, chart PNGs, a ZIP."""

from __future__ import annotations

import base64
import binascii
import io
import platform
import zipfile
from datetime import datetime
from importlib import metadata
from typing import Any

import pandas as pd
from django.utils import timezone, translation

from .. import vsr_text
from ..models import AnalysisResult, Project
from ..pipeline.common import _zscore, clock_at, clock_start
from ..pipeline.config import PipelineConfig
from ..pipeline.preprocessing import analysed_range, build_stage_export_frame
from ..pipeline.vsr import step_label
from .display import MINMAX, chart_scale
from .results import stored_pipeline_result

# Charts the Export screen captures from the same ECharts options as the screens,
# by report section. The browser posts them as PNG data URLs (field "chart_<key>").
CHART_KEYS = (
    "raw_overlay",
    "clean_overlay",
    "sync_signals",
    "sync_matrix",
    "coarousal_states",
)
MAX_CHART_BYTES = 8 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_DATA_URL_PREFIX = "data:image/png;base64,"

VERSIONED_PACKAGES = ("django", "numpy", "pandas", "scipy", "scikit-learn", "dtaidistance", "weasyprint")


def parse_chart_images(data: Any) -> dict[str, bytes]:
    """Decode posted chart PNGs; unknown keys and anything that is not a PNG are ignored."""
    charts: dict[str, bytes] = {}
    for key in CHART_KEYS:
        value = data.get(f"chart_{key}") or ""
        if not value.startswith(PNG_DATA_URL_PREFIX):
            continue
        try:
            image = base64.b64decode(value[len(PNG_DATA_URL_PREFIX):], validate=True)
        except (binascii.Error, ValueError):
            continue
        if image.startswith(PNG_MAGIC) and len(image) <= MAX_CHART_BYTES:
            charts[key] = image
    return charts


def _csv(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


def _join(values: Any) -> str:
    return "; ".join(str(value) for value in values or [])


def _lesson_clock(stored: AnalysisResult) -> datetime | None:
    """The recordings' own clock at lesson time 0 of a stored run."""
    return clock_start(stored.payload.get("preview", {}).get("alignment"))


def _minutes_clock(lesson_clock: datetime | None, minutes: Any) -> str | None:
    return clock_at(lesson_clock, minutes * 60.0) if isinstance(minutes, (int, float)) else None


def clean_signals_csv(project: Project, stored: AnalysisResult) -> bytes:
    """Cleaned signal per participant at full resolution, clipped to the time window.

    Each participant has its samples' own clock (``clock_<alias>``, HH:MM:SS.fff, next to the full
    ``timestamp_<alias>``) and the lesson time (``relative_time_minutes_<alias>``).
    """
    result, config = stored_pipeline_result(project, stored)
    frame = build_stage_export_frame(result, "clean", config)
    for column in [name for name in frame.columns if name.startswith("timestamp_")]:
        clock = pd.to_datetime(frame[column], format="ISO8601").map(
            lambda value: value.strftime("%H:%M:%S.%f")[:-3] if pd.notna(value) else None)
        frame.insert(frame.columns.get_loc(column) + 1, f"clock_{column.removeprefix('timestamp_')}", clock)
    return _csv(frame)


def merged_csv(project: Project, stored: AnalysisResult) -> bytes:
    """The analysis input: merged and downsampled signals on a shared time axis (clock and lesson minutes).

    The values are on the project's chart scale, named by it: ``<alias>_z`` (z-scores within the analysed window,
    the default) or ``<alias>_minmax`` (0–1).
    """
    result, _ = stored_pipeline_result(project, stored)
    minmax = chart_scale(project) == MINMAX
    frame = result.merged_decimated.rename(columns={"time": "relative_time_minutes"})
    for alias in result.aliases:
        column = f"microsiemens_{alias}"
        values = frame[column] if minmax else _zscore(frame[column].to_numpy(dtype=float))
        frame.insert(frame.columns.get_loc(column), f"{alias}_{'minmax' if minmax else 'z'}", values)
        frame = frame.drop(columns=column)
    lesson_clock = clock_start(result.alignment)
    frame.insert(0, "clock", [clock_at(lesson_clock, minutes * 60.0, milliseconds=True)
                              for minutes in frame["relative_time_minutes"]])
    return _csv(frame)


def synchrony_episodes_csv(synchrony: dict[str, Any], lesson_clock: datetime | None = None,
                           key: str = "episodes") -> bytes:
    """Synchronization episodes (``key`` "episodes"), or divergent ones ("divergent_episodes": strengths are r < 0)."""
    rows = [
        {
            "episode": index + 1,
            "clock_start": _minutes_clock(lesson_clock, episode["start_relative"]),
            "clock_end": _minutes_clock(lesson_clock, episode["end_relative"]),
            "start_minutes": episode["start_relative"],
            "end_minutes": episode["end_relative"],
            "duration_minutes": episode["end_relative"] - episode["start_relative"],
            "members": _join(episode["members"]),
            "size": episode["size"],
            "mean_strength": episode["mean_strength"],
            "peak_strength": episode["peak_strength"],
        }
        for index, episode in enumerate(synchrony.get(key, []))
    ]
    columns = ["episode", "clock_start", "clock_end", "start_minutes", "end_minutes", "duration_minutes", "members",
               "size", "mean_strength", "peak_strength"]
    return _csv(pd.DataFrame(rows, columns=columns))


def decoupled_episodes_csv(synchrony: dict[str, Any], lesson_clock: datetime | None = None) -> bytes:
    """Decoupling stretches: the pair, the mean r of the stretch's windows and (one-correlation rule) its r."""
    rows = [
        {
            "episode": index + 1,
            "clock_start": _minutes_clock(lesson_clock, episode["start_relative"]),
            "clock_end": _minutes_clock(lesson_clock, episode["end_relative"]),
            "start_minutes": episode["start_relative"],
            "end_minutes": episode["end_relative"],
            "duration_minutes": episode["end_relative"] - episode["start_relative"],
            "members": _join(episode["members"]),
            "mean_r": episode["mean_r"],
            # The one correlation over the stretch's data, under the "stretch_correlation" decoupling rule only.
            "stretch_r": episode.get("stretch_r"),
        }
        for index, episode in enumerate(synchrony.get("decoupled_episodes", []))
    ]
    columns = ["episode", "clock_start", "clock_end", "start_minutes", "end_minutes", "duration_minutes", "members",
               "mean_r", "stretch_r"]
    return _csv(pd.DataFrame(rows, columns=columns))


def synchrony_pairs_csv(synchrony: dict[str, Any]) -> bytes:
    columns = ["alias_a", "alias_b", "max_xcorr", "xcorr_zero_lag", "lag_seconds", "p_value", "null_mean", "null_sd",
               "coherence", "dtw_distance"]
    rows = [{column: pair.get(column) for column in columns} for pair in synchrony.get("pairs", [])]
    return _csv(pd.DataFrame(rows, columns=columns))


def synchrony_windows_csv(synchrony: dict[str, Any], lesson_clock: datetime | None = None) -> bytes:
    windows = synchrony.get("windows", {})
    frame = pd.DataFrame(
        {
            "center_clock": [_minutes_clock(lesson_clock, center) for center in windows.get("centers", [])],
            "center_minutes": windows.get("centers", []),
            "group_mean": windows.get("group_mean", []),
            "active_count": windows.get("active_counts", []),
        }
    )
    for pair in windows.get("pairs", []):
        frame[f"{pair['alias_a']}__{pair['alias_b']}"] = pair["values"]
    return _csv(frame)


def coarousal_episodes_csv(coarousal: dict[str, Any], lesson_clock: datetime | None = None) -> bytes:
    columns = ["episode", "direction", "clock_start", "clock_end", "start_s", "end_s", "duration_s", "n_contributors",
               "strength", "contributors", "non_contributors", "high_contributors", "low_contributors"]
    rows = [
        {
            **{key: episode.get(key) for key in ("direction", "start_s", "end_s", "duration_s", "n_contributors",
                                                  "strength")},
            "episode": index + 1,
            "clock_start": clock_at(lesson_clock, episode.get("start_s")),
            "clock_end": clock_at(lesson_clock, episode.get("end_s")),
            "contributors": _join(episode.get("contributors")),
            "non_contributors": _join(episode.get("non_contributors")),
            # Opposite arousal only: who was High and who was Low.
            "high_contributors": _join(episode.get("high_contributors")),
            "low_contributors": _join(episode.get("low_contributors")),
        }
        for index, episode in enumerate(coarousal.get("episodes", []))
    ]
    return _csv(pd.DataFrame(rows, columns=columns))


def coarousal_states_csv(coarousal: dict[str, Any], lesson_clock: datetime | None = None) -> bytes:
    """Mean state per participant per 1-minute bin (-1 Low, 0 neutral, +1 High), from the bin's start."""
    timeline = coarousal.get("state_timeline", {})
    times = timeline.get("times_min", [])
    frame = pd.DataFrame({"clock": [_minutes_clock(lesson_clock, minute) for minute in times], "minute": times})
    for row in timeline.get("per_alias", []):
        frame[row["alias"]] = row["states"]
    return _csv(frame)


def coarousal_state_percentages_csv(coarousal: dict[str, Any]) -> bytes:
    columns = ["alias", "high_pct", "neutral_pct", "low_pct"]
    return _csv(pd.DataFrame(coarousal.get("state_percentages", []), columns=columns))


VSR_MOMENT_COLUMNS = (
    "interview", "interview_type", "participants", "number", "status", "decision", "source", "step", "step_name",
    "moment_type", "pass", "lesson_start_s", "lesson_end_s", "time_changed", "proposed_start_s", "proposed_end_s",
    "proposed_clock_start", "proposed_clock_end", "clock_start", "clock_end", "analysis_episode", "correlation", "shared_state", "in_shared_state",
    "candidate_rank", "candidate_count", "justification", "reason", "note", "check", "stale",
)


def _episode_label(episode: dict[str, Any] | None, interview_kind: str) -> str | None:
    """The episode a moment came from: the interview's own analysis (named by kind), or for moments chosen from the
    whole-class Analysis, its number on the Analysis screen."""
    if not episode:
        return None
    if episode["source"] == "own":
        whose = "teacher and students' average" if interview_kind == "teacher" else "pair"
        return f"{episode.get('kind') or 'episode'} of the {whose}"
    if episode["source"] == "synchrony":
        prefix = {"divergent": "divergent D", "decoupled": "decoupling U"}.get(episode.get("kind") or "", "E")
        return f"synchronization {prefix}{episode['number']}"
    return f"co-arousal {episode['number']}"


def _vsr_justification(moment: dict[str, Any], kind: str) -> str:
    """A proposed moment's justification (it describes the proposed time); a manual moment's is the researcher's reason."""
    if moment["source"] == "manual":
        return f"Added by the researcher: {moment['reason']}"
    return vsr_text.justification(moment["proposal"], kind)


def vsr_moments_csv(vsr: dict[str, Any]) -> bytes:
    """The VSR interview moments of every interview, one row each, with the justification in English.

    The rows are the researcher's list from the Interviews step without its rejected moments (the moments chosen
    from the Analysis for an interview nobody has curated): ``decision`` is accepted, undecided or manual;
    ``status`` is primary or reserve by the place on the list; a retimed moment (``time_changed``) keeps the
    proposed times (lesson seconds and sensor clock) and the ``reason``; ``analysis_episode`` names the episode the
    moment came from.
    ``stale`` is True on every row when the participants, the number of moments or the analysis changed after the
    moments were chosen.
    """
    with translation.override("en"):
        rows = [
            {
                "interview": interview["key"],
                "interview_type": interview["kind"],
                "participants": _join(interview["participants"]),
                "number": moment["number"],
                "status": moment["status"],
                "decision": moment["decision"],
                "source": moment["source"],
                "step": step_label(moment["step"]) if moment["step"] else None,
                "step_name": vsr_text.step_name(moment["step"], moment["shared_state"]) if moment["step"] else None,
                "moment_type": moment["type"],
                "pass": moment["pass_number"],
                "lesson_start_s": moment["start_seconds"],
                "lesson_end_s": moment["end_seconds"],
                "time_changed": moment["time_changed"],
                "proposed_start_s": moment["original_start_seconds"],
                "proposed_end_s": moment["original_end_seconds"],
                "proposed_clock_start": moment["original_start_clock"],
                "proposed_clock_end": moment["original_end_clock"],
                "clock_start": moment["start_clock"],
                "clock_end": moment["end_clock"],
                "analysis_episode": _episode_label(moment["episode"], interview["kind"]),
                "correlation": moment["correlation"],
                "shared_state": moment["shared_state"],
                "in_shared_state": (f"{moment['in_state']}/{moment['people']}"
                                    if moment["shared_state"] and moment["in_state"] is not None else None),
                "candidate_rank": moment["candidate_rank"],
                "candidate_count": moment["candidate_count"],
                "justification": _vsr_justification(moment, interview["kind"]),
                "reason": moment["reason"],
                "note": moment["note"],
                # Marked for checking: "rules" (decided under the earlier rules) or "dropped" (no longer proposed).
                "check": moment["check_reason"],
                "stale": vsr["stale"],
            }
            for interview in vsr["interviews"]
            for moment in interview["moments"]
        ]
    # Object columns keep a whole number whole when a manual moment leaves the cell empty.
    return _csv(pd.DataFrame(rows, columns=list(VSR_MOMENT_COLUMNS), dtype=object))


def versions() -> dict[str, str]:
    found = {"python": platform.python_version()}
    for package in VERSIONED_PACKAGES:
        try:
            found[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    return found


def _with_clocks(items: list[dict[str, Any]], lesson_clock: datetime | None, start: str, end: str,
                 per_minute: bool) -> list[dict[str, Any]]:
    """Copies of episodes with ``start_clock`` / ``end_clock`` from their lesson times (minutes or seconds)."""
    factor = 60.0 if per_minute else 1.0

    def clock(value: Any) -> str | None:
        return clock_at(lesson_clock, value * factor) if isinstance(value, (int, float)) else None

    return [{**item, "start_clock": clock(item.get(start)), "end_clock": clock(item.get(end))} for item in items]


def results_document(project: Project, stored: AnalysisResult) -> dict[str, Any]:
    """Run settings, input files and results: enough for a recipient to check what was run.

    Episodes carry the recordings' own clock (``start_clock`` / ``end_clock``) next to their lesson times, the
    synchronization windows ``center_clocks`` and the state map ``clocks``; ``lesson_clock_start`` is the clock at
    lesson time 0, so any other lesson time converts the same way.
    """
    payload = stored.payload
    lesson_clock = _lesson_clock(stored)
    synchrony = payload.get("synchrony")
    if synchrony:
        synchrony = {**synchrony, **{
            key: _with_clocks(synchrony[key], lesson_clock, "start_relative", "end_relative", True)
            for key in ("episodes", "divergent_episodes", "decoupled_episodes") if key in synchrony}}
        if "windows" in synchrony:
            windows = synchrony["windows"]
            synchrony["windows"] = {**windows, "center_clocks": [_minutes_clock(lesson_clock, center)
                                                                 for center in windows.get("centers", [])]}
    coarousal = payload.get("coarousal")
    if coarousal:
        coarousal = {**coarousal,
                     "episodes": _with_clocks(coarousal.get("episodes", []), lesson_clock, "start_s", "end_s", False),
                     "primary_timeline": _with_clocks(coarousal.get("primary_timeline", []), lesson_clock,
                                                      "start_min", "end_min", True)}
        if "state_timeline" in coarousal:
            timeline = coarousal["state_timeline"]
            coarousal["state_timeline"] = {**timeline, "clocks": [_minutes_clock(lesson_clock, minute)
                                                                  for minute in timeline.get("times_min", [])]}
    preview = payload.get("preview", {})
    files = {str(dataset.id): dataset for dataset in project.datasets.all()}
    datasets = []
    for dataset_id, row in zip(stored.dataset_ids, payload.get("datasets", [])):
        dataset = files.get(dataset_id)
        datasets.append({**row, "file_size": dataset.file_size if dataset is not None else None})
    return {
        "app": "GSR_APP 2.0",
        "exported_at": timezone.now().isoformat(),
        "project": {"id": str(project.id), "name": project.name, "description": project.description},
        "run": {"computed_at": stored.computed_at.isoformat(), "duration_seconds": stored.duration_seconds},
        "config": PipelineConfig.from_dict(stored.config).model_dump(),
        "config_overrides": stored.config,
        "versions": versions(),
        "datasets": datasets,
        "alignment": preview.get("alignment"),
        "lesson_clock_start": clock_at(lesson_clock, 0.0, milliseconds=True),
        # Every result is computed on z-scores within the analysed window or on correlations; the chart scale is how
        # the signals chart, the PDF report and analysis_input_merged.csv show them.
        "analysis_normalization": "z-score within the analysed window",
        "display": {"chart_scale": chart_scale(project)},
        "time_range_minutes": analysed_range(preview, PipelineConfig.from_dict(stored.config).model_dump()),
        "cleaning_summary": preview.get("cleaning_summary"),
        "synchrony": synchrony,
        "coarousal": coarousal,
    }


def table_exports(stored: AnalysisResult) -> dict[str, bytes]:
    """CSV tables built from the stored results (no recompute); every time has the recordings' clock next to it."""
    synchrony = stored.payload.get("synchrony", {})
    coarousal = stored.payload.get("coarousal", {})
    lesson_clock = _lesson_clock(stored)
    return {
        "synchrony_episodes.csv": synchrony_episodes_csv(synchrony, lesson_clock),
        "synchrony_divergent_episodes.csv": synchrony_episodes_csv(synchrony, lesson_clock, "divergent_episodes"),
        "synchrony_decoupled_episodes.csv": decoupled_episodes_csv(synchrony, lesson_clock),
        "synchrony_pairs.csv": synchrony_pairs_csv(synchrony),
        "synchrony_windows.csv": synchrony_windows_csv(synchrony, lesson_clock),
        "coarousal_episodes.csv": coarousal_episodes_csv(coarousal, lesson_clock),
        "coarousal_state_timeline.csv": coarousal_states_csv(coarousal, lesson_clock),
        "coarousal_state_percentages.csv": coarousal_state_percentages_csv(coarousal),
    }


def build_zip(project: Project, stored: AnalysisResult, charts: dict[str, bytes], json_bytes: bytes,
              vsr: dict[str, Any] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("clean_signals.csv", clean_signals_csv(project, stored))
        archive.writestr("analysis_input_merged.csv", merged_csv(project, stored))
        for name, content in table_exports(stored).items():
            archive.writestr(name, content)
        if vsr is not None:
            archive.writestr("vsr_moments.csv", vsr_moments_csv(vsr))
        archive.writestr("run_settings_and_results.json", json_bytes)
        for key, image in charts.items():
            archive.writestr(f"charts/{key}.png", image)
    return buffer.getvalue()
