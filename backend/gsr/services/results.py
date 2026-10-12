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

from ..models import AnalysisResult, Dataset, Project
from ..pipeline.config import PipelineConfig
from ..pipeline.parser import ParseError, load_esense_csv
from ..pipeline.preprocessing import (
    PipelineResult,
    _detect_outliers_pilot,
    _should_use_legacy_downsample_factor,
    serialize_preview,
)
from .analysis import ANALYSES, AnalysisError, analyze_files_with_result, resolve_config, run_cleaning_pipeline
from .datasets import recording_range

# Samples per chart series sent to the browser. The unsampled preview of a
# 10 x 90 min session is ~27 MB; 3000 points per series keeps a page small.
PREVIEW_MAX_POINTS = 3000
REJECTED_MAX_POINTS = 2000

# GSRv2 SCIENTIFIC_PALETTE (frontend/src/constants.ts): one colour per dataset, by upload order.
SCIENTIFIC_PALETTE = (
    "#003049", "#0077b6", "#0a9396", "#94d2bd", "#ee9b00", "#bb3e03", "#9b2226", "#4c1d95", "#7c3aed", "#db2777",
    "#be123c", "#ea580c", "#ca8a04", "#65a30d", "#16a34a", "#059669", "#0891b2", "#2563eb", "#4338ca", "#6d28d9",
)

ANALYSIS_FIELD_PREFIXES = ("synchrony_", "contagion_")
CONFIG_FIELDS = tuple(field.name for field in dataclasses.fields(PipelineConfig))
CLEANING_FIELDS = tuple(name for name in CONFIG_FIELDS if not name.startswith(ANALYSIS_FIELD_PREFIXES))


def signal_colors(project: Project) -> dict[str, str]:
    return {
        dataset.alias: SCIENTIFIC_PALETTE[index % len(SCIENTIFIC_PALETTE)]
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
    return CLEANING_FIELDS if kind == AnalysisResult.KIND_CLEANING else CONFIG_FIELDS


def current_signature(project: Project, kind: str) -> str | None:
    try:
        config = run_config(project)
    except AnalysisError:
        return None
    return compute_signature(included_datasets(project), config, _fields_for(kind))


def get_result(project: Project, kind: str) -> AnalysisResult | None:
    return project.results.filter(kind=kind).first()


def is_stale(project: Project, result: AnalysisResult | None) -> bool:
    return result is not None and result.signature != current_signature(project, result.kind)


def _sample_xy(times: np.ndarray, values: np.ndarray, max_points: int) -> list[list[float]]:
    stride = max(1, int(np.ceil(len(times) / max_points))) if max_points > 0 else 1
    return [[float(x), float(y)] for x, y in zip(times[::stride], values[::stride])]


def _cleaning_overlays(result: PipelineResult, config: PipelineConfig) -> dict[str, dict[str, Any]]:
    """Synced raw trace and the samples the artifact filters rejected, in relative minutes.

    The rejection mask is recomputed with the pipeline's own detector and the same
    config, so its count equals the cleaning summary's ``artifact_rejected_points``.
    """
    overlays: dict[str, dict[str, Any]] = {}
    for alias in result.aliases:
        synced = result.dataset_states[alias].synced
        times = synced["relative_time"].to_numpy(dtype=float)
        values = synced["microsiemens"].to_numpy(dtype=float)
        mask = _detect_outliers_pilot(values, config)
        finite = np.isfinite(values)
        overlays[alias] = {
            "synced": _sample_xy(times[finite], values[finite], PREVIEW_MAX_POINTS),
            "rejected": _sample_xy(times[mask & finite], values[mask & finite], REJECTED_MAX_POINTS),
        }
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
        raise AnalysisError("Project has no datasets selected for analysis")
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
    overlays = _cleaning_overlays(result, config)
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
            row["error"] = str(exc) if isinstance(exc, ParseError) else "File is missing on disk. Upload it again."
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
    if ranged:
        earliest = min(row["start"] for row in ranged)
        latest = max(row["end"] for row in ranged)
        span = max((latest - earliest).total_seconds(), 1e-9)
        for row in ranged:
            row["bar_left_pct"] = round(100.0 * (row["start"] - earliest).total_seconds() / span, 2)
            row["bar_width_pct"] = round(100.0 * (row["end"] - row["start"]).total_seconds() / span, 2)
        common_start = max(row["start"] for row in ranged)
        common_end = min(row["end"] for row in ranged)
        overlap = {"start": common_start, "end": common_end, "ok": common_start < common_end}
        if overlap["ok"]:
            overlap["duration_minutes"] = (common_end - common_start).total_seconds() / 60.0
            overlap["bar_left_pct"] = round(100.0 * (common_start - earliest).total_seconds() / span, 2)
            overlap["bar_width_pct"] = round(100.0 * (common_end - common_start).total_seconds() / span, 2)
    return {"rows": rows, "overlap": overlap}
