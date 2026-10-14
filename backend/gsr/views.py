"""UI screens: Projects → Upload → Cleaning → Analysis (Synchronization | Co-arousal) → Interviews → Export.

Server-rendered pages; HTMX posts swap the main area of the current screen
(``HX-Request`` requests get the partial template). Every form also works as a
plain POST that redirects back to its page.
"""

from __future__ import annotations

import json
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
from .models import AnalysisResult, Dataset, Project
from .pipeline.config import PipelineConfig
from .pipeline.vsr import (
    PRIMARY,
    QUOTE_ASYNCHRONOUS,
    QUOTE_FIVE_MOMENTS,
    QUOTE_GSR_DISRUPTION,
    QUOTE_RESERVE,
    QUOTE_SYNCHRONOUS,
)
from .pipeline.vsr import STEPS as VSR_STEPS
from .pipeline.vsr_config import VsrConfig
from .reporting.report import render_report_pdf, report_text, resolve_lang
from .services import exports
from .services.analysis import AnalysisError
from .services.datasets import add_dataset, normalize_alias
from .services.interview import (
    InterviewConfig,
    InterviewRoles,
    apply_interview_config,
    forget_interview_alias,
    load_interview_config,
    rename_interview_alias,
    vsr_changed_label,
    vsr_field_rows,
)
from .services.results import (
    FALLBACK_SIGNAL_COLOR,
    compute_analysis,
    compute_cleaning,
    get_result,
    included_datasets,
    is_stale,
    recording_overview,
    signal_colors,
)
from .services.vsr import compute_interviews
from .vsr_params import VSR_PARAMS, vsr_summary

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
        return PipelineConfig().model_dump()


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
    stale = {"cleaning": is_stale(project, cleaning), "analysis": is_stale(project, analysis),
             "interviews": is_stale(project, interviews)}
    stale["export"] = stale["analysis"]
    status = {
        "upload": _recordings_label(recordings) if recordings else _("no recordings"),
        "cleaning": _cleaning_status(cleaning),
        "analysis": _("computed %(time)s") % {"time": _computed_time(analysis)} if analysis else _("not run"),
        "interviews": _interviews_status(interviews) if interviews else (
            _("not proposed") if analysis else _("needs analysis")),
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
        # Interview proposals use the analysis run's signals: rerun the analysis, then propose again on the screen.
        error = _run(compute_analysis, project, step)
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
        "settings_summary": params.analysis_summary(values, _applied_values(result)),
        "result": result,
        "stale": is_stale(project, result),
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
        coarousal_chart = charts.coarousal_states(coarousal)
        sync_episodes = [
            {**episode, "number": index + 1,
             "duration": episode["end_relative"] - episode["start_relative"],
             "bar": _mini_bar(episode["start_relative"], episode["end_relative"], span)}
            for index, episode in enumerate(synchrony.get("episodes", []))
        ]
        context.update(
            synchrony=synchrony,
            coarousal=coarousal,
            sync_signals=charts.sync_signals(synchrony, payload.get("preview", {}), colors),
            sync_matrix=charts.sync_matrix(synchrony),
            coarousal_states=coarousal_chart,
            sync_episodes=sync_episodes,
            sync_episode_minutes=sum(episode["duration"] for episode in sync_episodes),
            sync_episode_rule=_("subgroups of ≥ %(size)s people above %(threshold)s") % {
                "size": config.synchrony_episode_min_size,
                "threshold": params.display_number(config.synchrony_threshold)},
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
def analysis_reset(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    names = {param.name for param in params.ANALYSIS_PARAMS}
    project.pipeline_config = {key: value for key, value in (project.pipeline_config or {}).items()
                               if key not in names}
    project.save(update_fields=["pipeline_config", "updated_at"])
    tab = request.POST.get("tab") if request.POST.get("tab") in ANALYSIS_TABS else ANALYSIS_TABS[0]
    if not _is_htmx(request):
        return redirect(f"{reverse('gsr:analysis', args=[project.pk])}?tab={tab}")
    return _page(request, project, "analysis", "gsr/partials/analysis_main.html",
                 _analysis_context(project, tab))


# ─── 4 Interviews ────────────────────────────────────────────────────────────

# The page-1 criteria the Interviews screen states above the proposals.
VSR_CRITERIA = (QUOTE_FIVE_MOMENTS, QUOTE_RESERVE, QUOTE_SYNCHRONOUS, QUOTE_ASYNCHRONOUS)
MIN_PAIR_ROWS = 3  # the document's three student dyads (page 1: "6 students, 3 dyads")
TICK_STEPS_MINUTES = (1, 2, 5, 10, 15, 20, 30, 60)


def _interview_config(project: Project) -> InterviewConfig:
    """The stored roles and VSR parameters; defaults when the stored value no longer validates."""
    try:
        return load_interview_config(project)
    except ValueError:
        return InterviewConfig(roles=InterviewRoles(None, (), None), vsr=VsrConfig())


def _role_form(project: Project, roles: dict[str, Any]) -> dict[str, Any]:
    """The interview participants card: teacher, student pair rows (plus empty rows to add pairs), video start."""
    colors = signal_colors(project)
    pairs = [list(pair) for pair in roles.get("student_pairs") or []]
    pairs += [["", ""]] * max(MIN_PAIR_ROWS - len(pairs), 1)
    return {
        "recordings": [{"alias": dataset.alias, "color": colors[dataset.alias], "included": dataset.included}
                       for dataset in project.datasets.order_by("created_at")],
        "teacher": roles.get("teacher_alias") or "",
        "pairs": [{"number": index + 1, "left": left, "right": right} for index, (left, right) in enumerate(pairs)],
        "video_start_time": roles.get("video_start_time") or "",
    }


def _posted_roles(request: HttpRequest) -> dict[str, Any]:
    pairs = [[left.strip(), right.strip()]
             for left, right in zip(request.POST.getlist("pair_left"), request.POST.getlist("pair_right"))
             if left.strip() or right.strip()]
    return {
        "teacher_alias": (request.POST.get("teacher_alias") or "").strip() or None,
        "student_pairs": pairs,
        "video_start_time": request.POST.get("video_start_time") or "",
    }


def _interview_label(interview: dict[str, Any]) -> str:
    if interview["kind"] == "teacher":
        return _("Teacher %(alias)s") % {"alias": interview["participants"][0]}
    return _("Pair %(left)s + %(right)s") % {"left": interview["participants"][0], "right": interview["participants"][1]}


def _lesson_ticks(start: float, end: float, max_ticks: int = 8) -> list[dict[str, Any]]:
    """Whole-minute ticks of the lesson timeline (relative seconds), as % positions."""
    span = end - start
    if span <= 0:
        return []
    step = next((step * 60 for step in TICK_STEPS_MINUTES if span / (step * 60) <= max_ticks), 3600)
    first = -(-start // step) * step
    ticks = []
    seconds = first
    while seconds <= end:
        ticks.append({"seconds": seconds, "pct": f"{100.0 * (seconds - start) / span:.2f}"})
        seconds += step
    return ticks


def _moment_view(moment: dict[str, Any], start: float, span: float) -> dict[str, Any]:
    left = min(max((moment["start_seconds"] - start) / span, 0.0), 1.0) * 100 if span > 0 else 0.0
    width = min(max(moment["duration_seconds"] / span * 100, 0.6), 100 - left) if span > 0 else 100.0
    return {
        **moment,
        "primary": moment["status"] == PRIMARY,
        "step_type": vsr_text.quote(moment["synchrony_type"]),
        "sentences": vsr_text.justification(moment["justification"]),
        "r": vsr_text.correlation(moment["correlation"]),
        "shared_pct": vsr_text.percent(moment["shared_state_fraction"]) if moment["shared_state"] else None,
        "bar": {"left": f"{left:.2f}", "width": f"{width:.2f}"},
    }


def _interview_view(index: int, interview: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    lesson = payload.get("lesson") or {"start_seconds": 0.0, "end_seconds": 0.0}
    start, end = lesson["start_seconds"], lesson["end_seconds"]
    quality = payload.get("quality", {})
    first_pass = interview["passes"][0]["steps"] if interview["passes"] else []
    return {
        "tab": f"interview-{index + 1}",
        "kind": interview["kind"],
        "label": _interview_label(interview),
        "participants": interview["participants"],
        "data_basis": vsr_text.quote(interview["data_basis"]),
        "basis_notes": [vsr_text.quote(note) for note in interview["basis_notes"]],
        "steps": [{"number": number, "type": vsr_text.quote(step.synchrony_type.as_dict()),
                   "count": interview["candidate_counts"].get(str(number), 0)} for number, step in VSR_STEPS.items()],
        "empty_steps": [vsr_text.step_sentence(record["fragments"][0]) for record in first_pass
                        if record["answer"] == "NO"],
        "passes": [{"number": record["pass_number"], "reserve": record["purpose"] != PRIMARY,
                    "steps": [vsr_text.step_sentence(step["fragments"][0]) for step in record["steps"]],
                    "step_6": vsr_text.step_6_sentence(record["step_6"])} for record in interview["passes"]],
        "moments": [_moment_view(moment, start, end - start) for moment in interview["moments"]],
        "stats": interview["stats"],
        "ticks": _lesson_ticks(start, end),
        "lesson_start": start,
        "lesson_end": end,
        "poor_signals": [{"alias": alias, "pct": number_format(quality[alias]["rejected_pct"], 0)}
                         for alias in interview["participants"]
                         if quality.get(alias, {}).get("level") == "poor"],
    }


def _interviews_context(project: Project, tab: str | None = None, error: UserError | None = None,
                        posted_roles: dict[str, Any] | None = None) -> dict[str, Any]:
    config = _interview_config(project)
    values = config.vsr.model_dump()
    result = get_result(project, AnalysisResult.KIND_VSR)
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    applied = None
    if result is not None:
        try:
            applied = VsrConfig.from_dict(result.config.get("vsr")).model_dump()
        except ValueError:
            applied = None
    roles = posted_roles if posted_roles is not None else {
        "teacher_alias": config.roles.teacher_alias,
        "student_pairs": [list(pair) for pair in config.roles.student_pairs],
        "video_start_time": config.roles.video_start_time,
    }
    context: dict[str, Any] = {
        "drawer": "gsr/partials/interviews_settings.html",
        "form_id": "interviews-settings",
        "form_action": reverse("gsr:interviews_settings", args=[project.pk]),
        "reset_action": reverse("gsr:interviews_reset", args=[project.pk]),
        "apply_disabled": analysis is None,
        "changed_label": vsr_changed_label(values),
        "vsr_rows": vsr_field_rows(values),
        "settings_summary": vsr_summary(values, applied),
        "roles": _role_form(project, roles),
        "result": result,
        "stale": is_stale(project, result),
        "analysis": analysis,
        "analysis_stale": is_stale(project, analysis),
        "error": error,
        "colors": signal_colors(project),
        "criteria": [vsr_text.quote(item.as_dict()) for item in VSR_CRITERIA],
        "gsr_disruption_quote": vsr_text.quote(QUOTE_GSR_DISRUPTION.as_dict(), ellipsis=True),
    }
    if result is not None:
        payload = result.payload
        interviews = [_interview_view(index, interview, payload)
                      for index, interview in enumerate(payload.get("interviews", []))]
        tabs = [interview["tab"] for interview in interviews]
        context.update(
            interviews=interviews,
            tab=tab if tab in tabs else (tabs[0] if tabs else None),
            result_roles=payload.get("roles", {}),
        )
    return context


def _interviews_page(request: HttpRequest, project: Project, tab: str | None = None,
                     error: UserError | None = None, posted_roles: dict[str, Any] | None = None) -> HttpResponse:
    return _page(request, project, "interviews", "gsr/partials/interviews_main.html",
                 _interviews_context(project, tab, error, posted_roles))


def _save_interview_config(project: Project, updates: dict[str, Any]) -> UserError | None:
    try:
        project.interview_config = apply_interview_config(project, updates)
    except ValueError as exc:
        return UserError(str(exc))
    project.save(update_fields=["interview_config", "updated_at"])
    return None


def _interviews_redirect(request: HttpRequest, project: Project, error: UserError | None) -> HttpResponse:
    if error:
        messages.error(request, error.message)
    tab = request.POST.get("tab")
    url = reverse("gsr:interviews", args=[project.pk])
    return redirect(f"{url}?tab={tab}" if tab else url)


def interviews(request: HttpRequest, pk: Any) -> HttpResponse:
    """The Interviews step; POST saves the interview participants and proposes the moments."""
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


@require_POST
def interviews_settings(request: HttpRequest, pk: Any) -> HttpResponse:
    """The settings drawer's Apply: save the VSR parameters and propose the moments again."""
    project = _project(pk)
    tab = request.POST.get("tab")
    error = _save_interview_config(project, {"vsr": params.parse_params(VSR_PARAMS, request.POST)})
    if error is None:
        error = _run(compute_interviews, project, "interviews")
    if not _is_htmx(request):
        return _interviews_redirect(request, project, error)
    return _interviews_page(request, project, tab, error)


@require_POST
def interviews_reset(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    project.interview_config = {**(project.interview_config or {}), "vsr": VsrConfig().model_dump()}
    project.save(update_fields=["interview_config", "updated_at"])
    if not _is_htmx(request):
        return _interviews_redirect(request, project, None)
    return _interviews_page(request, project, request.POST.get("tab"))


# ─── 5 Export ────────────────────────────────────────────────────────────────

EXPORT_FILES = {
    "clean_signals.csv": "clean",
    "analysis_input_merged.csv": "merged",
    "run_settings_and_results.json": "json",
    **{name: "table" for name in (
        "synchrony_episodes.csv",
        "synchrony_pairs.csv",
        "synchrony_windows.csv",
        "coarousal_episodes.csv",
        "coarousal_state_timeline.csv",
        "coarousal_state_percentages.csv",
    )},
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


def _export_groups(project: Project, chart_count: int) -> list[dict[str, Any]]:
    """The Data card's rows: each with its download name(s); grouped CSVs share one menu."""
    def row(title: str, description: str, kind: str, files: list[dict[str, str]]) -> dict[str, Any]:
        return {"title": title, "description": description, "kind": kind, "files": files}

    return [
        row(_("Cleaned signals"), _("per participant, relative time, full resolution"), "CSV",
            [_export_file(project, "clean_signals.csv")]),
        row(_("Analysis-ready merged file"), _("scaled, downsampled, shared time axis"), "CSV",
            [_export_file(project, "analysis_input_merged.csv")]),
        row(_("Synchronization"), _("episodes, pairs, windows"), "CSV×3", [
            _export_file(project, "synchrony_episodes.csv", _("episodes")),
            _export_file(project, "synchrony_pairs.csv", _("pairs")),
            _export_file(project, "synchrony_windows.csv", _("windows")),
        ]),
        row(_("Co-arousal"), _("episodes, state timeline, time in states"), "CSV×3", [
            _export_file(project, "coarousal_episodes.csv", _("episodes")),
            _export_file(project, "coarousal_state_timeline.csv", _("state timeline")),
            _export_file(project, "coarousal_state_percentages.csv", _("time in states")),
        ]),
        row(_("Run settings and results"), _("for reproducibility: settings, library versions, file SHA-256"),
            "JSON", [_export_file(project, "run_settings_and_results.json")]),
        row(_("Charts"), ngettext("%(count)d report chart, 2× resolution, white background, in the ZIP",
                                  "%(count)d report charts, 2× resolution, white background, in the ZIP",
                                  chart_count) % {"count": chart_count}, "PNG", []),
    ]


def _report_contents(result: AnalysisResult, chart_count: int) -> list[str]:
    """The PDF report's sections as the report card lists them (the report skips analyses that did not run)."""
    payload = result.payload
    contents = [_("Summary page"), _("Import"), _("Cleaning")]
    if payload.get("synchrony"):
        contents.append(_("Synchronization"))
    if payload.get("coarousal"):
        contents.append(_("Co-arousal"))
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
    stale = is_stale(project, result)
    context: dict[str, Any] = {"result": result, "stale": stale, "error": error}
    if result is not None:
        values = PipelineConfig.from_dict(result.config).model_dump()
        report_charts = charts.report_charts(result.payload, signal_colors(project), values)
        languages = _report_languages(project, result)
        context.update(
            report_charts=report_charts,
            report_contents=_report_contents(result, len(report_charts)),
            report_languages=languages,
            report_lang=resolve_lang(get_language()),
            report_chart_labels={lang["code"]: lang["chart_labels"] for lang in languages},
            export_groups=_export_groups(project, len(report_charts)),
            zip_name=_download_name(project, "results.zip"),
            computed_time=_computed_time(result),
            stale_causes=_stale_causes(project, result) if stale else [],
        )
    return context


@require_POST
def export_rerun(request: HttpRequest, pk: Any) -> HttpResponse:
    """The outdated-run banner's "Run the analysis again": rerun with the current settings and signals."""
    project = _project(pk)
    error = _run(compute_analysis, project, "export")
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
        content = exports.build_zip(project, result, chart_images, _results_json(project, result))
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
        "stale": is_stale(project, result),
        "dataset_aliases": [row["alias"] for row in payload.get("datasets", [])],
        "datasets": payload.get("datasets", []),
        "config": PipelineConfig.from_dict(result.config).model_dump(),
        "preview": payload.get("preview", {}),
        "synchrony": payload.get("synchrony"),
        "coarousal": payload.get("coarousal"),
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
