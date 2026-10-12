"""Co-arousal (shared arousal, "emotional contagion" in GSRv2).

Carried over unchanged from GSRv2 ``gsr/pipeline/core.py`` (``compute_contagion``),
exposed here as ``compute_coarousal``. Its corrections relative to the original
standalone contagion script:
  * z-scoring is WITHIN the analysis window (merged_decimated), not over the
    whole recording;
  * episode "contributors" require positive (in-phase) correlation with the
    rest of the group (anti-phase excluded);
  * the co-arousal-index null uses an autocorrelation-preserving circular-
    shift surrogate instead of a per-sample shuffle.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
from scipy.stats import spearmanr

from .common import _estimate_step_seconds, _make_surrogate, _resolve_aliases, _zscore
from .config import PipelineConfig
from .preprocessing import PipelineResult


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


def compute_coarousal(
    result: PipelineResult,
    dataset_aliases: list[str] | None = None,
    config: PipelineConfig | None = None,
    cancel_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Shared-arousal analysis: High/Low/Co-movement episodes and the co-arousal index."""
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
