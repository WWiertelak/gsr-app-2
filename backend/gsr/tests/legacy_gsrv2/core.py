from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import ruptures as rpt

try:
    from kneed import KneeLocator
except ImportError:  # pragma: no cover - optional dependency fallback
    KneeLocator = None
from scipy.ndimage import gaussian_filter1d
from scipy.signal import butter, coherence, correlate, correlation_lags, filtfilt, find_peaks, medfilt
from scipy.stats import linregress, norm, spearmanr
from scipy.stats import t as student_t
from sklearn.cluster import AgglomerativeClustering
from sklearn.preprocessing import MinMaxScaler
from statsmodels.stats.multitest import multipletests

try:  # DTW is optional; the synchrony endpoint degrades gracefully without it.
    from dtaidistance import dtw as _dtw
except ImportError:  # pragma: no cover - optional dependency fallback
    _dtw = None

from .config import MAX_DATASETS, PipelineConfig
from .parser import ParseError, load_esense_csv

PHYSIO_MIN_US = 0.5
PHYSIO_MAX_US = 20.0
ZSCORE_THRESHOLD = 5.0
JUMP_SIGMA = 5.0
IQR_FACTOR = 1.5

FILTER_FS = 5.0
FILTER_CUTOFF = 0.05
FILTER_ORDER = 4

MAX_TESTED_BREAKPOINTS = 20
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
    alias_diagnostics: dict[str, dict[str, Any]]
    alias_curves: dict[str, np.ndarray]
    consensus_curve: np.ndarray
    breakpoints: list[int]
    segments: list[dict[str, Any]]
    used_downsample_factor: int
    alignment: dict[str, Any]
    identical_signal_groups: list[list[str]]


class PipelineError(ValueError):
    pass


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


def _estimate_step_seconds(time_minutes: np.ndarray) -> float:
    if len(time_minutes) < 2:
        return 0.2
    diffs = np.diff(time_minutes) * 60.0
    positive = diffs[np.isfinite(diffs) & (diffs > 0)]
    if positive.size == 0:
        return 0.2
    return float(np.median(positive))


def _estimate_step_minutes(relative_time_minutes: np.ndarray) -> float:
    if len(relative_time_minutes) < 2:
        return 0.2 / 60.0
    diffs = np.diff(relative_time_minutes)
    positive = diffs[np.isfinite(diffs) & (diffs > 0)]
    if positive.size == 0:
        return 0.2 / 60.0
    return float(np.median(positive))


def _change_intensity_curve(signal: np.ndarray, window_samples: int) -> np.ndarray:
    signal = np.asarray(signal, dtype=float)
    n = signal.size
    if n == 0:
        return signal

    window = max(1, int(window_samples))
    cumsum = np.concatenate([[0.0], np.cumsum(signal)])
    cumsum_sq = np.concatenate([[0.0], np.cumsum(signal * signal)])

    raw = np.zeros(n, dtype=float)

    for idx in range(n):
        left_start = max(0, idx - window)
        left_end = idx
        right_start = idx
        right_end = min(n, idx + window)

        left_count = left_end - left_start
        right_count = right_end - right_start

        if left_count <= 0:
            left_mean = signal[idx]
            left_std = 0.0
        else:
            left_sum = cumsum[left_end] - cumsum[left_start]
            left_sq_sum = cumsum_sq[left_end] - cumsum_sq[left_start]
            left_mean = left_sum / left_count
            left_var = max(0.0, (left_sq_sum / left_count) - left_mean**2)
            left_std = float(np.sqrt(left_var))

        if right_count <= 0:
            right_mean = signal[idx]
            right_std = 0.0
        else:
            right_sum = cumsum[right_end] - cumsum[right_start]
            right_sq_sum = cumsum_sq[right_end] - cumsum_sq[right_start]
            right_mean = right_sum / right_count
            right_var = max(0.0, (right_sq_sum / right_count) - right_mean**2)
            right_std = float(np.sqrt(right_var))

        raw[idx] = abs(right_mean - left_mean) + 0.5 * abs(right_std - left_std)

    p95 = float(np.percentile(raw, 95)) if np.isfinite(raw).any() else 0.0
    if p95 > 1e-12:
        raw = raw / p95

    return np.clip(raw, 0.0, None)


def _smooth_curve(curve: np.ndarray, sigma_seconds: float, step_seconds: float) -> np.ndarray:
    if curve.size == 0:
        return curve
    sigma_samples = max(1.0, float(sigma_seconds) / max(step_seconds, 1e-9))
    return gaussian_filter1d(curve, sigma=sigma_samples, mode="nearest")


def _elbow_k(ks: list[int], costs: list[float]) -> int:
    if len(ks) <= 2:
        return ks[0]

    x = np.asarray(ks, dtype=float)
    y = np.asarray(costs, dtype=float)

    if KneeLocator is not None:
        try:
            locator = KneeLocator(x, y, curve="convex", direction="decreasing")
            if locator.knee is not None:
                knee = float(locator.knee)
                nearest_idx = int(np.argmin(np.abs(x - knee)))
                return int(ks[nearest_idx])
        except Exception:
            pass

    x0, y0 = x[0], y[0]
    x1, y1 = x[-1], y[-1]

    denominator = float(np.hypot(y1 - y0, x1 - x0))
    if denominator <= 1e-12:
        return ks[int(np.argmin(y))]

    distances = np.abs((y1 - y0) * x - (x1 - x0) * y + x1 * y0 - y1 * x0) / denominator
    return ks[int(np.argmax(distances))]


def _compute_k_diagnostics(signal: np.ndarray, window_samples: int, manual_k_override: int | None = None) -> dict[str, Any]:
    signal = np.asarray(signal, dtype=float)
    n = signal.size

    diagnostics: dict[str, Any] = {
        "tested_k": [],
        "costs": [],
        "bics": [],
        "k_elbow": 1,
        "k_bic": 1,
        "selected_k": 1,
        "breakpoints": [],
    }

    if n < 4:
        return diagnostics

    width = max(2, min(int(window_samples), n - 1))

    try:
        algo = rpt.Window(width=width, model="rbf").fit(signal.reshape(-1, 1))
    except Exception:
        return diagnostics

    max_k = min(MAX_TESTED_BREAKPOINTS, max(1, n - 2))

    for k in range(1, max_k + 1):
        try:
            bkps = algo.predict(n_bkps=k)
            cost = float(algo.cost.sum_of_costs(bkps))
        except Exception:
            continue

        safe_cost = max(cost, 1e-12)
        bic = float(n * np.log(safe_cost / n) + k * np.log(n))

        diagnostics["tested_k"].append(k)
        diagnostics["costs"].append(safe_cost)
        diagnostics["bics"].append(bic)

    if not diagnostics["tested_k"]:
        return diagnostics

    ks: list[int] = diagnostics["tested_k"]
    costs: list[float] = diagnostics["costs"]
    bics: list[float] = diagnostics["bics"]

    diagnostics["k_elbow"] = _elbow_k(ks, costs)
    diagnostics["k_bic"] = ks[int(np.argmin(np.asarray(bics, dtype=float)))]

    if manual_k_override is not None and manual_k_override > 0:
        selected_k = int(np.clip(int(manual_k_override), ks[0], ks[-1]))
    else:
        selected_k = diagnostics["k_elbow"]
    diagnostics["selected_k"] = selected_k
    try:
        selected_bkps = algo.predict(n_bkps=selected_k)
    except Exception:
        selected_bkps = [n]

    internal = sorted({int(point) for point in selected_bkps if 1 <= int(point) < n})
    diagnostics["breakpoints"] = internal
    return diagnostics


def _detect_consensus_peaks(
    consensus_curve: np.ndarray,
    step_seconds: float,
    min_peak_distance_seconds: float,
) -> np.ndarray:
    if consensus_curve.size == 0:
        return np.array([], dtype=int)

    distance = max(1, int(np.ceil(min_peak_distance_seconds / max(step_seconds, 1e-9))))
    p75 = float(np.percentile(consensus_curve, 75)) if consensus_curve.size else 0.0
    p60 = float(np.percentile(consensus_curve, 60)) if consensus_curve.size else 0.0
    prominence = max(0.03, 0.35 * p75)

    peaks, _ = find_peaks(consensus_curve, distance=distance, prominence=prominence, height=p60)
    return peaks.astype(int)


