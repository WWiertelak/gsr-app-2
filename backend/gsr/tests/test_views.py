"""Screens, exports and the language switch, driven through the Django test client."""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import re
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import translation
from PIL import Image

from gsr import charts, params
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
def test_upload_screen_empty_state_offers_a_translated_drop_zone(client) -> None:
    project = Project.objects.create(name="P")
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    # Polish by default: our own picker label, not the browser's ("Choose files" in the EN UI was the bug).
    assert "Upuść tutaj pliki CSV" in page and "Wybierz pliki…" in page and "Nie ma jeszcze nagrań." in page
    assert "data-dropzone" in page and 'for="upload-files"' in page
    assert "upload-overview" not in page and "action-bar" not in page


@pytest.mark.django_db
def test_rail_keeps_text_names_when_it_collapses_to_icons_or_a_top_bar(client, project) -> None:
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    # Narrow screens hide the brand name and the project name visually; both stay in the markup for screen readers,
    # and the collapsed project switcher shows the project's initial instead.
    assert '<span class="brand-name">GSR Analyzer</span>' in page
    assert '<span class="initial" aria-hidden="true">S</span><span class="name">Sesja łódzka</span>' in page
    # The top bar orders the rail's parts by these hooks.
    assert '<nav class="rail-steps-nav" aria-labelledby="rail-steps-title">' in page and '<nav class="rail-app"' in page


