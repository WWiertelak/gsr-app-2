from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone, translation

from gsr.models import AnalysisResult, Project
from gsr.services.datasets import add_dataset
from gsr.services.interview import (
    apply_interview_config,
    compute_vsr_signature,
    load_interview_config,
    parse_video_start_time,
    validate_count,
)
from gsr.services.results import compute_analysis, current_signature, is_stale

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
    # The document's 5 moments and a reserve of 3 (8 in all).
    assert (config.primary_count, config.reserve_count) == (5, 3)


@pytest.mark.django_db
def test_apply_interview_config_stores_roles_and_moment_counts(lesson_project) -> None:
    stored = apply_interview_config(
        lesson_project,
        {
            "teacher_alias": "T",
            "student_pairs": [["S1", "S2"]],
            "video_start_time": "10:00:00",
            "primary_moment_count": "4",
            "reserve_moment_count": 2,
        },
    )
    lesson_project.interview_config = stored
    lesson_project.save(update_fields=["interview_config", "updated_at"])

    config = load_interview_config(lesson_project)
    assert config.roles.teacher_alias == "T"
    assert config.roles.student_pairs == (("S1", "S2"),)
    assert config.roles.video_start_time == "10:00:00"
    assert (config.primary_count, config.reserve_count) == (4, 2)
    assert set(stored) == {"teacher_alias", "student_pairs", "video_start_time", "primary_moment_count",
                           "reserve_moment_count"}


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


@pytest.mark.parametrize(("name", "value"), [
    ("primary_moment_count", 0), ("primary_moment_count", 21), ("primary_moment_count", "5.5"),
    ("primary_moment_count", "five"), ("primary_moment_count", True), ("reserve_moment_count", -1),
    ("reserve_moment_count", "nan"),
])
def test_moment_counts_must_be_whole_numbers_within_their_limits(name, value) -> None:
    with translation.override("en"), pytest.raises(ValueError, match="must be a whole number"):
        validate_count(name, value)


def test_moment_counts_accept_whole_number_text() -> None:
    assert validate_count("primary_moment_count", "4.0") == 4
    assert validate_count("reserve_moment_count", "0") == 0


@pytest.mark.django_db
def test_invalid_moment_count_is_explained_in_polish(lesson_project) -> None:
    with pytest.raises(ValueError, match="Liczba momentów głównych musi być liczbą całkowitą od 1 do 20"):
        apply_interview_config(lesson_project, {"primary_moment_count": "0"})


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
            "primary_moment_count": 4,
        },
    )
    lesson_project.save(update_fields=["interview_config", "updated_at"])

    assert current_signature(lesson_project, AnalysisResult.KIND_ANALYSIS) == signature_before
    assert not is_stale(lesson_project, result)
    assert compute_vsr_signature(lesson_project) != vsr_signature_before


@pytest.mark.django_db
def test_vsr_signature_changes_when_roles_or_moment_counts_change(lesson_project) -> None:
    base = compute_vsr_signature(lesson_project)
    lesson_project.interview_config = apply_interview_config(
        lesson_project, {"teacher_alias": "T", "student_pairs": [["S1", "S2"]]}
    )
    lesson_project.save(update_fields=["interview_config"])
    with_teacher = compute_vsr_signature(lesson_project)
    assert with_teacher != base

    lesson_project.interview_config = apply_interview_config(lesson_project, {"reserve_moment_count": 1})
    lesson_project.save(update_fields=["interview_config"])
    assert compute_vsr_signature(lesson_project) != with_teacher


@pytest.mark.django_db
def test_vsr_signature_follows_the_analysis_settings(lesson_project) -> None:
    compute_analysis(lesson_project)
    before = compute_vsr_signature(lesson_project)

    lesson_project.pipeline_config = {**lesson_project.pipeline_config, "synchrony_threshold": 0.7}
    lesson_project.save(update_fields=["pipeline_config"])
    compute_analysis(lesson_project)

    assert compute_vsr_signature(lesson_project) != before


@pytest.mark.django_db
def test_apply_interview_config_drops_the_removed_vsr_settings(lesson_project) -> None:
    lesson_project.interview_config = {"teacher_alias": "T", "vsr": {"vsr_sync_threshold": 0.75}}
    stored = apply_interview_config(lesson_project, {"vsr": {"vsr_window_seconds": 60}, "vsr_sync_threshold": 0.7})
    assert "vsr" not in stored and "vsr_sync_threshold" not in stored
    assert stored["teacher_alias"] == "T"


@pytest.mark.django_db
def test_migration_keeps_the_moment_counts_and_drops_the_old_vsr_settings(lesson_project) -> None:
    migration = import_module("gsr.migrations.0005_interview_moments_from_analysis")
    lesson_project.interview_config = {
        "teacher_alias": "T", "student_pairs": [["S1", "S2"]], "video_start_time": "09:00:00",
        "vsr": {"vsr_sync_threshold": 0.75, "vsr_window_seconds": 60.0, "vsr_primary_moment_count": 4,
                "vsr_reserve_moment_count": 99},
    }
    lesson_project.save(update_fields=["interview_config"])
    old = AnalysisResult.objects.create(project=lesson_project, kind=AnalysisResult.KIND_VSR, signature="x",
                                        payload={"config": {"vsr_sync_threshold": 0.75}}, computed_at=timezone.now())

    migration.forwards(django_apps, None)

    lesson_project.refresh_from_db()
    assert lesson_project.interview_config == {
        "teacher_alias": "T", "student_pairs": [["S1", "S2"]], "video_start_time": "09:00:00",
        # 99 reserve moments were outside the limits: the default comes back.
        "primary_moment_count": 4, "reserve_moment_count": 3,
    }
    assert not AnalysisResult.objects.filter(pk=old.pk).exists()
    assert load_interview_config(lesson_project).primary_count == 4


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
    stored = apply_interview_config(lesson_project, {"primary_moment_count": 3})
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
