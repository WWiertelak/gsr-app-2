from __future__ import annotations

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation

from gsr import params
from gsr.models import AnalysisResult, Project
from gsr.pipeline.vsr_config import VsrConfig, parse_video_start_time
from gsr.services.datasets import add_dataset
from gsr.services.interview import (
    apply_interview_config,
    compute_vsr_signature,
    load_interview_config,
    vsr_changed_label,
    vsr_field_rows,
)
from gsr.services.results import compute_analysis, current_signature, is_stale
from gsr.vsr_params import VSR_PARAMS

from .sample_data import Participant, write_group

FAST = {"synchrony_surrogate_count": 20, "contagion_surrogate_count": 20}


@pytest.fixture()
def lesson_project(tmp_path: Path, isolated_storage):
    paths = write_group(
        tmp_path,
        "lesson",
        {
            "T": Participant(tonic_us=5.0),
            "S1": Participant(tonic_us=4.0),
            "S2": Participant(tonic_us=4.2),
            "S3": Participant(tonic_us=4.1),
        },
        {},
        seconds=240.0,
        seed=3,
    )
    project = Project.objects.create(name="VSR", pipeline_config=dict(FAST))
    for alias, path in paths.items():
        add_dataset(project, SimpleUploadedFile(path.name, path.read_bytes()), alias=alias)
    return project


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("9:30:00", "09:30:00"),
        ("09:05:07", "09:05:07"),
    ],
)
def test_parse_video_start_time_accepts_clock_values(value, expected) -> None:
    assert parse_video_start_time(value) == expected


@pytest.mark.parametrize("value", ["9:30", "25:00:00", "12:60:00", "noon"])
def test_parse_video_start_time_rejects_invalid_values(value) -> None:
    with pytest.raises(ValueError, match="video_start_time"):
        parse_video_start_time(value)


@pytest.mark.django_db
def test_load_interview_config_defaults(lesson_project) -> None:
    config = load_interview_config(lesson_project)
    assert config.roles.teacher_alias is None
    assert config.roles.student_pairs == ()
    assert config.roles.video_start_time is None
    assert config.vsr.vsr_sync_threshold == 0.8
    assert config.vsr.vsr_async_threshold == 0.1


@pytest.mark.django_db
def test_apply_interview_config_stores_roles_and_vsr_parameters(lesson_project) -> None:
    stored = apply_interview_config(
        lesson_project,
        {
            "teacher_alias": "T",
            "student_pairs": [["S1", "S2"]],
            "video_start_time": "10:00:00",
            "vsr": {"vsr_sync_threshold": 0.75},
        },
    )
    lesson_project.interview_config = stored
    lesson_project.save(update_fields=["interview_config", "updated_at"])

    config = load_interview_config(lesson_project)
    assert config.roles.teacher_alias == "T"
    assert config.roles.student_pairs == (("S1", "S2"),)
    assert config.roles.video_start_time == "10:00:00"
    assert config.vsr.vsr_sync_threshold == 0.75


@pytest.mark.django_db
def test_apply_interview_config_rejects_unknown_alias_and_overlapping_students(lesson_project) -> None:
    with translation.override("en"):
        with pytest.raises(ValueError, match="Unknown teacher"):
            apply_interview_config(lesson_project, {"teacher_alias": "missing"})
        with pytest.raises(ValueError, match="only one pair"):
            apply_interview_config(lesson_project, {"student_pairs": [["S1", "S2"], ["S1", "S3"]]})
        with pytest.raises(ValueError, match="teacher"):
            apply_interview_config(lesson_project, {"teacher_alias": "T", "student_pairs": [["T", "S1"]]})
        with pytest.raises(ValueError, match="two different"):
            apply_interview_config(lesson_project, {"student_pairs": [["S1", "S1"]]})
    with pytest.raises(ValueError, match="tylko do jednej pary"):
        apply_interview_config(lesson_project, {"student_pairs": [["S1", "S2"], ["S1", "S3"]]})


@pytest.mark.django_db
def test_vsr_config_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="vsr_async_threshold"):
        VsrConfig(vsr_sync_threshold=0.2, vsr_async_threshold=0.5)


@pytest.mark.django_db
def test_vsr_changed_markers_reflect_non_default_parameters() -> None:
    defaults = VsrConfig().model_dump()
    assert vsr_changed_label(defaults) is None
    changed = {**defaults, "vsr_sync_threshold": 0.75, "vsr_window_seconds": 60.0}
    assert vsr_changed_label(changed) and vsr_changed_label(changed).startswith("2 ")
    rows = vsr_field_rows(changed)
    modified = [row["param"].name for row in rows if row["modified"]]
    assert set(modified) == {"vsr_sync_threshold", "vsr_window_seconds"}


