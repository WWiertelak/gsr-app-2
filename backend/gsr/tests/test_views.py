"""Screens, exports and the language switch, driven through the Django test client."""

from __future__ import annotations

import base64
import csv
import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import translation
from PIL import Image

from gsr import params
from gsr.models import AnalysisResult, Dataset, Project
from gsr.pipeline.config import PipelineConfig
from gsr.pipeline.preprocessing import build_stage_export_frame, run_pipeline, serialize_preview
from gsr.services.analysis import analyze_files
from gsr.services.datasets import add_dataset

from .sample_data import Participant, RecordingFormat, write_group

FAST = {"synchrony_surrogate_count": 20, "contagion_surrogate_count": 20}
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture()
def recordings(tmp_path: Path) -> dict[str, Path]:
    return write_group(
        tmp_path,
        "session",
        {
            "A": Participant(tonic_us=4.0),
            "B": Participant(tonic_us=6.0, lag_seconds=1.0),
            "C": Participant(tonic_us=5.0, coupling=0.6, own_events=2),
        },
        {
            "B": RecordingFormat(start_time="12:00:02:000", polish=True, artifacts={200: 45.0}),
            "C": RecordingFormat(start_time="11:59:58:000"),
        },
        seconds=240.0,
        seed=5,
    )


def _upload(path: Path) -> SimpleUploadedFile:
    return SimpleUploadedFile(path.name, path.read_bytes(), content_type="text/csv")


@pytest.fixture()
def project(recordings) -> Project:
    project = Project.objects.create(name="Sesja łódzka", description="Zajęcia grupowe", pipeline_config=dict(FAST))
    for alias, path in recordings.items():
        add_dataset(project, _upload(path), alias=alias)
    return project


def _analysis_form(project: Project, **overrides) -> dict[str, str]:
    """The Analysis screen's form as the browser sends it (checked boxes only)."""
    values = {**PipelineConfig.from_dict(project.pipeline_config).model_dump(), **overrides}
    data: dict[str, str] = {}
    for param in params.ANALYSIS_PARAMS:
        value = values[param.name]
        if param.kind == "bool":
            if value:
                data[param.name] = "1"
        else:
            data[param.name] = str(value)
    return data


def _cleaning_form(project: Project, **overrides) -> dict:
    values = {**PipelineConfig.from_dict(project.pipeline_config).model_dump(), **overrides}
    data: dict = {"datasets": [str(dataset.pk) for dataset in project.datasets.filter(included=True)]}
    for param in params.cleaning_params():
        value = values[param.name]
        if param.kind == "bool":
            if value:
                data[param.name] = "1"
        else:
            data[param.name] = "" if value is None else str(value)
    return data


def _png_data_url(color: str = "red") -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), color).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _run_analysis(client, project: Project):
    response = client.post(reverse("gsr:analysis", args=[project.pk]), _analysis_form(project), **HTMX)
    assert response.status_code == 200
    return response


# ─── Projects ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_projects_screen_creates_and_lists_projects(client) -> None:
    response = client.post(reverse("gsr:projects"), {"name": "  Sesja 1 ", "description": "Opis"})
    project = Project.objects.get()
    assert response.status_code == 302 and response["Location"] == reverse("gsr:upload", args=[project.pk])
    assert project.name == "Sesja 1"

    page = client.get(reverse("gsr:projects")).content.decode()
    assert "Sesja 1" in page and reverse("gsr:project", args=[project.pk]) in page

    response = client.post(reverse("gsr:projects"), {"name": " "})
    assert response.status_code == 200
    assert Project.objects.count() == 1


@pytest.mark.django_db
def test_project_update_and_delete_remove_files(client, project) -> None:
    client.post(reverse("gsr:project_update", args=[project.pk]), {"name": "Nowa", "description": "Inny opis"})
    project.refresh_from_db()
    assert (project.name, project.description) == ("Nowa", "Inny opis")

    stored = [Path(dataset.file.path) for dataset in project.datasets.all()]
    response = client.post(reverse("gsr:project_delete", args=[project.pk]))
    assert response.status_code == 302
    assert not Project.objects.exists() and not Dataset.objects.exists()
    assert not any(path.exists() for path in stored)


