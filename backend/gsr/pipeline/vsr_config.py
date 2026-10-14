"""VSR interview-moment selection parameters (VSR Interview Flow Chart, GSR section).

Defaults match the approved plan (sync 0.8, async 0.1, 30 s window, 5 s step,
z = ±1, shared arousal ≥ 60%, 5 primary + 3 reserve moments). The engine in
``vsr.py`` (PR 1) reads these values; they are stored on the project outside
``PipelineConfig`` so analysis thresholds stay independent.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Any

_FLOAT = "float"
_INT = "int"
_FIELD_RULES: dict[str, tuple[Any, ...]] = {
    "vsr_sync_threshold": (_FLOAT, 0.0, 1.0),
    "vsr_async_threshold": (_FLOAT, -1.0, 1.0),
    "vsr_window_seconds": (_FLOAT, 1.0, 600.0),
    "vsr_step_seconds": (_FLOAT, 0.5, 600.0),
    "vsr_high_z": (_FLOAT, 0.0, 10.0),
    "vsr_low_z": (_FLOAT, -10.0, 0.0),
    "vsr_shared_arousal_min_fraction": (_FLOAT, 0.0, 1.0),
    "vsr_primary_moment_count": (_INT, 1, 20),
    "vsr_reserve_moment_count": (_INT, 0, 20),
}

_TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        raise ValueError(f"{name} may not be null")
    kind, low, high = _FIELD_RULES[name]
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a valid {'integer' if kind == _INT else 'number'}")
    try:
        number = float(str(value).strip().replace(",", "."))
    except ValueError:
        number = math.nan
    if not math.isfinite(number) or (kind == _INT and not number.is_integer()):
        raise ValueError(f"{name} must be a valid {'integer' if kind == _INT else 'number'}")
    if kind == _INT:
        number = int(number)
    if low is not None and number < low:
        raise ValueError(f"{name} must be greater than or equal to {low}")
    if high is not None and number > high:
        raise ValueError(f"{name} must be less than or equal to {high}")
    return number


@dataclass(init=False)
class VsrConfig:
    vsr_sync_threshold: float = 0.8
    vsr_async_threshold: float = 0.1
    vsr_window_seconds: float = 30.0
    vsr_step_seconds: float = 5.0
    vsr_high_z: float = 1.0
    vsr_low_z: float = -1.0
    vsr_shared_arousal_min_fraction: float = 0.6
    vsr_primary_moment_count: int = 5
    vsr_reserve_moment_count: int = 3

    def __init__(self, **kwargs: Any) -> None:
        field_map = {f.name: f for f in dataclasses.fields(self)}
        unknown = set(kwargs) - set(field_map)
        if unknown:
            raise TypeError(f"Unknown VsrConfig fields: {', '.join(sorted(unknown))}")
        for name, spec in field_map.items():
            value = kwargs[name] if name in kwargs else spec.default
            object.__setattr__(self, name, _coerce(name, value))
        if self.vsr_async_threshold >= self.vsr_sync_threshold:
            raise ValueError("vsr_async_threshold must be lower than vsr_sync_threshold")
        if self.vsr_step_seconds > self.vsr_window_seconds:
            raise ValueError("vsr_step_seconds must not exceed vsr_window_seconds")
        if self.vsr_low_z >= self.vsr_high_z:
            raise ValueError("vsr_low_z must be lower than vsr_high_z")

    def model_dump(self) -> dict[str, Any]:
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in dataclasses.fields(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "VsrConfig":
        payload = payload or {}
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known})


def parse_video_start_time(value: Any) -> str | None:
    """Return ``HH:MM:SS`` or ``None`` when unset; raises ``ValueError`` when invalid."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    match = _TIME_RE.match(text)
    if not match:
        raise ValueError("video_start_time must be a clock time HH:MM:SS")
    hours = int(match.group(1))
    if hours > 23:
        raise ValueError("video_start_time must be a clock time HH:MM:SS")
    return f"{hours:02d}:{match.group(2)}:{match.group(3)}"
