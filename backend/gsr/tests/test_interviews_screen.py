"""The Interviews step: participants, proposals from the VSR protocol, staleness, PL/EN."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import translation

from gsr.models import AnalysisResult, Project
from gsr.services.datasets import add_dataset
from gsr.services.interview import apply_interview_config
from gsr.services.results import compute_analysis, get_result, is_stale
from gsr.services.vsr import compute_interviews, interview_students, video_time

from .sample_data import Participant, write_group

FAST = {"synchrony_surrogate_count": 20, "contagion_surrogate_count": 20}
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture()
def lesson(tmp_path: Path) -> Project:
    paths = write_group(
        tmp_path,
        "lesson",
        {
            "T": Participant(tonic_us=5.0),
            "S1": Participant(tonic_us=4.0),
            "S2": Participant(tonic_us=4.2, coupling=0.7),
            "S3": Participant(tonic_us=4.1),
            "S4": Participant(tonic_us=3.9, own_events=2),
        },
        {},
        seconds=600.0,
        seed=11,
    )
    project = Project.objects.create(name="Lekcja", pipeline_config=dict(FAST))
    for alias, path in paths.items():
        add_dataset(project, SimpleUploadedFile(path.name, path.read_bytes()), alias=alias)
    return project


def _roles(**overrides) -> dict:
    data = {
        "teacher_alias": "T",
        "pair_left": ["S1", "S3", ""],
        "pair_right": ["S2", "S4", ""],
        "video_start_time": "11:59:00",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_interviews_page_needs_the_analysis_first(client, lesson) -> None:
    with translation.override("en"):
        response = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="en")
    html = response.content.decode()
    assert response.status_code == 200
    assert "Run the analysis first" in html
    assert 'name="teacher_alias"' in html


@pytest.mark.django_db
def test_propose_moments_saves_roles_and_shows_one_tab_per_interview(client, lesson) -> None:
    compute_analysis(lesson)
    response = client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles(), **HTMX)
    html = response.content.decode()
    assert response.status_code == 200, html

    lesson.refresh_from_db()
    assert lesson.interview_config["teacher_alias"] == "T"
    assert lesson.interview_config["student_pairs"] == [["S1", "S2"], ["S3", "S4"]]
    result = get_result(lesson, AnalysisResult.KIND_VSR)
    assert result is not None and not is_stale(lesson, result)
    assert [interview["key"] for interview in result.payload["interviews"]] == ["S1+S2", "S3+S4", "T"]
    assert result.payload["roles"]["students"] == ["S1", "S2", "S3", "S4"]

    for tab in ("interview-1", "interview-2", "interview-3"):
        assert f'data-tab="{tab}"' in html
    # Justifications quote the document verbatim, with the page.
    assert "Synchronous = above 0.8" in html
    assert 'class="doc-quote" lang="en"' in html
    assert "lesson-timeline" in html
    assert 'id="rail-steps" hx-swap-oob="true"' in html


@pytest.mark.django_db
def test_moments_carry_lesson_clock_and_video_times(lesson) -> None:
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(
        lesson, {"student_pairs": [["S1", "S2"]], "video_start_time": "11:59:00"}
    )
    lesson.save()
    result = compute_interviews(lesson)
    moments = [moment for interview in result.payload["interviews"] for moment in interview["moments"]]
    assert moments
    for moment in moments:
        assert moment["start_clock"] and moment["start_video"] == video_time(moment["start_clock"], "11:59:00")


def test_video_time_counts_from_the_video_start() -> None:
    assert video_time("12:01:30", "12:00:00") == "0:01:30"
    assert video_time("11:59:59", "12:00:00") is None
    assert video_time("00:00:10", "23:59:00") == "0:01:10"
    assert video_time("12:00:00", None) is None


def test_teacher_interview_averages_the_paired_students_or_everyone_else() -> None:
    from gsr.services.interview import InterviewRoles

    paired = InterviewRoles("T", (("S1", "S2"),), None)
    assert interview_students(paired, ["T", "S1", "S2", "S3"]) == ["S1", "S2"]
    unpaired = InterviewRoles("T", (), None)
    assert interview_students(unpaired, ["T", "S1", "S2", "S3"]) == ["S1", "S2", "S3"]


class _SubmitButtons(HTMLParser):
    """Submit buttons of the rendered page: (form attribute, disabled, label)."""

    def __init__(self) -> None:
        super().__init__()
        self.buttons: list[list] = []
        self._open: list | None = None

    def handle_starttag(self, tag, attrs) -> None:
        attrs = dict(attrs)
        if tag == "button" and attrs.get("type") == "submit":
            self._open = [attrs.get("form"), "disabled" in attrs, ""]
            self.buttons.append(self._open)

    def handle_data(self, data) -> None:
        if self._open is not None:
            self._open[2] += data

    def handle_endtag(self, tag) -> None:
        if tag == "button":
            self._open = None


def _role_submits(client, project: Project) -> list[tuple]:
    with translation.override("en"):
        response = client.get(reverse("gsr:interviews", args=[project.pk]), HTTP_ACCEPT_LANGUAGE="en")
    parser = _SubmitButtons()
    parser.feed(response.content.decode())
    return [(disabled, label.strip()) for form, disabled, label in parser.buttons if form == "interview-roles"]


@pytest.mark.django_db
def test_run_bar_holds_the_single_propose_action(client, lesson) -> None:
    assert _role_submits(client, lesson) == [(True, "Propose moments")]

    compute_analysis(lesson)
    assert _role_submits(client, lesson) == [(False, "Propose moments")]

    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles(), **HTMX)
    assert _role_submits(client, lesson) == [(False, "Propose moments")]

    lesson.interview_config = apply_interview_config(lesson, {"vsr": {"vsr_sync_threshold": 0.7}})
    lesson.save()
    assert _role_submits(client, lesson) == [(False, "Recompute")]


@pytest.mark.django_db
def test_changing_roles_or_rerunning_the_analysis_marks_the_proposals_stale(lesson) -> None:
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(lesson, {"student_pairs": [["S1", "S2"]]})
    lesson.save()
    result = compute_interviews(lesson)
    assert not is_stale(lesson, result)

    lesson.interview_config = apply_interview_config(lesson, {"vsr": {"vsr_sync_threshold": 0.7}})
    lesson.save()
    assert is_stale(lesson, result)

    result = compute_interviews(lesson)
    lesson.pipeline_config = {**lesson.pipeline_config, "synchrony_threshold": 0.5}
    lesson.save()
    compute_analysis(lesson)
    assert is_stale(lesson, result)


@pytest.mark.django_db
def test_invalid_roles_show_the_error_and_keep_the_posted_rows(client, lesson) -> None:
    compute_analysis(lesson)
    with translation.override("en"):
        response = client.post(
            reverse("gsr:interviews", args=[lesson.pk]),
            _roles(pair_left=["S1", "S1"], pair_right=["S2", "S3"]),
            HTTP_ACCEPT_LANGUAGE="en",
            **HTMX,
        )
    html = response.content.decode()
    assert "only one pair" in html
    assert get_result(lesson, AnalysisResult.KIND_VSR) is None


@pytest.mark.django_db
def test_settings_drawer_applies_vsr_parameters_and_reset_restores_the_document(client, lesson) -> None:
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(lesson, {"student_pairs": [["S1", "S2"]]})
    lesson.save()
    response = client.post(
        reverse("gsr:interviews_settings", args=[lesson.pk]),
        {"vsr_sync_threshold": "0.7", "vsr_primary_moment_count": "4"},
        **HTMX,
    )
    assert response.status_code == 200
    result = get_result(lesson, AnalysisResult.KIND_VSR)
    assert result.config["vsr"]["vsr_sync_threshold"] == 0.7
    assert result.payload["interviews"][0]["stats"]["primary_target"] == 4

    client.post(reverse("gsr:interviews_reset", args=[lesson.pk]), **HTMX)
    lesson.refresh_from_db()
    assert lesson.interview_config["vsr"]["vsr_sync_threshold"] == 0.8
    assert lesson.interview_config["student_pairs"] == [["S1", "S2"]]


@pytest.mark.django_db
def test_rail_lists_interviews_between_analysis_and_export(client, lesson) -> None:
    with translation.override("pl"):
        html = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="pl").content.decode()
    labels = [label for label in ("Analiza", "Wywiady", "Eksport") if label in html]
    assert labels == ["Analiza", "Wywiady", "Eksport"]
    assert html.index("<b>Analiza</b>") < html.index("<b>Wywiady</b>") < html.index("<b>Eksport</b>")