@pytest.mark.django_db
def test_upload_screen_lists_recordings_in_one_table_with_inline_alias(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    assert page.count('<table class="data recordings">') == 1 and page.count("<table") == 1
    # Clock times as HH:MM:SS (not the CSV's 12:00:02:000), duration and grouped sample count.
    assert "<td>12:00:02</td>" in page and ":000<" not in page
    assert "4.0 min" in page and "<td>1,200</td>" in page
    # No Save button per row: the alias saves on Enter or blur.
    dataset = project.datasets.get(alias="B")
    rename = reverse("gsr:dataset_rename", args=[project.pk, dataset.pk])
    assert f'hx-post="{rename}" hx-trigger="submit, change"' in page and ">Save</button>" not in page.split("<details")[0]

    overview = client.get(reverse("gsr:upload_overview", args=[project.pk])).content.decode()
    # The timeline has a clock axis and the shaded shared window; no second start/end table.
    assert ">12:02</span>" in overview and "11:59:58–12:03:57" in overview
    assert 'class="tl-window"' in overview and "<table" not in overview
    assert 'data-view="overlay"' in overview and 'data-view-panel="overlay" hidden' in overview


@pytest.mark.django_db
def test_inline_alias_save_returns_the_row(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    dataset = project.datasets.get(alias="A")
    rename = reverse("gsr:dataset_rename", args=[project.pk, dataset.pk])
    response = client.post(rename, {"alias": " Anna "}, **HTMX)
    row = response.content.decode()
    assert response.status_code == 200 and row.lstrip().startswith("<tr") and "<html" not in row
    assert 'value="Anna"' in row and "✓ Saved" in row
    assert response["HX-Trigger"] == "gsr:datasets-changed"  # the timeline and charts reload with the new alias
    # htmx restores focus across the outerHTML swap by id, so the row's controls keep the ids the page rendered.
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    for control_id in (f'id="alias-{dataset.pk}"', f'id="delete-{dataset.pk}"'):
        assert page.count(control_id) == 1 and control_id in row
    dataset.refresh_from_db()
    assert dataset.alias == "Anna"

    response = client.post(rename, {"alias": "B"}, **HTMX)
    row = response.content.decode()
    assert 'value="B"' in row and 'aria-invalid="true"' in row and "Alias already exists in project: B" in row
    assert "HX-Trigger" not in response
    dataset.refresh_from_db()
    assert dataset.alias == "Anna"


@pytest.mark.django_db
def test_clearing_an_alias_restores_it_with_an_inline_error(client, project) -> None:
    dataset = project.datasets.get(alias="A")
    rename = reverse("gsr:dataset_rename", args=[project.pk, dataset.pk])
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    # No browser-side `required`: leaving a cleared field must reach the server instead of silently doing nothing.
    alias_input = page.split(f'id="alias-{dataset.pk}"')[1].split(">")[0]
    assert "required" not in alias_input

    for language, message in (("en", "Alias cannot be empty"), ("pl", "Alias nie może być pusty")):
        client.post(reverse("set_language"), {"language": language, "next": "/"})
        response = client.post(rename, {"alias": "  "}, **HTMX)
        row = response.content.decode()
        assert 'value="A"' in row and 'aria-invalid="true"' in row and message in row
        assert "HX-Trigger" not in response
    dataset.refresh_from_db()
    assert dataset.alias == "A"


def test_clock_ticks_use_round_clock_times() -> None:
    from gsr.services.results import clock_ticks

    ticks = clock_ticks(pd.Timestamp("1900-01-01T10:00:00"), pd.Timestamp("1900-01-01T10:12:24"))
    assert [tick["label"] for tick in ticks] == ["10:00", "10:02", "10:04", "10:06", "10:08", "10:10", "10:12"]
    assert ticks[0]["pct"] == 0.0 and ticks[-1]["pct"] == round(100 * 720 / 744, 2)
    short = clock_ticks(pd.Timestamp("1900-01-01T12:00:00.5"), pd.Timestamp("1900-01-01T12:00:59"))
    assert short[0]["label"] == "12:00:10" and all(0 <= tick["pct"] <= 100 for tick in short)
    assert clock_ticks(pd.Timestamp("1900-01-01"), pd.Timestamp("1900-01-01")) == []


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
    next_link = f'<a class="button lg" href="{reverse("gsr:cleaning", args=[project.pk])}">'
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
def test_inline_alias_save_refreshes_the_action_bar(client, late_project) -> None:
    project = late_project
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    assert 'id="upload-action-bar"' in page and "C does not overlap" in page
    rename = reverse("gsr:dataset_rename", args=[project.pk, project.datasets.get(alias="C").pk])
    fragment = client.post(rename, {"alias": "Carl"}, **HTMX).content.decode()
    bar = fragment[fragment.index('<div class="action-bar" id="upload-action-bar" hx-swap-oob="true">'):]
    assert "Carl does not overlap" in bar and 'aria-disabled="true"' in bar and "C does not overlap" not in bar

    # Without the outlier the bar offers the next step with the common window.
    client.post(reverse("gsr:dataset_exclude", args=[project.pk, project.datasets.get(alias="Carl").pk]), {"next": "upload"})
    fragment = client.post(rename, {"alias": "Cleo"}, **HTMX).content.decode()
    bar = fragment[fragment.index('id="upload-action-bar"'):]
    assert "3 recordings · common window" in bar and reverse("gsr:cleaning", args=[project.pk]) in bar


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
    assert 'hx-trigger="load"' not in page and "run-status stale" not in page


@pytest.mark.django_db
def test_action_bar_counts_match_the_rail_status(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX)
    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_CLEANING)
    stored.payload["preview"]["cleaning_summary"]["totals"]["artifact_rejected_points"] = 12345
    AnalysisResult.objects.filter(pk=stored.pk).update(payload=stored.payload)
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert '<small class="step-status">12,345 rejected samples</small>' in page
    assert '<span class="muted">12,345 rejected samples</span>' in page

    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    assert '<small class="step-status">3 recordings</small>' in page
    assert '<span class="muted">3 recordings · common window 3.9 min</span>' in page


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
    """The settings drawer's form: in #drawer-root on a full page, in the out-of-band block of an HTMX fragment."""
    start = page.index('<aside class="drawer" id="settings-drawer"')
    return page[start:page.index("</aside>", start)]


def _run_bar(page: str) -> str:
    start = page.index('<div class="run-bar"')
    return page[start:page.index("</div>", start)]


@pytest.mark.django_db
def test_cleaning_settings_live_in_a_drawer_with_groups_switches_and_changed_markers(client, project) -> None:
    project.pipeline_config = {**project.pipeline_config, "time_range_to": 3.0, "clean_filter_iqr": True,
                               "clean_filter_zscore_threshold": 4}
    project.save()
    project.datasets.filter(alias="C").update(included=False)
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    # The drawer replaces the old settings column: it mounts in #drawer-root, outside the HTMX swap target.
    assert "layout-sidebar" not in page and "settings-panel" not in page
    assert page.index("</main>") < page.index('<div id="drawer-root">') < page.index('<aside class="drawer"')
    form = _settings_form(page)
    assert "data-autoopen" not in form  # closed until Edit settings opens it
    # The run bar lists the values; its Edit settings button opens the drawer (an #anchor without JS).
    run_bar = _run_bar(page)
    assert ("<li>Signals: 2/3</li><li>Window: …–3 min</li><li>Filters: 5/5</li><li>Median: 5</li><li>3 changed</li>"
            in run_bar)
    assert ('href="#settings-drawer" data-open-settings aria-controls="settings-drawer" aria-expanded="false">'
            "Edit settings</a>") in run_bar
    assert "Not run yet" in run_bar
    assert '<h2 id="settings-drawer-title" tabindex="-1">Cleaning settings</h2>' in form
    assert '<span class="badge changed">3 changed</span>' in form
    # Groups: a filter's on/off switch sits in its header; the group explains itself inline, not in a tooltip that
    # would cover the fields.
    assert '<legend class="group-head"><span>Signals</span><span class="grow"></span><span class="group-meta">2/3</span>' in form
    assert 'class="switch" role="switch" id="f-clean_filter_zscore" name="clean_filter_zscore" value="1" checked>' in form
    assert '<label class="switch-row changed" for="f-clean_filter_iqr">' in form  # IQR is off by default
    assert '<p class="group-help">Rejects points that are statistical outliers against the full signal.' in form
    assert "tip-group-" not in form and 'class="hint"' not in form
    # Changed fields carry a marker and their default; untouched ones stay quiet.
    assert '<div class="f changed">' in form and "changed (default 5)" in form
    assert form.count("changed (default") == 1  # time_range_to is a range bound: marked, without a default note
    # Sticky footer: Apply, Reset and the unapplied-changes note.
    assert '<button type="submit" id="cleaning-settings-apply">Apply settings</button>' in form
    assert "Applying settings recomputes the preview" in form and "data-dirty-note hidden" in form
    # Every setting is still in the form, so Apply posts the same fields as before.
    for param in params.cleaning_params():
        assert f'name="{param.name}"' in form


@pytest.mark.django_db
def test_cleaning_drawer_is_refreshed_out_of_band_and_opens_on_errors(client, project) -> None:
    form = _cleaning_form(project, clean_filter_physiological_min_us=30, clean_filter_physiological_max_us=20)
    fragment = client.post(reverse("gsr:cleaning", args=[project.pk]), form, **HTMX).content.decode()
    assert '<div id="drawer-root" hx-swap-oob="innerHTML">' in fragment
    assert '<aside class="drawer" id="settings-drawer" aria-labelledby="settings-drawer-title" data-autoopen>' in fragment

    fragment = client.post(reverse("gsr:cleaning", args=[project.pk]), _cleaning_form(project), **HTMX).content.decode()
    assert '<div id="drawer-root" hx-swap-oob="innerHTML">' in fragment and "alert error" not in fragment
    assert "data-autoopen" not in fragment


@pytest.mark.django_db
def test_outdated_cleaning_preview_is_dimmed_with_a_stale_notice(client, project) -> None:
    client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX)
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert "result is-stale" not in page and 'class="alert warning stale-note"' not in page
    assert page.count('<section class="card result" aria-labelledby=') == 3
    assert 'class="changed"' not in _run_bar(page)

    project.datasets.filter(alias="C").update(included=False)
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert page.count('<section class="card result is-stale" aria-labelledby=') == 3
    assert ('<div class="alert warning stale-note">Settings or signals changed since this preview was computed.'
            ' Recompute it to see the current result.</div>' in page)
    assert page.count('form="cleaning-refresh">Recompute</button>') == 1
    assert 'form="cleaning-refresh">Recompute</button>' in _run_bar(page)

    # The run bar marks what differs from the shown preview, adding a chip for a setting it does not list.
    project.pipeline_config = {**project.pipeline_config, "median_kernel": 7, "clean_filter_zscore_threshold": 4}
    project.save()
    run_bar = _run_bar(client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode())
    marker = '<span class="sr-only"> (changed since the last run)</span></li>'
    assert f'<li class="changed">Signals: 2/3{marker}' in run_bar
    assert "<li>Window: full range</li>" in run_bar and "<li>Filters: " in run_bar
    assert f'<li class="changed">Median: 7{marker}' in run_bar
    assert f'<li class="changed">Z-score deviation · Threshold (SD): 4{marker}' in run_bar
    assert run_bar.count('class="changed"') == 3