# ─── 1 Upload ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_upload_screen_stores_files_and_shows_overlap(client, recordings) -> None:
    project = Project.objects.create(name="P")
    response = client.post(
        reverse("gsr:upload", args=[project.pk]), {"files": [_upload(path) for path in recordings.values()]}
    )
    assert response.status_code == 302
    assert list(project.datasets.values_list("alias", flat=True)) == ["session", "session_B", "session_C"]
    assert project.datasets.first().parsed_metadata["recorded_start"].startswith("1900-01-01T12:00:00")

    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    assert "session_B" in page and reverse("gsr:upload_overview", args=[project.pk]) in page

    overview = client.get(reverse("gsr:upload_overview", args=[project.pk])).content.decode()
    assert 'data-chart="rawSignal"' in overview and 'data-chart="overlay"' in overview
    # The shared window starts when the last sensor started (B, 12:00:02) and ends with the first to stop.
    assert "12:00:02" in overview and "alert success" in overview


@pytest.mark.django_db
def test_upload_rejects_unreadable_file_and_keeps_others(client, recordings) -> None:
    project = Project.objects.create(name="P")
    bad = SimpleUploadedFile("broken.csv", b"not;an;esense;file\n1;2;3;4\n", content_type="text/csv")
    response = client.post(
        reverse("gsr:upload", args=[project.pk]), {"files": [bad, _upload(recordings["A"])]}, follow=True
    )
    page = response.content.decode()
    assert "broken.csv:" in page and 'class="alert error"' in page
    # Translated reason (default language: Polish); no pipeline text, no stored file name.
    assert "To nie jest eksport CSV z eSense" in page
    assert "Cannot detect data header" not in page and "_broken.csv" not in page
    assert list(project.datasets.values_list("alias", flat=True)) == ["session"]


@pytest.fixture()
def late_project(tmp_path) -> Project:
    """A and B record together from 12:00:00; C starts at 13:00:00, so the three share no window."""
    files = write_group(
        tmp_path,
        "late",
        {"A": Participant(tonic_us=4.0), "B": Participant(tonic_us=5.0), "C": Participant(tonic_us=6.0)},
        {"C": RecordingFormat(start_time="13:00:00:000")},
        seconds=60.0,
        seed=1,
    )
    project = Project.objects.create(name="P", pipeline_config=dict(FAST))
    for alias, path in files.items():
        add_dataset(project, _upload(path), alias=alias)
    return project


@pytest.mark.django_db
def test_upload_overview_flags_recordings_without_shared_window(client, late_project) -> None:
    project = late_project
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    overview = client.get(reverse("gsr:upload_overview", args=[project.pk])).content.decode()
    assert "alert error" in overview and "alert success" not in overview
    assert "C does not overlap with the other recordings (their common window is 12:00:00–12:00:59)." in overview
    exclude = reverse("gsr:dataset_exclude", args=[project.pk, project.datasets.get(alias="C").pk])
    assert exclude in overview

    # Without a shared window the next step is not offered.
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    next_link = f'<a class="button" href="{reverse("gsr:cleaning", args=[project.pk])}">'
    assert 'aria-disabled="true"' in page and next_link not in page
    assert "Exclude or delete the recording that does not overlap." in page

    # Excluding C (the error's action) restores the window and the next step.
    response = client.post(exclude, {"next": "upload"})
    assert response.status_code == 302 and response["Location"] == reverse("gsr:upload", args=[project.pk])
    assert not project.datasets.get(alias="C").included
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    assert 'aria-disabled="true"' not in page and next_link in page
    overview = client.get(reverse("gsr:upload_overview", args=[project.pk])).content.decode()
    assert "alert success" in overview and "excluded" in overview


