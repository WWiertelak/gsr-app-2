"""UI screens: Projects → Upload → Cleaning → Analysis (Synchronization | Co-arousal) → Interviews → Export.

Server-rendered pages; HTMX posts swap the main area of the current screen
(``HX-Request`` requests get the partial template). Every form also works as a
plain POST that redirects back to its page.
"""

from __future__ import annotations

import json
from itertools import zip_longest
from typing import Any

import pandas as pd
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.formats import number_format
from django.utils.text import slugify
from django.utils.translation import get_language, ngettext
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import charts, params, vsr_text
from .errors import UserError, describe, no_overlap_message
from .models import AnalysisResult, Dataset, InterviewMoment, Project
from .pipeline.common import clock_seconds_of_day, clock_start
from .pipeline.config import PipelineConfig
from .pipeline.vsr import DEFAULT_ASYNCHRONY_THRESHOLD, PRIMARY, RESERVE
from .reporting.report import render_report_pdf, report_text, resolve_lang
from .reporting.vsr_charts import AVERAGE_COLOR, lesson_ticks
from .services import curation, exports
from .services.analysis import AnalysisError
from .services.datasets import add_dataset, normalize_alias
from .services.display import chart_scale, set_chart_scale
from .services.interview import (
    ASYNCHRONY_KEY,
    ASYNCHRONY_LIMITS,
    COUNT_LIMITS,
    InterviewConfig,
    InterviewRoles,
    apply_interview_config,
    forget_interview_alias,
    load_interview_config,
    rename_interview_alias,
)
from .services.results import (
    FALLBACK_SIGNAL_COLOR,
    compute_analysis,
    compute_cleaning,
    export_is_stale,
    get_result,
    included_datasets,
    is_stale,
    recording_overview,
    signal_colors,
)
from .services.vsr import compute_interviews, interview_colors
from .services.vsr import preview as moment_preview
from .services.vsr import report_data as vsr_report_data

STEPS = (
    ("upload", "gsr:upload"),
    ("cleaning", "gsr:cleaning"),
    ("analysis", "gsr:analysis"),
    ("interviews", "gsr:interviews"),
    ("export", "gsr:export"),
)
ANALYSIS_TABS = ("synchronization", "coarousal")
RAIL_PROJECTS = 6  # other projects offered by the rail's project switcher


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _project(pk: Any) -> Project:
    return get_object_or_404(Project, pk=pk)


def _current_values(project: Project) -> dict[str, Any]:
    try:
        return PipelineConfig.from_dict(project.pipeline_config).model_dump()
    except ValueError:
        return dict(params.DEFAULTS)


def _applied_values(result: AnalysisResult | None) -> dict[str, Any] | None:
    """The settings a stored result was computed with, or None without a result."""
    if result is None:
        return None
    try:
        return PipelineConfig.from_dict(result.config).model_dump()
    except ValueError:
        return None


def _steps(project: Project, current: str) -> list[dict[str, Any]]:
    """The project steps for the left rail, each with a one-line text status (not only a colour)."""
    labels = {"upload": _("Import"), "cleaning": _("Cleaning"), "analysis": _("Analysis"),
              "interviews": _("Interviews"), "export": _("Export")}
    cleaning = get_result(project, AnalysisResult.KIND_CLEANING)
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    interviews = get_result(project, AnalysisResult.KIND_VSR)
    recordings = project.datasets.count()
    done = {
        "upload": recordings > 0,
        "cleaning": cleaning is not None,
        "analysis": analysis is not None,
        "interviews": interviews is not None,
        "export": False,
    }
    stale = {"cleaning": is_stale(project, cleaning), "analysis": is_stale(project, analysis)}
    # The interview moments come from the analysis: an outdated analysis makes them outdated too.
    stale["interviews"] = interviews is not None and (is_stale(project, interviews) or stale["analysis"])
    stale["export"] = export_is_stale(project)
    status = {
        "upload": _recordings_label(recordings) if recordings else _("no recordings"),
        "cleaning": _cleaning_status(cleaning),
        "analysis": _("computed %(time)s") % {"time": _computed_time(analysis)} if analysis else _("not run"),
        "interviews": _interviews_status(interviews) if interviews else (
            _("not chosen") if analysis else _("needs analysis")),
        "export": _("ready") if analysis else _("needs analysis"),
    }
    return [
        {
            "key": key,
            "number": index + 1,
            "label": labels[key],
            "url": reverse(url_name, args=[project.pk]),
            "current": key == current,
            "done": done[key],
            "stale": stale.get(key, False),
            "status": _("outdated") if stale.get(key) else status[key],
        }
        for index, (key, url_name) in enumerate(STEPS)
    ]


def _stale_before(project: Project, step: str) -> dict[str, Any] | None:
    """The first outdated step before ``step``: refreshing it and the steps after it clears ``step`` too."""
    for item in _steps(project, step):
        if item["current"]:
            return None
        if item["stale"]:
            return item
    return None


def _cleaning_status(result: AnalysisResult | None) -> str:
    if result is None:
        return _("not run")
    return _rejected_label(result.payload["preview"]["cleaning_summary"]["totals"]["artifact_rejected_points"])


def _interviews_status(result: AnalysisResult) -> str:
    count = len(result.payload.get("interviews", []))
    return ngettext("%(count)s interview", "%(count)s interviews", count) % {"count": count}


def _recordings_label(count: int) -> str:
    return ngettext("%(count)s recording", "%(count)s recordings", count) % {
        "count": number_format(count, force_grouping=True)}


def _rejected_label(count: int) -> str:
    return ngettext("%(count)s rejected sample", "%(count)s rejected samples", count) % {
        "count": number_format(count, force_grouping=True)}


def _computed_time(result: AnalysisResult) -> str:
    """When a result was computed: the time for today's runs, the date for older ones."""
    computed = timezone.localtime(result.computed_at)
    return computed.strftime("%H:%M" if computed.date() == timezone.localdate() else "%Y-%m-%d")


def _page(request: HttpRequest, project: Project, step: str, partial: str, context: dict[str, Any]) -> HttpResponse:
    """A project screen: the full page, or only its main area (``partial``) for HTMX requests."""
    steps = _steps(project, step)
    context = {"project": project, "step": step, "steps": steps, "partial": partial, "htmx": _is_htmx(request),
               **context}
    if not context["htmx"]:
        context["step_label"] = next(item["label"] for item in steps if item["current"])
        context["other_projects"] = Project.objects.exclude(pk=project.pk).order_by("-updated_at")[:RAIL_PROJECTS]
    return render(request, partial if context["htmx"] else "gsr/project_page.html", context)


# ─── Projects ────────────────────────────────────────────────────────────────


def project_list(request: HttpRequest) -> HttpResponse:
    form_errors: list[str] = []
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        description = (request.POST.get("description") or "").strip()
        if not name:
            form_errors.append(_("Project name is required."))
        elif len(name) > 200:
            form_errors.append(_("Project name cannot exceed 200 characters."))
        else:
            project = Project.objects.create(name=name, description=description)
            return redirect("gsr:upload", project.pk)
    projects = Project.objects.annotate(dataset_count=Count("datasets"))
    return render(request, "gsr/projects.html", {"projects": projects, "form_errors": form_errors})


