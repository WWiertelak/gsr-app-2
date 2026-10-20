"""Signal and time helpers shared by preprocessing, synchrony, co-arousal and the interview moments."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np


class PipelineError(ValueError):
    """A pipeline failure; ``code``, ``alias`` and ``details`` let the UI explain it to a person.

    The message text matches GSRv2. ``details`` holds code-specific data, e.g. the
    recorded ``ranges`` ({alias: (start, end)}) for ``no_overlap``.
    """

    def __init__(
        self, message: str, *, code: str | None = None, alias: str | None = None, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.alias = alias
        self.details = details or {}


def clock_start(alignment: dict[str, Any] | None) -> datetime | None:
    """The recordings' own clock at lesson time 0 (their common start, ``PipelineResult.alignment``), or None.

    Lesson time counts from the common start, so the clock of any lesson time t is this plus t.
    """
    try:
        return datetime.fromisoformat(str((alignment or {})["start"]))
    except (KeyError, TypeError, ValueError):
        return None


def clock_at(start: datetime | None, seconds: float | None, *, milliseconds: bool = False) -> str | None:
    """The clock ``seconds`` of lesson time after ``start`` as HH:MM:SS (HH:MM:SS.fff with ``milliseconds``).

    Whole seconds are cut, not rounded, as everywhere the app has shown the sensor clock.
    """
    if start is None or seconds is None:
        return None
    moment = start + timedelta(seconds=float(seconds))
    return moment.strftime("%H:%M:%S.%f")[:-3] if milliseconds else moment.strftime("%H:%M:%S")


def clock_seconds_of_day(start: datetime | None) -> float | None:
    """``start`` as seconds after midnight, the origin the charts put the clock axis on."""
    if start is None:
        return None
    return (start - start.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()


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
