from __future__ import annotations

import contextlib
import copy
import dataclasses
import os
import re
from dataclasses import dataclass
from typing import Any

# Read from the environment (not Django settings) so the pipeline package stays
# importable without Django; settings.py reads the same variable.
MAX_DATASETS = int(os.getenv("MAX_DATASETS", "20"))

# Value rules of GSRv2's PipelineConfigSerializer, with DRF's coercion: numeric
# strings become int/float and DRF boolean literals become bool.
_MAX_STRING_LENGTH = 1000
_INT_DECIMAL = re.compile(r"\.0*\s*$")
_TRUE_VALUES = {"t", "y", "yes", "true", "on", "1", 1, True}
_FALSE_VALUES = {"f", "n", "no", "false", "off", "0", 0, 0.0, False}
_INT, _FLOAT, _BOOL, _CHOICE = "int", "float", "bool", "choice"
_FIELD_RULES: dict[str, tuple[Any, ...]] = {
    "median_kernel": (_INT, None, None),
    "scaling_enabled": (_BOOL,),
    "clean_filter_physiological_range": (_BOOL,),
    "clean_filter_zscore": (_BOOL,),
    "clean_filter_jump": (_BOOL,),
    "clean_filter_iqr": (_BOOL,),
    "clean_filter_butterworth": (_BOOL,),
    "clean_filter_physiological_min_us": (_FLOAT, 0.0, 100.0),
    "clean_filter_physiological_max_us": (_FLOAT, 0.0, 1000.0),
    "clean_filter_zscore_threshold": (_FLOAT, 0.1, 20.0),
    "clean_filter_jump_sigma": (_FLOAT, 0.1, 20.0),
    "clean_filter_iqr_factor": (_FLOAT, 0.1, 10.0),
    "clean_filter_butterworth_cutoff_hz": (_FLOAT, 0.001, 2.4),
    "clean_filter_butterworth_order": (_INT, 1, 10),
    "downsample_interval_seconds": (_INT, None, None),
    "downsample_factor": (_INT, None, None),
    "time_range_from": (_FLOAT, None, None),
    "time_range_to": (_FLOAT, None, None),
    "synchrony_window_seconds": (_FLOAT, 1.0, 600.0),
    "synchrony_step_seconds": (_FLOAT, 0.5, 600.0),
    "synchrony_threshold": (_FLOAT, 0.0, 1.0),
    "synchrony_correlation_method": (_CHOICE, ("pearson", "spearman")),
    "synchrony_max_lag_seconds": (_FLOAT, 0.0, 600.0),
    "synchrony_dtw_enabled": (_BOOL,),
    "synchrony_cluster_distance_threshold": (_FLOAT, 0.0, 2.0),
    "synchrony_signed": (_BOOL,),
    "synchrony_surrogate_enabled": (_BOOL,),
    "synchrony_surrogate_count": (_INT, 0, 1000),
    "synchrony_surrogate_method": (_CHOICE, ("circular", "phase")),
    "synchrony_surrogate_seed": (_INT, 0, 2**31 - 1),
    "synchrony_episode_min_size": (_INT, 2, 50),
    "synchrony_episode_min_duration_seconds": (_FLOAT, 0.0, 3600.0),
    "synchrony_episode_max_gap_seconds": (_FLOAT, 0.0, 600.0),
    "synchrony_interview_threshold": (_FLOAT, 0.0, 1.0),
    "synchrony_divergent_threshold": (_FLOAT, 0.0, 1.0),
    "synchrony_decoupled_threshold": (_FLOAT, 0.0, 1.0),
    "contagion_high_z": (_FLOAT, 0.0, 10.0),
    "contagion_low_z": (_FLOAT, -10.0, 0.0),
    "contagion_window_seconds": (_FLOAT, 1.0, 600.0),
    "contagion_step_seconds": (_FLOAT, 0.5, 600.0),
    "contagion_min_participants": (_INT, 2, 50),
    "contagion_min_agreement_pct": (_FLOAT, 0.0, 1.0),
    "contagion_min_episode_gap_seconds": (_FLOAT, 0.0, 600.0),
    "contagion_member_pct": (_FLOAT, 0.0, 1.0),
    "contagion_contrib_rho": (_FLOAT, -1.0, 1.0),
    "contagion_comovement_rho": (_FLOAT, -1.0, 1.0),
    "contagion_surrogate_count": (_INT, 0, 1000),
    "contagion_surrogate_seed": (_INT, 0, 2**31 - 1),
}
_NULLABLE = {"time_range_from", "time_range_to"}

