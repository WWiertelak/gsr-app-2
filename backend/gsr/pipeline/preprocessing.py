"""Upload parsing → absolute-time synchronization → cleaning → scaling → merge → downsampling.

Carried over from GSRv2 ``gsr/pipeline/core.py`` (``run_pipeline`` and the
preview/export serializers) without the segment-based analysis.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt, medfilt
from sklearn.preprocessing import MinMaxScaler

from .common import PipelineError, _resolve_aliases
from .config import MAX_DATASETS, PipelineConfig
from .parser import ParseError, load_esense_csv

PHYSIO_MIN_US = 0.5
PHYSIO_MAX_US = 20.0
ZSCORE_THRESHOLD = 5.0
JUMP_SIGMA = 5.0
IQR_FACTOR = 1.5

FILTER_FS = 5.0

SAFE_EXPORT_COLUMN_SUFFIX = re.compile(r"[^a-zA-Z0-9_]+")
SIGNAL_QUALITY_GOOD_MAX_REJECTED_PCT = 10.0
SIGNAL_QUALITY_WARNING_MAX_REJECTED_PCT = 30.0


@dataclass
class DatasetPipelineState:
    alias: str
    raw: pd.DataFrame
    synced: pd.DataFrame
    clean: pd.DataFrame
    scaled: pd.DataFrame


@dataclass
class DatasetCleaningSummary:
    alias: str
    synced_points: int
    artifact_rejected_points: int
    outside_time_window_points: int
    in_time_window_points: int


@dataclass
class PipelineCleaningSummaryTotals:
    synced_points: int
    artifact_rejected_points: int
    outside_time_window_points: int
    in_time_window_points: int
    merged_points_before_downsampling: int
    merged_points_after_downsampling: int
    downsampled_points: int


@dataclass
class PipelineResult:
    aliases: list[str]
    dataset_states: dict[str, DatasetPipelineState]
    cleaning_summary_by_alias: dict[str, DatasetCleaningSummary]
    cleaning_summary_totals: PipelineCleaningSummaryTotals
    merged_scaled: pd.DataFrame
    merged_decimated: pd.DataFrame
    used_downsample_factor: int
    alignment: dict[str, Any]
    identical_signal_groups: list[list[str]]


def _signal_quality_level(artifact_rejected_points: int, synced_points: int) -> tuple[float, str]:
    rejected_pct = 100.0 * artifact_rejected_points / synced_points if synced_points > 0 else 0.0
    if rejected_pct < SIGNAL_QUALITY_GOOD_MAX_REJECTED_PCT:
        level = "good"
    elif rejected_pct < SIGNAL_QUALITY_WARNING_MAX_REJECTED_PCT:
        level = "warning"
    else:
        level = "poor"
    return float(rejected_pct), level


def _identical_signal_groups(parsed: dict[str, pd.DataFrame], aliases: list[str]) -> list[list[str]]:
    """Find byte-identical parsed GSR value sequences without excluding them.

    Raw files can differ in metadata while carrying exactly the same measurements,
    so the digest is based on the normalized numeric signal rather than file bytes.
    """
    groups_by_digest: dict[str, list[str]] = {}
    arrays_by_digest: dict[str, np.ndarray] = {}
    for alias in aliases:
        values = np.ascontiguousarray(parsed[alias]["microsiemens"].to_numpy(dtype=np.float64))
        digest = hashlib.sha256(values.tobytes()).hexdigest()
        existing = arrays_by_digest.get(digest)
        if existing is not None and not np.array_equal(existing, values):
            # A cryptographic collision is extraordinarily unlikely, but do not
            # report a duplicate unless the parsed arrays truly match.
            digest = f"{digest}:{alias}"
        arrays_by_digest.setdefault(digest, values)
        groups_by_digest.setdefault(digest, []).append(alias)
    return [group for group in groups_by_digest.values() if len(group) > 1]


def _odd_kernel(value: int, series_len: int) -> int:
    kernel = max(1, int(value))
    if kernel % 2 == 0:
        kernel += 1
    if kernel > series_len:
        kernel = series_len if series_len % 2 == 1 else max(1, series_len - 1)
    return max(1, kernel)


def _sample_points(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    max_points: int | None = None,
) -> list[dict[str, float | str]]:
    if df.empty:
        return []
    if max_points is None or max_points <= 0:
        stride = 1
    else:
        stride = max(1, int(np.ceil(len(df) / max_points)))
    sampled = df.iloc[::stride]
    points: list[dict[str, float | str]] = []
    for _, row in sampled.iterrows():
        x = row[x_col]
        if hasattr(x, "isoformat"):
            x = x.isoformat()
        points.append({"x": x, "y": float(row[y_col])})
    return points


def _downsample_factor(preferred: int, signal_len: int) -> int:
    factor = max(1, int(preferred))
    if factor <= 1:
        return 1
    if signal_len < factor * 3:
        return 1
    return factor


def _should_use_legacy_downsample_factor(config: PipelineConfig) -> bool:
    # Backward compatibility mode for projects/clients that only set sample-based factor.
    return (
        "downsample_interval_seconds" not in config.model_fields_set
        and "downsample_factor" in config.model_fields_set
    )


def _resolve_downsample_interval_seconds(config: PipelineConfig) -> float:
    return float(max(1, int(config.downsample_interval_seconds)))


def _equivalent_downsample_factor(source_len: int, downsampled_len: int) -> int:
    if source_len <= 0 or downsampled_len <= 0:
        return 1
    return max(1, int(round(float(source_len) / float(downsampled_len))))


def _downsample_indexes(signal_len: int, factor: int) -> np.ndarray:
    if signal_len <= 0:
        return np.array([], dtype=int)
    if factor <= 1:
        return np.arange(signal_len, dtype=int)

    indexes = np.arange(0, signal_len, factor, dtype=int)
    if indexes.size == 0:
        return np.array([signal_len - 1], dtype=int)
    if int(indexes[-1]) != signal_len - 1:
        indexes = np.append(indexes, signal_len - 1)
    return indexes


def _build_decimated_frame_from_indexes(
    merged: pd.DataFrame,
    aliases: list[str],
    indexes: np.ndarray,
) -> pd.DataFrame:
    merged_decimated = pd.DataFrame(
        {
            "time": merged["relative_time"].iloc[indexes].to_numpy(dtype=float),
            "marker": merged["marker"].iloc[indexes].astype(str).to_numpy(),
        }
    )
    for alias in aliases:
        signal = merged[f"microsiemens_{alias}"].to_numpy(dtype=float)
        merged_decimated[f"microsiemens_{alias}"] = signal[indexes]
    return merged_decimated


def _build_decimated_frame_by_interval(
    merged: pd.DataFrame,
    aliases: list[str],
    interval_seconds: float,
) -> pd.DataFrame:
    if merged.empty:
        return pd.DataFrame(columns=["time", "marker", *[f"microsiemens_{alias}" for alias in aliases]])

    interval_minutes = max(float(interval_seconds), 1e-9) / 60.0
    relative_time = merged["relative_time"].to_numpy(dtype=float)
    anchor = float(relative_time[0])
    # Group points into consecutive fixed-width time buckets.
    bucket = np.floor(((relative_time - anchor) / interval_minutes) + 1e-9).astype(int)

    working = merged[["relative_time", "marker", *[f"microsiemens_{alias}" for alias in aliases]]].copy()
    working["_bucket"] = bucket

    agg_spec: dict[str, str] = {
        "relative_time": "mean",
        "marker": "first",
    }
    for alias in aliases:
        agg_spec[f"microsiemens_{alias}"] = "mean"

    grouped = working.groupby("_bucket", sort=True, as_index=False).agg(agg_spec)
    merged_decimated = pd.DataFrame(
        {
            "time": grouped["relative_time"].to_numpy(dtype=float),
            "marker": grouped["marker"].astype(str).to_numpy(),
        }
    )
    for alias in aliases:
        merged_decimated[f"microsiemens_{alias}"] = grouped[f"microsiemens_{alias}"].to_numpy(dtype=float)

    return merged_decimated


def _detect_outlier_masks_pilot(values: np.ndarray, config: PipelineConfig | None = None) -> dict[str, np.ndarray]:
    physio_min = config.clean_filter_physiological_min_us if config is not None else PHYSIO_MIN_US
    physio_max = config.clean_filter_physiological_max_us if config is not None else PHYSIO_MAX_US
    zscore_threshold = config.clean_filter_zscore_threshold if config is not None else ZSCORE_THRESHOLD
    jump_sigma = config.clean_filter_jump_sigma if config is not None else JUMP_SIGMA
    iqr_factor = config.clean_filter_iqr_factor if config is not None else IQR_FACTOR

    finite = np.isfinite(values)
    invalid_mask = ~finite

    if not finite.any():
        false_mask = np.zeros_like(values, dtype=bool)
        return {
            "invalid": invalid_mask,
            "physical": false_mask,
            "zscore": false_mask,
            "jump": false_mask,
            "iqr": false_mask,
        }

    # Physical range for GSR in microsiemens.
    physical_mask = finite & ((values < physio_min) | (values > physio_max))

    finite_values = values[finite]
    mean = float(np.mean(finite_values))
    std = float(np.std(finite_values))
    if std > 1e-9:
        zscore_mask = finite & (np.abs((values - mean) / std) > zscore_threshold)
    else:
        zscore_mask = np.zeros_like(values, dtype=bool)

    # Jump detection on first-order differences.
    diff = np.diff(np.where(finite, values, np.nan), prepend=np.nan)
    abs_diff = np.abs(diff)
    valid_diff = np.isfinite(abs_diff)
    if valid_diff.any():
        diff_mean = float(np.mean(abs_diff[valid_diff]))
        diff_std = float(np.std(abs_diff[valid_diff]))
        jump_threshold = diff_mean + jump_sigma * diff_std
        jump_mask = valid_diff & (abs_diff > jump_threshold)
    else:
        jump_mask = np.zeros_like(values, dtype=bool)

    if finite_values.size >= 4:
        q1, q3 = np.percentile(finite_values, [25, 75])
        iqr = q3 - q1
        low = q1 - iqr_factor * iqr
        high = q3 + iqr_factor * iqr
        iqr_mask = finite & ((values < low) | (values > high))
    else:
        iqr_mask = np.zeros_like(values, dtype=bool)

    return {
        "invalid": invalid_mask,
        "physical": physical_mask,
        "zscore": zscore_mask,
        "jump": jump_mask,
        "iqr": iqr_mask,
    }


def _detect_outliers_pilot(values: np.ndarray, config: PipelineConfig | None = None) -> np.ndarray:
    masks = _detect_outlier_masks_pilot(values, config)
    invalid_mask = masks["invalid"]

    if config is None:
        return invalid_mask | masks["physical"] | masks["zscore"] | masks["jump"] | masks["iqr"]

    outlier_mask = invalid_mask.copy()
    if bool(config.clean_filter_physiological_range):
        outlier_mask |= masks["physical"]
    if bool(config.clean_filter_zscore):
        outlier_mask |= masks["zscore"]
    if bool(config.clean_filter_jump):
        outlier_mask |= masks["jump"]
    if bool(config.clean_filter_iqr):
        outlier_mask |= masks["iqr"]
    return outlier_mask


def _pilot_clean_signal(values: np.ndarray, config: PipelineConfig) -> tuple[np.ndarray, int]:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return values, 0

    outliers = _detect_outliers_pilot(values, config=config)
    artifact_rejected_points = int(np.count_nonzero(outliers))
    cleaned = values.copy()
    cleaned[outliers] = np.nan

    series = pd.Series(cleaned)
    series = series.interpolate(method="linear", limit_direction="both")
    if series.isna().any():
        finite_original = values[np.isfinite(values)]
        fallback = float(np.median(finite_original)) if finite_original.size else 0.0
        series = series.ffill().bfill().fillna(fallback)
    cleaned = series.to_numpy(dtype=float)

    normalized_cutoff = config.clean_filter_butterworth_cutoff_hz / (FILTER_FS / 2.0)
    if bool(config.clean_filter_butterworth) and 0.0 < normalized_cutoff < 1.0 and cleaned.size >= 16:
        b, a = butter(config.clean_filter_butterworth_order, normalized_cutoff, btype="low", analog=False)
        padlen = 3 * (max(len(a), len(b)) - 1)
        if cleaned.size > padlen:
            try:
                cleaned = filtfilt(b, a, cleaned)
            except ValueError:
                pass

    if config.median_kernel > 0:
        kernel = _odd_kernel(config.median_kernel, len(cleaned))
        if kernel > 1:
            cleaned = medfilt(cleaned, kernel_size=kernel)

    return cleaned, artifact_rejected_points


def _estimate_step_minutes(relative_time_minutes: np.ndarray) -> float:
    if len(relative_time_minutes) < 2:
        return 0.2 / 60.0
    diffs = np.diff(relative_time_minutes)
    positive = diffs[np.isfinite(diffs) & (diffs > 0)]
    if positive.size == 0:
        return 0.2 / 60.0
    return float(np.median(positive))


def run_pipeline(dataset_files: dict[str, Path], config: PipelineConfig) -> PipelineResult:
    if not dataset_files:
        raise PipelineError("Project has no datasets")
    if len(dataset_files) > MAX_DATASETS:
        raise PipelineError(f"Maximum number of datasets per analysis is {MAX_DATASETS}")

    aliases = list(dataset_files.keys())
    parsed: dict[str, pd.DataFrame] = {}
    for alias, path in dataset_files.items():
        try:
            df, _ = load_esense_csv(path)
        except FileNotFoundError as exc:
            raise PipelineError(f"Dataset file for {alias} is missing on disk. Upload it again.") from exc
        except ParseError as exc:
            raise PipelineError(f"Dataset {alias} parse failed: {exc}") from exc
        parsed[alias] = df

    identical_signal_groups = _identical_signal_groups(parsed, aliases)
    sync_window_start = max(df["time"].min() for df in parsed.values())
    sync_window_stop = min(df["time"].max() for df in parsed.values())
    if sync_window_start >= sync_window_stop:
        ranges = "; ".join(
            f"{alias}: {parsed[alias]['time'].min().isoformat()}–{parsed[alias]['time'].max().isoformat()}"
            for alias in aliases
        )
        raise PipelineError(
            "Datasets do not share an absolute-time overlap. Group analysis requires recordings "
            f"from the same time window. Recorded ranges: {ranges}"
        )

    dataset_states: dict[str, DatasetPipelineState] = {}
    cleaning_summary_by_alias: dict[str, DatasetCleaningSummary] = {}
    for alias in aliases:
        raw_df = parsed[alias].copy()
        synced = raw_df[(raw_df["time"] >= sync_window_start) & (raw_df["time"] <= sync_window_stop)].reset_index(
            drop=True
        )
        if synced.empty:
            raise PipelineError(f"Dataset {alias} is empty after time synchronization")

        synced["relative_time"] = (synced["time"] - synced["time"].iloc[0]).dt.total_seconds() / 60.0

        clean = synced.copy()
        cleaned_values, artifact_rejected_points = _pilot_clean_signal(clean["microsiemens"].to_numpy(dtype=float), config)
        clean["microsiemens"] = cleaned_values

        synced_points = int(len(synced))

        ranged = clean
        if config.time_range_from is not None:
            ranged = ranged[ranged["relative_time"] >= float(config.time_range_from)]
        if config.time_range_to is not None:
            ranged = ranged[ranged["relative_time"] <= float(config.time_range_to)]
        ranged = ranged.reset_index(drop=True)
        if ranged.empty:
            raise PipelineError(f"Dataset {alias} is empty after applying time range")

        in_time_window_points = int(len(ranged))
        outside_time_window_points = max(0, synced_points - in_time_window_points)

        scaled = ranged.copy()
        if bool(config.scaling_enabled):
            scaler = MinMaxScaler()
            scaled["microsiemens"] = scaler.fit_transform(scaled[["microsiemens"]]).ravel()

        dataset_states[alias] = DatasetPipelineState(
            alias=alias,
            raw=raw_df,
            synced=synced,
            clean=clean,
            scaled=scaled,
        )
        cleaning_summary_by_alias[alias] = DatasetCleaningSummary(
            alias=alias,
            synced_points=synced_points,
            artifact_rejected_points=artifact_rejected_points,
            outside_time_window_points=outside_time_window_points,
            in_time_window_points=in_time_window_points,
        )

    alias_step_minutes = {
        alias: _estimate_step_minutes(dataset_states[alias].scaled["relative_time"].to_numpy(dtype=float))
        for alias in aliases
    }

    base_alias = aliases[0]
    merged = (
        dataset_states[base_alias]
        .scaled[["relative_time", "microsiemens", "marker"]]
        .sort_values("relative_time")
        .reset_index(drop=True)
        .rename(columns={"microsiemens": f"microsiemens_{base_alias}"})
    )

    for alias in aliases[1:]:
        state = dataset_states[alias]
        selected = (
            state.scaled[["relative_time", "microsiemens", "marker"]]
            .sort_values("relative_time")
            .reset_index(drop=True)
            .rename(
                columns={
                    "microsiemens": f"microsiemens_{alias}",
                    "marker": f"marker_{alias}",
                }
            )
        )
        tolerance_minutes = max(alias_step_minutes[base_alias], alias_step_minutes[alias]) * 0.51
        tolerance_minutes = max(tolerance_minutes, 1e-6)

        merged = pd.merge_asof(
            merged.sort_values("relative_time"),
            selected,
            on="relative_time",
            direction="nearest",
            tolerance=tolerance_minutes,
        )
        merged = merged.dropna(subset=[f"microsiemens_{alias}"]).reset_index(drop=True)

    if merged.empty:
        raise PipelineError("No overlapping points after merge")

    merged = merged.sort_values("relative_time").reset_index(drop=True)
    merged["relative_time"] = merged["relative_time"].astype(float)
    merged["marker"] = merged["marker"].astype(str)
    merged_points_before_downsampling = int(len(merged))

    if not bool(config.scaling_enabled):
        factor = 1
        downsampled_indexes = _downsample_indexes(len(merged), factor)
        merged_decimated = _build_decimated_frame_from_indexes(merged, aliases, downsampled_indexes)
    elif _should_use_legacy_downsample_factor(config):
        factor = _downsample_factor(config.downsample_factor, len(merged))
        downsampled_indexes = _downsample_indexes(len(merged), factor)
        merged_decimated = _build_decimated_frame_from_indexes(merged, aliases, downsampled_indexes)
    else:
        downsample_interval_seconds = _resolve_downsample_interval_seconds(config)
        merged_decimated = _build_decimated_frame_by_interval(
            merged,
            aliases,
            downsample_interval_seconds,
        )
        factor = _equivalent_downsample_factor(len(merged), len(merged_decimated))
    if len(merged_decimated) < 2:
        raise PipelineError("Not enough points for analysis")

    merged_points_after_downsampling = int(len(merged_decimated))
    downsampled_points = max(0, merged_points_before_downsampling - merged_points_after_downsampling)

    cleaning_summary_totals = PipelineCleaningSummaryTotals(
        synced_points=sum(summary.synced_points for summary in cleaning_summary_by_alias.values()),
        artifact_rejected_points=sum(summary.artifact_rejected_points for summary in cleaning_summary_by_alias.values()),
        outside_time_window_points=sum(summary.outside_time_window_points for summary in cleaning_summary_by_alias.values()),
        in_time_window_points=sum(summary.in_time_window_points for summary in cleaning_summary_by_alias.values()),
        merged_points_before_downsampling=merged_points_before_downsampling,
        merged_points_after_downsampling=merged_points_after_downsampling,
        downsampled_points=downsampled_points,
    )

    return PipelineResult(
        aliases=aliases,
        dataset_states=dataset_states,
        cleaning_summary_by_alias=cleaning_summary_by_alias,
        cleaning_summary_totals=cleaning_summary_totals,
        merged_scaled=merged,
        merged_decimated=merged_decimated,
        used_downsample_factor=factor,
        alignment={
            "mode": "absolute",
            "start": sync_window_start.isoformat(),
            "end": sync_window_stop.isoformat(),
            "duration_seconds": float((sync_window_stop - sync_window_start).total_seconds()),
        },
        identical_signal_groups=identical_signal_groups,
    )


def _describe_signal(values: pd.Series) -> dict[str, float | int | None]:
    """Descriptive statistics of one signal in microsiemens (std uses ddof=1)."""
    numeric = pd.to_numeric(values, errors="coerce").dropna()

    def _value(number: float) -> float | None:
        return None if not np.isfinite(number) else float(number)

    if numeric.empty:
        return {"count": 0, "mean": None, "std": None, "min": None, "median": None, "max": None}
    return {
        "count": int(numeric.size),
        "mean": _value(float(numeric.mean())),
        "std": _value(float(numeric.std(ddof=1))) if numeric.size > 1 else None,
        "min": _value(float(numeric.min())),
        "median": _value(float(numeric.median())),
        "max": _value(float(numeric.max())),
    }


def serialize_signal_statistics(result: PipelineResult) -> list[dict[str, Any]]:
    """Per-signal RAW (as parsed) and CLEAN (after filters) statistics computed on full data, not preview samples."""
    return [
        {
            "alias": alias,
            "raw": _describe_signal(result.dataset_states[alias].raw["microsiemens"]),
            "clean": _describe_signal(result.dataset_states[alias].clean["microsiemens"]),
        }
        for alias in result.aliases
    ]


def serialize_preview(result: PipelineResult, max_points: int | None = None) -> dict[str, Any]:
    datasets_payload: list[dict[str, Any]] = []
    for alias in result.aliases:
        state = result.dataset_states[alias]
        scaled_frame = result.merged_decimated[["time", f"microsiemens_{alias}"]].rename(
            columns={"time": "relative_time", f"microsiemens_{alias}": "microsiemens"}
        )
        datasets_payload.append(
            {
                "alias": alias,
                "raw": _sample_points(state.raw, "time", "microsiemens", max_points),
                "clean": _sample_points(state.clean, "relative_time", "microsiemens", max_points),
                # Keep scaled preview visually consistent with analysis traces.
                "scaled": _sample_points(scaled_frame, "relative_time", "microsiemens", max_points),
            }
        )

    per_alias_summary: list[dict[str, Any]] = []
    for alias in result.aliases:
        summary = result.cleaning_summary_by_alias.get(alias)
        if summary is None:
            continue
        artifact_rejected_pct, signal_quality = _signal_quality_level(
            summary.artifact_rejected_points,
            summary.synced_points,
        )
        per_alias_summary.append(
            {
                "alias": summary.alias,
                "synced_points": int(summary.synced_points),
                "artifact_rejected_points": int(summary.artifact_rejected_points),
                "artifact_rejected_pct": artifact_rejected_pct,
                "signal_quality": signal_quality,
                "outside_time_window_points": int(summary.outside_time_window_points),
                "in_time_window_points": int(summary.in_time_window_points),
            }
        )

    totals = result.cleaning_summary_totals

    preview_range_from = max(float(state.clean["relative_time"].min()) for state in result.dataset_states.values())
    preview_range_to = min(float(state.clean["relative_time"].max()) for state in result.dataset_states.values())

    return {
        "aliases": result.aliases,
        "datasets": datasets_payload,
        "range": {
            "from": preview_range_from,
            "to": preview_range_to,
        },
        "alignment": result.alignment,
        "cleaning_summary": {
            "per_alias": per_alias_summary,
            "signal_statistics": serialize_signal_statistics(result),
            "identical_signal_groups": result.identical_signal_groups,
            "totals": {
                "synced_points": int(totals.synced_points),
                "artifact_rejected_points": int(totals.artifact_rejected_points),
                "outside_time_window_points": int(totals.outside_time_window_points),
                "in_time_window_points": int(totals.in_time_window_points),
                "merged_points_before_downsampling": int(totals.merged_points_before_downsampling),
                "merged_points_after_downsampling": int(totals.merged_points_after_downsampling),
                "downsampled_points": int(totals.downsampled_points),
            },
        },
    }


def serialize_decimated_traces(result: PipelineResult, max_points: int | None = None) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    df = result.merged_decimated
    if max_points is None or max_points <= 0:
        stride = 1
    else:
        stride = max(1, int(np.ceil(len(df) / max_points)))
    sampled = df.iloc[::stride]
    for alias in result.aliases:
        column = f"microsiemens_{alias}"
        traces.append(
            {
                "alias": alias,
                "x": sampled["time"].astype(float).tolist(),
                "y": sampled[column].astype(float).tolist(),
            }
        )
    return traces


def _export_alias_suffix(alias: str, used_suffixes: set[str]) -> str:
    suffix = SAFE_EXPORT_COLUMN_SUFFIX.sub("_", alias.strip()).strip("_")
    if not suffix:
        suffix = "signal"
    if suffix[0].isdigit():
        suffix = f"alias_{suffix}"

    unique_suffix = suffix
    counter = 2
    while unique_suffix in used_suffixes:
        unique_suffix = f"{suffix}_{counter}"
        counter += 1
    used_suffixes.add(unique_suffix)
    return unique_suffix


def _with_relative_time_from_start(frame: pd.DataFrame) -> pd.DataFrame:
    export_frame = frame.copy()
    if "relative_time" not in export_frame.columns:
        export_frame["relative_time"] = (
            export_frame["time"] - export_frame["time"].iloc[0]
        ).dt.total_seconds() / 60.0
    return export_frame


def _clip_frame_to_config_time_range(frame: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    clipped = frame
    if config.time_range_from is not None:
        clipped = clipped[clipped["relative_time"] >= float(config.time_range_from)]
    if config.time_range_to is not None:
        clipped = clipped[clipped["relative_time"] <= float(config.time_range_to)]
    return clipped.reset_index(drop=True)


def _stage_export_source_frame(
    state: DatasetPipelineState,
    stage: str,
    config: PipelineConfig,
) -> pd.DataFrame:
    if stage == "raw":
        return _with_relative_time_from_start(state.raw).reset_index(drop=True)
    if stage == "clean":
        return _clip_frame_to_config_time_range(state.clean, config)
    if stage == "scaled":
        return state.scaled.reset_index(drop=True)
    raise PipelineError(f"Unknown export stage: {stage}")


def build_stage_export_frame(
    result: PipelineResult,
    stage: str,
    config: PipelineConfig,
    dataset_aliases: list[str] | None = None,
) -> pd.DataFrame:
    aliases = _resolve_aliases(result.aliases, dataset_aliases)
    source_frames = {
        alias: _stage_export_source_frame(result.dataset_states[alias], stage, config)
        for alias in aliases
    }
    row_count = max((len(frame) for frame in source_frames.values()), default=0)
    export_frame = pd.DataFrame({"row_index": np.arange(row_count, dtype=int)})

    used_suffixes: set[str] = set()
    for alias in aliases:
        suffix = _export_alias_suffix(alias, used_suffixes)
        source = source_frames[alias]
        timestamp = source["time"].map(lambda value: value.isoformat() if pd.notna(value) else value)

        export_frame[f"timestamp_{suffix}"] = timestamp.reindex(export_frame.index)
        export_frame[f"relative_time_minutes_{suffix}"] = source["relative_time"].reindex(export_frame.index)
        export_frame[f"microsiemens_{suffix}"] = source["microsiemens"].reindex(export_frame.index)

    return export_frame