@pytest.mark.django_db
def test_cleaning_error_names_the_participant_and_offers_exclusion(client, late_project) -> None:
    project = late_project
    fragment = client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX).content.decode()
    # Polish by default: the participant, HH:MM:SS clock times, no pipeline text or ISO timestamps.
    assert "Nagrania nie mają wspólnego okna czasu" in fragment
    assert "C nie nakłada się z pozostałymi nagraniami (ich wspólne okno to 12:00:00–12:00:59)." in fragment
    assert "<li>C: 13:00:00–13:00:59</li>" in fragment
    assert "absolute-time overlap" not in fragment and "1900-01-01" not in fragment
    assert "Wyklucz C" in fragment and reverse("gsr:upload", args=[project.pk]) in fragment
    assert not AnalysisResult.objects.exists()

    exclude = reverse("gsr:dataset_exclude", args=[project.pk, project.datasets.get(alias="C").pk])
    fragment = client.post(exclude, {"next": "cleaning"}, **HTMX).content.decode()
    assert "alert error" not in fragment and fragment.count('data-chart="cleanVsRaw"') == 2
    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_CLEANING)
    assert stored.payload["preview"]["aliases"] == ["A", "B"]


def test_overlap_outliers_names_recordings_outside_the_largest_shared_window() -> None:
    from gsr.errors import overlap_outliers

    ranges = {"A": (0, 10), "B": (2, 12), "C": (20, 30), "D": (1, 9)}
    assert overlap_outliers(ranges) == (["C"], (2, 9))
    # Two recordings that do not overlap: neither is the culprit.
    assert overlap_outliers({"A": (0, 10), "B": (20, 30)}) == ([], None)
    # Two equal groups: no single culprit either.
    assert overlap_outliers({"A": (0, 10), "B": (1, 11), "C": (20, 30), "D": (21, 31)}) == ([], None)


@pytest.mark.django_db
def test_dataset_rename_and_delete(client, project) -> None:
    dataset = project.datasets.get(alias="A")
    client.post(reverse("gsr:dataset_rename", args=[project.pk, dataset.pk]), {"alias": " Anna "})
    dataset.refresh_from_db()
    assert dataset.alias == "Anna"

    response = client.post(reverse("gsr:dataset_rename", args=[project.pk, dataset.pk]), {"alias": "B"}, follow=True)
    dataset.refresh_from_db()
    assert dataset.alias == "Anna" and "alert error" in response.content.decode()

    path = Path(dataset.file.path)
    client.post(reverse("gsr:dataset_delete", args=[project.pk, dataset.pk]))
    assert not project.datasets.filter(pk=dataset.pk).exists() and not path.exists()


# ─── 2 Cleaning ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_cleaning_screen_computes_preview_on_first_visit(client, project) -> None:
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert reverse("gsr:cleaning_refresh", args=[project.pk]) in page and 'hx-trigger="load"' in page
    assert not AnalysisResult.objects.exists()

    response = client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX)
    fragment = response.content.decode()
    assert response.status_code == 200 and "<html" not in fragment
    assert fragment.count('data-chart="cleanVsRaw"') == 3 and 'data-chart="overlay"' in fragment
    assert 'hx-swap-oob="true"' in fragment  # stepper refreshed with the new "done" state

    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_CLEANING)
    assert [row["alias"] for row in stored.payload["datasets"]] == ["A", "B", "C"]
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert 'hx-trigger="load"' not in page and "badge stale" not in page


@pytest.mark.django_db
def test_cleaning_preview_matches_pipeline_and_marks_rejected_samples(client, project) -> None:
    client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX)
    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_CLEANING)

    config = PipelineConfig.from_dict({**project.pipeline_config, "scaling_enabled": True})
    files = {dataset.alias: Path(dataset.file.path) for dataset in project.datasets.all()}
    expected = serialize_preview(run_pipeline(files, config), 3000)
    preview = stored.payload["preview"]
    assert preview["cleaning_summary"] == expected["cleaning_summary"]
    assert preview["alignment"] == expected["alignment"]
    for entry, expected_entry in zip(preview["datasets"], expected["datasets"]):
        assert entry["clean"] == expected_entry["clean"]
    # B carries one injected 45 µS artifact: the rejected overlay counts the same samples as the summary.
    for entry, summary in zip(preview["datasets"], preview["cleaning_summary"]["per_alias"]):
        assert len(entry["rejected"]) == summary["artifact_rejected_points"]
    assert any(value > 40 for _, value in preview["datasets"][1]["rejected"])