# The Analysis values the "VSR Interview Flow Chart" suggests (page 1, GSR participants: "Correlation criteria for
# types of synchrony ... Synchronous = above 0.8", read as the strength of the correlation): both co-movement
# thresholds behind its steps 1-2, and the strength an interview moment needs. New projects start with them and
# "Reset defaults" restores them; ``PipelineConfig`` keeps GSRv2's defaults for GSRv2's own settings, so saved
# projects keep theirs, and takes the settings GSRv2 did not have from here, so each is one number to change.
VSR_DOCUMENT_SETTINGS: dict[str, Any] = {
    "synchrony_threshold": 0.8,
    "contagion_comovement_rho": 0.8,
    # STEPS 1-2 take a moment whose r is at least this; Hassan's e-mail: "ideally above .80 for the interviews".
    "synchrony_interview_threshold": 0.8,
    # Divergent synchrony: windows whose r is at or below minus this (Hassan's e-mail: "negative correlations
    # above > .30"); the flow chart has no number for its detection.
    "synchrony_divergent_threshold": 0.3,
    # Decoupling: a stretch whose correlation lies within plus/minus this ("Asynchronous = below 0.1"; Hassan:
    # "between -.10 and +.10").
    "synchrony_decoupled_threshold": 0.1,
    # The High and Low states (shared High/Low, and opposite arousal: one High while another is Low), as GSRv2's
    # defaults; Hassan's script uses the same.
    "contagion_high_z": 1.0,
    "contagion_low_z": -1.0,
}


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        if name in _NULLABLE:
            return None
        raise ValueError(f"{name} may not be null")
    kind, *rule = _FIELD_RULES[name]
    if kind == _BOOL:
        lowered = value.lower() if isinstance(value, str) else value
        with contextlib.suppress(TypeError):
            if lowered in _TRUE_VALUES:
                return True
            if lowered in _FALSE_VALUES:
                return False
        raise ValueError(f"{name} must be a valid boolean")
    if kind == _CHOICE:
        if str(value) not in rule[0]:
            raise ValueError(f"{name} must be one of: {', '.join(rule[0])}")
        return str(value)
    if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
        raise ValueError(f"{name} must be a valid number")
    try:
        number = int(_INT_DECIMAL.sub("", str(value))) if kind == _INT else float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a valid {'integer' if kind == _INT else 'number'}") from None
    low, high = rule
    if low is not None and number < low:
        raise ValueError(f"{name} must be greater than or equal to {low}")
    if high is not None and number > high:
        raise ValueError(f"{name} must be less than or equal to {high}")
    return number


@dataclass(init=False)
class PipelineConfig:
    """Pipeline tuning parameters, carried over from GSRv2.

    Field names and defaults are unchanged, so a GSRv2 project config loads as-is
    through ``from_dict`` (keys of the dropped segment/correlation analyses are
    ignored). ``model_fields_set`` keeps the GSRv2 explicitly-set-fields
    semantics, which select the legacy sample-based downsampling mode. Values are
    coerced and validated against GSRv2's serializer bounds; invalid input raises
    ``ValueError``.
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
    # VSR interview moments (not in GSRv2): the correlation strength a moment needs to count as synchronous.
    synchrony_interview_threshold: float = VSR_DOCUMENT_SETTINGS["synchrony_interview_threshold"]
    # Divergent synchrony (not in GSRv2): subgroup episodes of windows whose r is at or below minus this strength.
    synchrony_divergent_threshold: float = VSR_DOCUMENT_SETTINGS["synchrony_divergent_threshold"]
    # Decoupling (not in GSRv2): stretches between a pair's synchronization and divergent episodes whose mean r lies
    # within plus/minus this strength.
    synchrony_decoupled_threshold: float = VSR_DOCUMENT_SETTINGS["synchrony_decoupled_threshold"]
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
            object.__setattr__(self, name, _coerce(name, value))
        if self.clean_filter_physiological_min_us >= self.clean_filter_physiological_max_us:
            raise ValueError("clean_filter_physiological_min_us must be lower than clean_filter_physiological_max_us")
        if self.synchrony_step_seconds > self.synchrony_window_seconds:
            raise ValueError("synchrony_step_seconds must not exceed synchrony_window_seconds")
        if self.contagion_step_seconds > self.contagion_window_seconds:
            raise ValueError("contagion_step_seconds must not exceed contagion_window_seconds")
        if self.contagion_low_z >= self.contagion_high_z:
            raise ValueError("contagion_low_z must be lower than contagion_high_z")
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
