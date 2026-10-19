"""How the screens, the PDF report and the exports show the signals, stored on ``Project.display_settings``.

The analysis always works on z-scores within the analysed window (co-arousal states, the
interview signals) or on correlations, which no linear rescaling changes. The chart scale
only chooses what the "Signals with marked episodes" chart and ``analysis_input_merged.csv``
show: z-scores (the default, comparable with the High/Low thresholds) or the 0–1 min-max
preview. It is kept outside ``pipeline_config``, so changing it leaves every result up to date.
"""

from __future__ import annotations

from django.utils.translation import gettext

from ..models import Project

CHART_SCALE_KEY = "chart_scale"
ZSCORE = "zscore"
MINMAX = "minmax"
CHART_SCALES = (ZSCORE, MINMAX)
DEFAULT_CHART_SCALE = ZSCORE


def chart_scale(project: Project) -> str:
    """The project's chart scale; z-score when none (or an unknown one) is stored."""
    value = (project.display_settings or {}).get(CHART_SCALE_KEY)
    return value if value in CHART_SCALES else DEFAULT_CHART_SCALE


def set_chart_scale(project: Project, value: str) -> None:
    """Store the chart scale; raises ``ValueError`` for an unknown one."""
    if value not in CHART_SCALES:
        raise ValueError(gettext("Choose the z-score or the min-max (0–1) chart scale."))
    project.display_settings = {**(project.display_settings or {}), CHART_SCALE_KEY: value}
    project.save(update_fields=["display_settings", "updated_at"])