@pytest.mark.django_db
def test_cleaning_apply_saves_settings_and_signal_selection(client, project) -> None:
    form = _cleaning_form(project, time_range_from=0.5, time_range_to=3.0, clean_filter_zscore_threshold=4)
    form["datasets"] = [str(project.datasets.get(alias=alias).pk) for alias in ("A", "C")]
    form["clean_filter_jump_sigma"] = "4,5"  # Polish decimal comma
    form["clean_filter_iqr"] = "1"
    del form["clean_filter_jump"]
    response = client.post(reverse("gsr:cleaning", args=[project.pk]), form, **HTMX)
    assert response.status_code == 200 and "alert error" not in response.content.decode()

    project.refresh_from_db()
    assert project.pipeline_config["time_range_from"] == 0.5
    assert project.pipeline_config["clean_filter_jump_sigma"] == 4.5
    assert project.pipeline_config["clean_filter_zscore_threshold"] == 4.0
    assert project.pipeline_config["clean_filter_iqr"] is True and project.pipeline_config["clean_filter_jump"] is False
    assert project.pipeline_config["synchrony_surrogate_count"] == 20  # untouched analysis settings
    assert not project.datasets.get(alias="B").included

    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_CLEANING)
    assert stored.payload["preview"]["aliases"] == ["A", "C"]
    assert all(row["outside_time_window_points"] > 0 for row in stored.payload["preview"]["cleaning_summary"]["per_alias"])


@pytest.mark.django_db
def test_cleaning_apply_rejects_invalid_settings(client, project) -> None:
    before = dict(project.pipeline_config)
    form = _cleaning_form(project, clean_filter_physiological_min_us=30, clean_filter_physiological_max_us=20)
    response = client.post(reverse("gsr:cleaning", args=[project.pk]), form, **HTMX)
    assert "alert error" in response.content.decode()
    project.refresh_from_db()
    assert project.pipeline_config == before
    assert not AnalysisResult.objects.exists()


@pytest.mark.django_db
def test_cleaning_reset_restores_default_cleaning_settings(client, project) -> None:
    project.pipeline_config = {**project.pipeline_config, "median_kernel": 11, "time_range_to": 2.0}
    project.save()
    client.post(reverse("gsr:cleaning_reset", args=[project.pk]), **HTMX)
    project.refresh_from_db()
    assert "median_kernel" not in project.pipeline_config and "time_range_to" not in project.pipeline_config
    assert project.pipeline_config["synchrony_surrogate_count"] == 20


@pytest.mark.django_db
def test_cleaning_without_datasets_points_to_upload(client) -> None:
    project = Project.objects.create(name="Empty")
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert reverse("gsr:upload", args=[project.pk]) in page and 'hx-trigger="load"' not in page


def _settings_form(page: str) -> str:
    start = page.index('<form class="card sidebar"')
    return page[start:page.index("</form>", start)]


@pytest.mark.django_db
def test_cleaning_settings_are_collapsed_with_a_summary_and_tooltips(client, project) -> None:
    project.pipeline_config = {**project.pipeline_config, "time_range_to": 3.0, "clean_filter_iqr": True}
    project.save()
    project.datasets.filter(alias="C").update(included=False)
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    form = _settings_form(page)
    assert '<details class="settings-panel">' in form  # closed by default
    assert "Signals: 2/3 · Window: …–3 min · Filters: 5/5 · Median: 5 · 2 changed" in form
    # Explanations moved from inline hints into keyboard-reachable tooltips.
    assert 'class="hint"' not in form
    assert 'aria-describedby="tip-group-zscore"' in form and 'role="tooltip" id="tip-group-zscore"' in form
    assert "Rejects points that are statistical outliers against the full signal." in form
    assert "Applying settings recomputes the preview" in form
    # Every setting is still in the form, so Apply posts the same fields as before.
    for param in params.cleaning_params():
        assert f'name="{param.name}"' in form


@pytest.mark.django_db
def test_cleaning_settings_panel_stays_open_on_errors(client, project) -> None:
    form = _cleaning_form(project, clean_filter_physiological_min_us=30, clean_filter_physiological_max_us=20)
    fragment = client.post(reverse("gsr:cleaning", args=[project.pk]), form, **HTMX).content.decode()
    assert '<details class="settings-panel" open>' in fragment

    fragment = client.post(reverse("gsr:cleaning", args=[project.pk]), _cleaning_form(project), **HTMX).content.decode()
    assert '<details class="settings-panel">' in fragment and "alert error" not in fragment


