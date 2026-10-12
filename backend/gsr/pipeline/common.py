"""Signal helpers shared by preprocessing, synchrony and co-arousal."""

from __future__ import annotations

import numpy as np


class PipelineError(ValueError):
    pass


def _estimate_step_seconds(time_minutes: np.ndarray) -> float:
    if len(time_minutes) < 2:
        return 0.2
    diffs = np.diff(time_minutes) * 60.0
    positive = diffs[np.isfinite(diffs) & (diffs > 0)]
    if positive.size == 0:
        return 0.2
    return float(np.median(positive))


def _resolve_aliases(all_aliases: list[str], requested_aliases: list[str] | None) -> list[str]:
    if not requested_aliases:
        return all_aliases
    missing = sorted(set(requested_aliases) - set(all_aliases))
    if missing:
        raise PipelineError(f"Unknown aliases requested: {', '.join(missing)}")
    return requested_aliases


def _zscore(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    std = float(np.std(values))
    if std < 1e-12:
        return np.zeros_like(values)
    return (values - float(np.mean(values))) / std


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
