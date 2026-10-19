"""Joint synchronization: pairwise coupling, rolling-window subgroup episodes and clustering.

``compute_synchrony`` is carried over unchanged from GSRv2 ``gsr/pipeline/core.py``.
``synchrony_types`` adds what GSRv2 did not have, from the same windows and with the same
episode rules: divergent synchrony (Hassan's e-mail; the VSR Interview Flow Chart's
criteria read as the strength of the correlation).
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy.signal import coherence, correlate, correlation_lags
from sklearn.cluster import AgglomerativeClustering

try:  # DTW is optional; synchrony degrades gracefully without it.
    from dtaidistance import dtw as _dtw
except ImportError:  # pragma: no cover - optional dependency fallback
    _dtw = None

from .common import _estimate_step_seconds, _make_surrogate, _resolve_aliases, _zscore
from .config import PipelineConfig
from .preprocessing import FILTER_FS, PipelineResult


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


def divergent_episodes(synchrony: dict[str, Any], config: PipelineConfig) -> list[dict[str, Any]]:
    """Divergent synchrony: subgroups whose every pair's windowed r is at or below ``-synchrony_divergent_threshold``.

    The signals move systematically in opposite directions: the same strength of relation as synchrony, with the
    opposite sign. The episode rules are the synchronization episodes' own (minimum size and duration, bridged
    gaps); ``mean_strength`` and ``peak_strength`` are the (negative) mean and lowest r. Signed mode only: in the
    legacy unsigned mode an anti-phase pair already counts as synchronized.
    """
    if not bool(config.synchrony_signed):
        return []
    windows = synchrony.get("windows") or {}
    aliases = list(synchrony.get("aliases") or [])
    index = {alias: position for position, alias in enumerate(aliases)}
    # Negated, the divergent windows clear the threshold as synchronized ones do.
    negated = {(index[pair["alias_a"]], index[pair["alias_b"]]): [-value for value in pair["values"]]
               for pair in windows.get("pairs", [])}
    rules = config.model_copy(update={"synchrony_threshold": config.synchrony_divergent_threshold})
    episodes, _active = _detect_sync_episodes(negated, list(windows.get("centers", [])), aliases, rules)
    return [{**episode, "mean_strength": -episode["mean_strength"], "peak_strength": -episode["peak_strength"]}
            for episode in episodes]


def synchrony_types(synchrony: dict[str, Any], config: PipelineConfig) -> dict[str, Any]:
    """``synchrony`` (``compute_synchrony``'s payload) with the types GSRv2 did not have: ``divergent_episodes``."""
    divergent = divergent_episodes(synchrony, config)
    return {
        **synchrony,
        "divergent_episodes": divergent,
        "stats": {**synchrony.get("stats", {}), "divergent_episode_count": len(divergent)},
    }