def _cluster_breakpoints(
    breakpoints_by_alias: dict[str, list[int]],
    all_aliases: list[str],
    tolerance_samples: int,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for alias, breakpoints in breakpoints_by_alias.items():
        for point in breakpoints:
            points.append({"index": int(point), "alias": alias})

    if not points:
        return []

    points.sort(key=lambda item: item["index"])
    clusters: list[list[dict[str, Any]]] = []

    current: list[dict[str, Any]] = [points[0]]
    centroid = float(points[0]["index"])

    for item in points[1:]:
        if abs(float(item["index"]) - centroid) <= tolerance_samples:
            current.append(item)
            centroid = float(np.mean([entry["index"] for entry in current]))
        else:
            clusters.append(current)
            current = [item]
            centroid = float(item["index"])

    clusters.append(current)

    alias_count = max(1, len(all_aliases))
    payload: list[dict[str, Any]] = []
    for cluster in clusters:
        indexes = [entry["index"] for entry in cluster]
        aliases = sorted({str(entry["alias"]) for entry in cluster})
        payload.append(
            {
                "center": int(round(float(np.mean(indexes)))),
                "indexes": indexes,
                "aliases": aliases,
                "support": float(len(aliases) / alias_count),
            }
        )

    payload.sort(key=lambda item: item["center"])
    return payload


def _filter_breakpoint_clusters(
    clusters: list[dict[str, Any]],
    signal_len: int,
    min_segment_samples: int,
) -> list[dict[str, Any]]:
    if not clusters:
        return []

    min_segment = max(1, int(min_segment_samples))
    accepted: list[dict[str, Any]] = []
    prev = 0

    for cluster in sorted(clusters, key=lambda item: item["center"]):
        boundary = int(cluster["center"])
        if boundary <= 0 or boundary >= signal_len:
            continue
        if boundary - prev < min_segment:
            continue
        accepted.append(cluster)
        prev = boundary

    while accepted and (signal_len - int(accepted[-1]["center"])) < min_segment:
        accepted.pop()

    return accepted


def _build_breakpoint_reasons(
    clusters: list[dict[str, Any]],
    consensus_curve: np.ndarray,
    consensus_peaks: np.ndarray,
    step_seconds: float,
) -> dict[int, dict[str, Any]]:
    reasons: dict[int, dict[str, Any]] = {}
    if consensus_curve.size == 0:
        return reasons

    local_window = max(1, int(np.ceil(10.0 / max(step_seconds, 1e-9))))

    for cluster in clusters:
        boundary = int(cluster["center"])
        idx = int(np.clip(boundary, 0, len(consensus_curve) - 1))

        left = max(0, idx - local_window)
        right = min(len(consensus_curve), idx + local_window + 1)
        local = consensus_curve[left:right]
        local_mean = float(np.mean(local)) if local.size else 0.0
        local_std = float(np.std(local)) if local.size else 0.0
        boundary_cost = float(consensus_curve[idx])
        local_zscore = (boundary_cost - local_mean) / local_std if local_std > 1e-12 else 0.0

        peaks_left = consensus_peaks[consensus_peaks < boundary]
        peaks_right = consensus_peaks[consensus_peaks > boundary]

        left_peak_idx = int(peaks_left[-1]) if peaks_left.size else None
        right_peak_idx = int(peaks_right[0]) if peaks_right.size else None

        left_peak_value = float(consensus_curve[left_peak_idx]) if left_peak_idx is not None else None
        right_peak_value = float(consensus_curve[right_peak_idx]) if right_peak_idx is not None else None

        if len(cluster["aliases"]) >= 2 and cluster["support"] >= 0.66:
            quality = "strong_shared"
        elif len(cluster["aliases"]) >= 2 or cluster["support"] >= 0.5 or local_zscore >= 0.5:
            quality = "moderate_shared"
        else:
            quality = "weak_or_individual"

        reasons[boundary] = {
            "quality": quality,
            "support": float(cluster["support"]),
            "source_aliases": list(cluster["aliases"]),
            "local_zscore": float(local_zscore),
            "evidence": {
                "boundary_cost": boundary_cost,
                "left_peak_index": left_peak_idx,
                "right_peak_index": right_peak_idx,
                "left_peak_value": left_peak_value,
                "right_peak_value": right_peak_value,
                "left_valley_depth": (left_peak_value - boundary_cost) if left_peak_value is not None else None,
                "right_valley_depth": (right_peak_value - boundary_cost) if right_peak_value is not None else None,
            },
        }

    return reasons


def _build_segments(
    merged_decimated: pd.DataFrame,
    breakpoints_with_end: list[int],
    boundary_reasons: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if merged_decimated.empty:
        return segments

    start_idx = 0
    segment_number = 1
    signal_len = len(merged_decimated)

    for breakpoint in breakpoints_with_end:
        if breakpoint <= start_idx:
            continue
        if breakpoint > signal_len:
            breakpoint = signal_len

        # Keep data slices non-overlapping, but report boundary timestamp
        # as the same instant where the next segment starts.
        end_index = breakpoint - 1
        boundary_display_index = breakpoint if breakpoint < signal_len else signal_len - 1
        reason_boundary_index = int(breakpoint)
        if breakpoint == signal_len and start_idx in boundary_reasons:
            # For the final segment, carry the attribution from the last
            # real breakpoint that opened this segment.
            reason = boundary_reasons[start_idx]
            reason_boundary_index = int(start_idx)
        else:
            reason = boundary_reasons.get(
                breakpoint,
                {
                    "quality": "terminal_boundary" if breakpoint == signal_len else "unclassified",
                    "support": 1.0 if breakpoint == signal_len else 0.0,
                    "source_aliases": [],
                    "local_zscore": 0.0,
                    "evidence": {},
                },
            )

        segment = {
            "segment_no": segment_number,
            "start_time": str(merged_decimated["marker"].iloc[start_idx]),
            "end_time": str(merged_decimated["marker"].iloc[boundary_display_index]),
            "start_relative": float(merged_decimated["time"].iloc[start_idx]),
            "end_relative": float(merged_decimated["time"].iloc[boundary_display_index]),
            "start_index": int(start_idx),
            "end_index": int(end_index),
            "segment_reason": {
                "boundary_index": reason_boundary_index,
                **reason,
            },
        }
        segments.append(segment)

        start_idx = breakpoint
        segment_number += 1

    return segments


def _build_signal_segments(
    merged_decimated: pd.DataFrame,
    breakpoints_with_end: list[int],
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if merged_decimated.empty:
        return segments

    start_idx = 0
    segment_number = 1
    signal_len = len(merged_decimated)

    for breakpoint in breakpoints_with_end:
        if breakpoint <= start_idx:
            continue
        if breakpoint > signal_len:
            breakpoint = signal_len

        end_index = breakpoint - 1
        boundary_display_index = breakpoint if breakpoint < signal_len else signal_len - 1
        segments.append(
            {
                "segment_no": segment_number,
                "start_time": str(merged_decimated["marker"].iloc[start_idx]),
                "end_time": str(merged_decimated["marker"].iloc[boundary_display_index]),
                "start_relative": float(merged_decimated["time"].iloc[start_idx]),
                "end_relative": float(merged_decimated["time"].iloc[boundary_display_index]),
                "start_index": int(start_idx),
                "end_index": int(end_index),
            }
        )
        start_idx = breakpoint
        segment_number += 1

    return segments


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
        raise PipelineError("Not enough points for segmentation")

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

    step_seconds = _estimate_step_seconds(merged_decimated["time"].to_numpy(dtype=float))
    segment_width_seconds = float(max(1, int(config.segment_width)))
    window_samples = max(2, int(np.ceil(segment_width_seconds / max(step_seconds, 1e-9))))

    min_peak_distance_seconds = segment_width_seconds
    min_segment_seconds = segment_width_seconds

    alias_breakpoints: dict[str, list[int]] = {}
    alias_curves: dict[str, np.ndarray] = {}
    alias_diagnostics: dict[str, dict[str, Any]] = {}

    sigma_seconds = max(2.0, segment_width_seconds / 2.0)
    segment_k_override = int(config.segment_k_override) if config.segment_k_override is not None else None
    raw_overrides = config.segment_k_overrides or {}
    segment_k_overrides: dict[str, int] = {}
    for alias_key, raw_value in raw_overrides.items():
        try:
            numeric_value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if numeric_value > 0:
            segment_k_overrides[str(alias_key)] = numeric_value

    for alias in aliases:
        signal = merged_decimated[f"microsiemens_{alias}"].to_numpy(dtype=float)
        curve = _change_intensity_curve(signal, window_samples)
        smoothed_curve = _smooth_curve(curve, sigma_seconds=sigma_seconds, step_seconds=step_seconds)
        manual_k_override = segment_k_overrides.get(alias, segment_k_override)
        diagnostics = _compute_k_diagnostics(signal, window_samples, manual_k_override=manual_k_override)

        alias_breakpoints[alias] = diagnostics["breakpoints"]
        alias_curves[alias] = smoothed_curve
        alias_diagnostics[alias] = diagnostics

    if alias_curves:
        consensus_curve = np.max(np.vstack([alias_curves[alias] for alias in aliases]), axis=0)
    else:
        consensus_curve = np.zeros(len(merged_decimated), dtype=float)

    consensus_curve = _smooth_curve(consensus_curve, sigma_seconds=sigma_seconds, step_seconds=step_seconds)
    consensus_peaks = _detect_consensus_peaks(
        consensus_curve,
        step_seconds=step_seconds,
        min_peak_distance_seconds=min_peak_distance_seconds,
    )

    cluster_tolerance_seconds = max(float(config.cluster_tolerance_seconds), segment_width_seconds)
    cluster_tolerance_samples = max(1, int(np.ceil(cluster_tolerance_seconds / max(step_seconds, 1e-9))))
    clusters = _cluster_breakpoints(alias_breakpoints, aliases, tolerance_samples=cluster_tolerance_samples)

    min_segment_samples = max(1, int(np.ceil(min_segment_seconds / max(step_seconds, 1e-9))))
    filtered_clusters = _filter_breakpoint_clusters(
        clusters,
        signal_len=len(merged_decimated),
        min_segment_samples=min_segment_samples,
    )

    boundary_reasons = _build_breakpoint_reasons(
        filtered_clusters,
        consensus_curve=consensus_curve,
        consensus_peaks=consensus_peaks,
        step_seconds=step_seconds,
    )

    internal_breakpoints = [int(cluster["center"]) for cluster in filtered_clusters]
    breakpoints = sorted({*internal_breakpoints, len(merged_decimated)})
    segments = _build_segments(merged_decimated, breakpoints, boundary_reasons)

    return PipelineResult(
        aliases=aliases,
        dataset_states=dataset_states,
        cleaning_summary_by_alias=cleaning_summary_by_alias,
        cleaning_summary_totals=cleaning_summary_totals,
        merged_scaled=merged,
        merged_decimated=merged_decimated,
        alias_diagnostics=alias_diagnostics,
        alias_curves=alias_curves,
        consensus_curve=consensus_curve,
        breakpoints=breakpoints,
        segments=segments,
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
    """Descriptive statistics of one signal in microsiemens (std uses ddof=1, like segment statistics)."""
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


def serialize_signal_diagnostics(result: PipelineResult, max_points: int | None = None) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    df = result.merged_decimated
    if df.empty:
        return payload

    if max_points is None or max_points <= 0:
        stride = 1
    else:
        stride = max(1, int(np.ceil(len(df) / max_points)))
    sampled = df.iloc[::stride]

    for alias in result.aliases:
        diagnostics = result.alias_diagnostics.get(alias, {})
        tested_k = [int(value) for value in diagnostics.get("tested_k", [])]
        costs = [float(value) for value in diagnostics.get("costs", [])]
        bics = [float(value) for value in diagnostics.get("bics", [])]
        breakpoints = sorted({int(point) for point in diagnostics.get("breakpoints", []) if 1 <= int(point) < len(df)})
        boundary_times = [float(df["time"].iloc[point]) for point in breakpoints]
        signal_segments = _build_signal_segments(df, [*breakpoints, len(df)])

        payload.append(
            {
                "alias": alias,
                "tested_k": tested_k,
                "costs": costs,
                "bics": bics,
                "k_elbow": int(diagnostics.get("k_elbow", 1)),
                "k_bic": int(diagnostics.get("k_bic", 1)),
                "selected_k": int(diagnostics.get("selected_k", diagnostics.get("k_elbow", 1))),
                "breakpoints": breakpoints,
                "boundary_times": boundary_times,
                "segments": signal_segments,
                "signal": {
                    "x": sampled["time"].astype(float).tolist(),
                    "y": sampled[f"microsiemens_{alias}"].astype(float).tolist(),
                },
            }
        )

    return payload


def serialize_consensus_chart(result: PipelineResult, max_points: int | None = None) -> dict[str, Any]:
    df = result.merged_decimated
    if df.empty:
        return {"x": [], "consensus": [], "per_alias": [], "breakpoint_times": []}

    if max_points is None or max_points <= 0:
        stride = 1
    else:
        stride = max(1, int(np.ceil(len(df) / max_points)))
    sampled_idx = np.arange(0, len(df), stride)

    x = df["time"].to_numpy(dtype=float)[sampled_idx].tolist()

    consensus_sampled = result.consensus_curve[sampled_idx].tolist() if result.consensus_curve.size else []

    per_alias: list[dict[str, Any]] = []
    for alias in result.aliases:
        curve = result.alias_curves.get(alias)
        if curve is not None and curve.size > 0:
            per_alias.append({
                "alias": alias,
                "y": curve[sampled_idx].tolist(),
            })

    internal_bkps = sorted(set(result.breakpoints) - {len(df)})
    breakpoint_times = [float(df["time"].iloc[bp]) for bp in internal_bkps if 0 <= bp < len(df)]

    return {
        "x": x,
        "consensus": consensus_sampled,
        "per_alias": per_alias,
        "breakpoint_times": breakpoint_times,
    }


def _resolve_aliases(all_aliases: list[str], requested_aliases: list[str] | None) -> list[str]:
    if not requested_aliases:
        return all_aliases
    missing = sorted(set(requested_aliases) - set(all_aliases))
    if missing:
        raise PipelineError(f"Unknown aliases requested: {', '.join(missing)}")
    return requested_aliases


def _spearman_pvalue_matrix(data: np.ndarray, n_vars: int) -> np.ndarray:
    """N×N matrix of two-sided Spearman p-values; diagonal is 0."""
    if data.shape[0] < 3 or n_vars < 2:
        mat = np.ones((n_vars, n_vars), dtype=float)
        np.fill_diagonal(mat, 0.0)
        return mat
    result = spearmanr(data, axis=0)
    pvalues = np.asarray(result.pvalue, dtype=float)
    if pvalues.ndim == 0:  # exactly two variables → scalar p-value
        p = float(pvalues) if np.isfinite(pvalues) else 1.0
        return np.array([[0.0, p], [p, 0.0]])
    pvalues = np.where(np.isfinite(pvalues), pvalues, 1.0)
    np.fill_diagonal(pvalues, 0.0)
    return pvalues


def _correct_pvalue_matrix(
    pvalues: np.ndarray, method: str, alpha: float
) -> tuple[list[list[float]], list[list[bool]]]:
    """Apply multiple-comparison correction to the off-diagonal upper triangle."""
    n = pvalues.shape[0]
    upper = np.triu_indices(n, k=1)
    flat = np.where(np.isfinite(pvalues[upper]), pvalues[upper], 1.0)

    corrected = np.zeros((n, n), dtype=float)
    reject = np.zeros((n, n), dtype=bool)
    if flat.size:
        if method == "none":
            corrected_flat = flat
            reject_flat = flat < alpha
        else:
            reject_flat, corrected_flat, _, _ = multipletests(flat, alpha=alpha, method=method)
        for (i, j), value, significant in zip(zip(*upper), corrected_flat, reject_flat):
            corrected[i, j] = corrected[j, i] = float(value)
            reject[i, j] = reject[j, i] = bool(significant)
    return corrected.tolist(), reject.tolist()


def _fisher_ci_matrices(
    r_matrix: np.ndarray, n_samples: int, confidence_level: float
) -> tuple[list[list[float | None]], list[list[float | None]]]:
    """Fisher z-transform confidence intervals for each correlation cell."""
    n = r_matrix.shape[0]
    if n_samples <= 3:
        empty: list[list[float | None]] = [[None] * n for _ in range(n)]
        return empty, [row[:] for row in empty]
    r = np.clip(r_matrix, -0.999999, 0.999999)
    z = np.arctanh(r)
    se = 1.0 / np.sqrt(n_samples - 3)
    z_crit = float(norm.ppf(1.0 - (1.0 - confidence_level) / 2.0))
    lower = np.tanh(z - z_crit * se)
    upper = np.tanh(z + z_crit * se)
    np.fill_diagonal(lower, 1.0)
    np.fill_diagonal(upper, 1.0)
    return lower.tolist(), upper.tolist()


def _lag1_autocorr(series: np.ndarray) -> float:
    """Lag-1 autocorrelation of a series (0 when undefined)."""
    s = np.asarray(series, dtype=float)
    s = s[np.isfinite(s)]
    if s.size < 3:
        return 0.0
    s = s - s.mean()
    denom = float(np.dot(s, s))
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(s[:-1], s[1:]) / denom)


def _effective_df_significance(
    r_matrix: np.ndarray, data: np.ndarray, config: PipelineConfig
) -> dict[str, Any]:
    """Autocorrelation-adjusted p-values and Fisher CIs.

    GSR seconds are strongly autocorrelated, so the naive n=#samples hugely
    overstates the degrees of freedom. Per pair we deflate n to an effective
    sample size n_eff = n * (1 - r1*r2) / (1 + r1*r2) (Quenouille/Bartlett, with
    r1,r2 the lag-1 autocorrelations) and recompute the t-test p-value and the
    Fisher CI against n_eff-3.
    """
    n_vars = r_matrix.shape[0]
    n_samples = int(data.shape[0])
    z_crit = float(norm.ppf(1.0 - (1.0 - float(config.correlation_confidence_level)) / 2.0))

    acf1 = [_lag1_autocorr(data[:, k]) for k in range(n_vars)]
    n_eff = np.full((n_vars, n_vars), float(n_samples))
    p_eff = np.zeros((n_vars, n_vars), dtype=float)
    ci_low: list[list[float | None]] = [[None] * n_vars for _ in range(n_vars)]
    ci_high: list[list[float | None]] = [[None] * n_vars for _ in range(n_vars)]

    for i in range(n_vars):
        p_eff[i, i] = 0.0
        ci_low[i][i] = 1.0
        ci_high[i][i] = 1.0
        for j in range(i + 1, n_vars):
            ratio = (1.0 - acf1[i] * acf1[j]) / (1.0 + acf1[i] * acf1[j] + 1e-12)
            neff = float(np.clip(n_samples * ratio, 3.0, float(n_samples)))
            n_eff[i, j] = n_eff[j, i] = neff

            r = float(np.clip(r_matrix[i, j], -0.999999, 0.999999))
            if neff > 2:
                t_stat = r * np.sqrt((neff - 2.0) / (1.0 - r * r))
                p = float(2.0 * student_t.sf(abs(t_stat), neff - 2.0))
            else:
                p = 1.0
            p_eff[i, j] = p_eff[j, i] = p

            if neff > 3:
                se = 1.0 / np.sqrt(neff - 3.0)
                zf = np.arctanh(r)
                lo = float(np.tanh(zf - z_crit * se))
                hi = float(np.tanh(zf + z_crit * se))
            else:
                lo = hi = None
            ci_low[i][j] = ci_low[j][i] = lo
            ci_high[i][j] = ci_high[j][i] = hi

    alpha = 1.0 - float(config.correlation_confidence_level)
    corrected_eff, significant_eff = _correct_pvalue_matrix(p_eff, config.correlation_correction_method, alpha)
    return {
        "n_eff": n_eff.tolist(),
        "pvalues_effective": p_eff.tolist(),
        "pvalues_effective_corrected": corrected_eff,
        "significant_effective": significant_eff,
        "ci_lower_effective": ci_low,
        "ci_upper_effective": ci_high,
    }


def _correlation_significance(
    r_matrix: np.ndarray, data: np.ndarray, config: PipelineConfig
) -> dict[str, Any]:
    n_vars = r_matrix.shape[0]
    n_samples = int(data.shape[0])
    pvalues = _spearman_pvalue_matrix(data, n_vars)
    alpha = 1.0 - float(config.correlation_confidence_level)
    corrected, significant = _correct_pvalue_matrix(pvalues, config.correlation_correction_method, alpha)
    ci_lower, ci_upper = _fisher_ci_matrices(r_matrix, n_samples, config.correlation_confidence_level)
    payload: dict[str, Any] = {
        "pvalues": pvalues.tolist(),
        "pvalues_corrected": corrected,
        "significant": significant,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
    }
    if bool(config.correlation_effective_df):
        payload.update(_effective_df_significance(r_matrix, data, config))
    return payload


def compute_correlations(
    result: PipelineResult,
    dataset_aliases: list[str] | None = None,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    aliases = _resolve_aliases(result.aliases, dataset_aliases)
    columns = [f"microsiemens_{alias}" for alias in aliases]
    with_stats = config is not None and config.correlation_pvalues

    global_corr = result.merged_scaled[columns].corr(method="spearman").fillna(0.0)
    global_matrix = global_corr.to_numpy(dtype=float).tolist()

    per_segment: list[dict[str, Any]] = []
    for segment in result.segments:
        segment_df = result.merged_decimated.iloc[segment["start_index"] : segment["end_index"] + 1]
        if len(segment_df) < 2:
            corr_df = pd.DataFrame(np.eye(len(columns)), columns=columns, index=columns)
        else:
            corr_df = segment_df[columns].corr(method="spearman").fillna(0.0)
        entry = {
            "segment_no": segment["segment_no"],
            "start_time": segment["start_time"],
            "end_time": segment["end_time"],
            "start_relative": segment["start_relative"],
            "end_relative": segment["end_relative"],
            "matrix": corr_df.to_numpy(dtype=float).tolist(),
        }
        if with_stats:
            entry.update(
                _correlation_significance(
                    corr_df.to_numpy(dtype=float),
                    segment_df[columns].to_numpy(dtype=float),
                    config,
                )
            )
        per_segment.append(entry)

    payload: dict[str, Any] = {
        "aliases": aliases,
        "global_matrix": global_matrix,
        "per_segment": per_segment,
    }
    if with_stats:
        stats = _correlation_significance(
            np.asarray(global_matrix, dtype=float),
            result.merged_scaled[columns].to_numpy(dtype=float),
            config,
        )
        payload["global_pvalues"] = stats["pvalues"]
        payload["global_pvalues_corrected"] = stats["pvalues_corrected"]
        payload["global_significant"] = stats["significant"]
        payload["global_ci_lower"] = stats["ci_lower"]
        payload["global_ci_upper"] = stats["ci_upper"]
        if "n_eff" in stats:
            payload["global_effective_n"] = stats["n_eff"]
            payload["global_pvalues_effective"] = stats["pvalues_effective"]
            payload["global_pvalues_effective_corrected"] = stats["pvalues_effective_corrected"]
            payload["global_significant_effective"] = stats["significant_effective"]
            payload["global_ci_lower_effective"] = stats["ci_lower_effective"]
            payload["global_ci_upper_effective"] = stats["ci_upper_effective"]
    return payload


def _linregress_stats(t: np.ndarray, values: np.ndarray) -> dict[str, float | None]:
    """slope/intercept/p-value/R²/stderr and a 95% slope CI via scipy.linregress."""
    n = len(values)
    if n < 3 or not np.isfinite(values).all() or np.allclose(values, values[0]):
        slope = float(np.polyfit(np.arange(n), values, 1)[0]) if n >= 2 else 0.0
        return {
            "slope": slope,
            "intercept": float(values[0]) if n else 0.0,
            "pvalue": None,
            "rvalue": None,
            "rsquared": None,
            "stderr": None,
            "ci_low": None,
            "ci_high": None,
        }
    reg = linregress(t, values)
    t_crit = float(student_t.ppf(0.975, n - 2))
    margin = t_crit * float(reg.stderr)
    return {
        "slope": float(reg.slope),
        "intercept": float(reg.intercept),
        "pvalue": float(reg.pvalue),
        "rvalue": float(reg.rvalue),
        "rsquared": float(reg.rvalue) ** 2,
        "stderr": float(reg.stderr),
        "ci_low": float(reg.slope) - margin,
        "ci_high": float(reg.slope) + margin,
    }


def compute_trends(
    result: PipelineResult,
    dataset_aliases: list[str] | None = None,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    aliases = _resolve_aliases(result.aliases, dataset_aliases)
    segments_payload: list[dict[str, Any]] = []

    for segment in result.segments:
        segment_df = result.merged_decimated.iloc[segment["start_index"] : segment["end_index"] + 1]
        t = np.arange(len(segment_df))
        trends: dict[str, float] = {}
        trend_stats: dict[str, dict[str, float | None]] = {}
        for alias in aliases:
            column = f"microsiemens_{alias}"
            values = segment_df[column].to_numpy(dtype=float)
            slope = float(np.polyfit(t, values, 1)[0]) if len(values) >= 2 else 0.0
            trends[alias] = slope
            if config is not None:
                trend_stats[alias] = _linregress_stats(t, values)

        entry = {
            "segment_no": segment["segment_no"],
            "start_time": segment["start_time"],
            "end_time": segment["end_time"],
            "start_relative": segment["start_relative"],
            "end_relative": segment["end_relative"],
            "trends": trends,
        }
        if config is not None:
            entry["trend_stats"] = trend_stats
        segments_payload.append(entry)

    return {"aliases": aliases, "segments": segments_payload}


def _zscore(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    std = float(np.std(values))
    if std < 1e-12:
        return np.zeros_like(values)
    return (values - float(np.mean(values))) / std


def _window_correlation(a: np.ndarray, b: np.ndarray, method: str) -> float:
    if a.size < 2 or np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return 0.0
    if method == "spearman":
        a = pd.Series(a).rank().to_numpy(dtype=float)
        b = pd.Series(b).rank().to_numpy(dtype=float)
    value = np.corrcoef(a, b)[0, 1]
    return float(value) if np.isfinite(value) else 0.0


def _mean_coherence(x: np.ndarray, y: np.ndarray, fs: float) -> float:
    n = min(len(x), len(y))
    if n < 8:
        return 0.0
    try:
        freqs, cxy = coherence(x, y, fs=fs, nperseg=min(256, n))
    except ValueError:
        return 0.0
    if freqs.size == 0:
        return 0.0
    band = freqs <= min(1.0, fs / 2.0)
    values = cxy[band] if band.any() else cxy
    return float(np.mean(values)) if values.size else 0.0


def _lagged_xcorr(
    xz: np.ndarray,
    yz: np.ndarray,
    fs: float,
    max_lag_seconds: float,
    *,
    magnitude: bool = False,
) -> tuple[float, float, float]:
    """Cross-correlation of two already-z-scored series, restricted to +-max_lag.

    Returns (max_xcorr, lag_seconds_at_max, zero_lag_xcorr). Reporting both the
    lag-maximised value and the zero-lag value makes the positive bias of "max
    over lags" visible instead of hidden.
    """
    length = max(len(xz), 1)
    corr = correlate(xz, yz, mode="full") / length
    lags = correlation_lags(len(xz), len(yz), mode="full")
    max_lag = int(round(max_lag_seconds * fs))
    if max_lag > 0:
        mask = np.abs(lags) <= max_lag
        if mask.any():
            corr = corr[mask]
            lags = lags[mask]
    if corr.size == 0:
        return 0.0, 0.0, 0.0
    best = int(np.argmax(np.abs(corr))) if magnitude else int(np.argmax(corr))
    max_xcorr = float(corr[best])
    lag_seconds = float(lags[best] / fs) if fs > 0 else 0.0
    zero_lag = float(corr[int(np.argmin(np.abs(lags)))])
    return max_xcorr, lag_seconds, zero_lag


def _make_surrogate(series: np.ndarray, rng: np.random.Generator, method: str) -> np.ndarray:
    """One autocorrelation-preserving surrogate of a series.

    "circular" randomly circularly shifts the series (exactly preserves its own
    autocorrelation and marginal distribution). "phase" randomises Fourier phases
    while keeping the power spectrum (preserves linear autocorrelation). Both
    destroy the cross-timing between two series, which is what the null needs.
    """
    n = series.size
    if n < 3:
        return series.copy()
    if method == "phase":
        spectrum = np.fft.rfft(series)
        magnitudes = np.abs(spectrum)
        phases = np.angle(spectrum)
        random_phases = rng.uniform(0.0, 2.0 * np.pi, size=phases.shape)
        random_phases[0] = phases[0]  # keep DC phase so the surrogate stays real-valued
        if n % 2 == 0:
            random_phases[-1] = phases[-1]  # keep Nyquist phase for even-length series
        surrogate = np.fft.irfft(magnitudes * np.exp(1j * random_phases), n=n)
        return np.asarray(surrogate, dtype=float)
    shift = int(rng.integers(1, n))  # circular shift by 1..n-1
    return np.roll(series, shift)


def _surrogate_xcorr_pvalue(
    xz: np.ndarray,
    yz: np.ndarray,
    fs: float,
    config: PipelineConfig,
    rng: np.random.Generator,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """One-sided surrogate p-value for a pair's max cross-correlation.

    The null is built by re-maximising over lags on autocorrelation-preserving
    surrogates, so the same lag-search bias is present under the null and the
    p-value tests coupling BEYOND each series' own temporal structure.
    """
    magnitude = not bool(config.synchrony_signed)
    observed_raw, _, _ = _lagged_xcorr(
        xz,
        yz,
        fs,
        config.synchrony_max_lag_seconds,
        magnitude=magnitude,
    )
    observed = abs(observed_raw) if magnitude else observed_raw
    n_surrogates = int(config.synchrony_surrogate_count)
    if n_surrogates <= 0 or xz.size < 3:
        return {"p_value": None, "null_mean": None, "null_sd": None}
    null = np.empty(n_surrogates, dtype=float)
    for k in range(n_surrogates):
        if cancel_callback is not None and k % 10 == 0:
            cancel_callback()
        surrogate = _make_surrogate(yz, rng, config.synchrony_surrogate_method)
        null_raw, _, _ = _lagged_xcorr(
            xz,
            surrogate,
            fs,
            config.synchrony_max_lag_seconds,
            magnitude=magnitude,
        )
        null[k] = abs(null_raw) if magnitude else null_raw
    p_value = float((int(np.count_nonzero(null >= observed)) + 1) / (n_surrogates + 1))
    return {"p_value": p_value, "null_mean": float(null.mean()), "null_sd": float(null.std())}


def _pair_synchrony(
    x: np.ndarray,
    y: np.ndarray,
    fs: float,
    config: PipelineConfig,
    rng: np.random.Generator | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    xz = _zscore(x)
    yz = _zscore(y)
    max_xcorr, lag_seconds, zero_lag = _lagged_xcorr(
        xz,
        yz,
        fs,
        config.synchrony_max_lag_seconds,
        magnitude=not bool(config.synchrony_signed),
    )

    dtw_distance: float | None = None
    if config.synchrony_dtw_enabled and _dtw is not None and len(xz) >= 2:
        try:
            distance = _dtw.distance_fast(np.ascontiguousarray(xz), np.ascontiguousarray(yz))
        except Exception:  # pragma: no cover - fall back to pure-python DTW
            distance = _dtw.distance(xz, yz)
        dtw_distance = float(distance / len(xz))

    metrics: dict[str, Any] = {
        "max_xcorr": max_xcorr,
        "xcorr_zero_lag": zero_lag,
        "lag_seconds": lag_seconds,
        "coherence": _mean_coherence(x, y, fs),
        "dtw_distance": dtw_distance,
    }
    if rng is not None and bool(config.synchrony_surrogate_enabled):
        metrics.update(_surrogate_xcorr_pvalue(xz, yz, fs, config, rng, cancel_callback))
    return metrics


def _window_active_groups(
    values_by_pair: dict[tuple[int, int], float],
    n_aliases: int,
    threshold: float,
    min_size: int,
    signed: bool = True,
) -> list[frozenset[int]]:
    """Deterministic maximal cliques of the per-window synchrony graph.

    Edge exists when the pair correlation clears the threshold. When ``signed``
    only positive (in-phase) coupling counts; otherwise magnitude is used and an
    anti-phase pair is treated as synchronized (legacy behaviour).
    """
    adjacency: list[set[int]] = [set() for _ in range(n_aliases)]
    for (i, j), value in values_by_pair.items():
        edge_value = value if signed else abs(value)
        if edge_value >= threshold:
            adjacency[i].add(j)
            adjacency[j].add(i)

    cliques: list[frozenset[int]] = []

    def bron_kerbosch(r: set[int], p: set[int], x: set[int]) -> None:
        if not p and not x:
            if len(r) >= min_size:
                cliques.append(frozenset(r))
            return
        candidates_for_pivot = p | x
        pivot = (
            max(sorted(candidates_for_pivot), key=lambda node: len(p & adjacency[node]))
            if candidates_for_pivot
            else None
        )
        candidates = p - (adjacency[pivot] if pivot is not None else set())
        for node in sorted(candidates):
            bron_kerbosch(r | {node}, p & adjacency[node], x & adjacency[node])
            p.remove(node)
            x.add(node)

    bron_kerbosch(set(), set(range(n_aliases)), set())
    return sorted(set(cliques), key=lambda group: tuple(sorted(group)))


def _detect_sync_episodes(
    pair_series: dict[tuple[int, int], list[float]],
    centers: list[float],
    aliases: list[str],
    config: PipelineConfig,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Detect subgroup synchrony episodes: runs of consecutive windows where the
    same set of participants (>= min_size) stays pairwise-synchronized.

    Returns (episodes, active_counts) where active_counts[i] is the number of
    aliases belonging to any active group in window i. Times are window centers
    in relative minutes.
    """
    threshold = float(config.synchrony_threshold)
    signed = bool(config.synchrony_signed)
    min_size = max(2, int(config.synchrony_episode_min_size))
    min_duration_minutes = float(config.synchrony_episode_min_duration_seconds) / 60.0
    max_gap_minutes = float(config.synchrony_episode_max_gap_seconds) / 60.0

    n_windows = len(centers)
    active_counts = [0] * n_windows
    if not pair_series or n_windows == 0:
        return [], active_counts

    step_minutes = centers[1] - centers[0] if n_windows > 1 else 0.0
    # A run survives missing windows as long as the inactive stretch between two
    # active centers does not exceed max_gap (one step apart == no gap).
    allowed_center_gap = step_minutes + max_gap_minutes + 1e-9

    episodes: list[dict[str, Any]] = []
    open_runs: dict[frozenset[int], dict[str, Any]] = {}

    def group_strength(members: frozenset[int], idx: int) -> float:
        values = [
            (series[idx] if signed else abs(series[idx]))
            for (i, j), series in pair_series.items()
            if i in members and j in members
        ]
        return float(np.mean(values)) if values else 0.0

    def close_run(run: dict[str, Any]) -> None:
        if run["last_center"] - run["start_center"] + 1e-9 < min_duration_minutes:
            return
        members = sorted(run["members"])
        episodes.append(
            {
                "start_relative": run["start_center"],
                "end_relative": run["last_center"],
                "members": [aliases[i] for i in members],
                "size": len(members),
                "mean_strength": float(np.mean(run["strengths"])),
                "peak_strength": float(np.max(run["strengths"])),
            }
        )

    for idx, center in enumerate(centers):
        values_by_pair = {pair: series[idx] for pair, series in pair_series.items()}
        groups = _window_active_groups(values_by_pair, len(aliases), threshold, min_size, signed=signed)
        if groups:
            active_counts[idx] = len(frozenset.union(*groups))
        for group in groups:
            strength = group_strength(group, idx)
            run = open_runs.get(group)
            if run is not None and center - run["last_center"] <= allowed_center_gap:
                run["last_center"] = center
                run["strengths"].append(strength)
            else:
                if run is not None:
                    close_run(run)
                open_runs[group] = {
                    "members": group,
                    "start_center": center,
                    "last_center": center,
                    "strengths": [strength],
                }

    for run in open_runs.values():
        close_run(run)

    episodes.sort(key=lambda episode: (episode["start_relative"], episode["end_relative"]))
    return episodes, active_counts


def _rolling_synchrony(
    signals: dict[str, np.ndarray],
    aliases: list[str],
    rel: np.ndarray,
    fs: float,
    config: PipelineConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    n_samples = len(rel)
    window = max(2, int(round(config.synchrony_window_seconds * fs)))
    step = max(1, int(round(config.synchrony_step_seconds * fs)))
    method = config.synchrony_correlation_method
    pair_indices = [(i, j) for i in range(len(aliases)) for j in range(i + 1, len(aliases))]

    centers: list[float] = []
    pair_series: dict[tuple[int, int], list[float]] = {pair: [] for pair in pair_indices}
    group_mean: list[float] = []

    if pair_indices and window <= n_samples:
        for start in range(0, n_samples - window + 1, step):
            end = start + window
            centers.append(float(rel[start + window // 2]))
            per_pair: list[float] = []
            for i, j in pair_indices:
                value = _window_correlation(signals[aliases[i]][start:end], signals[aliases[j]][start:end], method)
                pair_series[(i, j)].append(value)
                per_pair.append(value if bool(config.synchrony_signed) else abs(value))
            group_mean.append(float(np.mean(per_pair)) if per_pair else 0.0)

    threshold = float(config.synchrony_threshold)
    episodes, active_counts = _detect_sync_episodes(pair_series, centers, aliases, config)

    windows_payload = {
        "centers": centers,
        "group_mean": group_mean,
        "threshold": threshold,
        "active_counts": active_counts,
        "pairs": [
            {"alias_a": aliases[i], "alias_b": aliases[j], "values": pair_series[(i, j)]}
            for i, j in pair_indices
        ],
    }
    return windows_payload, episodes


def _cluster_signals(aliases: list[str], sync_matrix: np.ndarray, config: PipelineConfig) -> dict[str, Any]:
    n = len(aliases)
    if n < 2:
        return {
            "labels": {alias: 0 for alias in aliases},
            "cluster_count": n,
            "clusters": [{"cluster": 0, "members": list(aliases), "mean_synchrony": None}] if n else [],
        }

    # Signed similarity: anti-phase pairs (negative synchrony) are far apart, so
    # they do not end up clustered together. Legacy magnitude mode keeps |corr|.
    similarity = np.clip(sync_matrix, -1.0, 1.0) if bool(config.synchrony_signed) else np.abs(sync_matrix)
    distance = np.clip(1.0 - similarity, 0.0, 2.0)
    np.fill_diagonal(distance, 0.0)
    model = AgglomerativeClustering(
        metric="precomputed",
        linkage="average",
        distance_threshold=float(config.synchrony_cluster_distance_threshold),
        n_clusters=None,
    )
    labels = model.fit_predict(distance)
    label_map = {alias: int(label) for alias, label in zip(aliases, labels)}

    clusters: list[dict[str, Any]] = []
    for label in sorted(set(labels.tolist())):
        members_idx = [i for i in range(n) if labels[i] == label]
        members = [aliases[i] for i in members_idx]
        if len(members_idx) > 1:
            values = [sync_matrix[a, b] for a in members_idx for b in members_idx if a < b]
            transformed = values if bool(config.synchrony_signed) else np.abs(values)
            mean_sync = float(np.mean(transformed)) if values else None
        else:
            mean_sync = None
        clusters.append({"cluster": int(label), "members": members, "mean_synchrony": mean_sync})

    return {"labels": label_map, "cluster_count": len(clusters), "clusters": clusters}


def _group_surrogate_pvalue(
    signals: dict[str, np.ndarray],
    aliases: list[str],
    fs: float,
    config: PipelineConfig,
    observed_mean: float,
    signed: bool,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """One-sided surrogate p-value for the group mean pairwise max-xcorr."""
    n = len(aliases)
    n_surrogates = int(config.synchrony_surrogate_count)
    if n_surrogates <= 0 or n < 2:
        return {"p_value": None, "null_mean": None, "null_sd": None}
    zsig = {alias: _zscore(signals[alias]) for alias in aliases}
    rng = np.random.default_rng(int(config.synchrony_surrogate_seed) + 1)
    pair_indices = [(i, j) for i in range(n) for j in range(i + 1, n)]
    null = np.empty(n_surrogates, dtype=float)
    for k in range(n_surrogates):
        if cancel_callback is not None and k % 10 == 0:
            cancel_callback()
        surrogate = {alias: _make_surrogate(zsig[alias], rng, config.synchrony_surrogate_method) for alias in aliases}
        values = []
        for i, j in pair_indices:
            mx, _, _ = _lagged_xcorr(
                surrogate[aliases[i]],
                surrogate[aliases[j]],
                fs,
                config.synchrony_max_lag_seconds,
                magnitude=not signed,
            )
            values.append(mx if signed else abs(mx))
        null[k] = float(np.mean(values)) if values else 0.0
    return {
        "p_value": float((int(np.count_nonzero(null >= observed_mean)) + 1) / (n_surrogates + 1)),
        "null_mean": float(null.mean()),
        "null_sd": float(null.std()),
    }


def compute_synchrony(
    result: PipelineResult,
    dataset_aliases: list[str] | None = None,
    config: PipelineConfig | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Pairwise synchrony metrics + rolling-window subgroup episodes + clustering."""
    cfg = config if config is not None else PipelineConfig()
    aliases = _resolve_aliases(result.aliases, dataset_aliases)
    columns = [f"microsiemens_{alias}" for alias in aliases]
    frame = result.merged_decimated
    rel = frame["time"].to_numpy(dtype=float)  # relative time in minutes
    step_seconds = _estimate_step_seconds(rel)
    fs = 1.0 / step_seconds if step_seconds > 0 else FILTER_FS
    signals = {alias: frame[column].to_numpy(dtype=float) for alias, column in zip(aliases, columns)}
    n = len(aliases)

    signed = bool(cfg.synchrony_signed)
    surrogate_enabled = bool(cfg.synchrony_surrogate_enabled)
    # Single generator threaded through all pairs → deterministic given the seed.
    rng = np.random.default_rng(int(cfg.synchrony_surrogate_seed)) if surrogate_enabled else None

    sync_matrix = np.eye(n)
    pairs: list[dict[str, Any]] = []
    for i in range(n):
        for j in range(i + 1, n):
            if cancel_callback is not None:
                cancel_callback()
            metrics = _pair_synchrony(
                signals[aliases[i]],
                signals[aliases[j]],
                fs,
                cfg,
                rng=rng,
                cancel_callback=cancel_callback,
            )
            pairs.append({"alias_a": aliases[i], "alias_b": aliases[j], **metrics})
            strength = metrics["max_xcorr"] if signed else abs(metrics["max_xcorr"])
            sync_matrix[i, j] = sync_matrix[j, i] = strength

    windows_payload, episodes = _rolling_synchrony(signals, aliases, rel, fs, cfg)
    clusters = _cluster_signals(aliases, sync_matrix, cfg)
    mean_sync = float(sync_matrix[np.triu_indices(n, 1)].mean()) if n > 1 else 0.0

    stats: dict[str, Any] = {
        "mean_synchrony": mean_sync,
        "episode_count": len(episodes),
        "max_episode_size": max((episode["size"] for episode in episodes), default=0),
        "cluster_count": clusters["cluster_count"],
        "window_count": len(windows_payload["centers"]),
        "signed": signed,
    }
    if surrogate_enabled and n > 1:
        group_surrogate = _group_surrogate_pvalue(
            signals,
            aliases,
            fs,
            cfg,
            mean_sync,
            signed,
            cancel_callback,
        )
        stats["mean_synchrony_p_value"] = group_surrogate["p_value"]
        stats["mean_synchrony_null_mean"] = group_surrogate["null_mean"]
        stats["mean_synchrony_null_sd"] = group_surrogate["null_sd"]
        stats["surrogate_method"] = str(cfg.synchrony_surrogate_method)
        stats["surrogate_count"] = int(cfg.synchrony_surrogate_count)

    return {
        "aliases": aliases,
        "fs": float(fs),
        "pairs": pairs,
        "sync_matrix": sync_matrix.tolist(),
        "windows": windows_payload,
        "episodes": episodes,
        "clusters": clusters,
        "stats": stats,
    }


def _mean_pairwise_spearman(sub: np.ndarray) -> float:
    """Mean pairwise (signed) Spearman correlation across rows of a window."""
    n = sub.shape[0]
    rhos: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            if sub.shape[1] >= 3 and np.std(sub[i]) > 1e-9 and np.std(sub[j]) > 1e-9:
                r, _ = spearmanr(sub[i], sub[j])
                if r == r:
                    rhos.append(float(r))
    return float(np.mean(rhos)) if rhos else 0.0


def _leave_one_out_contributors(
    signal_matrix: np.ndarray, aliases: list[str], s_idx: int, e_idx: int, contrib_rho: float
) -> list[str]:
    """Participants whose signal is positively (in-phase) correlated with the
    REST of the group over [s_idx, e_idx) — leave-one-out signed Spearman.
    Excludes flat/independent/anti-phase participants who merely sit near the mean.
    """
    n = signal_matrix.shape[0]
    sub = signal_matrix[:, s_idx:e_idx]
    contributors: list[str] = []
    for i in range(n):
        others = np.delete(sub, i, axis=0)
        if others.shape[0] == 0:
            continue
        others_mean = others.mean(axis=0)
        if sub.shape[1] >= 3 and np.std(sub[i]) > 1e-9 and np.std(others_mean) > 1e-9:
            r, _ = spearmanr(sub[i], others_mean)
            if r == r and r >= contrib_rho:
                contributors.append(aliases[i])
    return contributors


def _merge_index_episodes(
    episodes: list[tuple[int, int, float]], gap_samples: int
) -> list[tuple[int, int, float]]:
    """Merge (start_idx, end_idx, strength) windows whose gap <= gap_samples."""
    if not episodes:
        return []
    ordered = sorted(episodes, key=lambda item: item[0])
    merged: list[list[float]] = [list(ordered[0])]
    for start, end, strength in ordered[1:]:
        if start - merged[-1][1] <= gap_samples:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = max(merged[-1][2], strength)
        else:
            merged.append([start, end, strength])
    return [(int(s), int(e), float(strength)) for s, e, strength in merged]


def compute_contagion(
    result: PipelineResult,
    dataset_aliases: list[str] | None = None,
    config: PipelineConfig | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Shared-arousal ("emotional contagion") analysis.

    Ports the standalone contagion script into the pipeline, corrected:
      * z-scoring is WITHIN the analysis window (merged_decimated), not over the
        whole recording;
      * episode "contributors" require positive (in-phase) correlation with the
        rest of the group (anti-phase excluded);
      * the co-arousal-index null uses an autocorrelation-preserving circular-
        shift surrogate instead of a per-sample shuffle.
    """
    cfg = config if config is not None else PipelineConfig()
    aliases = _resolve_aliases(result.aliases, dataset_aliases)
    frame = result.merged_decimated
    n = len(aliases)
    rel_min = frame["time"].to_numpy(dtype=float)
    secs = rel_min * 60.0
    step_seconds = _estimate_step_seconds(rel_min)
    total = len(frame)

    if n == 0 or total == 0:
        return {
            "aliases": aliases,
            "state_percentages": [],
            "episodes": [],
            "primary_timeline": [],
            "state_timeline": {"times_min": [], "per_alias": []},
            "coarousal_index": 0.0,
            "coarousal_p_value": None,
            "coarousal_null_mean": None,
            "coarousal_null_sd": None,
            "high_z": float(cfg.contagion_high_z),
            "low_z": float(cfg.contagion_low_z),
        }

    signal_matrix = np.vstack([_zscore(frame[f"microsiemens_{alias}"].to_numpy(dtype=float)) for alias in aliases])

    high_z = float(cfg.contagion_high_z)
    low_z = float(cfg.contagion_low_z)
    state = np.zeros_like(signal_matrix, dtype=int)
    state[signal_matrix > high_z] = 1
    state[signal_matrix < low_z] = -1
    n_high = (state == 1).sum(axis=0)
    n_low = (state == -1).sum(axis=0)
    sync_index = np.maximum(n_high, n_low)

    state_percentages = [
        {
            "alias": alias,
            "high_pct": float((state[i] == 1).mean() * 100.0),
            "neutral_pct": float((state[i] == 0).mean() * 100.0),
            "low_pct": float((state[i] == -1).mean() * 100.0),
        }
        for i, alias in enumerate(aliases)
    ]

    win = max(1, int(round(cfg.contagion_window_seconds / max(step_seconds, 1e-9))))
    step = max(1, int(round(cfg.contagion_step_seconds / max(step_seconds, 1e-9))))
    min_participants = int(cfg.contagion_min_participants)
    agreement = float(cfg.contagion_min_agreement_pct)
    comovement_rho = float(cfg.contagion_comovement_rho)
    gap_samples = max(1, int(round(cfg.contagion_min_episode_gap_seconds / max(step_seconds, 1e-9))))

    raw_high: list[tuple[int, int, float]] = []
    raw_low: list[tuple[int, int, float]] = []
    raw_comov: list[tuple[int, int, float]] = []
    if total >= win and n >= 2:
        for s in range(0, total - win + 1, step):
            if cancel_callback is not None and s % max(step * 100, 1) == 0:
                cancel_callback()
            e = s + win
            if (n_high[s:e] >= min_participants).mean() >= agreement:
                raw_high.append((s, e, float(n_high[s:e].mean())))
            if (n_low[s:e] >= min_participants).mean() >= agreement:
                raw_low.append((s, e, float(n_low[s:e].mean())))
            mean_rho = _mean_pairwise_spearman(signal_matrix[:, s:e])
            if mean_rho >= comovement_rho:
                raw_comov.append((s, e, mean_rho))

    merged_high = _merge_index_episodes(raw_high, gap_samples)
    merged_low = _merge_index_episodes(raw_low, gap_samples)
    merged_comov = _merge_index_episodes(raw_comov, gap_samples)

    member_pct = float(cfg.contagion_member_pct)
    contrib_rho = float(cfg.contagion_contrib_rho)

    def summarize(s_idx: int, e_idx: int, direction: str) -> dict[str, Any]:
        s_idx = max(0, s_idx)
        e_idx = min(total, e_idx)
        if direction in ("High", "Low"):
            code = 1 if direction == "High" else -1
            frac = (state[:, s_idx:e_idx] == code).mean(axis=1)
            contributors = [aliases[i] for i in range(n) if frac[i] >= member_pct]
        else:  # Co-movement
            contributors = _leave_one_out_contributors(signal_matrix, aliases, s_idx, e_idx, contrib_rho)
        non = [alias for alias in aliases if alias not in contributors]
        n_c = len(contributors)
        start_s = float(secs[s_idx])
        end_s = float(secs[min(e_idx - 1, total - 1)])
        return {
            "direction": direction,
            "start_s": round(start_s, 1),
            "end_s": round(end_s, 1),
            "start_min": round(start_s / 60.0, 2),
            "end_min": round(end_s / 60.0, 2),
            "duration_s": round(end_s - start_s, 1),
            "n_contributors": n_c,
            "strength": "full" if n_c >= n else "strong" if n_c >= n - 1 else "partial",
            "contributors": contributors,
            "non_contributors": non,
        }

    episodes: list[dict[str, Any]] = []
    for s_idx, e_idx, _ in merged_high:
        episodes.append(summarize(s_idx, e_idx, "High"))
    for s_idx, e_idx, _ in merged_low:
        episodes.append(summarize(s_idx, e_idx, "Low"))
    for s_idx, e_idx, _ in merged_comov:
        episodes.append(summarize(s_idx, e_idx, "Co-movement"))
    episodes.sort(key=lambda ep: ep["start_s"])

    # Primary (non-overlapping) timeline: High/Low win over Co-movement.
    primary = np.array([""] * total, dtype=object)
    precedence = ["Co-movement", "Low", "High"]  # applied low→high so High overwrites
    merged_by_direction = {"High": merged_high, "Low": merged_low, "Co-movement": merged_comov}
    for direction in precedence:
        for s_idx, e_idx, _ in merged_by_direction[direction]:
            primary[max(0, s_idx):min(total, e_idx)] = direction
    primary_timeline: list[dict[str, Any]] = []
    k = 0
    while k < total:
        label = primary[k]
        if label == "":
            k += 1
            continue
        j = k
        while j < total and primary[j] == label:
            j += 1
        primary_timeline.append(
            {
                "label": label,
                "start_min": round(float(secs[k]) / 60.0, 2),
                "end_min": round(float(secs[j - 1]) / 60.0, 2),
                "duration_s": round(float(secs[j - 1] - secs[k]), 1),
            }
        )
        k = j

    # Co-arousal index + autocorrelation-preserving surrogate null.
    observed_index = float(sync_index.mean())
    coarousal_p = coarousal_null_mean = coarousal_null_sd = None
    n_surrogates = int(cfg.contagion_surrogate_count)
    if n_surrogates > 0 and n >= 2:
        rng = np.random.default_rng(int(cfg.contagion_surrogate_seed))
        null = np.empty(n_surrogates, dtype=float)
        for idx in range(n_surrogates):
            if cancel_callback is not None and idx % 10 == 0:
                cancel_callback()
            shuffled = np.vstack([_make_surrogate(signal_matrix[i], rng, "circular") for i in range(n)])
            sh_state = np.zeros_like(shuffled, dtype=int)
            sh_state[shuffled > high_z] = 1
            sh_state[shuffled < low_z] = -1
            sh_high = (sh_state == 1).sum(axis=0)
            sh_low = (sh_state == -1).sum(axis=0)
            null[idx] = float(np.maximum(sh_high, sh_low).mean())
        coarousal_p = float((int(np.count_nonzero(null >= observed_index)) + 1) / (n_surrogates + 1))
        coarousal_null_mean = float(null.mean())
        coarousal_null_sd = float(null.std())

    # 1-minute-binned per-participant state, for a heatmap.
    bin_samples = max(1, int(round(60.0 / max(step_seconds, 1e-9))))
    n_bins = max(1, int(np.ceil(total / bin_samples)))
    times_min = [round(float(secs[min(b * bin_samples, total - 1)]) / 60.0, 2) for b in range(n_bins)]
    per_alias_states = [
        {
            "alias": alias,
            "states": [
                float(state[i, b * bin_samples:min((b + 1) * bin_samples, total)].mean())
                for b in range(n_bins)
            ],
        }
        for i, alias in enumerate(aliases)
    ]

    return {
        "aliases": aliases,
        "state_percentages": state_percentages,
        "episodes": episodes,
        "primary_timeline": primary_timeline,
        "state_timeline": {"times_min": times_min, "per_alias": per_alias_states},
        "coarousal_index": observed_index,
        "coarousal_p_value": coarousal_p,
        "coarousal_null_mean": coarousal_null_mean,
        "coarousal_null_sd": coarousal_null_sd,
        "high_z": high_z,
        "low_z": low_z,
        "stats": {
            "episode_count": len(episodes),
            "high_episode_count": len(merged_high),
            "low_episode_count": len(merged_low),
            "comovement_episode_count": len(merged_comov),
            "coarousal_significant": (coarousal_p is not None and coarousal_p < 0.05),
        },
    }


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


def build_segment_statistics(
    result: PipelineResult,
    dataset_aliases: list[str] | None = None,
    segment_numbers: list[int] | None = None,
) -> list[dict[str, Any]]:
    aliases = _resolve_aliases(result.aliases, dataset_aliases)
    selected_segments = result.segments
    if segment_numbers:
        segment_set = set(segment_numbers)
        selected_segments = [segment for segment in selected_segments if segment["segment_no"] in segment_set]

    rows: list[dict[str, Any]] = []
    for segment in selected_segments:
        start_rel = segment["start_relative"]
        end_rel = segment["end_relative"]
        mask = (result.merged_scaled["relative_time"] >= start_rel) & (result.merged_scaled["relative_time"] <= end_rel)

        for alias in aliases:
            column = f"microsiemens_{alias}"
            values = result.merged_scaled.loc[mask, column].dropna()
            if values.empty:
                continue
            description = values.describe(percentiles=[0.25, 0.5, 0.75])
            rows.append(
                {
                    "dataset_alias": alias,
                    "segment_no": segment["segment_no"],
                    "start_time": segment["start_time"],
                    "end_time": segment["end_time"],
                    "count": float(description.get("count", np.nan)),
                    "mean": float(description.get("mean", np.nan)),
                    "std": float(description.get("std", np.nan)),
                    "min": float(description.get("min", np.nan)),
                    "25%": float(description.get("25%", np.nan)),
                    "50%": float(description.get("50%", np.nan)),
                    "75%": float(description.get("75%", np.nan)),
                    "max": float(description.get("max", np.nan)),
                }
            )

    return rows


def build_segment_export_rows(
    result: PipelineResult,
    project_id: str,
    dataset_aliases: list[str] | None = None,
    segment_numbers: list[int] | None = None,
) -> list[dict[str, Any]]:
    return [
        {"project_id": project_id, **row}
        for row in build_segment_statistics(
            result,
            dataset_aliases=dataset_aliases,
            segment_numbers=segment_numbers,
        )
    ]
