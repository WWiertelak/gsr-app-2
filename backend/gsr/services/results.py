"""Computed results behind the UI screens: run, store and check for staleness.

Every run recomputes (the pipeline has no cache), so the last cleaning preview
and the last analysis run are stored per project (``AnalysisResult``). A stored
result is stale when the datasets or the settings it was computed from changed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from django.utils import timezone
from django.utils.translation import gettext

from ..errors import no_overlap_message, parse_reason
from ..models import AnalysisResult, Dataset, Project
from ..pipeline.config import PipelineConfig
from ..pipeline.parser import ParseError, load_esense_csv
from ..pipeline.preprocessing import (
    PipelineResult,
    _should_use_legacy_downsample_factor,
    serialize_preview,
)
from .analysis import ANALYSES, AnalysisError, analyze_files_with_result, resolve_config, run_cleaning_pipeline
from .datasets import recording_range

# Samples per chart series sent to the browser. The unsampled preview of a
# 10 x 90 min session is ~27 MB; 3000 points per series keeps a page small.
PREVIEW_MAX_POINTS = 3000

# One colour per dataset, by upload order: Dark2 with two colours darkened so each
# reaches 3:1 on white. RESERVED_SCIENCE_COLORS are never a participant colour:
# red (High), blue (Low), slate grey (co-move) and crimson (threshold).
# From the 9th dataset the colours repeat and the charts dash the repeated line.
PARTICIPANT_PALETTE = ("#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#55901c", "#a87d00", "#a6761d", "#666666")
# Mirrored by app.css --high/--low/--comove/--threshold and static/gsr/js/charts.js.
RESERVED_SCIENCE_COLORS = {"high": "#b2182b", "low": "#2166ac", "comove": "#3f4b57", "threshold": "#9f1239"}
FALLBACK_SIGNAL_COLOR = "#617080"

# The rules the stored Analysis and interview results follow; a result computed under earlier rules carries none (1).
# 2: the VSR flow chart's correlation criteria read as strength, with divergent synchrony, decoupling and opposite
# arousal (2026-10). Such results are outdated anyway (their settings signature lacks the new settings); the version
# lets the screens say why, and marks the researcher's decisions on moments chosen under the earlier rules.
RESULTS_VERSION = 2

ANALYSIS_FIELD_PREFIXES = ("synchrony_", "contagion_")
CONFIG_FIELDS = tuple(field.name for field in dataclasses.fields(PipelineConfig))
CLEANING_FIELDS = tuple(name for name in CONFIG_FIELDS if not name.startswith(ANALYSIS_FIELD_PREFIXES))


def signal_colors(project: Project) -> dict[str, str]:
    return {
        dataset.alias: PARTICIPANT_PALETTE[index % len(PARTICIPANT_PALETTE)]
        for index, dataset in enumerate(project.datasets.order_by("created_at"))
    }


def included_datasets(project: Project) -> list[Dataset]:
    return list(project.datasets.filter(included=True).order_by("created_at"))


def config_overrides(project: Project) -> dict[str, Any]:
    """The stored settings as ``PipelineConfig.from_dict`` input, with scaling always on.

    The UI has no scaling step: analyses refuse unscaled signals, so every run scales.
    """
    known = set(CONFIG_FIELDS)
    stored = {key: value for key, value in (project.pipeline_config or {}).items() if key in known}
    return {**stored, "scaling_enabled": True}


def run_config(project: Project) -> PipelineConfig:
    return resolve_config(project, {"scaling_enabled": True})


def compute_signature(datasets: list[Dataset], config: PipelineConfig, fields: tuple[str, ...]) -> str:
    values = config.model_dump()
    document = {
        "datasets": [[str(dataset.id), dataset.alias, dataset.file_sha256] for dataset in datasets],
        "config": {name: values[name] for name in fields},
        "legacy_downsample": _should_use_legacy_downsample_factor(config),
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def _fields_for(kind: str) -> tuple[str, ...]:
    if kind == AnalysisResult.KIND_CLEANING:
        return CLEANING_FIELDS
    if kind == AnalysisResult.KIND_ANALYSIS:
        return CONFIG_FIELDS
    raise ValueError(f"Unsupported result kind for pipeline signature: {kind}")


def current_signature(project: Project, kind: str) -> str | None:
    if kind == AnalysisResult.KIND_VSR:
        from .interview import compute_vsr_signature  # interview imports this module

        try:
            return compute_vsr_signature(project)
        except ValueError:
            return None
    try:
        config = run_config(project)
    except AnalysisError:
        return None
    return compute_signature(included_datasets(project), config, _fields_for(kind))


def get_result(project: Project, kind: str) -> AnalysisResult | None:
    return project.results.filter(kind=kind).first()


def predates_rules(result: AnalysisResult | None) -> bool:
    """Whether a stored Analysis or interview result was computed under earlier rules (``RESULTS_VERSION``)."""
    return result is not None and result.kind != AnalysisResult.KIND_CLEANING and int(
        result.payload.get("results_version", 1)) < RESULTS_VERSION


def is_stale(project: Project, result: AnalysisResult | None) -> bool:
    """A saved result depends on its inputs and every preceding saved step.

    Re-running an earlier step invalidates later results even with identical
    settings. Missing cleaning previews are allowed: Analysis also runs cleaning.
    """
    if result is None:
        return False
    if result.signature != current_signature(project, result.kind):
        return True
    order = (AnalysisResult.KIND_CLEANING, AnalysisResult.KIND_ANALYSIS, AnalysisResult.KIND_VSR)
    for kind in order[:order.index(result.kind)]:
        earlier = get_result(project, kind)
        if earlier is not None and (earlier.computed_at > result.computed_at or is_stale(project, earlier)):
            return True
    return False


def export_is_stale(project: Project) -> bool:
    """Exports are built on demand from the saved analysis and interview results."""
    return any(is_stale(project, get_result(project, kind)) for kind in (
        AnalysisResult.KIND_CLEANING, AnalysisResult.KIND_ANALYSIS, AnalysisResult.KIND_VSR,
    ))


def _sample_xy(times: np.ndarray, values: np.ndarray, max_points: int) -> list[list[float]]:
    stride = max(1, int(np.ceil(len(times) / max_points))) if max_points > 0 else 1
    return [[float(x), float(y)] for x, y in zip(times[::stride], values[::stride])]


def _cleaning_overlays(result: PipelineResult) -> dict[str, dict[str, Any]]:
    """Synced raw trace, in relative minutes, drawn under the cleaned signal."""
    overlays: dict[str, dict[str, Any]] = {}
    for alias in result.aliases:
        synced = result.dataset_states[alias].synced
        times = synced["relative_time"].to_numpy(dtype=float)
        values = synced["microsiemens"].to_numpy(dtype=float)
        finite = np.isfinite(values)
        overlays[alias] = {"synced": _sample_xy(times[finite], values[finite], PREVIEW_MAX_POINTS)}
    return overlays


def _dataset_summary(result: PipelineResult, datasets: list[Dataset]) -> list[dict[str, Any]]:
    rows = []
    for dataset in datasets:
        state = result.dataset_states[dataset.alias]
        summary = result.cleaning_summary_by_alias[dataset.alias]
        rows.append(
            {
                "alias": dataset.alias,
                "filename": dataset.original_filename,
                "sha256": dataset.file_sha256,
                "raw_points": int(len(state.raw)),
                "synced_points": int(summary.synced_points),
                "in_window_points": int(summary.in_time_window_points),
                "recorded_start": state.raw["time"].min().isoformat(),
                "recorded_end": state.raw["time"].max().isoformat(),
            }
        )
    return rows


def _dataset_files(datasets: list[Dataset]) -> dict[str, Path]:
    if not datasets:
        raise AnalysisError("Project has no datasets selected for analysis", code="no_signals")
    return {dataset.alias: Path(dataset.file.path) for dataset in datasets}


def _store(
    project: Project, kind: str, datasets: list[Dataset], config: PipelineConfig, payload: dict[str, Any], started: float
) -> AnalysisResult:
    result, _ = AnalysisResult.objects.update_or_create(
        project=project,
        kind=kind,
        defaults={
            "signature": compute_signature(datasets, config, _fields_for(kind)),
            "config": config_overrides(project),
            "dataset_ids": [str(dataset.id) for dataset in datasets],
            "payload": payload,
            "duration_seconds": round(time.perf_counter() - started, 2),
            "computed_at": timezone.now(),
        },
    )
    return result


def compute_cleaning(project: Project) -> AnalysisResult:
    """Clean and synchronize the included recordings and store the preview."""
    started = time.perf_counter()
    datasets = included_datasets(project)
    config = run_config(project)
    result = run_cleaning_pipeline(_dataset_files(datasets), config)
    preview = serialize_preview(result, PREVIEW_MAX_POINTS)
    overlays = _cleaning_overlays(result)
    for entry in preview["datasets"]:
        entry.update(overlays[entry["alias"]])
    payload = {"preview": preview, "datasets": _dataset_summary(result, datasets)}
    return _store(project, AnalysisResult.KIND_CLEANING, datasets, config, payload, started)


def compute_analysis(project: Project) -> AnalysisResult:
    """Run joint synchronization and co-arousal on the included recordings and store both."""
    started = time.perf_counter()
    datasets = included_datasets(project)
    config = run_config(project)
    result, payload = analyze_files_with_result(_dataset_files(datasets), config, ANALYSES, None, PREVIEW_MAX_POINTS)
    payload["datasets"] = _dataset_summary(result, datasets)
    payload["results_version"] = RESULTS_VERSION
    return _store(project, AnalysisResult.KIND_ANALYSIS, datasets, config, payload, started)


def stored_pipeline_result(project: Project, stored: AnalysisResult) -> tuple[PipelineResult, PipelineConfig]:
    """Re-run the pipeline exactly as for a stored result (for the full-resolution CSV exports)."""
    by_id = {str(dataset.id): dataset for dataset in project.datasets.all()}
    missing = [dataset_id for dataset_id in stored.dataset_ids if dataset_id not in by_id]
    if missing:
        raise AnalysisError("A dataset of the last run was deleted. Run the analysis again.")
    datasets = [by_id[dataset_id] for dataset_id in stored.dataset_ids]
    try:
        config = PipelineConfig.from_dict(stored.config)
    except ValueError as exc:
        raise AnalysisError(str(exc)) from exc
    aliases = [row["alias"] for row in stored.payload["datasets"]]
    files = {alias: Path(dataset.file.path) for alias, dataset in zip(aliases, datasets)}
    return run_cleaning_pipeline(files, config), config


def recording_overview(datasets: list[Dataset], max_points: int = PREVIEW_MAX_POINTS) -> dict[str, Any]:
    """Raw traces and clock ranges of the uploaded recordings, plus their shared time window."""
    rows: list[dict[str, Any]] = []
    for dataset in datasets:
        row: dict[str, Any] = {"dataset": dataset, "points": [], "error": None}
        try:
            frame, _ = load_esense_csv(Path(dataset.file.path))
        except (FileNotFoundError, ParseError) as exc:
            row["error"] = (
                parse_reason(exc.code, exc.row)
                if isinstance(exc, ParseError)
                else gettext("The file is missing. Upload it again.")
            )
            rows.append(row)
            continue
        recorded = recording_range(frame)
        start, end = pd.Timestamp(recorded["recorded_start"]), pd.Timestamp(recorded["recorded_end"])
        stride = max(1, int(np.ceil(len(frame) / max_points)))
        sampled = frame.iloc[::stride]
        row.update(
            start=start,
            end=end,
            duration_minutes=(end - start).total_seconds() / 60.0,
            points=[[moment.isoformat(), float(value)] for moment, value in zip(sampled["time"], sampled["microsiemens"])],
        )
        rows.append(row)

    ranged = [row for row in rows if row["error"] is None]
    overlap: dict[str, Any] | None = None
    ticks: list[dict[str, Any]] = []
    if ranged:
        earliest = min(row["start"] for row in ranged)
        latest = max(row["end"] for row in ranged)
        span = max((latest - earliest).total_seconds(), 1e-9)
        for row in ranged:
            row["bar_left_pct"] = round(100.0 * (row["start"] - earliest).total_seconds() / span, 2)
            row["bar_width_pct"] = round(100.0 * (row["end"] - row["start"]).total_seconds() / span, 2)
        ticks = clock_ticks(earliest, latest)
        # The window of the included recordings only: those are what Cleaning will synchronize.
        included = [row for row in ranged if row["dataset"].included] or ranged
        common_start = max(row["start"] for row in included)
        common_end = min(row["end"] for row in included)
        overlap = {"start": common_start, "end": common_end, "ok": common_start < common_end}
        if not overlap["ok"]:
            ranges = {row["dataset"].alias: (row["start"], row["end"]) for row in included}
            overlap["message"], _details, overlap["outliers"] = no_overlap_message(ranges)
        if overlap["ok"]:
            overlap["duration_minutes"] = (common_end - common_start).total_seconds() / 60.0
            overlap["bar_left_pct"] = round(100.0 * (common_start - earliest).total_seconds() / span, 2)
            overlap["bar_width_pct"] = round(100.0 * (common_end - common_start).total_seconds() / span, 2)
    return {"rows": rows, "overlap": overlap, "ticks": ticks}


TICK_STEPS_SECONDS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def clock_ticks(earliest: pd.Timestamp, latest: pd.Timestamp, max_ticks: int = 7) -> list[dict[str, Any]]:
    """Round clock times between ``earliest`` and ``latest`` for the overlap timeline's axis, as % positions."""
    span = (latest - earliest).total_seconds()
    if span <= 0:
        return []
    step = next((step for step in TICK_STEPS_SECONDS if span / step <= max_ticks), TICK_STEPS_SECONDS[-1])
    midnight = earliest.normalize()
    offset = int(np.ceil((earliest - midnight).total_seconds() / step)) * step
    ticks = []
    while (moment := midnight + pd.Timedelta(seconds=offset)) <= latest:
        ticks.append({
            "label": moment.strftime("%H:%M" if step >= 60 else "%H:%M:%S"),
            "pct": round(100.0 * (moment - earliest).total_seconds() / span, 2),
        })
        offset += step
    return ticks
