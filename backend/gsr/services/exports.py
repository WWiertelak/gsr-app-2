"""Export files of the last analysis run: CSV tables, a reproducibility JSON, chart PNGs, a ZIP."""

from __future__ import annotations

import base64
import binascii
import io
import platform
import zipfile
from importlib import metadata
from typing import Any

import pandas as pd
from django.utils import timezone, translation

from .. import vsr_text
from ..models import AnalysisResult, Project
from ..pipeline.config import PipelineConfig
from ..pipeline.preprocessing import analysed_range, build_stage_export_frame
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


def clean_signals_csv(project: Project, stored: AnalysisResult) -> bytes:
    """Cleaned signal per participant at full resolution, clipped to the time window (relative minutes)."""
    result, config = stored_pipeline_result(project, stored)
    return _csv(build_stage_export_frame(result, "clean", config))


def merged_csv(project: Project, stored: AnalysisResult) -> bytes:
    """The analysis input: scaled, merged and downsampled signals on a shared time axis."""
    result, _ = stored_pipeline_result(project, stored)
    frame = result.merged_decimated.rename(columns={"time": "relative_time_minutes"})
    return _csv(frame)


def synchrony_episodes_csv(synchrony: dict[str, Any]) -> bytes:
    rows = [
        {
            "episode": index + 1,
            "start_minutes": episode["start_relative"],
            "end_minutes": episode["end_relative"],
            "duration_minutes": episode["end_relative"] - episode["start_relative"],
            "members": _join(episode["members"]),
            "size": episode["size"],
            "mean_strength": episode["mean_strength"],
            "peak_strength": episode["peak_strength"],
        }
        for index, episode in enumerate(synchrony.get("episodes", []))
    ]
    columns = ["episode", "start_minutes", "end_minutes", "duration_minutes", "members", "size", "mean_strength",
               "peak_strength"]
    return _csv(pd.DataFrame(rows, columns=columns))


def synchrony_pairs_csv(synchrony: dict[str, Any]) -> bytes:
    columns = ["alias_a", "alias_b", "max_xcorr", "xcorr_zero_lag", "lag_seconds", "p_value", "null_mean", "null_sd",
               "coherence", "dtw_distance"]
    rows = [{column: pair.get(column) for column in columns} for pair in synchrony.get("pairs", [])]
    return _csv(pd.DataFrame(rows, columns=columns))


def synchrony_windows_csv(synchrony: dict[str, Any]) -> bytes:
    windows = synchrony.get("windows", {})
    frame = pd.DataFrame(
        {
            "center_minutes": windows.get("centers", []),
            "group_mean": windows.get("group_mean", []),
            "active_count": windows.get("active_counts", []),
        }
    )
    for pair in windows.get("pairs", []):
        frame[f"{pair['alias_a']}__{pair['alias_b']}"] = pair["values"]
    return _csv(frame)


def coarousal_episodes_csv(coarousal: dict[str, Any]) -> bytes:
    columns = ["episode", "direction", "start_s", "end_s", "duration_s", "n_contributors", "strength", "contributors",
               "non_contributors"]
    rows = [
        {
            **{key: episode.get(key) for key in ("direction", "start_s", "end_s", "duration_s", "n_contributors",
                                                  "strength")},
            "episode": index + 1,
            "contributors": _join(episode.get("contributors")),
            "non_contributors": _join(episode.get("non_contributors")),
        }
        for index, episode in enumerate(coarousal.get("episodes", []))
    ]
    return _csv(pd.DataFrame(rows, columns=columns))


def coarousal_states_csv(coarousal: dict[str, Any]) -> bytes:
    """Mean state per participant per 1-minute bin (-1 Low, 0 neutral, +1 High)."""
    timeline = coarousal.get("state_timeline", {})
    frame = pd.DataFrame({"minute": timeline.get("times_min", [])})
    for row in timeline.get("per_alias", []):
        frame[row["alias"]] = row["states"]
    return _csv(frame)


def coarousal_state_percentages_csv(coarousal: dict[str, Any]) -> bytes:
    columns = ["alias", "high_pct", "neutral_pct", "low_pct"]
    return _csv(pd.DataFrame(coarousal.get("state_percentages", []), columns=columns))


VSR_MOMENT_COLUMNS = (
    "interview", "interview_type", "participants", "number", "status", "decision", "source", "step", "step_name",
    "pass", "lesson_start_s", "lesson_end_s", "time_changed", "proposed_start_s", "proposed_end_s", "clock_start",
    "clock_end", "analysis_episode", "correlation", "shared_state", "in_shared_state",
    "candidate_rank", "candidate_count", "justification", "reason", "note", "stale",
)


def _episode_label(episode: dict[str, Any] | None) -> str | None:
    """The Analysis episode a moment came from, as the Analysis screen numbers it."""
    if not episode:
        return None
    if episode["source"] == "synchrony":
        return f"synchronization E{episode['number']}"
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
    proposed times and the ``reason``; ``analysis_episode`` names the Analysis episode the moment came from.
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
                "step": moment["step"],
                "step_name": vsr_text.step_name(moment["step"], moment["shared_state"]) if moment["step"] else None,
                "pass": moment["pass_number"],
                "lesson_start_s": moment["start_seconds"],
                "lesson_end_s": moment["end_seconds"],
                "time_changed": moment["time_changed"],
                "proposed_start_s": moment["original_start_seconds"],
                "proposed_end_s": moment["original_end_seconds"],
                "clock_start": moment["start_clock"],
                "clock_end": moment["end_clock"],
                "analysis_episode": _episode_label(moment["episode"]),
                "correlation": moment["correlation"],
                "shared_state": moment["shared_state"],
                "in_shared_state": (f"{moment['in_state']}/{moment['people']}"
                                    if moment["shared_state"] and moment["in_state"] is not None else None),
                "candidate_rank": moment["candidate_rank"],
                "candidate_count": moment["candidate_count"],
                "justification": _vsr_justification(moment, interview["kind"]),
                "reason": moment["reason"],
                "note": moment["note"],
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


def results_document(project: Project, stored: AnalysisResult) -> dict[str, Any]:
    """Run settings, input files and results: enough for a recipient to check what was run."""
    payload = stored.payload
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
        "time_range_minutes": analysed_range(preview, PipelineConfig.from_dict(stored.config).model_dump()),
        "cleaning_summary": preview.get("cleaning_summary"),
        "synchrony": payload.get("synchrony"),
        "coarousal": payload.get("coarousal"),
    }


def table_exports(stored: AnalysisResult) -> dict[str, bytes]:
    """CSV tables built from the stored results (no recompute)."""
    synchrony = stored.payload.get("synchrony", {})
    coarousal = stored.payload.get("coarousal", {})
    return {
        "synchrony_episodes.csv": synchrony_episodes_csv(synchrony),
        "synchrony_pairs.csv": synchrony_pairs_csv(synchrony),
        "synchrony_windows.csv": synchrony_windows_csv(synchrony),
        "coarousal_episodes.csv": coarousal_episodes_csv(coarousal),
        "coarousal_state_timeline.csv": coarousal_states_csv(coarousal),
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