# ─── 3 Analysis ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_analysis_run_stores_both_results_in_two_tabs(client, project) -> None:
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert 'data-tab-panel="coarousal" hidden' in page and 'data-chart="syncTimeline"' not in page

    fragment = _run_analysis(client, project).content.decode()
    for kind in ("syncTimeline", "episodeSignals", "heatmap", "coarousalStates"):
        assert f'data-chart="{kind}"' in fragment
    assert 'hx-swap-oob="true"' in fragment and "run-status fresh" in fragment

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
    # The rail states each step's status in text, not only by colour; the work area is headed by the step.
    assert '<aside class="rail"' in page and "<h1>Analysis</h1>" in page
    assert '<small class="step-status">3 recordings</small>' in page and "outdated" not in page
    assert '<small class="step-status">ready</small>' in page
    # The settings drawer mounts outside the HTMX swap target.
    assert page.index('id="main"') < page.index("</main>") < page.index('id="drawer-root"')

    project.save()
    project.datasets.filter(alias="C").update(included=False)
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert '<small class="step-status">outdated</small>' in page

    client.post(reverse("set_language"), {"language": "pl", "next": "/"})
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert '<small class="step-status">3 nagrania</small>' in page
    assert '<small class="step-status">nieaktualne</small>' in page
    assert 'role="img" aria-label="Macierz synchronizacji"' in page


