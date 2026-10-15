"""The Interviews step: participants, moments chosen from the Analysis, previews, staleness, PL/EN."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import NoReverseMatch, reverse
from django.utils import translation

from gsr.models import AnalysisResult, Project
from gsr.services.datasets import add_dataset
from gsr.services.interview import apply_interview_config
from gsr.services.results import compute_analysis, get_result, is_stale
from gsr.services.vsr import compute_interviews, interview_students

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


def _chart_data(html: str) -> list[dict]:
    """The data of every moment preview on the page."""
    return [json.loads(match) for match in re.findall(
        r'<script id="moment-chart-[^"]+-data" type="application/json">(.*?)</script>', html)]


@pytest.mark.django_db
def test_interviews_page_needs_the_analysis_first(client, lesson) -> None:
    with translation.override("en"):
        response = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="en")
    html = response.content.decode()
    assert response.status_code == 200
    assert "Run the Analysis first." in html
    assert 'name="teacher_alias"' in html
    assert 'name="primary_moment_count" value="5"' in html and 'name="reserve_moment_count" value="3"' in html


@pytest.mark.django_db
def test_interviews_step_has_no_settings_of_its_own(client, lesson) -> None:
    compute_analysis(lesson)
    html = client.get(reverse("gsr:interviews", args=[lesson.pk])).content.decode()
    assert 'id="settings-drawer"' not in html
    assert "vsr_sync_threshold" not in html and "vsr_window_seconds" not in html
    # The bar names the Analysis settings the moments come from and links to them.
    assert "Próg: 0,6" in html and "Okno: 30 s" in html
    assert reverse("gsr:analysis", args=[lesson.pk]) in html
    for name in ("gsr:interviews_settings", "gsr:interviews_reset"):
        with pytest.raises(NoReverseMatch):
            reverse(name, args=[lesson.pk])


@pytest.mark.django_db
def test_choose_moments_saves_roles_and_shows_one_tab_per_interview(client, lesson) -> None:
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
    assert "lesson-timeline" in html
    assert 'id="rail-steps" hx-swap-oob="true"' in html


@pytest.mark.django_db
def test_every_moment_has_a_preview_and_one_polish_sentence(client, lesson) -> None:
    compute_analysis(lesson)
    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles())
    html = client.get(reverse("gsr:interviews", args=[lesson.pk])).content.decode()
    result = get_result(lesson, AnalysisResult.KIND_VSR)
    moment_count = sum(len(interview["moments"]) for interview in result.payload["interviews"])

    assert moment_count
    assert html.count('data-chart="momentSignals"') == moment_count
    assert html.count('class="moment-sentence"') == moment_count
    assert "schematu VSR:" in html
    # No English quotes from the VSR document.
    assert "doc-quote" not in html and "Synchronous = above 0.8" not in html and "Select this moment" not in html

    charts = _chart_data(html)
    assert len(charts) == moment_count
    for chart in charts:
        assert [len(series["points"]) > 2 for series in chart["series"]] == [True, True]
        # The moment is shaded inside the shown range, with context around it.
        assert chart["range"]["min"] <= chart["moment"]["start"] < chart["moment"]["end"] <= chart["range"]["max"]
        assert chart["series"][0]["points"][0][0] <= chart["moment"]["start"]


@pytest.mark.django_db
def test_sentences_follow_the_ui_language(client, lesson) -> None:
    compute_analysis(lesson)
    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles())
    with translation.override("en"):
        html = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert "VSR flow chart step" in html
    assert "schematu VSR" not in html


@pytest.mark.django_db
def test_the_number_of_moments_is_the_interview_settings_only(client, lesson) -> None:
    compute_analysis(lesson)
    response = client.post(reverse("gsr:interviews", args=[lesson.pk]),
                           _roles(primary_moment_count="2", reserve_moment_count="1"), **HTMX)
    assert response.status_code == 200

    result = get_result(lesson, AnalysisResult.KIND_VSR)
    assert result.payload["counts"] == {"primary": 2, "reserve": 1}
    for interview in result.payload["interviews"]:
        assert len(interview["moments"]) <= 3
        assert interview["stats"]["primary_target"] == 2


@pytest.mark.django_db
def test_the_asynchrony_threshold_is_set_next_to_the_number_of_moments(client, lesson) -> None:
    compute_analysis(lesson)
    page = client.get(reverse("gsr:interviews", args=[lesson.pk])).content.decode()
    assert 'name="asynchrony_threshold" value="0.1"' in page and "Krok 5: korelacja poniżej" in page

    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles(asynchrony_threshold="0,2"), **HTMX)
    lesson.refresh_from_db()
    assert lesson.interview_config["asynchrony_threshold"] == 0.2
    assert get_result(lesson, AnalysisResult.KIND_VSR).payload["asynchrony_threshold"] == 0.2

    with translation.override("en"):
        html = client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles(asynchrony_threshold="2"),
                           HTTP_ACCEPT_LANGUAGE="en", **HTMX).content.decode()
    assert "The asynchrony threshold must be a number from -1.0 to 1.0." in html
    lesson.refresh_from_db()
    assert lesson.interview_config["asynchrony_threshold"] == 0.2


@pytest.mark.django_db
def test_an_outdated_analysis_is_not_worked_around(client, lesson) -> None:
    compute_analysis(lesson)
    lesson.pipeline_config = {**lesson.pipeline_config, "synchrony_threshold": 0.5}
    lesson.save()

    with translation.override("en"):
        response = client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles(), HTTP_ACCEPT_LANGUAGE="en",
                               **HTMX)
    html = response.content.decode()
    assert "Run the Analysis first" in html
    assert get_result(lesson, AnalysisResult.KIND_VSR) is None
    assert _role_submits(client, lesson) == [(True, "Choose moments")]


@pytest.mark.django_db
def test_moments_of_an_outdated_analysis_are_shown_as_outdated(client, lesson) -> None:
    compute_analysis(lesson)
    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles())
    lesson.pipeline_config = {**lesson.pipeline_config, "synchrony_threshold": 0.5}
    lesson.save()

    with translation.override("en"):
        html = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert "Outdated result" in html and "Up to date" not in html
    assert "step is outdated: refresh it and the following steps in order." in html
    assert f'<a href="{reverse("gsr:analysis", args=[lesson.pk])}">Analysis</a>' in html
    assert _role_submits(client, lesson) == [(True, "Choose again")]


@pytest.mark.django_db
def test_moments_carry_lesson_and_sensor_clock_times(lesson) -> None:
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(
        lesson, {"student_pairs": [["S1", "S2"]], "video_start_time": "11:59:00"}
    )
    lesson.save()
    result = compute_interviews(lesson)
    moments = [moment for interview in result.payload["interviews"] for moment in interview["moments"]]
    assert moments
    for moment in moments:
        assert moment["start_clock"] and moment["end_clock"]
        assert moment["end_seconds"] > moment["start_seconds"]
        assert "start_video" not in moment and "end_video" not in moment


def test_teacher_interview_averages_the_paired_students_or_everyone_else() -> None:
    from gsr.services.interview import InterviewRoles

    paired = InterviewRoles("T", (("S1", "S2"),))
    assert interview_students(paired, ["T", "S1", "S2", "S3"]) == ["S1", "S2"]
    unpaired = InterviewRoles("T", ())
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
def test_run_bar_holds_the_single_choose_action(client, lesson) -> None:
    assert _role_submits(client, lesson) == [(True, "Choose moments")]

    compute_analysis(lesson)
    assert _role_submits(client, lesson) == [(False, "Choose moments")]

    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles(), **HTMX)
    assert _role_submits(client, lesson) == [(False, "Choose again")]


@pytest.mark.django_db
def test_changing_roles_or_rerunning_the_analysis_marks_the_moments_stale(lesson) -> None:
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(lesson, {"student_pairs": [["S1", "S2"]]})
    lesson.save()
    result = compute_interviews(lesson)
    assert not is_stale(lesson, result)

    lesson.interview_config = apply_interview_config(lesson, {"primary_moment_count": 4})
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
def test_legacy_video_start_is_ignored_and_not_rendered(client, lesson) -> None:
    compute_analysis(lesson)
    with translation.override("pl"):
        response = client.post(
            reverse("gsr:interviews", args=[lesson.pk]),
            _roles(video_start_time="25:99:00"),
            HTTP_ACCEPT_LANGUAGE="pl",
            **HTMX,
        )
    html = response.content.decode()
    assert "video_start_time" not in html
    assert "Start nagrania wideo" not in html
    assert get_result(lesson, AnalysisResult.KIND_VSR) is not None


@pytest.mark.django_db
def test_rail_lists_interviews_between_analysis_and_export(client, lesson) -> None:
    with translation.override("pl"):
        html = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="pl").content.decode()
    labels = [label for label in ("Analiza", "Wywiady", "Eksport") if label in html]
    assert labels == ["Analiza", "Wywiady", "Eksport"]
    assert html.index("<b>Analiza</b>") < html.index("<b>Wywiady</b>") < html.index("<b>Eksport</b>")


@pytest.mark.django_db
def test_staleness_cascades_and_recomputing_clears_only_that_step(client, lesson) -> None:
    import csv
    import io

    from gsr.services.exports import vsr_moments_csv
    from gsr.services.results import compute_cleaning, export_is_stale
    from gsr.services.vsr import report_data
    from gsr.views import _steps, report_payload

    cleaning = compute_cleaning(lesson)
    analysis = compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(lesson, {"student_pairs": [["S1", "S2"]]})
    lesson.save()
    interviews = compute_interviews(lesson)
    assert not export_is_stale(lesson)

    lesson.pipeline_config = {**lesson.pipeline_config, "median_kernel": 7}
    lesson.save()
    assert all(is_stale(lesson, result) for result in (cleaning, analysis, interviews))
    # Even an analysis with current inputs stays outdated while Cleaning is outdated.
    analysis = compute_analysis(lesson)
    assert is_stale(lesson, analysis)
    assert {step["key"] for step in _steps(lesson, "export") if step["stale"]} == {
        "cleaning", "analysis", "interviews", "export",
    }
    for screen in ("cleaning", "analysis", "interviews"):
        assert "run-status stale" in client.get(reverse(f"gsr:{screen}", args=[lesson.pk])).content.decode()
    assert "data-stale-banner" in client.get(reverse("gsr:export", args=[lesson.pk])).content.decode()
    assert report_payload(lesson, analysis, {}, "en")["stale"]
    rows = list(csv.DictReader(io.StringIO(vsr_moments_csv(report_data(lesson)).decode())))
    assert rows and all(row["stale"] == "True" for row in rows)

    cleaning = compute_cleaning(lesson)
    assert not is_stale(lesson, cleaning)
    assert is_stale(lesson, analysis) and is_stale(lesson, interviews)
    analysis = compute_analysis(lesson)
    assert not is_stale(lesson, analysis)
    assert is_stale(lesson, interviews) and export_is_stale(lesson)
    assert report_payload(lesson, analysis, {}, "en")["stale"]
    interviews = compute_interviews(lesson)
    assert not is_stale(lesson, interviews) and not export_is_stale(lesson)
    # Same settings, new upstream run: the later results still need refreshing.
    compute_cleaning(lesson)
    assert is_stale(lesson, analysis) and is_stale(lesson, interviews)


@pytest.mark.django_db
def test_pairs_can_be_added_and_removed(client, lesson) -> None:
    compute_analysis(lesson)
    pairs = [("S1", "S2"), ("S3", "S4")]
    for configured in (pairs, pairs[:1]):
        response = client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles(
            pair_left=[left for left, _ in configured], pair_right=[right for _, right in configured],
        ), **HTMX)
        assert response.status_code == 200
        lesson.refresh_from_db()
        assert lesson.interview_config["student_pairs"] == [list(pair) for pair in configured]
        result = get_result(lesson, AnalysisResult.KIND_VSR)
        assert [item["key"] for item in result.payload["interviews"]] == ["+".join(pair) for pair in configured] + ["T"]
        assert result.payload["roles"]["students"] == [alias for pair in configured for alias in pair]
        html = response.content.decode()
        assert html.count('name="pair_left"') == len(configured)
        assert "data-add-pair" in html and html.count("data-remove-pair") == len(configured)
        assert "video_start_time" not in html


@pytest.mark.django_db
def test_outdated_steps_point_to_the_first_outdated_step_and_refresh_in_order(client, lesson) -> None:
    from gsr.services.results import compute_cleaning, export_is_stale

    compute_cleaning(lesson)
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(lesson, {"student_pairs": [["S1", "S2"]]})
    lesson.save()
    compute_interviews(lesson)
    lesson.pipeline_config = {**lesson.pipeline_config, "median_kernel": 7}
    lesson.save()
    compute_analysis(lesson)

    cleaning_link = f'<a href="{reverse("gsr:cleaning", args=[lesson.pk])}">Cleaning</a> step is outdated'
    with translation.override("en"):
        for screen in ("analysis", "interviews"):
            html = client.get(reverse(f"gsr:{screen}", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
            assert cleaning_link in html
    client.post(reverse("gsr:export_rerun", args=[lesson.pk]))
    assert not export_is_stale(lesson)

    dataset = lesson.datasets.get(alias="S4")
    client.post(reverse("gsr:dataset_exclude", args=[lesson.pk, dataset.pk]), {"next": "analysis"})
    assert not is_stale(lesson, get_result(lesson, AnalysisResult.KIND_CLEANING))
    assert not is_stale(lesson, get_result(lesson, AnalysisResult.KIND_ANALYSIS))


@pytest.mark.django_db
def test_the_timeline_shows_every_candidate_and_the_whole_lesson_signals(client, lesson) -> None:
    compute_analysis(lesson)
    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles())
    result = get_result(lesson, AnalysisResult.KIND_VSR)
    interviews = result.payload["interviews"]
    for interview in interviews:
        # Every candidate of every step is stored, selected or not; the selected moments are among them.
        steps = [candidate["step"] for candidate in interview["candidates"]]
        assert steps == sorted(steps)
        assert {str(step): steps.count(step) for step in range(1, 6)} == interview["candidate_counts"]
        spans = {(item["step"], item["start_seconds"], item["end_seconds"]) for item in interview["candidates"]}
        assert all((moment["step"], moment["start_seconds"], moment["end_seconds"]) in spans
                   for moment in interview["moments"])
    assert any(len(interview["candidates"]) > len(interview["moments"]) for interview in interviews)

    with translation.override("en"):
        html = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    candidate_total = sum(len(interview["candidates"]) for interview in interviews)
    assert html.count('class="lt-candidate ') == candidate_total
    assert html.count("data-candidate-panel") == candidate_total
    assert html.count('class="lt-candidate other"') == html.count('class="candidate-detail other"') > 0
    assert html.count('class="lt-candidate primary"') + html.count('class="lt-candidate reserve"') == sum(
        len(interview["moments"]) for interview in interviews)
    assert html.count('class="lt-lane-label" title=') == 5 * len(interviews)
    assert html.count('name="step"') == html.count('class="candidate-detail other"') + html.count(
        'class="candidate-detail rejected"')
    assert html.count('data-chart="lessonSignals"') == len(interviews)
    charts = [json.loads(match) for match in re.findall(
        r'<script id="lesson-chart-[^"]+-data" type="application/json">(.*?)</script>', html)]
    for chart, interview in zip(charts, interviews):
        assert len(chart["candidates"]) == len(interview["candidates"])
        assert [len(series["points"]) for series in chart["series"]] == [len(result.payload["trace_seconds"])] * 2
        assert {candidate["state"] for candidate in chart["candidates"]} <= {"primary", "reserve", "other", "rejected"}
        # A moment on the list carries its number and card id so the chart can label it and scroll to its card.
        on_list = [candidate for candidate in chart["candidates"] if candidate["state"] in ("primary", "reserve")]
        assert len(on_list) == len(interview["moments"])
        assert all(candidate["number"] and f'id="moment-{candidate["moment_id"]}"' in html for candidate in on_list)
        assert all(candidate["moment_id"] is None for candidate in chart["candidates"] if candidate not in on_list)
    # The key to the shading is drawn on the chart in the UI language.
    assert {"candidatePrimary", "candidateReserve", "candidateOther", "candidateRejected"} <= set(
        json.loads(re.search(r'id="gsr-chart-labels" type="application/json">(.*?)</script>', html).group(1)))


@pytest.mark.django_db
def test_moments_chosen_before_the_candidates_were_kept_ask_to_choose_again(client, lesson) -> None:
    compute_analysis(lesson)
    client.post(reverse("gsr:interviews", args=[lesson.pk]), _roles())
    result = get_result(lesson, AnalysisResult.KIND_VSR)
    for interview in result.payload["interviews"]:
        del interview["candidates"]
    result.save()
    with translation.override("en"):
        html = client.get(reverse("gsr:interviews", args=[lesson.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert "Choose again to see all the moments the Analysis found." in html
    assert 'class="lt-candidate' not in html and 'class="lt-moment' in html
