"""Synthetic eSense GSR recordings for equivalence tests.

No real participant data: every recording is generated from a seeded model of a
group session (shared skin-conductance responses to common events, individual
tonic levels and drift, noise) and written in the eSense CSV export layout with
the quirks the parser handles (metadata preamble, pause rows, comma decimals,
Excel-mangled decimals, markers without milliseconds, cp1250 encoding).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

SAMPLE_PERIOD_MS = 200  # eSense sensors record at ~5 Hz


@dataclass(frozen=True)
class Participant:
    """How one participant reacts to the shared session events."""

    tonic_us: float
    drift_us_per_min: float = 0.0
    # +1 in-phase responder, -1 anti-phase, 0 ignores shared events.
    coupling: float = 1.0
    response_us: float = 0.8
    lag_seconds: float = 0.0
    own_events: int = 0
    noise_us: float = 0.02


@dataclass(frozen=True)
class RecordingFormat:
    start_time: str = "12:00:00:000"
    polish: bool = False
    encoding: str = "utf-8"
    pause_rows: tuple[int, ...] = ()
    paused_label: str = "Wstrzymane"
    # Rows whose MICROSIEMENS cell is replaced with an Excel-mangled date token.
    excel_tokens: dict[int, str] = field(default_factory=dict)
    # Rows whose time marker loses its milliseconds (rebuilt from the SECOND column).
    markers_without_ms: tuple[int, ...] = ()
    # Rows replaced with artifacts: index -> value in microsiemens.
    artifacts: dict[int, float] = field(default_factory=dict)


def _scr(t: np.ndarray, onset: float, rise: float = 2.0, decay: float = 8.0) -> np.ndarray:
    """Skin-conductance response shape (bi-exponential), zero before onset."""
    dt = np.clip(t - onset, 0.0, None)
    shape = np.exp(-dt / decay) - np.exp(-dt / rise)
    shape[t < onset] = 0.0
    peak = shape.max()
    return shape / peak if peak > 0 else shape


def simulate_group(
    participants: dict[str, Participant],
    *,
    seconds: float,
    seed: int,
    shared_events: int = 6,
    start_offsets: dict[str, float] | None = None,
) -> dict[str, np.ndarray]:
    """Return a microsiemens trace per alias sampled every SAMPLE_PERIOD_MS.

    ``start_offsets`` (seconds after the session start) shift when each sensor
    started recording, so shared events land at different row indexes.
    """
    rng = np.random.default_rng(seed)
    rows = int(seconds * 1000 / SAMPLE_PERIOD_MS)
    onsets = np.sort(rng.uniform(10.0, max(11.0, seconds - 30.0), size=shared_events))
    traces: dict[str, np.ndarray] = {}
    for alias, spec in participants.items():
        t = (start_offsets or {}).get(alias, 0.0) + np.arange(rows) * SAMPLE_PERIOD_MS / 1000.0
        signal = spec.tonic_us + spec.drift_us_per_min * t / 60.0
        for onset in onsets:
            amplitude = spec.response_us * rng.uniform(0.6, 1.4)
            signal = signal + spec.coupling * amplitude * _scr(t, onset + spec.lag_seconds)
        for onset in rng.uniform(5.0, max(6.0, seconds - 20.0), size=spec.own_events):
            signal = signal + spec.response_us * rng.uniform(0.5, 1.2) * _scr(t, onset)
        signal = signal + rng.normal(0.0, spec.noise_us, size=rows)
        traces[alias] = signal
    return traces


def write_esense_csv(path: Path, microsiemens: np.ndarray, fmt: RecordingFormat = RecordingFormat()) -> Path:
    base = datetime.strptime(fmt.start_time, "%H:%M:%S:%f")
    if fmt.polish:
        preamble = ["Urządzenie: eSense Skin Response;", "OPIS;WARTOŚĆ", "Jednostka;µS"]
        header = "SEKUNDA;MICROSIEMENS;ZNACZNIKI CZASU;SCR;SCR/MIN;ZNACZNIK"
    else:
        preamble = ["Device: eSense Skin Response;", "DESCRIPTION;VALUE", "Unit;uS"]
        header = "SECOND;MICROSIEMENS;TIMESTAMP;SCR;SCR/MIN;MARKER"
    lines = [*preamble, *(f"META_{idx};value" for idx in range(20)), "STATISTICS;;;;;", header]

    for idx, value in enumerate(microsiemens):
        moment = base + timedelta(milliseconds=SAMPLE_PERIOD_MS * idx)
        if idx in fmt.markers_without_ms:
            marker = moment.strftime("%H:%M:%S")
        else:
            marker = moment.strftime("%H:%M:%S:%f")[:-3]
        if idx in fmt.pause_rows:
            marker = fmt.paused_label
        value = fmt.artifacts.get(idx, value)
        micro_text = fmt.excel_tokens.get(idx, f"{value:.2f}".replace(".", ","))
        second = f"{idx * SAMPLE_PERIOD_MS / 1000:.1f}".replace(".", ",")
        lines.append(f"{second};{micro_text};{marker};0;0;")

    path.write_text("\n".join(lines) + "\n", encoding=fmt.encoding)
    return path


def write_group(
    directory: Path,
    name: str,
    participants: dict[str, Participant],
    formats: dict[str, RecordingFormat],
    *,
    seconds: float,
    seed: int,
    shared_events: int = 6,
) -> dict[str, Path]:
    starts = {
        alias: datetime.strptime(formats.get(alias, RecordingFormat()).start_time, "%H:%M:%S:%f")
        for alias in participants
    }
    session_start = min(starts.values())
    offsets = {alias: (start - session_start).total_seconds() for alias, start in starts.items()}
    traces = simulate_group(
        participants, seconds=seconds, seed=seed, shared_events=shared_events, start_offsets=offsets
    )
    return {
        alias: write_esense_csv(directory / f"{name}_{alias}.csv", trace, formats.get(alias, RecordingFormat()))
        for alias, trace in traces.items()
    }