@pytest.mark.django_db
def test_analysis_settings_live_in_the_drawer_with_run_and_apply_buttons(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    form = _settings_form(page)
    assert '<form class="drawer-form" id="analysis-settings"' in form and "Analysis settings</h2>" in form
    assert '<input type="hidden" name="tab" value="synchronization" data-tab-input>' in form
    assert ("<li>Threshold: 0.6</li><li>Window: 30 s</li><li>High/Low z: 1/-1</li><li>Min. participants: 2</li>"
            "<li>2 changed</li>") in _run_bar(page)
    assert 'class="hint"' not in form and 'role="tooltip" id="tip-contagion_surrogate_count"' in form
    assert ">Apply settings</button>" in form
    # Advanced sections holding a changed setting open (both surrogate counts differ here); others stay shut.
    assert '<details class="advanced" id="advanced-synchrony" open>' in form
    assert '<details class="advanced" id="advanced-coarousal" open>' in form
    project.pipeline_config = {}
    project.save()
    form = _settings_form(client.get(reverse("gsr:analysis", args=[project.pk])).content.decode())
    assert '<details class="advanced" id="advanced-synchrony">' in form and 'class="badge changed"' not in form
    project.pipeline_config = dict(FAST, synchrony_dtw_enabled=False)
    project.save()
    form = _settings_form(client.get(reverse("gsr:analysis", args=[project.pk])).content.decode())
    assert '<div class="f bool changed">' in form and '<span class="badge changed">3 changed</span>' in form
    project.pipeline_config = dict(FAST)
    project.save()
    for param in params.ANALYSIS_PARAMS:
        assert f'name="{param.name}"' in form
    # Without a result the empty state offers the first run as a large button; it submits the collapsed settings form.
    assert '<button type="submit" class="lg" form="analysis-settings">Run analysis</button>' in page

    fragment = _run_analysis(client, project).content.decode()
    assert 'form="analysis-settings"' not in fragment  # fresh result: nothing to recompute
    project.pipeline_config = {**project.pipeline_config, "synchrony_threshold": 0.7}
    project.save()
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert "run-status stale" in page and 'form="analysis-settings">Recompute</button>' in _run_bar(page)
    marker = '<span class="sr-only"> (changed since the last run)</span></li>'
    assert f'<li class="changed">Threshold: 0.7{marker}' in _run_bar(page)
    assert _run_bar(page).count('class="changed"') == 1

    # The outdated result stays readable but dimmed; the run bar's Recompute is the single recompute action.
    assert page.count('<div class="result is-stale" role="tabpanel"') == 2
    assert ('<div class="alert warning stale-note">Settings or signals changed since this run. Run the analysis again.'
            '</div>' in page)
    assert page.count('form="analysis-settings">Recompute</button>') == 1

    form = _analysis_form(project, contagion_min_participants=5)
    fragment = client.post(reverse("gsr:analysis", args=[project.pk]), form, **HTMX).content.decode()
    assert '<div id="drawer-root" hx-swap-oob="innerHTML">' in fragment and "data-autoopen" in _settings_form(fragment)


@pytest.mark.django_db
def test_polish_screens_use_polish_numbers_and_labels(client, project) -> None:
    project.pipeline_config = {**project.pipeline_config, "time_range_to": 2.5, "synchrony_threshold": 0.75}
    project.save()
    assert "Okno: …–2,5 min" in _run_bar(client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode())
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert "Próg: 0,75" in _run_bar(page) and "Próg wysoki/niski z: 1/-1" in _run_bar(page)
    form = _settings_form(page)
    assert "zmienione (domyślnie 0,6)" in form
    # The input fields keep dot decimals: that is what <input type="number"> accepts.
    assert 'name="synchrony_threshold" step="any" value="0.75"' in form
    fragment = _run_analysis(client, project).content.decode()
    assert "przesunięcie kołowe × 20" in fragment and ">circular" not in fragment
    # The mini timeline positions are CSS, so they keep dot decimals in Polish.
    assert re.search(r'<i style="left: \d+\.\d\d%; width: \d+\.\d\d%">', fragment)
    assert not re.search(r'(left|width): \d+,\d', fragment)


@pytest.mark.django_db
def test_upload_step_is_called_import_in_both_languages(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:upload", args=[project.pk])).content.decode()
    assert "<h1>Import</h1>" in page and "<b>Import</b>" in page and "<b>Upload</b>" not in page


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
def test_cleaning_shows_quality_tiles_and_tips_instead_of_hint_paragraphs(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    fragment = client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX).content.decode()
    rows = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_CLEANING).payload["preview"][
        "cleaning_summary"]["per_alias"]
    assert fragment.count('<li class="tile">') == len(rows) == 3
    for row in rows:
        assert f"rejected {row['artifact_rejected_points']:,} of {row['synced_points']:,} samples" in fragment
    assert 'class="hint"' not in fragment
    assert 'role="tooltip" id="tip-quality"' in fragment and 'role="tooltip" id="tip-clean"' in fragment


@pytest.mark.django_db
def test_analysis_results_interpret_figures_and_place_episodes_in_time(client, project) -> None:
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    fragment = _run_analysis(client, project).content.decode()
    payload = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_ANALYSIS).payload
    synchrony, coarousal = payload["synchrony"], payload["coarousal"]
    assert 'class="hint"' not in fragment

    # Tabs carry the episode counts; the KPI tiles say what the test means.
    count = synchrony["stats"]["episode_count"]
    assert f'Synchronization <span class="count">{count} episode{"" if count == 1 else "s"}</span>' in fragment
    p_value = synchrony["stats"]["mean_synchrony_p_value"]
    verdict = ('<span class="badge sig">above chance</span>' if p_value < 0.05
               else '<span class="badge neutral">not above chance</span>')
    assert f"{verdict} circular shift × 20" in fragment
    assert "subgroups of ≥ 2 people above 0.6" in fragment

    # Two-panel timeline: the pair curves are behind a switch.
    assert 'data-pairs-for="sync-timeline"' in fragment and "Pair curves (3)" in fragment
    timeline = json.loads(re.search(r'id="sync-timeline-data" type="application/json">(.*?)</script>',
                                    fragment).group(1))
    assert timeline["span"] == [math.floor(min(timeline["centers"])), math.ceil(max(timeline["centers"]))]

    # Episode rows: E-numbers as on the chart, with a mini timeline.
    for number, _episode in enumerate(synchrony["episodes"], start=1):
        assert f"<strong>E{number}</strong>" in fragment
    assert fragment.count('class="minibar"') == len(synchrony["episodes"])

    # Significant pairs are bold, the rest recede.
    significant = sum(pair["p_value"] < 0.05 for pair in synchrony["pairs"])
    assert fragment.count('<tr class="sig">') == significant
    assert fragment.count('<tr class="nsig">') == len(synchrony["pairs"]) - significant

    # Co-arousal: one episode table with a "merge overlaps" switch instead of a second table; state bars.
    if coarousal["episodes"]:
        assert 'data-merge-toggle aria-controls="coarousal-episodes coarousal-merged"' in fragment
        assert 'id="coarousal-merged" hidden' in fragment
    assert fragment.count('class="state-bar"') == len(coarousal["state_percentages"])
    states = json.loads(re.search(r'id="coarousal-states-data" type="application/json">(.*?)</script>',
                                  fragment).group(1))
    assert len(states["episodes"]) == len(coarousal["episodes"])