# ─── 3 Analysis ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_analysis_run_stores_both_results_in_two_tabs(client, project) -> None:
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert 'data-tab-panel="coarousal" hidden' in page and 'data-chart="syncTimeline"' not in page

    fragment = _run_analysis(client, project).content.decode()
    for kind in ("syncTimeline", "episodeSignals", "heatmap", "coarousalStates"):
        assert f'data-chart="{kind}"' in fragment
    assert 'hx-swap-oob="true"' in fragment and "badge fresh" in fragment

    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_ANALYSIS)
    assert set(stored.payload) == {"preview", "synchrony", "coarousal", "datasets"}
    assert stored.config["scaling_enabled"] is True

    page = client.get(reverse("gsr:analysis", args=[project.pk]) + "?tab=coarousal").content.decode()
    assert 'data-tab-panel="synchronization" hidden' in page and 'data-tab-panel="coarousal">' in page


@pytest.mark.django_db
def test_analysis_screen_has_landmarks_aria_tabs_and_labelled_charts(client, project) -> None:
    _run_analysis(client, project)
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert page.count("<main") == 1 and "<h4" not in page
    assert '<h2 class="sr-only" id="analysis-results-title">Analysis results</h2>' in page
    # Tabs: each tab controls its panel; only the selected one is in the tab order.
    assert ('id="tab-synchronization" aria-controls="panel-synchronization"' in page
            and 'aria-selected="true" tabindex="0">Synchronization' in page
            and 'aria-selected="false" tabindex="-1">Co-arousal' in page)
    assert 'role="tabpanel" id="panel-coarousal" aria-labelledby="tab-coarousal"' in page
    # Charts are canvases, so each one carries a text alternative.
    assert 'data-chart="heatmap"' in page and 'role="img" aria-label="Synchronization matrix"' in page
    # The stepper states each step in text, not only by colour.
    assert '<span class="sr-only">(done)</span>' in page and 'class="step-status"' not in page

    project.save()
    project.datasets.filter(alias="C").update(included=False)
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert '<span class="step-status">outdated</span>' in page

    client.post(reverse("set_language"), {"language": "pl", "next": "/"})
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert '<span class="sr-only">(gotowe)</span>' in page and '<span class="step-status">nieaktualne</span>' in page
    assert 'role="img" aria-label="Macierz synchronizacji"' in page


