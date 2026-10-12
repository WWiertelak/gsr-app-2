"""UI screens: Projects → Upload → Cleaning → Analysis (Synchronization | Co-arousal) → Export.

Server-rendered pages; HTMX posts swap the main area of the current screen
(``HX-Request`` requests get the partial template). Every form also works as a
plain POST that redirects back to its page.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import charts, params
from .models import AnalysisResult, Dataset, Project
from .pipeline.config import PipelineConfig
from .reporting.pdf import render_report_pdf
from .services import exports
from .services.analysis import AnalysisError
from .services.datasets import add_dataset, normalize_alias
from .services.results import (
    compute_analysis,
    compute_cleaning,
    get_result,
    included_datasets,
    is_stale,
    recording_overview,
    signal_colors,
)

STEPS = (
    ("upload", "gsr:upload"),
    ("cleaning", "gsr:cleaning"),
    ("analysis", "gsr:analysis"),
    ("export", "gsr:export"),
)
ANALYSIS_TABS = ("synchronization", "coarousal")


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _project(pk: Any) -> Project:
    return get_object_or_404(Project, pk=pk)


def _current_values(project: Project) -> dict[str, Any]:
    try:
        return PipelineConfig.from_dict(project.pipeline_config).model_dump()
    except ValueError:
        return PipelineConfig().model_dump()


def _steps(project: Project, current: str) -> list[dict[str, Any]]:
    labels = {"upload": _("Upload"), "cleaning": _("Cleaning"), "analysis": _("Analysis"), "export": _("Export")}
    cleaning = get_result(project, AnalysisResult.KIND_CLEANING)
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    done = {
        "upload": project.datasets.exists(),
        "cleaning": cleaning is not None,
        "analysis": analysis is not None,
        "export": False,
    }
    stale = {"cleaning": is_stale(project, cleaning), "analysis": is_stale(project, analysis)}
    return [
        {
            "key": key,
            "number": index + 1,
            "label": labels[key],
            "url": reverse(url_name, args=[project.pk]),
            "current": key == current,
            "done": done[key],
            "stale": stale.get(key, False),
        }
        for index, (key, url_name) in enumerate(STEPS)
    ]


def _page(request: HttpRequest, project: Project, step: str, partial: str, context: dict[str, Any]) -> HttpResponse:
    """A project screen: the full page, or only its main area (``partial``) for HTMX requests."""
    context = {"project": project, "step": step, "steps": _steps(project, step), "partial": partial,
               "htmx": _is_htmx(request), **context}
    return render(request, partial if _is_htmx(request) else "gsr/project_page.html", context)


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
    return {"datasets": [{"dataset": dataset, "color": colors[dataset.alias]} for dataset in datasets]}


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
    context = {
        "project": project,
        "overview": overview,
        "raw_series": raw_series,
        "raw_overlay": charts.overlay(raw_series, axis="time") if len(raw_series) > 1 else None,
    }
    return render(request, "gsr/partials/upload_overview.html", context)


@require_POST
def dataset_rename(request: HttpRequest, pk: Any, dataset_pk: Any) -> HttpResponse:
    project = _project(pk)
    dataset = get_object_or_404(Dataset, pk=dataset_pk, project=project)
    try:
        alias = normalize_alias(request.POST.get("alias") or "")
        if project.datasets.exclude(pk=dataset.pk).filter(alias=alias).exists():
            raise ValidationError(_("Alias already exists in project: %(alias)s") % {"alias": alias})
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        dataset.alias = alias
        dataset.save(update_fields=["alias"])
        messages.success(request, _("Signal renamed to %(alias)s.") % {"alias": alias})
    return redirect("gsr:upload", project.pk)


@require_POST
def dataset_delete(request: HttpRequest, pk: Any, dataset_pk: Any) -> HttpResponse:
    project = _project(pk)
    dataset = get_object_or_404(Dataset, pk=dataset_pk, project=project)
    alias = dataset.alias
    dataset.delete()
    messages.success(request, _("Signal %(alias)s deleted.") % {"alias": alias})
    return redirect("gsr:upload", project.pk)


# ─── 2 Cleaning ──────────────────────────────────────────────────────────────


def _cleaning_context(project: Project, error: str | None = None) -> dict[str, Any]:
    values = _current_values(project)
    result = get_result(project, AnalysisResult.KIND_CLEANING)
    colors = signal_colors(project)
    groups = [
        {
            "group": group,
            "toggle": params.field_rows((group.toggle,), values)[0] if group.toggle else None,
            "rows": params.field_rows(group.params, values),
        }
        for group in params.CLEANING_GROUPS
    ]
    context: dict[str, Any] = {
        "groups": groups,
        "datasets": [{"dataset": dataset, "color": colors[dataset.alias]}
                     for dataset in project.datasets.order_by("created_at")],
        "result": result,
        "stale": is_stale(project, result),
        "error": error,
        "included_count": len(included_datasets(project)),
    }
    if result is not None:
        preview = result.payload["preview"]
        result_values = PipelineConfig.from_dict(result.config).model_dump()
        context["charts"] = charts.cleaning_charts(preview, colors, result_values)
        context["summary"] = preview["cleaning_summary"]
        context["alignment"] = preview["alignment"]
        context["preview_range"] = preview["range"]
        context["quality_rows"] = [
            {**row, "color": colors.get(row["alias"], "#005f73")} for row in preview["cleaning_summary"]["per_alias"]
        ]
    return context


def cleaning(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    error = None
    if request.method == "POST":
        error = _save_cleaning(request, project)
        if error is None:
            error = _run(compute_cleaning, project)
        if not _is_htmx(request):
            if error:
                messages.error(request, error)
            return redirect("gsr:cleaning", project.pk)
    return _page(request, project, "cleaning", "gsr/partials/cleaning_main.html",
                 _cleaning_context(project, error))


def _save_cleaning(request: HttpRequest, project: Project) -> str | None:
    selected = set(request.POST.getlist("datasets"))
    updates = params.parse_params(params.cleaning_params(), request.POST)
    try:
        settings = params.apply_settings(project.pipeline_config, updates)
    except ValueError as exc:
        return str(exc)
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
    error = _run(compute_cleaning, project)
    if not _is_htmx(request):
        if error:
            messages.error(request, error)
        return redirect("gsr:cleaning", project.pk)
    return _page(request, project, "cleaning", "gsr/partials/cleaning_main.html",
                 _cleaning_context(project, error))


@require_POST
def cleaning_refresh(request: HttpRequest, pk: Any) -> HttpResponse:
    """Compute the cleaning preview with the saved settings (first visit, or after uploads changed)."""
    project = _project(pk)
    error = _run(compute_cleaning, project)
    if not _is_htmx(request):
        if error:
            messages.error(request, error)
        return redirect("gsr:cleaning", project.pk)
    return _page(request, project, "cleaning", "gsr/partials/cleaning_main.html",
                 _cleaning_context(project, error))


def cleaning_csv(request: HttpRequest, pk: Any) -> HttpResponse:
    """Full-resolution cleaned signals of the last cleaning preview."""
    project = _project(pk)
    result = get_result(project, AnalysisResult.KIND_CLEANING)
    if result is None:
        raise Http404("Compute the cleaning preview first.")
    try:
        content = exports.clean_signals_csv(project, result)
    except AnalysisError as exc:
        messages.error(request, str(exc))
        return redirect("gsr:cleaning", project.pk)
    return _attachment(content, _download_name(project, "clean_signals.csv"), "text/csv; charset=utf-8")


def _run(compute: Any, project: Project) -> str | None:
    try:
        compute(project)
    except AnalysisError as exc:
        return str(exc)
    return None


# ─── 3 Analysis ──────────────────────────────────────────────────────────────


def _analysis_context(project: Project, tab: str, error: str | None = None) -> dict[str, Any]:
    values = _current_values(project)
    result = get_result(project, AnalysisResult.KIND_ANALYSIS)
    colors = signal_colors(project)
    context: dict[str, Any] = {
        "tab": tab,
        "tabs": ANALYSIS_TABS,
        "sync_essential": params.field_rows(params.SYNCHRONY_ESSENTIAL, values),
        "sync_advanced": params.field_rows(params.SYNCHRONY_ADVANCED, values),
        "coarousal_essential": params.field_rows(params.COAROUSAL_ESSENTIAL, values),
        "coarousal_advanced": params.field_rows(params.COAROUSAL_ADVANCED, values),
        "result": result,
        "stale": is_stale(project, result),
        "error": error,
        "included_count": len(included_datasets(project)),
        "colors": colors,
    }
    if result is not None:
        payload = result.payload
        synchrony = payload.get("synchrony", {})
        coarousal = payload.get("coarousal", {})
        context.update(
            synchrony=synchrony,
            coarousal=coarousal,
            sync_timeline=charts.sync_timeline(synchrony),
            sync_signals=charts.sync_signals(synchrony, payload.get("preview", {}), colors),
            sync_matrix=charts.sync_matrix(synchrony),
            coarousal_states=charts.coarousal_states(coarousal),
            sync_episodes=[
                {**episode, "number": index + 1,
                 "duration": episode["end_relative"] - episode["start_relative"]}
                for index, episode in enumerate(synchrony.get("episodes", []))
            ],
            coarousal_episodes=[
                {**episode, "number": index + 1} for index, episode in enumerate(coarousal.get("episodes", []))
            ],
            has_pair_p=any("p_value" in pair for pair in synchrony.get("pairs", [])),
            coarousal_height=max(180, len(coarousal.get("aliases", [])) * 60 + 100),
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
            error = str(exc)
        else:
            project.save(update_fields=["pipeline_config", "updated_at"])
            error = _run(compute_analysis, project)
        if not _is_htmx(request):
            if error:
                messages.error(request, error)
            return redirect(f"{reverse('gsr:analysis', args=[project.pk])}?tab={tab}")
    return _page(request, project, "analysis", "gsr/partials/analysis_main.html",
                 _analysis_context(project, tab, error))


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


# ─── 4 Export ────────────────────────────────────────────────────────────────

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


def export(request: HttpRequest, pk: Any) -> HttpResponse:
    project = _project(pk)
    result = get_result(project, AnalysisResult.KIND_ANALYSIS)
    context: dict[str, Any] = {"result": result, "stale": is_stale(project, result)}
    if result is not None:
        values = PipelineConfig.from_dict(result.config).model_dump()
        context["report_charts"] = charts.report_charts(result.payload, signal_colors(project), values)
    return _page(request, project, "export", "gsr/partials/export_main.html", context)


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
        messages.error(request, str(exc))
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
        messages.error(request, str(exc))
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
    """The PDF report in the current UI language; charts come from the Export screen's capture."""
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    project = _project(pk)
    result = _analysis_result_or_404(project)
    chart_images = exports.parse_chart_images(request.POST) if request.method == "POST" else {}
    lang = get_language() or "pl"
    content = render_report_pdf(report_payload(project, result, chart_images, lang))
    return _attachment(content, _download_name(project, f"report_{lang[:2]}.pdf"), "application/pdf")