def test_coarousal_chart_stacks_overlapping_episodes_in_lanes() -> None:
    data = charts.coarousal_states({
        "state_timeline": {"times_min": [0.0, 1.0], "per_alias": []},
        "episodes": [
            {"direction": "Co-movement", "start_min": 2.0, "end_min": 3.0, "contributors": ["A", "B"]},
            {"direction": "High", "start_min": 0.5, "end_min": 2.5, "contributors": ["A"]},
            {"direction": "Low", "start_min": 2.5, "end_min": 4.0, "contributors": []},
        ],
    })
    assert [(episode["direction"], episode["lane"]) for episode in data["episodes"]] == [
        ("High", 0), ("Co-movement", 1), ("Low", 0)]
    assert data["lanes"] == 2
    assert charts.coarousal_states({})["lanes"] == 0


def test_sync_timeline_spans_whole_minutes_around_the_windows() -> None:
    assert charts.sync_timeline({"windows": {"centers": [0.25, 5.5, 11.6]}})["span"] == [0, 12]
    assert charts.sync_timeline({"windows": {"centers": [3.0]}})["span"] == [3, 4]
    assert charts.sync_timeline({})["span"] is None


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
    assert "run-status stale" not in page

    # A cleaning change makes the analysis stale, but an analysis change does not touch the cleaning preview.
    client.post(reverse("gsr:cleaning_refresh", args=[project.pk]), **HTMX)
    project.pipeline_config = {**project.pipeline_config, "median_kernel": 7}
    project.save()
    assert "run-status stale" in client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    project.pipeline_config = {**project.pipeline_config, "median_kernel": 5, "synchrony_threshold": 0.7}
    project.save()
    assert "run-status stale" not in client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert "run-status stale" in client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()


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