@pytest.mark.django_db
def test_analysis_settings_are_collapsed_with_run_and_apply_buttons(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    form = _settings_form(page)
    assert '<details class="settings-panel">' in form
    assert "Threshold: 0.6 · Window: 30 s · High/Low z: 1/-1 · Min. participants: 2 · 2 changed" in form
    assert 'class="hint"' not in form and 'role="tooltip" id="tip-contagion_surrogate_count"' in form
    assert ">Apply settings</button>" in form
    for param in params.ANALYSIS_PARAMS:
        assert f'name="{param.name}"' in form
    # Without a result the run bar offers the first run; it submits the (collapsed) settings form.
    assert 'form="analysis-settings">Run analysis</button>' in page

    fragment = _run_analysis(client, project).content.decode()
    assert 'form="analysis-settings"' not in fragment  # fresh result: nothing to recompute
    project.pipeline_config = {**project.pipeline_config, "synchrony_threshold": 0.7}
    project.save()
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert "badge stale" in page and 'form="analysis-settings"' in page

    form = _analysis_form(project, contagion_min_participants=5)
    fragment = client.post(reverse("gsr:analysis", args=[project.pk]), form, **HTMX).content.decode()
    assert '<details class="settings-panel" open>' in fragment


@pytest.mark.django_db
def test_polish_screens_use_polish_numbers_and_labels(client, project) -> None:
    project.pipeline_config = {**project.pipeline_config, "time_range_to": 2.5, "synchrony_threshold": 0.75}
    project.save()
    form = _settings_form(client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode())
    assert "Okno: …–2,5 min" in form
    form = _settings_form(client.get(reverse("gsr:analysis", args=[project.pk])).content.decode())
    assert "Próg: 0,75" in form and "Próg wysoki/niski z: 1/-1" in form
    assert "zmienione (domyślnie 0,6)" in form
    # The input fields keep dot decimals: that is what <input type="number"> accepts.
    assert 'name="synchrony_threshold" step="any" value="0.75"' in form
    fragment = _run_analysis(client, project).content.decode()
    assert "przesunięcie kołowe · N=20" in fragment and ">circular" not in fragment


@pytest.mark.django_db
def test_upload_step_is_called_import_in_both_languages(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    assert "1. Import" in page and "Upload</a>" not in page


@pytest.mark.django_db
def test_analysis_results_equal_the_analysis_service(client, project) -> None:
    _run_analysis(client, project)
    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_ANALYSIS)
    files = {dataset.alias: Path(dataset.file.path) for dataset in project.datasets.order_by("created_at")}
    expected = analyze_files(files, PipelineConfig.from_dict(stored.config))
    assert json.loads(json.dumps(expected["synchrony"])) == stored.payload["synchrony"]
    assert json.loads(json.dumps(expected["coarousal"])) == stored.payload["coarousal"]
    assert expected["preview"]["cleaning_summary"] == stored.payload["preview"]["cleaning_summary"]


@pytest.mark.django_db
def test_analysis_settings_are_saved_and_result_goes_stale(client, project) -> None:
    form = _analysis_form(project, synchrony_threshold=0.5)
    del form["synchrony_dtw_enabled"]
    client.post(reverse("gsr:analysis", args=[project.pk]), form, **HTMX)
    project.refresh_from_db()
    assert project.pipeline_config["synchrony_threshold"] == 0.5
    assert project.pipeline_config["synchrony_dtw_enabled"] is False
    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_ANALYSIS)
    assert all(pair["dtw_distance"] is None for pair in stored.payload["synchrony"]["pairs"])

    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert "badge stale" not in page

    # A cleaning change makes the analysis stale, but an analysis change does not touch the cleaning preview.
    client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX)
    project.pipeline_config = {**project.pipeline_config, "median_kernel": 7}
    project.save()
    assert "badge stale" in client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    project.pipeline_config = {**project.pipeline_config, "median_kernel": 5, "synchrony_threshold": 0.7}
    project.save()
    assert "badge stale" not in client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert "badge stale" in client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()


@pytest.mark.django_db
def test_analysis_errors_are_shown_and_nothing_is_stored(client, project) -> None:
    form = _analysis_form(project, contagion_min_participants=5)
    response = client.post(reverse("gsr:analysis", args=[project.pk]), form, **HTMX)
    assert "contagion_min_participants cannot exceed the selected dataset count" in response.content.decode()
    assert not AnalysisResult.objects.exists()

    project.datasets.exclude(alias="A").update(included=False)
    response = client.post(reverse("gsr:analysis", args=[project.pk]), _analysis_form(project), **HTMX)
    fragment = response.content.decode()
    assert "Analiza grupowa wymaga co najmniej dwóch sygnałów." in fragment
    assert "at least two distinct datasets" not in fragment
    assert (
        reverse("gsr:cleaning", args=[project.pk]) in fragment and reverse("gsr:upload", args=[project.pk]) in fragment
    )


@pytest.mark.django_db
def test_analysis_reset_keeps_cleaning_settings(client, project) -> None:
    project.pipeline_config = {**project.pipeline_config, "median_kernel": 9}
    project.save()
    client.post(reverse("gsr:analysis_reset", args=[project.pk]), {"tab": "coarousal"})
    project.refresh_from_db()
    assert project.pipeline_config == {"median_kernel": 9}


# ─── 4 Export ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_export_needs_an_analysis_run(client, project) -> None:
    page = client.get(reverse("gsr:export", args=[project.pk])).content.decode()
    assert reverse("gsr:analysis", args=[project.pk]) in page and "data-capture-charts" not in page
    assert client.get(reverse("gsr:export_pdf", args=[project.pk])).status_code == 404
    assert client.get(reverse("gsr:export_file", args=[project.pk, "synchrony_pairs.csv"])).status_code == 404


@pytest.mark.django_db
def test_export_screen_offers_report_charts_for_capture(client, project) -> None:
    _run_analysis(client, project)
    page = client.get(reverse("gsr:export", args=[project.pk])).content.decode()
    for key in ("raw_overlay", "clean_overlay", "sync_timeline", "sync_signals", "sync_matrix", "coarousal_states"):
        assert f'data-key="{key}"' in page and f'name="chart_{key}"' in page
    assert page.count("data-capture-charts") == 2


