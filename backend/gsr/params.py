"""Settings form fields of the Cleaning and Analysis screens.

Each field is one ``PipelineConfig`` value; labels and help texts come from
GSRv2 (``frontend/src/i18n/labels.ts``). Posted values are validated by
``PipelineConfig`` itself, so the bounds live in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.http import QueryDict
from django.utils.formats import number_format
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _

from .pipeline.config import VSR_DOCUMENT_SETTINGS, PipelineConfig
from .templatetags.gsr_tags import input_value

# The application's defaults: GSRv2's, with the values the VSR Interview Flow Chart suggests.
DEFAULTS = {**PipelineConfig().model_dump(), **VSR_DOCUMENT_SETTINGS}


@dataclass(frozen=True)
class Param:
    name: str
    label: Any
    help: Any = ""
    kind: str = "float"  # float | int | bool | choice
    step: str = "any"
    choices: tuple[tuple[str, Any], ...] = ()
    nullable: bool = False

    @property
    def default(self) -> Any:
        return DEFAULTS[self.name]


@dataclass(frozen=True)
class Group:
    key: str
    title: Any
    help: Any
    params: tuple[Param, ...]
    # A toggle parameter that switches the whole group on or off (cleaning filters).
    toggle: Param | None = None


TIME_WINDOW = Group(
    "time_window",
    _("Time window"),
    _(
        "Time range (in minutes) defines which part of the recording is analyzed. After import, signals are "
        "synchronized to a common window. Narrow the range, e.g. to skip warm-up or end-of-session noise."
    ),
    (
        Param("time_range_from", _("From (min)"), nullable=True),
        Param("time_range_to", _("To (min)"), nullable=True),
    ),
)

CLEANING_FILTERS = (
    Group(
        "physiological",
        _("Physiological range"),
        _("Rejects values outside the configured physiological GSR range."),
        (
            Param("clean_filter_physiological_min_us", _("Min (µS)")),
            Param("clean_filter_physiological_max_us", _("Max (µS)")),
        ),
        toggle=Param(
            "clean_filter_physiological_range",
            _("Physiological range"),
            _("Protects against non-physiological values caused by measurement glitches or sensor issues."),
            kind="bool",
        ),
    ),
    Group(
        "zscore",
        _("Z-score artifact filter"),
        _("Rejects points that are statistical outliers against the full signal. A filter, not a normalization: the "
          "analysis normalizes every signal to z-scores within the analysed period on its own."),
        (Param("clean_filter_zscore_threshold", _("Threshold (SD)")),),
        toggle=Param(
            "clean_filter_zscore",
            _("Z-score artifact filter"),
            _("The farther from the mean (in SD units), the more likely a point is an artifact."),
            kind="bool",
        ),
    ),
    Group(
        "jump",
        _("Abrupt jumps"),
        _("Rejects samples with unnaturally large changes between consecutive points."),
        (Param("clean_filter_jump_sigma", _("Sigma")),),
        toggle=Param(
            "clean_filter_jump",
            _("Abrupt jumps"),
            _("Helps remove sudden spikes typically caused by electrode movement or transient interference."),
            kind="bool",
        ),
    ),
    Group(
        "iqr",
        _("Interquartile range (IQR)"),
        _("Rejects points outside [Q1-k*IQR, Q3+k*IQR]."),
        (Param("clean_filter_iqr_factor", _("IQR factor")),),
        toggle=Param(
            "clean_filter_iqr",
            _("Interquartile range (IQR)"),
            _("IQR filtering is robust for non-normal distributions and catches heavy-tail points well."),
            kind="bool",
        ),
    ),
    Group(
        "butterworth",
        _("Butterworth filter"),
        _("Smooths the signal with a low-pass filter after interpolation."),
        (
            Param("clean_filter_butterworth_cutoff_hz", _("Cutoff (Hz)")),
            Param("clean_filter_butterworth_order", _("Order"), kind="int", step="1"),
        ),
        toggle=Param(
            "clean_filter_butterworth",
            _("Butterworth filter"),
            _("Removes high-frequency noise and keeps slower GSR signal dynamics."),
            kind="bool",
        ),
    ),
)

MEDIAN_FILTER = Group(
    "median",
    _("Median filter"),
    _(
        "Filter window size (in samples). 0 disables the filter. Small values (e.g. 3–11) preserve details but "
        "reduce noise less. Large values (e.g. 51–201) smooth more aggressively. Only odd values are used — even "
        "values are rounded up automatically."
    ),
    (Param("median_kernel", _("Window size (samples)"), kind="int", step="1"),),
)

CLEANING_GROUPS = (TIME_WINDOW, *CLEANING_FILTERS, MEDIAN_FILTER)

SYNCHRONY_ESSENTIAL = (
    Param(
        "synchrony_threshold",
        _("Synchronization threshold"),
        _("Pairs whose windowed correlation reaches this value count as synchronized in that window."),
    ),
    Param(
        "synchrony_divergent_threshold",
        _("Divergent synchrony threshold (r ≤ −)"),
        _(
            "Pairs whose windowed correlation is at or below minus this value move in opposite directions in that "
            "window: divergent synchrony (Hassan: negative correlations of at least .30)."
        ),
    ),
    Param(
        "synchrony_decoupled_threshold",
        _("Decoupling threshold (|r| <)"),
        _(
            "A stretch between a pair's synchronization and divergent episodes whose correlation lies within "
            "plus/minus this value is decoupled: no synchrony (the VSR flow chart: “Asynchronous = below 0.1”). "
            "The decoupling rule says how the stretch's correlation is judged."
        ),
    ),
    Param(
        "synchrony_decoupled_rule",
        _("Decoupling rule"),
        _(
            "Every window: every window of the stretch has |r| below the decoupling threshold, for at least the "
            "minimum episode duration (as in Hassan's script). One correlation over the stretch: a stretch without a "
            "window at the divergent strength, whose one correlation over its data lies within the threshold."
        ),
        kind="choice",
        choices=(("every_window", _("every window")), ("stretch_correlation", _("one correlation over the stretch"))),
    ),
    Param("synchrony_window_seconds", _("Window (s)")),
)
SYNCHRONY_ADVANCED = (
    Param("synchrony_step_seconds", _("Step (s)")),
    Param(
        "synchrony_correlation_method",
        _("Correlation method"),
        kind="choice",
        choices=(("pearson", "Pearson"), ("spearman", "Spearman")),
    ),
    Param("synchrony_max_lag_seconds", _("Max lag (s)")),
    Param("synchrony_signed", _("Signed mode (positive agreement only)"), kind="bool"),
    Param("synchrony_dtw_enabled", _("Compute DTW"), kind="bool"),
    Param("synchrony_surrogate_enabled", _("Synchronization surrogate test"), kind="bool"),
    Param("synchrony_surrogate_count", _("Synchronization surrogate count"), kind="int", step="1"),
    Param(
        "synchrony_surrogate_method",
        _("Surrogate method"),
        kind="choice",
        choices=(("circular", _("circular shift")), ("phase", _("phase randomization"))),
    ),
    Param("synchrony_surrogate_seed", _("Surrogate seed"), kind="int", step="1"),
    Param("synchrony_episode_min_size", _("Min. people per episode"), kind="int", step="1"),
    Param("synchrony_episode_min_duration_seconds", _("Min. episode duration (s)")),
    Param(
        "synchrony_episode_max_gap_seconds",
        _("Max gap (s)"),
        _(
            "Synchronization and divergent episodes closer than this are joined into one (default 15 s, as in GSRv2). "
            "A dyad joins none (Dyads: no joining across gaps)."
        ),
    ),
    Param("synchrony_cluster_distance_threshold", _("Clustering threshold")),
    Param(
        "downsample_interval_seconds",
        _("Downsampling interval (s)"),
        _(
            "The downsampling interval defines time sampling: 1 s = 1 Hz, 2 s = every 2 seconds, etc. It reduces "
            "the number of analyzed points in whole seconds while preserving the signal trend."
        ),
        kind="int",
        step="1",
    ),
)

COAROUSAL_ESSENTIAL = (
    Param(
        "contagion_high_z",
        _("High threshold (z)"),
        _("z above this = High-arousal state (relative to the participant's own baseline)."),
    ),
    Param("contagion_low_z", _("Low threshold (z)"), _("z below this = Low-arousal state.")),
    Param(
        "contagion_min_participants",
        _("Min. participants"),
        _("Minimum number of participants in the same state for a second to count toward an episode."),
        kind="int",
        step="1",
    ),
)
COAROUSAL_ADVANCED = (
    Param(
        "contagion_window_seconds",
        _("Window (s)"),
        _(
            "Length of the sliding window for episode detection. Note: with the 0.05 Hz filter the signal is very "
            "smooth, so pairwise correlation in a short window can be inflated by a shared slow trend."
        ),
    ),
    Param("contagion_step_seconds", _("Step (s)")),
    Param(
        "contagion_min_agreement_pct",
        _("Agreement threshold"),
        _("Fraction of the window where the condition (≥ min. participants in state) must hold to form an episode."),
    ),
    Param(
        "contagion_comovement_rho",
        _("Co-movement threshold (ρ)"),
        _("Minimum mean pairwise Spearman correlation in a window to count it as co-movement."),
    ),
    Param(
        "contagion_min_episode_gap_seconds",
        _("Max gap (s)"),
        _(
            "Co-arousal episodes of one kind closer than this are joined into one (default 30 s, as in GSRv2). A dyad "
            "joins none (Dyads: no joining across gaps)."
        ),
    ),
    Param(
        "contagion_surrogate_count",
        _("Surrogate count"),
        _("Number of random circular shifts for the co-arousal index null distribution. 0 = skip the significance test."),
        kind="int",
        step="1",
    ),
    Param(
        "contagion_surrogate_seed",
        _("Surrogate seed"),
        _("A fixed seed makes the co-arousal surrogate test reproducible."),
        kind="int",
        step="1",
    ),
)

INTERVIEW_ESSENTIAL = (
    Param(
        "synchrony_interview_threshold",
        _("Interview moment correlation (|r| at least)"),
        _(
            "The strength of correlation a moment needs to count as synchronous on the Interviews step: STEPS 1–2 "
            "take a co-movement episode whose r is at least this, STEP 4b a divergent one whose r is at most minus "
            "this (the VSR flow chart: “Synchronous = above 0.8”)."
        ),
    ),
    Param(
        "synchrony_dyad_without_gap_joining",
        _("Dyads: no joining across gaps"),
        _(
            "Joining episodes across gaps keeps a subgroup's episode together when the group's membership changes. "
            "Two people have no subgroups, so a dyad (a two-person project, or an interview's own two signals) joins "
            "none, as Hassan's dyad script. Off: dyads join gaps like a class (the Max gap settings)."
        ),
        kind="bool",
    ),
    Param(
        "synchrony_interview_shared_without_comovement",
        _("Shared High/Low only without co-movement (STEPS 3–4)"),
        _(
            "Off (default, as in Hassan's script, where the categories may overlap): STEPS 3–4 take a whole shared "
            "High or Low episode, also one with co-movement during it. On: only those without co-movement of the "
            "interview strength."
        ),
        kind="bool",
    ),
)

ANALYSIS_PARAMS = (*SYNCHRONY_ESSENTIAL, *SYNCHRONY_ADVANCED, *COAROUSAL_ESSENTIAL, *COAROUSAL_ADVANCED,
                   *INTERVIEW_ESSENTIAL)


def analysis_window(values: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
    """The analysed period inside the common recording window, in whole seconds from the window's start: the Cleaning
    time range clipped to the window; a missing bound is the window's own edge. ``max`` is the last whole second."""
    limit = max(0, int(duration_seconds))

    def seconds(minutes: Any, default: int) -> int:
        return default if minutes is None else min(max(0, round(float(minutes) * 60.0)), limit)

    start, end = seconds(values.get("time_range_from"), 0), seconds(values.get("time_range_to"), limit)
    return {"from": start, "to": end, "max": limit, "full": start == 0 and end == limit}