@pytest.mark.django_db
def test_export_screen_shows_report_card_language_select_and_file_names(client, project) -> None:
    _run_analysis(client, project)
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:export", args=[project.pk])).content.decode()
    assert "Outdated" not in page and "data-stale-banner" not in page and "is-stale" not in page
    for item in ("Summary page", "Synchronization", "Co-arousal", "Methodology", "Reproducibility", "6 charts"):
        assert f"</svg>{item}</li>" in page
    assert '<select name="lang" data-report-lang-select>' in page
    assert '<option value="en" selected>English</option>' in page and '<option value="pl">Polski</option>' in page
    assert "sesja-odzka_report_en.pdf" in page and "sesja-odzka_report_pl.pdf" in page
    assert "GSR Analysis Report" in page and "Raport analizy GSR" in page  # page-1 preview per report language
    for name in ("clean_signals.csv", "analysis_input_merged.csv", "synchrony_pairs.csv",
                 "coarousal_state_percentages.csv", "run_settings_and_results.json", "results.zip"):
        assert f"sesja-odzka_{name}" in page
    labels = json.loads(page.split('<script id="report-chart-labels" type="application/json">')[1].split("</script>")[0])
    assert labels["pl"]["activeCount"] == "Liczba zsynchronizowanych" and labels["pl"]["locale"] == "pl"
    assert labels["en"]["activeCount"] == "Synchronized count"


@pytest.mark.django_db
def test_export_outdated_banner_names_the_change_and_reruns(client, project) -> None:
    _run_analysis(client, project)
    stored = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_ANALYSIS)
    project.refresh_from_db()
    project.pipeline_config = {**project.pipeline_config, "contagion_high_z": 1.5}
    project.save()
    dataset = project.datasets.get(alias="C")
    client.post(reverse("gsr:dataset_rename", args=[project.pk, dataset.pk]), {"alias": "Cezary"})
    client.post(reverse("set_language"), {"language": "en", "next": "/"})

    page = client.get(reverse("gsr:export", args=[project.pk])).content.decode()
    assert "data-stale-banner" in page and 'class="export-grid is-stale"' in page
    assert "The export describes the run from" in page and "Export the" in page
    assert "High threshold (z): 1 → 1.5" in page and "Signal renamed: C → Cezary" in page
    assert reverse("gsr:export_rerun", args=[project.pk]) in page

    response = client.post(reverse("gsr:export_rerun", args=[project.pk]), **HTMX)
    assert response.status_code == 200
    page = response.content.decode()
    assert "data-stale-banner" not in page and 'id="rail-steps" hx-swap-oob="true"' in page
    rerun = AnalysisResult.objects.get(project=project, kind=AnalysisResult.KIND_ANALYSIS)
    assert rerun.computed_at > stored.computed_at and rerun.config["contagion_high_z"] == 1.5


