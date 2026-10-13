"""Pipeline failures as messages a person can act on.

The pipeline keeps GSRv2's English messages (they name stored files and print
ISO timestamps). The screens show a translated message built from the error's
``code`` instead: it names the participant, prints clock times as HH:MM:SS,
never shows a stored file name, and offers a way out (exclude the signal, or
go back to Upload).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .models import Project
from .pipeline.common import PipelineError


@dataclass
class UserError:
    message: str
    details: list[str] = field(default_factory=list)
    exclude: list[Any] = field(default_factory=list)  # Datasets the person may exclude to get past the error
    links: list[dict[str, str]] = field(default_factory=list)  # {"url", "label"}

    def __str__(self) -> str:
        return self.message


def clock(moment: Any) -> str:
    return pd.Timestamp(moment).strftime("%H:%M:%S")


def overlap_outliers(ranges: dict[str, tuple]) -> tuple[list[str], tuple | None]:
    """Recordings outside the largest group that shares a time window, and that group's window.

    ``ranges`` maps alias to (start, end). When the largest group is not unique, or has
    fewer than two recordings, no single culprit can be named: returns ([], None).
    """
    best: list[tuple[str, ...]] = []
    for point, _end in ranges.values():  # the deepest overlap begins where some recording starts
        group = tuple(alias for alias, (start, end) in ranges.items() if start <= point < end)
        if not best or len(group) > len(best[0]):
            best = [group]
        elif len(group) == len(best[0]) and group not in best:
            best.append(group)
    if len(best) != 1 or len(best[0]) < 2:
        return [], None
    group = best[0]
    window = (max(ranges[alias][0] for alias in group), min(ranges[alias][1] for alias in group))
    return [alias for alias in ranges if alias not in group], window


def parse_reason(code: str | None, row: int | None = None) -> str:
    """Why an eSense CSV could not be read, without the file name."""
    if code == "missing_header":
        return _(
            "This is not an eSense CSV export: no header row with a MICROSIEMENS column "
            "was found in the first 80 lines."
        )
    if code == "encoding":
        return _("The file's text encoding is not supported. Save it as a UTF-8 CSV and upload it again.")
    if code == "missing_time_column":
        return _("The file has no time column (ZNACZNIKI CZASU or TIMESTAMP).")
    if code == "missing_signal_column":
        return _("The file has no MICROSIEMENS column.")
    if code == "non_monotonic":
        return _("The time goes backwards at data row %(row)s; only a midnight rollover may do that.") % {"row": row}
    if code == "no_rows":
        return _("The file contains no readable signal rows.")
    return _("The file could not be read as an eSense CSV export.")


def _upload_link(project: Project) -> dict[str, str]:
    return {"url": reverse("gsr:upload", args=[project.pk]), "label": _("Go to Import")}


def _cleaning_link(project: Project) -> dict[str, str]:
    return {"url": reverse("gsr:cleaning", args=[project.pk]), "label": _("Go to Cleaning")}


def _excludable(project: Project, aliases: list[str]) -> list[Any]:
    """Included datasets among ``aliases``; excluding them only helps while at least two others stay in."""
    included = list(project.datasets.filter(included=True).order_by("created_at"))
    if len(included) - len(aliases) < 2:
        return []
    return [dataset for dataset in included if dataset.alias in aliases]


def no_overlap_message(ranges: dict[str, tuple[Any, Any]]) -> tuple[str, list[str], list[str]]:
    """Headline, per-participant clock ranges and the participants outside the common window."""
    outliers, window = overlap_outliers(ranges)
    message = _("The recordings do not share a common time window, so they cannot be analysed together.")
    if outliers:
        message += " " + ngettext(
            "%(aliases)s does not overlap with the other recordings (their common window is %(start)s–%(end)s).",
            "%(aliases)s do not overlap with the other recordings (their common window is %(start)s–%(end)s).",
            len(outliers),
        ) % {"aliases": ", ".join(outliers), "start": clock(window[0]), "end": clock(window[1])}
    details = [f"{alias}: {clock(start)}–{clock(end)}" for alias, (start, end) in ranges.items()]
    return message, details, outliers


def describe(exc: Exception, project: Project, step: str) -> UserError:
    """The person-facing version of a failed cleaning or analysis run on ``step``."""
    code = getattr(exc, "code", None) if isinstance(exc, PipelineError) else None
    alias = getattr(exc, "alias", None)
    details = getattr(exc, "details", {})
    upload = _upload_link(project)

    if code == "no_overlap":
        message, lines, outliers = no_overlap_message(details["ranges"])
        return UserError(message, lines, _excludable(project, outliers), [upload])
    if code == "parse":
        message = _("Signal %(alias)s cannot be read. %(reason)s") % {
            "alias": alias,
            "reason": parse_reason(details.get("reason"), details.get("row")),
        }
        return UserError(message, exclude=_excludable(project, [alias]), links=[upload])
    if code == "missing_file":
        message = _("The file of signal %(alias)s is missing. Upload it again.") % {"alias": alias}
        return UserError(message, exclude=_excludable(project, [alias]), links=[upload])
    if code == "empty_after_sync":
        message = _("Signal %(alias)s has no samples in the common time window.") % {"alias": alias}
        return UserError(message, exclude=_excludable(project, [alias]), links=[upload])
    if code == "empty_time_range":
        message = _(
            "Signal %(alias)s has no samples in the selected time range. Widen the time range in the cleaning settings."
        ) % {"alias": alias}
        return UserError(message, exclude=_excludable(project, [alias]))
    if code == "no_merge_overlap":
        return UserError(_("The recordings have no time points in common after synchronization."), links=[upload])
    if code == "too_few_points":
        return UserError(_("Too few samples remain for analysis. Widen the time range in the cleaning settings."))
    if code in ("too_few_signals", "no_signals"):
        if code == "too_few_signals":
            message = _("Group analysis needs at least two signals.")
        else:
            message = _("No signals are selected.")
        message += " " + _("Include more signals on the Cleaning step or upload more recordings.")
        links = [upload] if step == "cleaning" else [_cleaning_link(project), upload]
        return UserError(message, links=links)
    return UserError(str(exc))