@pytest.mark.django_db
def test_interview_changes_do_not_invalidate_analysis_signature(lesson_project) -> None:
    result = compute_analysis(lesson_project)
    signature_before = current_signature(lesson_project, AnalysisResult.KIND_ANALYSIS)
    vsr_signature_before = compute_vsr_signature(lesson_project)

    lesson_project.interview_config = apply_interview_config(
        lesson_project,
        {
            "teacher_alias": "T",
            "student_pairs": [["S1", "S2"]],
            "video_start_time": "08:00:00",
            "vsr": {"vsr_primary_moment_count": 4},
        },
    )
    lesson_project.save(update_fields=["interview_config", "updated_at"])

    assert current_signature(lesson_project, AnalysisResult.KIND_ANALYSIS) == signature_before
    assert not is_stale(lesson_project, result)
    assert compute_vsr_signature(lesson_project) != vsr_signature_before


@pytest.mark.django_db
def test_vsr_signature_changes_when_roles_or_vsr_parameters_change(lesson_project) -> None:
    base = compute_vsr_signature(lesson_project)
    lesson_project.interview_config = apply_interview_config(
        lesson_project, {"teacher_alias": "T", "student_pairs": [["S1", "S2"]]}
    )
    lesson_project.save(update_fields=["interview_config"])
    with_teacher = compute_vsr_signature(lesson_project)
    assert with_teacher != base

    lesson_project.interview_config = apply_interview_config(lesson_project, {"vsr": {"vsr_step_seconds": 10}})
    lesson_project.save(update_fields=["interview_config"])
    assert compute_vsr_signature(lesson_project) != with_teacher


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"vsr_sync_threshold": "nan"}, "vsr_sync_threshold must be a valid number"),
        ({"vsr_high_z": float("inf")}, "vsr_high_z must be a valid number"),
        ({"vsr_primary_moment_count": "5.5"}, "vsr_primary_moment_count must be a valid integer"),
        ({"vsr_primary_moment_count": "five"}, "vsr_primary_moment_count must be a valid integer"),
    ],
)
def test_vsr_config_rejects_non_finite_and_non_integer_values(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        VsrConfig(**kwargs)


def test_vsr_config_accepts_whole_number_floats_for_counts() -> None:
    config = VsrConfig(vsr_primary_moment_count=4.0, vsr_reserve_moment_count="2.0")
    assert config.vsr_primary_moment_count == 4 and isinstance(config.vsr_primary_moment_count, int)
    assert config.vsr_reserve_moment_count == 2


def test_vsr_params_render_and_parse_with_their_own_defaults() -> None:
    changed = {**VsrConfig().model_dump(), "vsr_sync_threshold": 0.75}
    row = next(row for row in vsr_field_rows(changed) if row["param"].name == "vsr_sync_threshold")
    html = render_to_string("gsr/partials/param_field.html", {"row": row})
    assert "(domyślnie 0,8)" in html
    assert params.parse_params(VSR_PARAMS, QueryDict("vsr_sync_threshold="))["vsr_sync_threshold"] == 0.8


@pytest.mark.django_db
def test_apply_interview_config_ignores_flat_vsr_keys(lesson_project) -> None:
    stored = apply_interview_config(lesson_project, {"vsr_sync_threshold": 0.75})
    assert stored["vsr"]["vsr_sync_threshold"] == 0.8


@pytest.mark.django_db
def test_renaming_a_recording_keeps_its_interview_roles(client, lesson_project) -> None:
    lesson_project.interview_config = apply_interview_config(
        lesson_project, {"teacher_alias": "T", "student_pairs": [["S1", "S2"]]}
    )
    lesson_project.save(update_fields=["interview_config"])
    for old, new in (("S1", "Ola"), ("T", "Teacher")):
        dataset = lesson_project.datasets.get(alias=old)
        client.post(reverse("gsr:dataset_rename", args=[lesson_project.pk, dataset.pk]), {"alias": new})

    lesson_project.refresh_from_db()
    config = load_interview_config(lesson_project)
    assert config.roles.teacher_alias == "Teacher"
    assert config.roles.student_pairs == (("Ola", "S2"),)
    stored = apply_interview_config(lesson_project, {"vsr": {"vsr_sync_threshold": 0.7}})
    assert stored["student_pairs"] == [["Ola", "S2"]]
    compute_vsr_signature(lesson_project)


@pytest.mark.django_db
def test_deleting_a_recording_drops_it_from_interview_roles(client, lesson_project) -> None:
    lesson_project.interview_config = apply_interview_config(
        lesson_project, {"teacher_alias": "T", "student_pairs": [["S1", "S2"]], "video_start_time": "09:00:00"}
    )
    lesson_project.save(update_fields=["interview_config"])
    for alias in ("S1", "T"):
        dataset = lesson_project.datasets.get(alias=alias)
        client.post(reverse("gsr:dataset_delete", args=[lesson_project.pk, dataset.pk]))

    lesson_project.refresh_from_db()
    config = load_interview_config(lesson_project)
    assert config.roles.teacher_alias is None
    assert config.roles.student_pairs == ()
    assert config.roles.video_start_time == "09:00:00"
    compute_vsr_signature(lesson_project)