def _csv_rows(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


@pytest.mark.django_db
def test_export_csv_files(client, project) -> None:
    _run_analysis(client, project)
    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_ANALYSIS)

    def download(name: str):
        response = client.get(reverse("gsr:export_file", args=[project.pk, name]))
        assert response.status_code == 200, name
        assert response["Content-Type"].startswith("text/csv")
        assert f'filename="sesja-odzka_{name}"' in response["Content-Disposition"]
        return response.content

    files = {dataset.alias: Path(dataset.file.path) for dataset in project.datasets.order_by("created_at")}
    config = PipelineConfig.from_dict(stored.config)
    expected_clean = build_stage_export_frame(run_pipeline(files, config), "clean", config)
    clean = pd.read_csv(io.BytesIO(download("clean_signals.csv")))
    assert list(clean.columns) == list(expected_clean.columns) and len(clean) == len(expected_clean)

    merged = _csv_rows(download("analysis_input_merged.csv"))
    assert set(merged[0]) == {"relative_time_minutes", "marker", "microsiemens_A", "microsiemens_B", "microsiemens_C"}

    pairs = _csv_rows(download("synchrony_pairs.csv"))
    assert [(row["alias_a"], row["alias_b"]) for row in pairs] == [("A", "B"), ("A", "C"), ("B", "C")]
    episodes = _csv_rows(download("synchrony_episodes.csv"))
    assert len(episodes) == len(stored.payload["synchrony"]["episodes"])
    windows = _csv_rows(download("synchrony_windows.csv"))
    assert len(windows) == len(stored.payload["synchrony"]["windows"]["centers"]) and "A__B" in windows[0]

    assert len(_csv_rows(download("coarousal_episodes.csv"))) == len(stored.payload["coarousal"]["episodes"])
    states = _csv_rows(download("coarousal_state_timeline.csv"))
    assert set(states[0]) == {"minute", "A", "B", "C"}
    percentages = _csv_rows(download("coarousal_state_percentages.csv"))
    assert [row["alias"] for row in percentages] == ["A", "B", "C"]
    assert client.get(reverse("gsr:export_file", args=[project.pk, "coarousal_primary_timeline.csv"])).status_code == 404

    assert client.get(reverse("gsr:export_file", args=[project.pk, "secrets.csv"])).status_code == 404

    cleaning_csv = client.get(reverse("gsr:cleaning_csv", args=[project.pk]))
    assert cleaning_csv.status_code == 404  # no cleaning preview computed yet
    client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX)
    assert client.get(reverse("gsr:cleaning_csv", args=[project.pk])).status_code == 200


@pytest.mark.django_db
def test_export_json_records_settings_versions_and_inputs(client, project) -> None:
    _run_analysis(client, project)
    response = client.get(reverse("gsr:export_file", args=[project.pk, "run_settings_and_results.json"]))
    document = json.loads(response.content)
    assert document["project"]["name"] == "Sesja łódzka"
    assert document["config"]["synchrony_surrogate_count"] == 20 and document["config"]["scaling_enabled"] is True
    assert {"python", "numpy", "pandas", "scipy", "weasyprint"} <= set(document["versions"])
    hashes = {row["alias"]: row["sha256"] for row in document["datasets"]}
    assert hashes == {dataset.alias: dataset.file_sha256 for dataset in project.datasets.all()}
    assert document["synchrony"]["stats"] and document["coarousal"]["stats"]


@pytest.mark.django_db
def test_exports_keep_the_aliases_of_the_last_run_after_a_rename(client, project) -> None:
    _run_analysis(client, project)
    dataset = project.datasets.get(alias="A")
    client.post(reverse("gsr:dataset_rename", args=[project.pk, dataset.pk]), {"alias": "Anna"})

    clean = _csv_rows(client.get(reverse("gsr:export_file", args=[project.pk, "clean_signals.csv"])).content)
    assert "microsiemens_A" in clean[0] and not any("Anna" in column for column in clean[0])
    document = json.loads(client.get(reverse("gsr:export_file", args=[project.pk, "run_settings_and_results.json"])).content)
    sizes = {row["alias"]: row["file_size"] for row in document["datasets"]}
    assert sizes["A"] == dataset.file_size