def project_detail(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    return redirect("gsr:upload", project.pk)


@require_POST
def project_update(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    name = (request.POST.get("name") or "").strip()
    if not name or len(name) > 200:
        messages.error(request, _("Project name is required (up to 200 characters)."))
    else:
        project.name = name
        project.description = (request.POST.get("description") or "").strip()
        project.save(update_fields=["name", "description", "updated_at"])
        messages.success(request, _("Project details saved."))
    return redirect("gsr:upload", project.pk)


@require_POST
def project_delete(request: HttpRequest, pk: Any) -> HttpResponse:
    # Cascades to the datasets; their post_delete handler removes the stored files.
    _project(pk).delete()
    messages.success(request, _("Project deleted."))
    return redirect("gsr:projects")


# ─── 1 Upload ────────────────────────────────────────────────────────────────


def upload(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    if request.method == "POST":
        files = request.FILES.getlist("files")
        if not files:
            messages.error(request, _("Choose at least one CSV file."))
        for upload_file in files:
            try:
                dataset = add_dataset(project, upload_file)
            except ValidationError as exc:
                messages.error(request, f"{upload_file.name}: {' '.join(exc.messages)}")
            else:
                messages.success(request, _("Uploaded %(file)s as %(alias)s.") % {"file": upload_file.name,
                                                                                   "alias": dataset.alias})
        if files:
            project.save(update_fields=["updated_at"])
        return redirect("gsr:upload", project.pk)
    return _page(request, project, "upload", "gsr/partials/upload_main.html", _upload_context(project))


def _upload_context(project: Project) -> dict[str, Any]:
    datasets = list(project.datasets.order_by("created_at"))
    colors = signal_colors(project)
    ranges = _recorded_ranges(datasets)
    overlap_error = _overlap_error(ranges)
    recordings_label = _recordings_label(len(datasets))
    if not overlap_error and len(ranges) > 1:
        window = min(end for _start, end in ranges.values()) - max(start for start, _end in ranges.values())
        recordings_label = _("%(recordings)s · common window %(minutes)s min") % {
            "recordings": recordings_label, "minutes": number_format(round(window.total_seconds() / 60.0, 1), 1)}
    return {
        "datasets": [_dataset_row(dataset, colors[dataset.alias]) for dataset in datasets],
        "overlap_error": overlap_error,
        "recordings_label": recordings_label,
        "max_upload_mb": settings.MAX_UPLOAD_MB,
    }


def _dataset_row(dataset: Dataset, color: str) -> dict[str, Any]:
    """One row of the recordings table: clock range, duration and sample count from the upload metadata."""
    metadata = dataset.parsed_metadata or {}
    row: dict[str, Any] = {"dataset": dataset, "color": color, "start": None, "end": None, "minutes": None,
                           "samples": None}
    if {"recorded_start", "recorded_end"} <= set(metadata):
        start, end = pd.Timestamp(metadata["recorded_start"]), pd.Timestamp(metadata["recorded_end"])
        row.update(start=start.strftime("%H:%M:%S"), end=end.strftime("%H:%M:%S"),
                   minutes=(end - start).total_seconds() / 60.0)
    if metadata.get("row_count") is not None:
        row["samples"] = number_format(metadata["row_count"], force_grouping=True)
    return row


def _recorded_ranges(datasets: list[Dataset]) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """Clock range of each included recording, from upload metadata."""
    return {
        dataset.alias: (
            pd.Timestamp(dataset.parsed_metadata["recorded_start"]),
            pd.Timestamp(dataset.parsed_metadata["recorded_end"]),
        )
        for dataset in datasets
        if dataset.included and {"recorded_start", "recorded_end"} <= set(dataset.parsed_metadata or {})
    }


def _overlap_error(ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> str | None:
    """Why the included recordings cannot be cleaned together (no shared clock window)."""
    if len(ranges) < 2 or max(start for start, _end in ranges.values()) < min(end for _start, end in ranges.values()):
        return None
    return no_overlap_message(ranges)[0]


def upload_overview(request: HttpRequest, pk: Any) -> HttpResponse:
    """Clock ranges, shared window and raw traces of the uploaded files (loaded after the page)."""
    project = _project(pk)
    colors = signal_colors(project)
    overview = recording_overview(list(project.datasets.order_by("created_at")))
    raw_series = []
    for row in overview["rows"]:
        row["color"] = colors[row["dataset"].alias]
        if row["points"]:
            raw_series.append(charts.raw_signal(row["dataset"].alias, row["color"], row["points"]))
    overlap = overview["overlap"]
    window = None
    if overlap and overlap["ok"]:
        window = _window_context(overlap["start"], overlap["end"], _current_values(project))
        if window:
            # The analysed period's band on the timeline, inside the shaded common window.
            window["band_left_pct"] = round(overlap["bar_left_pct"] + overlap["bar_width_pct"] * window["from_pct"] / 100, 2)
            window["band_width_pct"] = round(overlap["bar_width_pct"] * (window["to_pct"] - window["from_pct"]) / 100, 2)
    context = {
        "project": project,
        "overview": overview,
        "window": window,
        "raw_series": raw_series,
        "raw_overlay": charts.overlay(raw_series, axis="time") if len(raw_series) > 1 else None,
    }
    return render(request, "gsr/partials/upload_overview.html", context)


def _window_context(start: pd.Timestamp, end: pd.Timestamp, values: dict[str, Any]) -> dict[str, Any] | None:
    """The analysed period picked on the overlap timeline (seconds from the common start, clock times, % of the common
    window), or None when the common window is too short for two handles."""
    window = params.analysis_window(values, (end - start).total_seconds())
    if window["max"] < 2:
        return None
    window.update(
        start=start,
        end=end,
        duration_minutes=(end - start).total_seconds() / 60.0,
        from_clock=(start + pd.Timedelta(seconds=window["from"])).strftime("%H:%M:%S"),
        to_clock=(start + pd.Timedelta(seconds=window["to"])).strftime("%H:%M:%S"),
        minutes=(window["to"] - window["from"]) / 60.0,
        from_pct=round(100.0 * window["from"] / window["max"], 4),
        to_pct=round(100.0 * window["to"] / window["max"], 4),
        # Seconds after midnight of the common start, so the page can show the handles' clock times while dragging.
        clock_offset=start.hour * 3600 + start.minute * 60 + start.second,
    )
    return window


def _posted_seconds(request: HttpRequest, name: str) -> int:
    try:
        return int(request.POST.get(name, ""))
    except ValueError:
        raise ValueError(_("The analysed period must lie inside the common window and end after it starts.")) from None


@require_POST
def upload_window(request: HttpRequest, pk: Any) -> HttpResponse:
    """Save the analysed period picked with the overlap timeline's handles as the Cleaning time range."""
    project = _project(pk)
    ranges = _recorded_ranges(list(project.datasets.all()))
    error = _overlap_error(ranges) or (None if ranges else _("Include at least one recording."))
    window = None
    if error is None:
        start = max(start for start, _end in ranges.values())
        end = min(end for _start, end in ranges.values())
        try:
            if request.POST.get("reset"):
                updates: dict[str, Any] = {"time_range_from": None, "time_range_to": None}
            else:
                updates = params.window_settings(
                    _posted_seconds(request, "window_from"), _posted_seconds(request, "window_to"),
                    (end - start).total_seconds(),
                )
            settings = params.apply_settings(project.pipeline_config, updates)
        except ValueError as exc:
            error = str(exc)
        else:
            # No bound is the default: drop it rather than store None, as Reset defaults does.
            project.pipeline_config = {key: value for key, value in settings.items()
                                       if not (key in updates and value is None)}
            project.save(update_fields=["pipeline_config", "updated_at"])
        window = _window_context(start, end, _current_values(project))
    if not _is_htmx(request):
        if error:
            messages.error(request, error)
        else:
            messages.success(request, _("Analysed period saved."))
        return redirect("gsr:upload", project.pk)
    return render(request, "gsr/partials/upload_window.html", {"project": project, "window": window,
                                                                "window_error": error})


@require_POST
def dataset_rename(request: HttpRequest, pk: Any, dataset_pk: Any) -> HttpResponse:
    """Rename a signal. HTMX (inline save on Enter or blur) gets its table row back, with the result."""
    project = _project(pk)
    dataset = get_object_or_404(Dataset, pk=dataset_pk, project=project)
    error = None
    posted = request.POST.get("alias") or ""
    try:
        if not posted.strip():
            raise ValidationError(_("Alias cannot be empty"))
        alias = normalize_alias(posted)
        if project.datasets.exclude(pk=dataset.pk).filter(alias=alias).exists():
            raise ValidationError(_("Alias already exists in project: %(alias)s") % {"alias": alias})
    except ValidationError as exc:
        error = " ".join(exc.messages)
    else:
        with transaction.atomic():
            rename_interview_alias(project, dataset.alias, alias)
            dataset.alias = alias
            dataset.save(update_fields=["alias"])
    if _is_htmx(request):
        context = {
            "project": project,
            "item": _dataset_row(dataset, signal_colors(project)[dataset.alias]),
            # A cleared field gets the saved alias back; other rejected input stays for the user to fix.
            "alias_value": posted if error and posted.strip() else dataset.alias,
            "alias_error": error,
            "alias_saved": error is None,
        }
        response = render(request, "gsr/partials/upload_row.html", context)
        if error is None:
            # The overlap timeline and the raw charts show the alias too: reload them.
            response["HX-Trigger"] = "gsr:datasets-changed"
            response.write(render_to_string(
                "gsr/partials/upload_action_bar.html", {**_upload_context(project), "project": project, "oob": True},
                request))
        return response
    if error:
        messages.error(request, error)
    else:
        messages.success(request, _("Signal renamed to %(alias)s.") % {"alias": dataset.alias})
    return redirect("gsr:upload", project.pk)


@require_POST
def dataset_exclude(request: HttpRequest, pk: Any, dataset_pk: Any) -> HttpResponse:
    """Leave one signal out of cleaning and analysis (an error's recovery action), then rerun ``next``."""
    project = _project(pk)
    dataset = get_object_or_404(Dataset, pk=dataset_pk, project=project)
    dataset.included = False
    dataset.save(update_fields=["included"])
    step = request.POST.get("next")
    if not _is_htmx(request):
        messages.success(request, _("Signal %(alias)s excluded from cleaning and analysis.") % {"alias": dataset.alias})
    if step == "cleaning":
        error = _run(compute_cleaning, project, step)
    elif step in ("analysis", "interviews", "export"):
        # The interview moments come from the analysis run: rerun the analysis, then choose them again on the screen.
        error = None
        if get_result(project, AnalysisResult.KIND_CLEANING) is not None:
            error = _run(compute_cleaning, project, step)
        error = error or _run(compute_analysis, project, step)
    else:
        return redirect("gsr:upload", project.pk)
    if not _is_htmx(request):
        if error:
            messages.error(request, error.message)
        return redirect(f"gsr:{step}", project.pk)
    if step == "cleaning":
        return _page(request, project, step, "gsr/partials/cleaning_main.html", _cleaning_context(project, error))
    if step == "export":
        return _page(request, project, step, "gsr/partials/export_main.html", _export_context(project, error))
    if step == "interviews":
        return _interviews_page(request, project, error=error)
    return _page(
        request, project, step, "gsr/partials/analysis_main.html", _analysis_context(project, ANALYSIS_TABS[0], error)
    )


@require_POST
def dataset_delete(request: HttpRequest, pk: Any, dataset_pk: Any) -> HttpResponse:
    project = _project(pk)
    dataset = get_object_or_404(Dataset, pk=dataset_pk, project=project)
    alias = dataset.alias
    with transaction.atomic():
        dataset.delete()
        forget_interview_alias(project, alias)
    messages.success(request, _("Signal %(alias)s deleted.") % {"alias": alias})
    return redirect("gsr:upload", project.pk)


# ─── 2 Cleaning ──────────────────────────────────────────────────────────────


def _cleaning_context(project: Project, error: UserError | None = None) -> dict[str, Any]:
    values = _current_values(project)
    result = get_result(project, AnalysisResult.KIND_CLEANING)
    colors = signal_colors(project)
    groups = [
        {
            "group": group,
            "toggle": params.field_rows((group.toggle,), values)[0] if group.toggle else None,
            "help": " ".join(str(text) for text in (group.help, group.toggle and group.toggle.help) if text),
            "rows": params.field_rows(group.params, values),
        }
        for group in params.CLEANING_GROUPS
    ]
    datasets = [{"dataset": dataset, "color": colors[dataset.alias]}
                for dataset in project.datasets.order_by("created_at")]
    included_count = len(included_datasets(project))
    context: dict[str, Any] = {
        "drawer": "gsr/partials/cleaning_settings.html",
        "form_id": "cleaning-settings",
        "form_action": reverse("gsr:cleaning", args=[project.pk]),
        "reset_action": reverse("gsr:cleaning_reset", args=[project.pk]),
        "changed_label": params.changed_label(params.cleaning_params(), values),
        "groups": groups,
        "settings_summary": params.cleaning_summary(
            values, included_count, len(datasets), _applied_values(result),
            result is not None and set(result.dataset_ids) != {str(row["dataset"].id) for row in datasets
                                                                if row["dataset"].included},
        ),
        "datasets": datasets,
        "result": result,
        "stale": is_stale(project, result),
        "error": error,
        "included_count": included_count,
    }
    if result is not None:
        preview = result.payload["preview"]
        result_values = PipelineConfig.from_dict(result.config).model_dump()
        context["charts"] = charts.cleaning_charts(preview, colors, result_values)
        context["summary"] = preview["cleaning_summary"]
        context["rejected_label"] = _rejected_label(preview["cleaning_summary"]["totals"]["artifact_rejected_points"])
        context["alignment"] = preview["alignment"]
        context["preview_range"] = preview["range"]
        context["quality_rows"] = [
            {**row, "color": colors.get(row["alias"], FALLBACK_SIGNAL_COLOR),
             "tile_note": _("rejected %(rejected)s of %(total)s samples") % {
                 "rejected": number_format(row["artifact_rejected_points"], force_grouping=True),
                 "total": number_format(row["synced_points"], force_grouping=True)}}
            for row in preview["cleaning_summary"]["per_alias"]
        ]
    return context


def cleaning(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    error = None
    if request.method == "POST":
        error = _save_cleaning(request, project)
        if error is None:
            error = _run(compute_cleaning, project, "cleaning")
        if not _is_htmx(request):
            if error:
                messages.error(request, error.message)
            return redirect("gsr:cleaning", project.pk)
    return _page(request, project, "cleaning", "gsr/partials/cleaning_main.html", _cleaning_context(project, error))


def _save_cleaning(request: HttpRequest, project: Project) -> UserError | None:
    selected = set(request.POST.getlist("datasets"))
    updates = params.parse_params(params.cleaning_params(), request.POST)
    try:
        settings = params.apply_settings(project.pipeline_config, updates)
    except ValueError as exc:
        return UserError(str(exc))
    with transaction.atomic():
        for dataset in project.datasets.all():
            included = str(dataset.pk) in selected
            if dataset.included != included:
                dataset.included = included
                dataset.save(update_fields=["included"])
        project.pipeline_config = settings
        project.save(update_fields=["pipeline_config", "updated_at"])
    return None


@require_POST
def cleaning_reset(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    names = {param.name for param in params.cleaning_params()}
    project.pipeline_config = {key: value for key, value in (project.pipeline_config or {}).items()
                               if key not in names}
    project.save(update_fields=["pipeline_config", "updated_at"])
    error = _run(compute_cleaning, project, "cleaning")
    if not _is_htmx(request):
        if error:
            messages.error(request, error.message)
        return redirect("gsr:cleaning", project.pk)
    return _page(request, project, "cleaning", "gsr/partials/cleaning_main.html", _cleaning_context(project, error))


@require_POST
def cleaning_refresh(request: HttpRequest, pk: Any) -> HttpResponse:
    """Compute the cleaning preview with the saved settings (first visit, or after uploads changed)."""
    project = _project(pk)
    error = _run(compute_cleaning, project, "cleaning")
    if not _is_htmx(request):
        if error:
            messages.error(request, error.message)
        return redirect("gsr:cleaning", project.pk)
    return _page(request, project, "cleaning", "gsr/partials/cleaning_main.html", _cleaning_context(project, error))


def cleaning_csv(request: HttpRequest, pk: Any) -> HttpResponse:
    """Full-resolution cleaned signals of the last cleaning preview."""
    project = _project(pk)
    result = get_result(project, AnalysisResult.KIND_CLEANING)
    if result is None:
        raise Http404("Compute the cleaning preview first.")
    try:
        content = exports.clean_signals_csv(project, result)
    except AnalysisError as exc:
        messages.error(request, describe(exc, project, "export").message)
        return redirect("gsr:cleaning", project.pk)
    return _attachment(content, _download_name(project, "clean_signals.csv"), "text/csv; charset=utf-8")


def _run(compute: Any, project: Project, step: str) -> UserError | None:
    try:
        compute(project)
    except AnalysisError as exc:
        return describe(exc, project, step)
    return None


# ─── 3 Analysis ──────────────────────────────────────────────────────────────


SIGNIFICANCE_LEVEL = 0.05


def _significant(p_value: Any) -> bool | None:
    """Whether a surrogate p-value is below 0.05; None when there was no test."""
    return None if p_value is None else p_value < SIGNIFICANCE_LEVEL


def _mini_bar(start: float, end: float, span: list[float] | None) -> dict[str, str] | None:
    """Where an episode sits on the timeline, as CSS percentages for the episode table's mini bar."""
    if not span or span[1] <= span[0]:
        return None
    width = span[1] - span[0]
    left = min(max((start - span[0]) / width, 0.0), 1.0) * 100
    size = min(max((end - start) / width * 100, 1.0), 100 - left)
    return {"left": f"{left:.2f}", "width": f"{size:.2f}"}


def _analysis_context(project: Project, tab: str, error: UserError | None = None) -> dict[str, Any]:
    values = _current_values(project)
    result = get_result(project, AnalysisResult.KIND_ANALYSIS)
    colors = signal_colors(project)
    analysis_blocked = len(included_datasets(project)) < 2
    sync_advanced = params.field_rows(params.SYNCHRONY_ADVANCED, values)
    coarousal_advanced = params.field_rows(params.COAROUSAL_ADVANCED, values)
    context: dict[str, Any] = {
        "drawer": "gsr/partials/analysis_settings.html",
        "form_id": "analysis-settings",
        "form_action": reverse("gsr:analysis", args=[project.pk]),
        "reset_action": reverse("gsr:analysis_reset", args=[project.pk]),
        "apply_disabled": analysis_blocked,
        "changed_label": params.changed_label(params.ANALYSIS_PARAMS, values),
        "tab": tab,
        "tabs": ANALYSIS_TABS,
        "sync_essential": params.field_rows(params.SYNCHRONY_ESSENTIAL, values),
        "sync_advanced": sync_advanced,
        "sync_advanced_changed": any(row["modified"] for row in sync_advanced),
        "coarousal_essential": params.field_rows(params.COAROUSAL_ESSENTIAL, values),
        "coarousal_advanced": coarousal_advanced,
        "coarousal_advanced_changed": any(row["modified"] for row in coarousal_advanced),
        "interview_essential": params.field_rows(params.INTERVIEW_ESSENTIAL, values),
        "settings_summary": params.analysis_summary(values, _applied_values(result)),
        "result": result,
        "stale": is_stale(project, result),
        "stale_from": _stale_before(project, "analysis"),
        "error": error,
        "analysis_blocked": analysis_blocked,
        "colors": colors,
    }
    if result is not None:
        payload = result.payload
        synchrony = payload.get("synchrony", {})
        coarousal = payload.get("coarousal", {})
        config = PipelineConfig.from_dict(result.config)
        span = charts.timeline_span(synchrony)
        coarousal_chart = charts.coarousal_states(coarousal, charts.preview_clock(payload.get("preview", {})))
        def numbered(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{**episode, "number": index + 1, "duration": episode["end_relative"] - episode["start_relative"],
                     "bar": _mini_bar(episode["start_relative"], episode["end_relative"], span)}
                    for index, episode in enumerate(episodes)]

        sync_episodes = numbered(synchrony.get("episodes", []))
        divergent_episodes = numbered(synchrony.get("divergent_episodes", []))
        scale = chart_scale(project)
        context.update(
            synchrony=synchrony,
            coarousal=coarousal,
            # The recordings' own clock at lesson time 0: every table shows it next to the lesson time.
            clock_start=clock_start(payload.get("preview", {}).get("alignment")),
            chart_scale=scale,
            applied=config.model_dump(),
            sync_signals=charts.sync_signals(synchrony, payload.get("preview", {}), colors, scale, config.model_dump()),
            sync_matrix=charts.sync_matrix(synchrony),
            coarousal_states=coarousal_chart,
            sync_episodes=sync_episodes,
            sync_episode_minutes=sum(episode["duration"] for episode in sync_episodes),
            sync_episode_rule=_("subgroups of ≥ %(size)s people above %(threshold)s") % {
                "size": config.synchrony_episode_min_size,
                "threshold": params.display_number(config.synchrony_threshold)},
            # Results stored before divergent synchrony was found have none to show (None), not zero.
            divergent_episodes=divergent_episodes if "divergent_episodes" in synchrony else None,
            divergent_rule=_("r ≤ −%(threshold)s in every window") % {
                "threshold": params.display_number(config.synchrony_divergent_threshold)},
            cluster_members=" | ".join(" · ".join(cluster["members"])
                                       for cluster in synchrony.get("clusters", {}).get("clusters", [])),
            pairs=[{**pair, "significant": _significant(pair.get("p_value"))} for pair in synchrony.get("pairs", [])],
            coarousal_episodes=[
                {**episode, "number": index + 1} for index, episode in enumerate(coarousal.get("episodes", []))
            ],
            state_bars=[
                {**row, "high_width": f"{row['high_pct']:.2f}", "neutral_width": f"{row['neutral_pct']:.2f}",
                 "low_width": f"{row['low_pct']:.2f}"}
                for row in coarousal.get("state_percentages", [])
            ],
            has_pair_p=any("p_value" in pair for pair in synchrony.get("pairs", [])),
            sync_significant=_significant(synchrony.get("stats", {}).get("mean_synchrony_p_value")),
            # The episode track adds a lane per overlapping episode above the state map.
            coarousal_height=max(180, len(coarousal.get("aliases", [])) * 60 + 112)
            + (coarousal_chart["lanes"] * 22 + 16 if coarousal_chart["lanes"] else 0),
        )
    return context



def analysis(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    tab = request.GET.get("tab") or request.POST.get("tab") or ANALYSIS_TABS[0]
    if tab not in ANALYSIS_TABS:
        tab = ANALYSIS_TABS[0]
    error = None
    if request.method == "POST":
        updates = params.parse_params(params.ANALYSIS_PARAMS, request.POST)
        try:
            project.pipeline_config = params.apply_settings(project.pipeline_config, updates)
        except ValueError as exc:
            error = UserError(str(exc))
        else:
            project.save(update_fields=["pipeline_config", "updated_at"])
            error = _run(compute_analysis, project, "analysis")
        if not _is_htmx(request):
            if error:
                messages.error(request, error.message)
            return redirect(f"{reverse('gsr:analysis', args=[project.pk])}?tab={tab}")
    return _page(
        request, project, "analysis", "gsr/partials/analysis_main.html", _analysis_context(project, tab, error)
    )


@require_POST
def analysis_chart_scale(request: HttpRequest, pk: Any) -> HttpResponse:
    """Show the signals chart, the PDF report and the merged CSV on z-scores or min-max (0–1); nothing is rerun."""
    project = _project(pk)
    error = None
    try:
        set_chart_scale(project, request.POST.get("chart_scale", ""))
    except ValueError as exc:
        error = UserError(str(exc))
    if not _is_htmx(request):
        if error:
            messages.error(request, error.message)
        return redirect(f"{reverse('gsr:analysis', args=[project.pk])}?tab={ANALYSIS_TABS[0]}")
    return _page(request, project, "analysis", "gsr/partials/analysis_main.html",
                 _analysis_context(project, ANALYSIS_TABS[0], error))


@require_POST
def analysis_reset(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    project.pipeline_config = params.reset_analysis(project.pipeline_config)
    project.interview_config = {**(project.interview_config or {}), ASYNCHRONY_KEY: DEFAULT_ASYNCHRONY_THRESHOLD}
    project.save(update_fields=["pipeline_config", "interview_config", "updated_at"])
    return _analysis_after_settings(request, project)


def _analysis_after_settings(request: HttpRequest, project: Project) -> HttpResponse:
    tab = request.POST.get("tab") if request.POST.get("tab") in ANALYSIS_TABS else ANALYSIS_TABS[0]
    if not _is_htmx(request):
        return redirect(f"{reverse('gsr:analysis', args=[project.pk])}?tab={tab}")
    return _page(request, project, "analysis", "gsr/partials/analysis_main.html",
                 _analysis_context(project, tab))


# ─── 4 Interviews ────────────────────────────────────────────────────────────

MOMENT_CHART_HEIGHT = 190
LESSON_CHART_HEIGHT = 340


def _interview_config(project: Project) -> InterviewConfig:
    """The stored roles and moment counts; defaults when the stored value no longer validates."""
    try:
        return load_interview_config(project)
    except ValueError:
        return InterviewConfig(roles=InterviewRoles(None, ()))


def _role_form(project: Project, roles: dict[str, Any]) -> dict[str, Any]:
    """The interview participants card, with at least one editable pair row."""
    pairs = [list(pair) for pair in roles.get("student_pairs") or []]
    pairs = pairs or [["", ""]]
    return {
        "recordings": [{"alias": dataset.alias, "included": dataset.included}
                       for dataset in project.datasets.order_by("created_at")],
        "teacher": roles.get("teacher_alias") or "",
        "pairs": [{"number": index + 1, "left": left, "right": right} for index, (left, right) in enumerate(pairs)],
        "primary_moment_count": roles.get("primary_moment_count"),
        "reserve_moment_count": roles.get("reserve_moment_count"),
        "asynchrony_threshold": roles.get(ASYNCHRONY_KEY),
        "limits": COUNT_LIMITS,
        "asynchrony_limits": ASYNCHRONY_LIMITS,
    }


def _posted_roles(request: HttpRequest) -> dict[str, Any]:
    pairs = [[left.strip(), right.strip()]
             for left, right in zip_longest(request.POST.getlist("pair_left"), request.POST.getlist("pair_right"), fillvalue="")
             if left.strip() or right.strip()]
    roles = {
        "teacher_alias": (request.POST.get("teacher_alias") or "").strip() or None,
        "student_pairs": pairs,
    }
    for name in (*COUNT_LIMITS, ASYNCHRONY_KEY):
        if name in request.POST:
            roles[name] = request.POST[name]
    return roles


def _interview_label(interview: dict[str, Any]) -> str:
    if interview["kind"] == "teacher":
        return _("Teacher %(alias)s") % {"alias": interview["participants"][0]}
    return _("Pair %(left)s + %(right)s") % {"left": interview["participants"][0], "right": interview["participants"][1]}


def _signal_names(interview: dict[str, Any]) -> list[str]:
    """The names of an interview's two signals: the pair's two people, or the teacher and the students' average."""
    participants = interview["participants"]
    return [participants[0], _("Students' average") if interview["kind"] == "teacher" else participants[1]]


def _moment_chart(payload: dict[str, Any], interview: dict[str, Any], row: InterviewMoment,
                  colors: list[str | None], number: int | None) -> dict[str, Any] | None:
    """The moment preview for ``momentSignals`` (static/gsr/js/charts.js): both signals around the moment, shaded."""
    window = moment_preview(payload, interview, row.start_seconds, row.end_seconds)
    if window is None:
        return None
    settings = payload.get("analysis_settings") or {}
    minutes = [round(seconds / 60.0, 4) for seconds in window["seconds"]]
    series = [
        {"alias": name, "color": color or AVERAGE_COLOR, "points": [[x, y] for x, y in zip(minutes, values)]}
        for name, color, values in zip(_signal_names(interview), colors, (window["a"], window["b"]))
    ]
    shared = row.proposal.get("shared_state") if not row.is_manual else None
    return {
        "series": series,
        "moment": {"start": window["start"] / 60.0, "end": window["end"] / 60.0, "label": str(number or "×")},
        "range": {"min": window["low"] / 60.0, "max": window["high"] / 60.0},
        "clock_start": clock_seconds_of_day(curation.lesson_clock_start(payload)),
        # The Co-arousal thresholds, drawn when the moment is about a shared state.
        "high": settings.get("contagion_high_z") if shared == "HIGH" else None,
        "low": settings.get("contagion_low_z") if shared == "LOW" else None,
    }


def _moment_view(item: curation.CuratedMoment, interview: dict[str, Any], payload: dict[str, Any],
                 colors: list[str | None]) -> dict[str, Any]:
    """One card of an interview's list: the moment as the researcher kept it, its preview and its justification."""
    row = item.row
    lesson = payload.get("lesson") or {"start_seconds": 0.0, "end_seconds": 0.0}
    duration = row.end_seconds - row.start_seconds
    view = {
        "id": str(row.pk),
        "row": row,
        "number": item.number,
        "primary": item.role == PRIMARY,
        "role": item.role,
        "rejected": row.status == InterviewMoment.STATUS_REJECTED,
        "accepted": row.status == InterviewMoment.STATUS_ACCEPTED,
        "manual": row.is_manual,
        "time_changed": row.time_changed,
        # A proposed reserve moment that moved up, or a primary one that moved down, says what the engine proposed.
        "proposed_role": item.proposed_role if item.role and item.proposed_role not in (None, item.role) else None,
        "start_seconds": row.start_seconds,
        "end_seconds": row.end_seconds,
        "duration_seconds": duration,
        "start_clock": item.start_clock,
        "end_clock": item.end_clock,
        "bar": _bar(row.start_seconds, row.end_seconds, lesson),
        "from_timeline": not row.is_manual and bool(row.proposal.get(curation.FROM_TIMELINE)),
        "chart": _moment_chart(payload, interview, row, colors, item.number),
        "chart_height": MOMENT_CHART_HEIGHT,
    }
    if not row.is_manual and row.proposal.get("step") in range(1, 6):
        view.update(step=row.proposal["step"], step_title=vsr_text.step_title(row.proposal["step"]),
                    sentence=vsr_text.justification(row.proposal, interview["kind"]))
    return view


def _bar(start: float, end: float, lesson: dict[str, Any]) -> dict[str, str]:
    """Left edge and width (percent of the analysed period) of a stretch on the lesson timeline."""
    origin, span = lesson["start_seconds"], lesson["end_seconds"] - lesson["start_seconds"]
    left = min(max((start - origin) / span, 0.0), 1.0) * 100 if span > 0 else 0.0
    width = min(max((end - start) / span * 100, 0.6), 100 - left) if span > 0 else 100.0
    return {"left": f"{left:.2f}", "width": f"{width:.2f}"}


# A candidate's place relative to the interview's list: on it (primary or reserve), rejected from it, or not on it.
CANDIDATE_OTHER = "other"
CANDIDATE_REJECTED = "rejected"


def _candidate_views(interview: dict[str, Any], lesson: dict[str, Any],
                     curated: list[curation.CuratedMoment], tab: str) -> list[dict[str, Any]]:
    """Every candidate moment of the interview (``pipeline/vsr`` ``candidates``) with where it stands on the list."""
    on_list = {identity: item for item in curated if (identity := curation.row_identity(item.row)) is not None}
    views = []
    for candidate in interview.get("candidates", []):
        item = on_list.get(curation.candidate_identity(candidate))
        if item is None:
            state, number, moment_id = CANDIDATE_OTHER, None, None
        else:
            state, number, moment_id = item.role or CANDIDATE_REJECTED, item.number, str(item.row.pk)
        step, rank = candidate["step"], candidate["candidate_rank"]
        views.append({
            "id": f"{tab}-c{step}-{rank}",
            "step": step,
            "rank": rank,
            "count": candidate["candidate_count"],
            "state": state,
            "number": number,
            "moment_id": moment_id,
            "start_seconds": candidate["start_seconds"],
            "end_seconds": candidate["end_seconds"],
            "duration_seconds": candidate["end_seconds"] - candidate["start_seconds"],
            "start_clock": candidate.get("start_clock"),
            "end_clock": candidate.get("end_clock"),
            "bar": _bar(candidate["start_seconds"], candidate["end_seconds"], lesson),
            "step_title": vsr_text.step_title(step),
            "sentence": vsr_text.justification(candidate, interview["kind"]),
        })
    return views


def _lesson_chart(payload: dict[str, Any], interview: dict[str, Any], colors: list[str | None],
                  candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The interview's two signals over the whole analysed period with every candidate shaded, for ``lessonSignals``
    (static/gsr/js/charts.js); None for moments chosen before the signals were stored."""
    minutes = [round(seconds / 60.0, 4) for seconds in payload.get("trace_seconds") or []]
    trace = interview.get("trace") or {}
    if len(minutes) < 2 or any(len(trace.get(side) or []) != len(minutes) for side in ("a", "b")):
        return None
    return {
        "series": [
            {"alias": name, "color": color or AVERAGE_COLOR, "points": [[x, y] for x, y in zip(minutes, values)]}
            for name, color, values in zip(_signal_names(interview), colors, (trace["a"], trace["b"]))
        ],
        "candidates": [
            {"id": item["id"], "start": item["start_seconds"] / 60.0, "end": item["end_seconds"] / 60.0,
             "step": item["step"], "state": item["state"], "number": item["number"], "moment_id": item["moment_id"]}
            for item in candidates
        ],
        "range": {"min": minutes[0], "max": minutes[-1]},
        "clock_start": clock_seconds_of_day(curation.lesson_clock_start(payload)),
    }


def _interview_view(index: int, interview: dict[str, Any], payload: dict[str, Any],
                    curated: list[curation.CuratedMoment], colors: dict[str, str]) -> dict[str, Any]:
    lesson = payload.get("lesson") or {"start_seconds": 0.0, "end_seconds": 0.0}
    quality = payload.get("quality", {})
    line_colors = interview_colors(colors, interview)
    counts = interview.get("candidate_counts", {})
    tab = f"interview-{index + 1}"
    candidates = _candidate_views(interview, lesson, curated, tab)
    return {
        "tab": tab,
        "kind": interview["kind"],
        "label": _interview_label(interview),
        "participants": interview["participants"],
        "signals": [{"name": name, "color": color or AVERAGE_COLOR}
                    for name, color in zip(_signal_names(interview), line_colors)],
        "steps": [{**step, "count": counts.get(str(step["number"]), 0)} for step in vsr_text.steps()],
        "key": interview["key"],
        "moments": [_moment_view(item, interview, payload, line_colors) for item in curated],
        "primary_count": sum(1 for item in curated if item.role == PRIMARY),
        "reserve_count": sum(1 for item in curated if item.role == RESERVE),
        "rejected_count": sum(1 for item in curated if item.role is None),
        "kept_count": sum(1 for item in curated if item.role is not None),
        "stats": interview["stats"],
        "ticks": lesson_ticks(lesson["start_seconds"], lesson["end_seconds"]),
        # The recordings' own clock at lesson time 0: the timeline shows it under the lesson time.
        "clock_start": curation.lesson_clock_start(payload),
        # Moments chosen before every candidate was stored have none: choosing again adds them.
        "has_candidates": "candidates" in interview,
        "candidate_lanes": [
            {**step, "candidates": [item for item in candidates if item["step"] == step["number"]]}
            for step in vsr_text.steps()
        ],
        "candidate_list": candidates,
        "lesson_chart": _lesson_chart(payload, interview, line_colors, candidates),
        "lesson_chart_height": LESSON_CHART_HEIGHT,
        "poor_signals": [{"alias": alias, "pct": number_format(quality[alias]["rejected_pct"], 0)}
                         for alias in interview["participants"]
                         if quality.get(alias, {}).get("level") == "poor"],
    }


def _interviews_context(project: Project, tab: str | None = None, error: UserError | None = None,
                        posted_roles: dict[str, Any] | None = None,
                        moment_form: dict[str, Any] | None = None) -> dict[str, Any]:
    config = _interview_config(project)
    result = get_result(project, AnalysisResult.KIND_VSR)
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    analysis_stale = is_stale(project, analysis)
    roles = {**config.to_storage(), **(posted_roles or {})}
    context: dict[str, Any] = {
        "roles": _role_form(project, roles),
        "result": result,
        # The moments come from the analysis: an outdated analysis makes them outdated too.
        "stale": result is not None and (is_stale(project, result) or analysis_stale),
        "analysis": analysis,
        "analysis_stale": analysis_stale,
        "analysis_ready": analysis is not None and not analysis_stale,
        "stale_from": _stale_before(project, "interviews"),
        "analysis_settings": params.analysis_summary(_applied_values(analysis) or {}) if analysis else [],
        "error": error,
        "step_names": vsr_text.steps(),
        # The card form the last action came from, reopened with its values and its error.
        "moment_form": moment_form or {},
    }
    if result is not None:
        payload = result.payload
        curation.ensure_synced(project, result)
        curated = curation.curated_interviews(project, result)
        colors = signal_colors(project)
        interviews = [_interview_view(index, interview, payload, curated.get(interview["key"], []), colors)
                      for index, interview in enumerate(payload.get("interviews", []))]
        tabs = [interview["tab"] for interview in interviews]
        context.update(
            interviews=interviews,
            tab=tab if tab in tabs else (tabs[0] if tabs else None),
            result_roles=payload.get("roles", {}),
        )
    return context


def _interviews_page(request: HttpRequest, project: Project, tab: str | None = None,
                     error: UserError | None = None, posted_roles: dict[str, Any] | None = None,
                     moment_form: dict[str, Any] | None = None) -> HttpResponse:
    return _page(request, project, "interviews", "gsr/partials/interviews_main.html",
                 _interviews_context(project, tab, error, posted_roles, moment_form))


def _save_interview_config(project: Project, updates: dict[str, Any]) -> UserError | None:
    try:
        project.interview_config = apply_interview_config(project, updates)
    except ValueError as exc:
        return UserError(str(exc))
    project.save(update_fields=["interview_config", "updated_at"])
    return None


def _interviews_redirect(request: HttpRequest, project: Project, error: UserError | None,
                         anchor: str = "") -> HttpResponse:
    if error:
        messages.error(request, error.message)
    tab = request.POST.get("tab")
    url = reverse("gsr:interviews", args=[project.pk])
    return redirect((f"{url}?tab={tab}" if tab else url) + (f"#{anchor}" if anchor else ""))


def interviews(request: HttpRequest, pk: Any) -> HttpResponse:
    """The Interviews step; POST saves the interview participants and chooses the moments from the Analysis."""
    project = _project(pk)
    tab = request.GET.get("tab") or request.POST.get("tab")
    if request.method != "POST":
        return _interviews_page(request, project, tab)
    posted = _posted_roles(request)
    error = _save_interview_config(project, posted)
    if error is None:
        error = _run(compute_interviews, project, "interviews")
        posted = None
    if not _is_htmx(request):
        return _interviews_redirect(request, project, error)
    return _interviews_page(request, project, tab, error, posted)


# The card actions on an interview's list; "time" and "note" also post their form fields.
MOMENT_STATUS_ACTIONS = {
    "accept": InterviewMoment.STATUS_ACCEPTED,
    "reject": InterviewMoment.STATUS_REJECTED,
    "restore": InterviewMoment.STATUS_PENDING,
}
MOMENT_MOVES = {"up": -1, "down": 1}


def _vsr_result_or_404(project: Project) -> AnalysisResult:
    result = get_result(project, AnalysisResult.KIND_VSR)
    if result is None:
        raise Http404("Choose the moments first.")
    return result


def _moment_times(request: HttpRequest) -> tuple[float, float]:
    return (curation.parse_lesson_time(request.POST.get("start", "")),
            curation.parse_lesson_time(request.POST.get("end", "")))


def _moment_response(request: HttpRequest, project: Project, error: str | None, form: dict[str, Any],
                     anchor: str) -> HttpResponse:
    """The Interviews page after a card action; a failed form comes back open with its values and error."""
    if not _is_htmx(request):
        return _interviews_redirect(request, project, UserError(error) if error else None, anchor)
    return _interviews_page(request, project, request.POST.get("tab"),
                            moment_form={**form, "error": error} if error else None)


@require_POST
def interview_moment(request: HttpRequest, pk: Any, moment_pk: Any) -> HttpResponse:
    """A card action on one moment: accept, reject, restore, move up or down, change the time, note, remove."""
    project = _project(pk)
    result = _vsr_result_or_404(project)
    moment = get_object_or_404(InterviewMoment, pk=moment_pk, project=project)
    action = request.POST.get("action", "")
    form = {"id": str(moment.pk), "action": action, "start": request.POST.get("start", ""),
            "end": request.POST.get("end", ""), "reason": request.POST.get("reason", ""),
            "note": request.POST.get("note", "")}
    anchor = f"moment-{moment.pk}"
    try:
        if action in MOMENT_STATUS_ACTIONS:
            curation.set_status(moment, MOMENT_STATUS_ACTIONS[action])
        elif action in MOMENT_MOVES:
            curation.move(moment, MOMENT_MOVES[action])
        elif action == "time":
            start, end = _moment_times(request)
            curation.change_time(moment, result, start, end, request.POST.get("reason", ""))
        elif action == "note":
            curation.set_note(moment, request.POST.get("note", ""))
        elif action == "remove":
            curation.remove_manual(moment)
            anchor = ""
        else:
            raise Http404("Unknown moment action.")
    except ValueError as exc:
        return _moment_response(request, project, str(exc), form, anchor)
    return _moment_response(request, project, None, form, anchor)


@require_POST
def interview_moment_add(request: HttpRequest, pk: Any) -> HttpResponse:
    """Add a moment to an interview's list by hand; the researcher's reason is its justification."""
    project = _project(pk)
    result = _vsr_result_or_404(project)
    key = request.POST.get("interview", "")
    form = {"id": f"add-{key}", "action": "add", "start": request.POST.get("start", ""),
            "end": request.POST.get("end", ""), "reason": request.POST.get("reason", ""),
            "note": request.POST.get("note", "")}
    try:
        start, end = _moment_times(request)
        moment = curation.add_manual(project, result, key, start, end, request.POST.get("reason", ""),
                                     request.POST.get("note", ""))
    except ValueError as exc:
        return _moment_response(request, project, str(exc), form, "")
    return _moment_response(request, project, None, form, f"moment-{moment.pk}")


@require_POST
def interview_candidate_add(request: HttpRequest, pk: Any) -> HttpResponse:
    """Put a candidate moment from the lesson timeline on the interview's list (or restore it when rejected)."""
    project = _project(pk)
    result = _vsr_result_or_404(project)
    key = request.POST.get("interview", "")
    try:
        step, rank = int(request.POST.get("step", "")), int(request.POST.get("rank", ""))
    except ValueError:
        raise Http404("Unknown candidate.") from None
    form = {"id": f"candidate-{key}", "action": "candidate"}
    try:
        moment = curation.add_candidate(project, result, key, step, rank)
    except ValueError as exc:
        return _moment_response(request, project, str(exc), form, "")
    return _moment_response(request, project, None, form, f"moment-{moment.pk}")


# ─── 5 Export ────────────────────────────────────────────────────────────────

EXPORT_FILES = {
    "clean_signals.csv": "clean",
    "analysis_input_merged.csv": "merged",
    "run_settings_and_results.json": "json",
    **{name: "table" for name in (
        "synchrony_episodes.csv",
        "synchrony_divergent_episodes.csv",
        "synchrony_pairs.csv",
        "synchrony_windows.csv",
        "coarousal_episodes.csv",
        "coarousal_state_timeline.csv",
        "coarousal_state_percentages.csv",
    )},
    "vsr_moments.csv": "vsr",
}


def _analysis_result_or_404(project: Project) -> AnalysisResult:
    result = get_result(project, AnalysisResult.KIND_ANALYSIS)
    if result is None:
        raise Http404("Run the analysis before exporting.")
    return result


def _download_name(project: Project, name: str) -> str:
    return f"{slugify(project.name) or 'project'}_{name}"


def _attachment(content: bytes, filename: str, content_type: str) -> HttpResponse:
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _stale_causes(project: Project, result: AnalysisResult) -> list[str]:
    """What changed since the exported run: settings as "label old → new", then added, removed or renamed signals."""
    try:
        causes = params.changed_settings(PipelineConfig.from_dict(result.config).model_dump(), _current_values(project))
    except ValueError:
        causes = []
    before = dict(zip(result.dataset_ids, (row["alias"] for row in result.payload.get("datasets", []))))
    now = {str(dataset.id): dataset.alias for dataset in included_datasets(project)}
    added = [alias for key, alias in now.items() if key not in before]
    removed = [alias for key, alias in before.items() if key not in now]
    if added:
        causes.append(_("Signals added: %(aliases)s") % {"aliases": ", ".join(added)})
    if removed:
        causes.append(_("Signals removed: %(aliases)s") % {"aliases": ", ".join(removed)})
    causes += [_("Signal renamed: %(old)s → %(new)s") % {"old": alias, "new": now[key]}
               for key, alias in before.items() if key in now and now[key] != alias]
    return causes


def _export_file(project: Project, name: str, label: str = "") -> dict[str, str]:
    return {"name": _download_name(project, name), "label": label,
            "url": reverse("gsr:export_file", args=[project.pk, name])}


def _export_groups(project: Project, chart_count: int, vsr: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The Data card's rows: each with its download name(s); grouped CSVs share one menu."""
    def row(title: str, description: str, kind: str, files: list[dict[str, str]],
            stale: bool = False) -> dict[str, Any]:
        return {"title": title, "description": description, "kind": kind, "files": files, "stale": stale}

    vsr_rows = [row(_("VSR interview moments"),
                    _("per interview: step, lesson and sensor clock times, correlation, justification"), "CSV",
                    [_export_file(project, "vsr_moments.csv")], vsr["stale"])] if vsr is not None else []
    return [
        row(_("Cleaned signals"), _("per participant, sensor clock and lesson time, full resolution"), "CSV",
            [_export_file(project, "clean_signals.csv")]),
        row(_("Analysis-ready merged file"),
            _("z-scores, downsampled, sensor clock and lesson time") if chart_scale(project) != "minmax"
            else _("min-max (0–1), downsampled, sensor clock and lesson time"), "CSV",
            [_export_file(project, "analysis_input_merged.csv")]),
        row(_("Synchronization"), _("episodes, divergent episodes, pairs, windows"), "CSV×4", [
            _export_file(project, "synchrony_episodes.csv", _("episodes")),
            _export_file(project, "synchrony_divergent_episodes.csv", _("divergent episodes")),
            _export_file(project, "synchrony_pairs.csv", _("pairs")),
            _export_file(project, "synchrony_windows.csv", _("windows")),
        ]),
        row(_("Co-arousal"), _("episodes, state timeline, time in states"), "CSV×3", [
            _export_file(project, "coarousal_episodes.csv", _("episodes")),
            _export_file(project, "coarousal_state_timeline.csv", _("state timeline")),
            _export_file(project, "coarousal_state_percentages.csv", _("time in states")),
        ]),
        *vsr_rows,
        row(_("Run settings and results"), _("for reproducibility: settings, library versions, file SHA-256"),
            "JSON", [_export_file(project, "run_settings_and_results.json")]),
        row(_("Charts"), ngettext("%(count)d report chart, 2× resolution, white background, in the ZIP",
                                  "%(count)d report charts, 2× resolution, white background, in the ZIP",
                                  chart_count) % {"count": chart_count}, "PNG", []),
    ]


def _report_contents(result: AnalysisResult, chart_count: int, has_vsr: bool) -> list[str]:
    """The PDF report's sections as the report card lists them (the report skips analyses that did not run)."""
    payload = result.payload
    contents = [_("Summary page"), _("Import"), _("Cleaning")]
    if payload.get("synchrony"):
        contents.append(_("Synchronization"))
    if payload.get("coarousal"):
        contents.append(_("Co-arousal"))
    if has_vsr:
        contents.append(_("VSR interview moments"))
    contents += [_("Methodology"), _("Reproducibility"),
                 ngettext("%(count)d chart", "%(count)d charts", chart_count) % {"count": chart_count}]
    return contents


def _report_languages(project: Project, result: AnalysisResult) -> list[dict[str, Any]]:
    """Per report language: its name, PDF file name, page-1 preview texts and chart labels (charts are captured
    in the browser, so an English report needs English chart labels whatever the UI language)."""
    languages = []
    for code, name in settings.LANGUAGES:
        with translation.override(code):
            labels = {**charts.chart_labels(), "locale": code}
        languages.append({
            "code": code,
            "name": name,
            "file_name": _download_name(project, f"report_{code}.pdf"),
            "title": report_text(code, "report_title"),
            "meta": [
                (report_text(code, "computed_at"), timezone.localtime(result.computed_at).strftime("%Y-%m-%d %H:%M")),
                (report_text(code, "signals"),
                 ", ".join(row["alias"] for row in result.payload.get("datasets", [])) or "-"),
            ],
            "chart_labels": labels,
        })
    return languages


def export(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    return _page(request, project, "export", "gsr/partials/export_main.html", _export_context(project))


def _export_context(project: Project, error: UserError | None = None) -> dict[str, Any]:
    result = get_result(project, AnalysisResult.KIND_ANALYSIS)
    stale = export_is_stale(project)
    context: dict[str, Any] = {"result": result, "stale": stale, "error": error}
    if result is not None:
        values = PipelineConfig.from_dict(result.config).model_dump()
        report_charts = charts.report_charts(result.payload, signal_colors(project), values, chart_scale(project))
        languages = _report_languages(project, result)
        vsr = vsr_report_data(project)
        context.update(
            report_charts=report_charts,
            report_contents=_report_contents(result, len(report_charts), vsr is not None),
            report_languages=languages,
            report_lang=resolve_lang(get_language()),
            report_chart_labels={lang["code"]: lang["chart_labels"] for lang in languages},
            export_groups=_export_groups(project, len(report_charts), vsr),
            zip_name=_download_name(project, "results.zip"),
            computed_time=_computed_time(result),
            stale_causes=_stale_causes(project, result) if stale else [],
            stale_steps=[step for step in _steps(project, "export") if step["stale"] and step["key"] != "export"],
        )
    return context


@require_POST
def export_rerun(request: HttpRequest, pk: Any) -> HttpResponse:
    """The outdated-run banner's "Refresh outdated steps": rerun each outdated step in order."""
    project = _project(pk)
    error = None
    for kind, compute in ((AnalysisResult.KIND_CLEANING, compute_cleaning),
                          (AnalysisResult.KIND_ANALYSIS, compute_analysis),
                          (AnalysisResult.KIND_VSR, compute_interviews)):
        if error is None and is_stale(project, get_result(project, kind)):
            error = _run(compute, project, "export")
    if not _is_htmx(request):
        if error:
            messages.error(request, error.message)
        return redirect("gsr:export", project.pk)
    return _page(request, project, "export", "gsr/partials/export_main.html", _export_context(project, error))


def _results_json(project: Project, result: AnalysisResult) -> bytes:
    document = exports.results_document(project, result)
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def export_file(request: HttpRequest, pk: Any, name: str) -> HttpResponse:
    project = _project(pk)
    kind = EXPORT_FILES.get(name)
    if kind is None:
        raise Http404("Unknown export")
    result = _analysis_result_or_404(project)
    try:
        if kind == "clean":
            content = exports.clean_signals_csv(project, result)
        elif kind == "merged":
            content = exports.merged_csv(project, result)
        elif kind == "json":
            content = _results_json(project, result)
        elif kind == "vsr":
            vsr = vsr_report_data(project)
            if vsr is None:
                raise Http404("Choose the interview moments before exporting them.")
            content = exports.vsr_moments_csv(vsr)
        else:
            content = exports.table_exports(result)[name]
    except AnalysisError as exc:
        messages.error(request, describe(exc, project, "export").message)
        return redirect("gsr:export", project.pk)
    content_type = "application/json" if name.endswith(".json") else "text/csv; charset=utf-8"
    return _attachment(content, _download_name(project, name), content_type)


def export_zip(request: HttpRequest, pk: Any) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    project = _project(pk)
    result = _analysis_result_or_404(project)
    chart_images = exports.parse_chart_images(request.POST) if request.method == "POST" else {}
    try:
        content = exports.build_zip(project, result, chart_images, _results_json(project, result),
                                    vsr_report_data(project))
    except AnalysisError as exc:
        messages.error(request, describe(exc, project, "export").message)
        return redirect("gsr:export", project.pk)
    return _attachment(content, _download_name(project, "results.zip"), "application/zip")


def report_payload(project: Project, result: AnalysisResult, chart_images: dict[str, bytes],
                   lang: str) -> dict[str, Any]:
    payload = result.payload
    return {
        "lang": lang,
        "project_name": project.name,
        "project_description": project.description,
        "generated_at": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
        "computed_at": timezone.localtime(result.computed_at).strftime("%Y-%m-%d %H:%M"),
        "stale": export_is_stale(project),
        "dataset_aliases": [row["alias"] for row in payload.get("datasets", [])],
        "datasets": payload.get("datasets", []),
        "config": PipelineConfig.from_dict(result.config).model_dump(),
        "chart_scale": chart_scale(project),
        "preview": payload.get("preview", {}),
        "synchrony": payload.get("synchrony"),
        "coarousal": payload.get("coarousal"),
        "vsr": vsr_report_data(project),
        "versions": exports.versions(),
        "chart_images": chart_images,
    }


def export_pdf(request: HttpRequest, pk: Any) -> HttpResponse:
    """The PDF report in the chosen report language (the UI language by default); charts come from the Export
    screen's capture."""
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    project = _project(pk)
    result = _analysis_result_or_404(project)
    chart_images = exports.parse_chart_images(request.POST) if request.method == "POST" else {}
    lang = resolve_lang(request.POST.get("lang") or request.GET.get("lang") or get_language())
    content = render_report_pdf(report_payload(project, result, chart_images, lang))
    return _attachment(content, _download_name(project, f"report_{lang[:2]}.pdf"), "application/pdf")