def window_settings(from_seconds: int, to_seconds: int, duration_seconds: float) -> dict[str, float | None]:
    """The Cleaning time range (minutes) for a period picked on the Upload overlap timeline. An edge of the common window
    is stored as no bound, so the full window keeps the default settings. Raises ``ValueError`` when out of the window."""
    limit = max(0, int(duration_seconds))
    if not 0 <= from_seconds < to_seconds <= limit:
        raise ValueError(gettext("The analysed period must lie inside the common window and end after it starts."))
    return {
        "time_range_from": None if from_seconds == 0 else round(from_seconds / 60.0, 4),
        "time_range_to": None if to_seconds == limit else round(to_seconds / 60.0, 4),
    }


def cleaning_params() -> tuple[Param, ...]:
    params: list[Param] = []
    for group in CLEANING_GROUPS:
        if group.toggle is not None:
            params.append(group.toggle)
        params.extend(group.params)
    return tuple(params)


def parse_params(params: tuple[Param, ...], data: QueryDict) -> dict[str, Any]:
    """Read posted form values; an empty numeric field means "use the default" (or "none" for a range bound)."""
    values: dict[str, Any] = {}
    for param in params:
        if param.kind == "bool":
            values[param.name] = param.name in data
            continue
        raw = (data.get(param.name) or "").strip()
        if not raw:
            values[param.name] = None if param.nullable else param.default
            continue
        values[param.name] = raw.replace(",", ".") if param.kind in ("float", "int") else raw
    return values