@pytest.mark.django_db
def test_export_zip_bundles_tables_json_and_posted_chart_pngs(client, project) -> None:
    _run_analysis(client, project)
    response = client.post(
        reverse("gsr:export_zip", args=[project.pk]),
        {
            "chart_sync_timeline": _png_data_url(),
            "chart_sync_matrix": "data:image/png;base64,bm90IGEgcG5n",  # "not a png"
            "chart_unknown": _png_data_url(),
        },
    )
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {
        "clean_signals.csv", "analysis_input_merged.csv", "synchrony_episodes.csv", "synchrony_pairs.csv",
        "synchrony_windows.csv", "coarousal_episodes.csv", "coarousal_state_timeline.csv",
        "coarousal_state_percentages.csv", "run_settings_and_results.json",
        "charts/sync_timeline.png",
    } == names
    assert archive.read("charts/sync_timeline.png").startswith(b"\x89PNG")


@pytest.mark.django_db
def test_export_pdf_follows_ui_language_and_embeds_charts(client, project) -> None:
    _run_analysis(client, project)
    url = reverse("gsr:export_pdf", args=[project.pk])

    without_charts = client.get(url)
    assert without_charts.status_code == 200 and without_charts["Content-Type"] == "application/pdf"
    assert without_charts.content.startswith(b"%PDF") and "report_pl.pdf" in without_charts["Content-Disposition"]

    with_charts = client.post(url, {"chart_sync_timeline": _png_data_url(), "chart_coarousal_states": _png_data_url()})
    assert with_charts.content.startswith(b"%PDF") and len(with_charts.content) > len(without_charts.content)

    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    english = client.get(url)
    assert "report_en.pdf" in english["Content-Disposition"]


# ─── Language switch ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_language_switch_translates_screens_and_chart_labels(client, project) -> None:
    page = client.get(reverse("gsr:projects")).content.decode()
    assert '<html lang="pl">' in page and "Nowy projekt" in page and "Liczba zsynchronizowanych" in page

    response = client.post(reverse("set_language"), {"language": "en", "next": reverse("gsr:cleaning", args=[project.pk])})
    assert response.status_code == 302 and response["Location"] == reverse("gsr:cleaning", args=[project.pk])
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert '<html lang="en">' in page and "Cleaning controls" in page and "Synchronized count" in page

    client.post(reverse("set_language"), {"language": "pl", "next": "/"})
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert "Panel czyszczenia" in page and "Zakres fizjologiczny" in page
    assert "Sygnały: 3/3" in page and "Ustawienia" in page and 'aria-label="Więcej informacji"' in page


def test_polish_catalog_is_compiled() -> None:
    with translation.override("pl"):
        assert translation.gettext("Run analysis") == "Uruchom analizę"
        assert translation.ngettext("%(counter)s Excel fix", "%(counter)s Excel fixes", 3) % {"counter": 3} == (
            "3 poprawki Excela"
        )


def test_chart_labels_translate_clip_marker() -> None:
    from gsr.charts import chart_labels

    with translation.override("pl"):
        assert chart_labels()["clipped"] == "przycięty (poza skalą)"
    with translation.override("en"):
        assert chart_labels()["clipped"] == "clipped (off scale)"


# ─── Settings form parsing ───────────────────────────────────────────────────


def test_parse_params_reads_checkboxes_decimal_commas_and_blanks() -> None:
    from django.http import QueryDict

    data = QueryDict(mutable=True)
    data.update({"time_range_from": "1,5", "time_range_to": "", "median_kernel": "", "clean_filter_zscore": "1"})
    values = params.parse_params(params.cleaning_params(), data)
    assert values["time_range_from"] == "1.5" and values["time_range_to"] is None
    assert values["median_kernel"] == 5  # blank = default
    assert values["clean_filter_zscore"] is True and values["clean_filter_iqr"] is False

    stored = params.apply_settings({"synchrony_threshold": 0.4}, values)
    assert stored["time_range_from"] == 1.5 and stored["synchrony_threshold"] == 0.4
    with pytest.raises(ValueError):
        params.apply_settings({}, {"clean_filter_zscore_threshold": "50"})