@pytest.mark.django_db
def test_export_banner_lists_added_and_removed_signals(client, project) -> None:
    project.datasets.filter(alias="B").update(included=False)
    _run_analysis(client, project)
    project.datasets.filter(alias="B").update(included=True)
    project.datasets.filter(alias="C").update(included=False)
    client.post(reverse("set_language"), {"language": "en", "next": "/"})
    page = client.get(reverse("gsr:export", args=[project.pk])).content.decode()
    assert "Signals added: B" in page and "Signals removed: C" in page


@pytest.mark.django_db
def test_export_rerun_failure_shows_the_error_on_the_export_screen(client, project) -> None:
    _run_analysis(client, project)
    project.datasets.exclude(alias="A").update(included=False)
    response = client.post(reverse("gsr:export_rerun", args=[project.pk]), **HTMX)
    page = response.content.decode()
    assert 'class="alert error"' in page and "data-stale-banner" in page

    response = client.post(reverse("gsr:export_rerun", args=[project.pk]))
    assert response.status_code == 302 and response["Location"] == reverse("gsr:export", args=[project.pk])
    assert client.get(reverse("gsr:export_rerun", args=[project.pk])).status_code == 405


@pytest.mark.django_db
def test_export_pdf_uses_the_chosen_report_language(client, project, monkeypatch) -> None:
    from gsr import views

    seen = {}
    monkeypatch.setattr(views, "render_report_pdf", lambda payload: seen.setdefault("lang", payload["lang"]) and b"%PDF")
    _run_analysis(client, project)
    url = reverse("gsr:export_pdf", args=[project.pk])
    response = client.post(url, {"lang": "en"})
    assert seen["lang"] == "en" and "sesja-odzka_report_en.pdf" in response["Content-Disposition"]
    seen.clear()
    response = client.post(url, {"lang": "de"})
    assert seen["lang"] == "pl" and "report_pl.pdf" in response["Content-Disposition"]


# ─── Language switch ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_language_switch_translates_screens_and_chart_labels(client, project) -> None:
    page = client.get(reverse("gsr:projects")).content.decode()
    assert '<html lang="pl">' in page and "Nowy projekt" in page and "Liczba zsynchronizowanych" in page

    response = client.post(reverse("set_language"), {"language": "en", "next": reverse("gsr:cleaning", args=[project.pk])})
    assert response.status_code == 302 and response["Location"] == reverse("gsr:cleaning", args=[project.pk])
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert '<html lang="en">' in page and "Cleaning settings" in page and "Synchronized count" in page

    client.post(reverse("set_language"), {"language": "pl", "next": "/"})
    page = client.get(reverse("gsr:cleaning", args=[project.pk])).content.decode()
    assert "Ustawienia czyszczenia" in page and "Zakres fizjologiczny" in page
    assert "Sygnały: 3/3" in page and "Edytuj ustawienia" in page and 'aria-label="Więcej informacji"' in page


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


def test_changed_settings_name_each_change_with_old_and_new_values() -> None:
    before = params.DEFAULTS
    after = {**before, "clean_filter_zscore_threshold": 4.0, "clean_filter_iqr": not before["clean_filter_iqr"],
             "synchrony_surrogate_method": "phase", "time_range_to": 9.5}
    with translation.override("en"):
        changes = params.changed_settings(before, after)
    assert changes == [
        "Time window · To (min): — → 9.5",
        "Z-score deviation · Threshold (SD): 5 → 4",
        "Interquartile range (IQR): off → on",
        "Surrogate method: circular shift → phase randomization",
    ]
    assert params.changed_settings(before, dict(before)) == []
