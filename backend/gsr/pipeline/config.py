from __future__ import annotations

import copy
import dataclasses
import os
from dataclasses import dataclass
from typing import Any

# Read from the environment (not Django settings) so the pipeline package stays
# importable without Django; settings.py reads the same variable.
MAX_DATASETS = int(os.getenv("MAX_DATASETS", "20"))


@dataclass(init=False)
class PipelineConfig:
    """Pipeline tuning parameters, carried over from GSRv2.

    Field names and defaults are unchanged, so a GSRv2 project config loads as-is
    through ``from_dict`` (keys of the dropped segment/correlation analyses are
    ignored). ``model_fields_set`` keeps the GSRv2 explicitly-set-fields
    semantics, which select the legacy sample-based downsampling mode.
    """

    median_kernel: int = 5
    scaling_enabled: bool = False
    clean_filter_physiological_range: bool = True
    clean_filter_zscore: bool = True
    clean_filter_jump: bool = True
    clean_filter_iqr: bool = False
    clean_filter_butterworth: bool = True
    # Conservative EDA cleaning defaults: preserve sustained responses, remove
    # only extreme global outliers/jumps, and keep the aggressive IQR rule opt-in.
    clean_filter_physiological_min_us: float = 0.5
    clean_filter_physiological_max_us: float = 20.0
    clean_filter_zscore_threshold: float = 5.0
    clean_filter_jump_sigma: float = 5.0
    clean_filter_iqr_factor: float = 1.5
    clean_filter_butterworth_cutoff_hz: float = 0.05
    clean_filter_butterworth_order: int = 4
    downsample_interval_seconds: int = 1
    downsample_factor: int = 100
    time_range_from: float | None = None
    time_range_to: float | None = None
    # Joint synchronization.
    synchrony_window_seconds: float = 30.0
    synchrony_step_seconds: float = 5.0
    synchrony_threshold: float = 0.6
    synchrony_correlation_method: str = "pearson"
    synchrony_max_lag_seconds: float = 10.0
    synchrony_dtw_enabled: bool = True
    synchrony_cluster_distance_threshold: float = 0.5
    # Signed synchrony: when True (default) only IN-PHASE coupling counts as
    # synchrony — anti-phase pairs (one rises while the other falls) are NOT
    # merged into the same synchronized subgroup/cluster. Set False for the
    # magnitude-only behaviour that treats anti-correlation as synchrony.
    synchrony_signed: bool = True
    # Surrogate significance test: builds a per-pair null that PRESERVES each
    # signal's autocorrelation (unlike a plain shuffle), so the reported p-value
    # reflects coupling beyond each series' own temporal structure.
    synchrony_surrogate_enabled: bool = True
    synchrony_surrogate_count: int = 200
    synchrony_surrogate_method: str = "circular"  # "circular" | "phase"
    synchrony_surrogate_seed: int = 42
    # Subgroup episode detection: contiguous stretches where a subset of
    # participants (>= min_size) stays pairwise-synchronized above
    # synchrony_threshold.
    synchrony_episode_min_size: int = 2
    # Default min duration >= one analysis window: episodes shorter than the
    # window aren't independently resolvable, so this filters transient blips.
    synchrony_episode_min_duration_seconds: float = 30.0
    synchrony_episode_max_gap_seconds: float = 15.0
    # Co-arousal (shared arousal, "contagion" in GSRv2). The ``contagion_*`` names
    # are kept so GSRv2 configs load unchanged. z-scoring is computed WITHIN the
    # analysis window, contributors require positive (in-phase) correlation, and
    # the co-arousal null uses an autocorrelation-preserving surrogate.
    contagion_high_z: float = 1.0
    contagion_low_z: float = -1.0
    contagion_window_seconds: float = 30.0
    contagion_step_seconds: float = 5.0
    contagion_min_participants: int = 2
    contagion_min_agreement_pct: float = 0.6
    contagion_min_episode_gap_seconds: float = 30.0
    contagion_member_pct: float = 0.5
    contagion_contrib_rho: float = 0.3
    contagion_comovement_rho: float = 0.3
    contagion_surrogate_count: int = 200
    contagion_surrogate_seed: int = 42

    def __init__(self, **kwargs: Any) -> None:
        field_map = {f.name: f for f in dataclasses.fields(self)}
        unknown = set(kwargs) - set(field_map)
        if unknown:
            raise TypeError(f"Unknown PipelineConfig fields: {', '.join(sorted(unknown))}")
        for name, spec in field_map.items():
            if name in kwargs:
                value = kwargs[name]
            elif spec.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                value = spec.default_factory()  # type: ignore[misc]
            else:
                value = spec.default
            object.__setattr__(self, name, value)
        object.__setattr__(self, "model_fields_set", set(kwargs))

    def model_dump(self) -> dict[str, Any]:
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in dataclasses.fields(self)}

    def model_copy(self, update: dict[str, Any] | None = None) -> "PipelineConfig":
        update = update or {}
        clone = PipelineConfig(**{**self.model_dump(), **update})
        object.__setattr__(clone, "model_fields_set", set(self.model_fields_set) | set(update))
        return clone

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "PipelineConfig":
        payload = payload or {}
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known})
