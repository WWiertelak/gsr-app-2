"""VSR settings form metadata (step 4 Wywiady); values validate through ``VsrConfig``."""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from .params import Param, _summary, display_number
from .pipeline.vsr_config import VsrConfig

DEFAULTS = VsrConfig().model_dump()


class VsrParam(Param):
    @property
    def default(self) -> Any:
        return DEFAULTS[self.name]


VSR_PARAMS = (
    VsrParam(
        "vsr_sync_threshold",
        _("Synchronization threshold"),
        _("Pairs count as synchronous when windowed correlation is above this value (document: above 0.8)."),
    ),
    VsrParam(
        "vsr_async_threshold",
        _("Asynchrony threshold"),
        _("Asynchronous moments use correlation below this value (document: below 0.1)."),
    ),
    VsrParam("vsr_window_seconds", _("Window (s)"), _("Rolling correlation window length.")),
    VsrParam("vsr_step_seconds", _("Step (s)"), _("Window step for rolling correlation.")),
    VsrParam(
        "vsr_high_z",
        _("High threshold (z)"),
        _("Shared HIGH arousal uses z above this value (same scale as co-arousal)."),
    ),
    VsrParam("vsr_low_z", _("Low threshold (z)"), _("Shared LOW arousal uses z below this value.")),
    VsrParam(
        "vsr_shared_arousal_min_fraction",
        _("Shared arousal agreement"),
        _("Minimum fraction of a window where both participants share the same arousal state."),
    ),
    VsrParam(
        "vsr_primary_moment_count",
        _("Primary moments"),
        _("Target number of discussion moments per interview (document: 5)."),
        kind="int",
        step="1",
    ),
    VsrParam(
        "vsr_reserve_moment_count",
        _("Reserve moments"),
        _("Additional backup moments (document: e.g. 8 total)."),
        kind="int",
        step="1",
    ),
)


def vsr_summary(values: dict[str, Any], applied: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run bar chips of the Interviews step; ``applied`` = the VSR parameters of the shown proposals."""
    value = {name: display_number(values.get(name)) for name in DEFAULTS}
    return _summary(
        [
            (gettext("Synchronous: r > %(value)s") % {"value": value["vsr_sync_threshold"]}, ("vsr_sync_threshold",)),
            (gettext("Asynchronous: r < %(value)s") % {"value": value["vsr_async_threshold"]},
             ("vsr_async_threshold",)),
            (gettext("Window: %(window)s s, step %(step)s s") % {"window": value["vsr_window_seconds"],
                                                               "step": value["vsr_step_seconds"]},
             ("vsr_window_seconds", "vsr_step_seconds")),
            (gettext("Moments: %(primary)s + %(reserve)s") % {"primary": value["vsr_primary_moment_count"],
                                                           "reserve": value["vsr_reserve_moment_count"]},
             ("vsr_primary_moment_count", "vsr_reserve_moment_count")),
        ],
        VSR_PARAMS, values, applied, {param.name: gettext("Interviews") for param in VSR_PARAMS},
    )
