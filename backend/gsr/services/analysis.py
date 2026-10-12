"""Run the pipeline and the group analyses (joint synchronization, co-arousal)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from ..models import Dataset, Project
from ..pipeline.coarousal import compute_coarousal
from ..pipeline.common import PipelineError
from ..pipeline.config import PipelineConfig
from ..pipeline.preprocessing import PipelineResult, run_pipeline, serialize_preview
from ..pipeline.synchrony import compute_synchrony

ANALYSES: tuple[str, ...] = ("synchrony", "coarousal")


class AnalysisError(ValueError):
    pass


def resolve_config(project: Project, overrides: dict[str, Any] | None = None) -> PipelineConfig:
    try:
        return PipelineConfig.from_dict({**(project.pipeline_config or {}), **(overrides or {})})
    except ValueError as exc:
        raise AnalysisError(str(exc)) from exc


def resolve_datasets(project: Project, dataset_ids: list[str] | None = None) -> list[Dataset]:
    datasets = list(project.datasets.order_by("created_at"))
    if not datasets:
        raise AnalysisError("Project has no uploaded datasets")

    if dataset_ids:
        if len(dataset_ids) != len(set(dataset_ids)):
            raise AnalysisError("dataset_ids must contain distinct values")
        by_id = {str(dataset.id): dataset for dataset in datasets}
        missing = [dataset_id for dataset_id in dataset_ids if dataset_id not in by_id]
        if missing:
            raise AnalysisError("One or more datasets do not belong to this project")
        return [by_id[dataset_id] for dataset_id in dataset_ids]

    return datasets


def check_analysis_request(dataset_files: dict[str, Path], config: PipelineConfig, analyses: Iterable[str]) -> list[str]:
    """Validate the requested analyses against the config and dataset count (GSRv2 endpoint guards)."""
    analyses = list(analyses)
    unknown = sorted(set(analyses) - set(ANALYSES))
    if unknown:
        raise AnalysisError(f"Unsupported analysis: {', '.join(unknown)}")
    if analyses:
        if not config.scaling_enabled:
            raise AnalysisError("Enable scaling before running synchrony or co-arousal analysis")
        if len(dataset_files) < 2:
            raise AnalysisError("Synchrony and co-arousal require at least two distinct datasets")
        if "synchrony" in analyses and config.synchrony_episode_min_size > len(dataset_files):
            raise AnalysisError("synchrony_episode_min_size cannot exceed the selected dataset count")
        if "coarousal" in analyses and config.contagion_min_participants > len(dataset_files):
            raise AnalysisError("contagion_min_participants cannot exceed the selected dataset count")
    return analyses


def run_cleaning_pipeline(dataset_files: dict[str, Path], config: PipelineConfig) -> PipelineResult:
    try:
        return run_pipeline(dataset_files, config)
    except PipelineError as exc:
        raise AnalysisError(str(exc)) from exc


def analyze_files_with_result(
    dataset_files: dict[str, Path],
    config: PipelineConfig,
    analyses: Iterable[str] = ANALYSES,
    cancel_callback: Callable[[], None] | None = None,
    max_points: int | None = None,
) -> tuple[PipelineResult, dict[str, Any]]:
    """``analyze_files`` that also returns the pipeline result the payload was built from."""
    analyses = check_analysis_request(dataset_files, config, analyses)
    result = run_cleaning_pipeline(dataset_files, config)

    aliases = list(dataset_files)
    payload: dict[str, Any] = {"preview": serialize_preview(result, max_points)}
    if "synchrony" in analyses:
        payload["synchrony"] = compute_synchrony(result, aliases, config, cancel_callback)
    if "coarousal" in analyses:
        payload["coarousal"] = compute_coarousal(result, aliases, config, cancel_callback)
    return result, payload


def analyze_files(
    dataset_files: dict[str, Path],
    config: PipelineConfig,
    analyses: Iterable[str] = ANALYSES,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Clean and synchronize the recordings, then run the requested group analyses.

    The returned ``preview`` payload is the cleaning/synchronization summary; the
    analysis payloads are keyed by analysis name. The guards mirror the GSRv2
    analysis endpoint: analyses run on scaled signals of at least two recordings.
    """
    return analyze_files_with_result(dataset_files, config, analyses, cancel_callback)[1]


def run_project_analysis(
    project: Project,
    *,
    analyses: Iterable[str] = ANALYSES,
    dataset_ids: list[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    datasets = resolve_datasets(project, dataset_ids)
    config = resolve_config(project, config_overrides)
    files = {dataset.alias: Path(dataset.file.path) for dataset in datasets}
    return analyze_files(files, config, analyses, cancel_callback)