def apply_settings(stored: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Merge validated updates into stored settings; raises ``ValueError`` with the reason when invalid."""
    merged = {**(stored or {}), **updates}
    config = PipelineConfig.from_dict(merged)
    values = config.model_dump()
    # Store the coerced values ("5" -> 5) of the fields the form sent, keep the rest untouched.
    return {**merged, **{name: values[name] for name in updates}}


def field_rows(params: tuple[Param, ...], values: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "param": param,
            "value": values.get(param.name),
            "modified": values.get(param.name) != param.default,
        }
        for param in params
    ]


def display_number(value: Any) -> str:
    """A setting value for display text, in the UI language's decimal format ("0,6" in Polish)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return input_value(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    return number_format(value)


def choice_label(name: str, value: Any) -> Any:
    """The translated label of a choice setting's stored value (e.g. "circular" → "circular shift")."""
    for param in ANALYSIS_PARAMS:
        if param.name == name:
            return dict(param.choices).get(value, value)
    return value


def changed_label(params: tuple[Param, ...], values: dict[str, Any]) -> str | None:
    """How many of ``params`` differ from their defaults ("2 changed"), or None when none do."""
    changed = sum(1 for param in params if values.get(param.name) != param.default)
    if not changed:
        return None
    return ngettext("%(count)d changed", "%(count)d changed", changed) % {"count": changed}


def _value_text(param: Param, value: Any) -> str:
    if param.kind == "bool":
        return gettext("on") if value else gettext("off")
    if param.kind == "choice":
        return str(dict(param.choices).get(value, value))
    return display_number(value) or "–"


def _summary(
    chips: list[tuple[str, tuple[str, ...]]],
    params: tuple[Param, ...],
    values: dict[str, Any],
    applied: dict[str, Any] | None,
    sections: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run bar chips: each marked when a setting it shows differs from the applied run (``applied``); a changed setting
    no chip shows gets its own chip, and the last chip says how many settings differ from the defaults."""
    changed = set() if applied is None else {
        param.name for param in params if values.get(param.name) != applied.get(param.name)
    }
    shown = {name for _, names in chips for name in names}
    items = [{"text": text, "changed": bool(changed.intersection(names))} for text, names in chips]
    items += [
        {"text": gettext("%(section)s · %(label)s: %(value)s") % {
            "section": sections[param.name], "label": param.label, "value": _value_text(param, values.get(param.name))},
         "changed": True}
        for param in params if param.name in changed and param.name not in shown
    ]
    items.append({"text": changed_label(params, values) or gettext("defaults"), "changed": False})
    return items


def cleaning_summary(
    values: dict[str, Any], included: int, total: int, applied: dict[str, Any] | None = None,
    signals_changed: bool = False,
) -> list[dict[str, Any]]:
    """One-line description of the Cleaning settings for the run bar; ``applied`` = the settings of the shown preview."""
    start, end = values.get("time_range_from"), values.get("time_range_to")
    if start is None and end is None:
        window = gettext("full range")
    else:
        window = f"{display_number(start) or '…'}–{display_number(end) or '…'} min"
    enabled = sum(1 for group in CLEANING_FILTERS if group.toggle and values.get(group.toggle.name))
    kernel = values.get("median_kernel")
    items = _summary(
        [
            (gettext("Signals: %(included)d/%(total)d") % {"included": included, "total": total}, ()),
            (gettext("Window: %(window)s") % {"window": window}, ("time_range_from", "time_range_to")),
            (gettext("Filters: %(enabled)d/%(total)d") % {"enabled": enabled, "total": len(CLEANING_FILTERS)},
             tuple(group.toggle.name for group in CLEANING_FILTERS if group.toggle)),
            (gettext("Median: %(value)s") % {"value": display_number(kernel) if kernel else gettext("off")},
             ("median_kernel",)),
        ],
        cleaning_params(), values, applied,
        {param.name: group.title for group in CLEANING_GROUPS for param in group.params},
    )
    items[0]["changed"] = signals_changed
    return items


def analysis_summary(values: dict[str, Any], applied: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One-line description of the Analysis settings: the essential values plus how many differ from defaults;
    ``applied`` = the settings of the shown run."""
    value = {name: display_number(values.get(name)) for name in (
        "synchrony_threshold", "synchrony_window_seconds", "contagion_high_z", "contagion_low_z",
        "contagion_min_participants",
    )}
    return _summary(
        [
            (gettext("Threshold: %(value)s") % {"value": value["synchrony_threshold"]}, ("synchrony_threshold",)),
            (gettext("Window: %(value)s s") % {"value": value["synchrony_window_seconds"]},
             ("synchrony_window_seconds",)),
            (gettext("High/Low z: %(high)s/%(low)s") % {"high": value["contagion_high_z"],
                                                        "low": value["contagion_low_z"]},
             ("contagion_high_z", "contagion_low_z")),
            (gettext("Min. participants: %(value)s") % {"value": value["contagion_min_participants"]},
             ("contagion_min_participants",)),
        ],
        ANALYSIS_PARAMS, values, applied,
        {**{param.name: _("Synchronization") for param in (*SYNCHRONY_ESSENTIAL, *SYNCHRONY_ADVANCED)},
         **{param.name: _("Co-arousal") for param in (*COAROUSAL_ESSENTIAL, *COAROUSAL_ADVANCED)},
         **{param.name: _("Interviews") for param in INTERVIEW_ESSENTIAL}},
    )


def reset_analysis(stored: dict[str, Any]) -> dict[str, Any]:
    """The stored settings with every Analysis setting back at the application's defaults."""
    names = {param.name for param in ANALYSIS_PARAMS}
    return {**{key: value for key, value in (stored or {}).items() if key not in names}, **VSR_DOCUMENT_SETTINGS}


def _labelled_params() -> list[tuple[Param, str]]:
    """Every setting with the label a change note shows; a filter's own fields carry the filter name."""
    labelled: list[tuple[Param, str]] = []
    for group in CLEANING_GROUPS:
        if group.toggle is not None:
            labelled.append((group.toggle, str(group.toggle.label)))
        labelled.extend((param, f"{group.title} · {param.label}") for param in group.params)
    labelled.extend((param, str(param.label)) for param in ANALYSIS_PARAMS)
    return labelled


def _change_value(param: Param, value: Any) -> str:
    if value is None:
        return "—"
    if param.kind == "bool":
        return gettext("on") if value else gettext("off")
    if param.choices:
        return str(dict(param.choices).get(value, value))
    return display_number(value)


def changed_settings(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The settings that differ between two runs, as "label: old → new" (e.g. "Threshold (SD): 5 → 4")."""
    return [
        f"{label}: {_change_value(param, before.get(param.name))} → {_change_value(param, after.get(param.name))}"
        for param, label in _labelled_params()
        if before.get(param.name) != after.get(param.name)
    ]
