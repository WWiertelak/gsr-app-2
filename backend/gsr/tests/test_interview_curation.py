"""The researcher's curation of the VSR moments: decisions, roles from the list order, choosing again, PL/EN."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone, translation

from gsr.models import AnalysisResult, InterviewMoment, Project
from gsr.pipeline.vsr import PRIMARY, RESERVE
from gsr.services import curation
from gsr.services.datasets import add_dataset
from gsr.services.interview import apply_interview_config
from gsr.services.results import compute_analysis
from gsr.services.vsr import compute_interviews

from .sample_data import Participant, write_group

FAST = {"synchrony_surrogate_count": 20, "contagion_surrogate_count": 20}
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def english():
    """The service messages below are matched in English; the app's default language is Polish."""
    with translation.override("en"):
        yield


@pytest.fixture()
def lesson(tmp_path: Path) -> Project:
    paths = write_group(
        tmp_path,
        "lesson",
        {
            "T": Participant(tonic_us=5.0),
            "S1": Participant(tonic_us=4.0),
            "S2": Participant(tonic_us=4.2, coupling=0.7),
        },
        {},
        seconds=600.0,
        seed=11,
    )
    project = Project.objects.create(name="Lekcja", pipeline_config=dict(FAST))
    for alias, path in paths.items():
        add_dataset(project, SimpleUploadedFile(path.name, path.read_bytes()), alias=alias)
    return project


@pytest.fixture()
def proposed(lesson: Project) -> tuple[Project, AnalysisResult]:
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(
        lesson,
        {
            "student_pairs": [["S1", "S2"]],
            "video_start_time": "11:59:00",
            # A 10-minute sample lesson: two primary and two reserve moments keep every role on the list.
            "primary_moment_count": 2,
            "reserve_moment_count": 2,
        },
    )
    lesson.save()
    return lesson, compute_interviews(lesson)


def _list(project: Project, result: AnalysisResult, key: str = "S1+S2") -> list[curation.CuratedMoment]:
    return curation.curated_interviews(project, result)[key]


def _free_span(project: Project, result: AnalysisResult, key: str = "S1+S2", length: float = 20.0) -> tuple[float, float]:
    """A lesson span no kept moment of the interview overlaps."""
    lesson = result.payload["lesson"]
    taken = sorted((row.start_seconds, row.end_seconds) for row in project.interview_moments.filter(
        interview_key=key).exclude(status=InterviewMoment.STATUS_REJECTED))
    start = lesson["start_seconds"] + 1
    for begin, end in taken:
        if start + length < begin:
            break
        start = max(start, end + 1)
    assert start + length < lesson["end_seconds"]
    return float(round(start)), float(round(start + length))


@pytest.mark.django_db
def test_proposing_creates_one_undecided_row_per_proposal_in_engine_order(proposed) -> None:
    project, result = proposed
    moments = result.payload["interviews"][0]["moments"]
    items = _list(project, result)
    assert [(item.row.start_seconds, item.row.end_seconds) for item in items] == [
        (moment["start_seconds"], moment["end_seconds"]) for moment in moments
    ]
    assert all(item.row.status == InterviewMoment.STATUS_PENDING for item in items)
    assert [item.role for item in items] == [moment["status"] for moment in moments]
    first = items[0]
    assert first.start_clock == moments[0]["start_clock"]
    assert not hasattr(first, "start_video")


@pytest.mark.django_db
def test_rejecting_a_primary_moment_moves_the_first_reserve_up(proposed) -> None:
    project, result = proposed
    items = _list(project, result)
    target = curation.primary_target(result)
    assert len(items) > target, "the sample lesson must yield reserve moments"
    first_reserve = items[target].row

    curation.set_status(items[0].row, InterviewMoment.STATUS_REJECTED)
    items = _list(project, result)
    assert items[-1].row.status == InterviewMoment.STATUS_REJECTED and items[-1].number is None
    promoted = next(item for item in items if item.row.pk == first_reserve.pk)
    assert promoted.role == PRIMARY and promoted.proposed_role == RESERVE
    assert promoted.number == target

    curation.set_status(items[-1].row, InterviewMoment.STATUS_PENDING)
    assert _list(project, result)[0].row.pk == items[-1].row.pk


@pytest.mark.django_db
def test_moving_a_moment_changes_its_place_and_role(proposed) -> None:
    project, result = proposed
    items = _list(project, result)
    target = curation.primary_target(result)
    reserve = items[target].row

    curation.move(reserve, -1)
    items = _list(project, result)
    assert items[target - 1].row.pk == reserve.pk and items[target - 1].role == PRIMARY
    assert items[target].role == RESERVE

    curation.move(items[0].row, -1)  # already first: stays
    assert _list(project, result)[0].row.pk == items[0].row.pk


@pytest.mark.django_db
def test_changing_the_time_needs_a_reason_unless_it_goes_back_to_the_proposal(proposed) -> None:
    project, result = proposed
    row = _list(project, result)[0].row
    start, end = row.original_start_seconds, row.original_end_seconds

    with pytest.raises(ValueError, match="reason"):
        curation.change_time(row, result, start + 5, end, "")
    curation.change_time(row, result, start + 5, end, "The pair starts talking 5 s later.")
    row.refresh_from_db()
    assert row.time_changed and row.start_seconds == round(start + 5, 1) and row.reason
    item = _list(project, result)[0]
    assert item.start_clock != result.payload["interviews"][0]["moments"][0]["start_clock"]
    assert item.start_clock is not None

    # Whole seconds in the form: within half a second of the proposal is the proposal.
    curation.change_time(row, result, round(start), round(end), "")
    row.refresh_from_db()
    assert not row.time_changed and row.reason == ""


@pytest.mark.django_db
def test_times_must_stay_in_the_lesson_and_not_overlap_another_moment(proposed) -> None:
    project, result = proposed
    items = _list(project, result)
    lesson = result.payload["lesson"]
    first, second = items[0].row, items[1].row

    with pytest.raises(ValueError, match="end after"):
        curation.change_time(first, result, 30, 20, "x")
    with pytest.raises(ValueError, match="analysed period"):
        curation.change_time(first, result, lesson["start_seconds"], lesson["end_seconds"] + 60, "x")
    with pytest.raises(ValueError, match="overlaps"):
        curation.change_time(first, result, second.start_seconds, second.end_seconds, "x")

    curation.set_status(second, InterviewMoment.STATUS_REJECTED)
    curation.change_time(first, result, second.start_seconds, second.end_seconds, "Same span as the rejected one.")
    with pytest.raises(ValueError, match="overlaps"):
        curation.set_status(second, InterviewMoment.STATUS_PENDING)


@pytest.mark.django_db
def test_a_manual_moment_needs_a_reason_which_is_its_justification(proposed) -> None:
    project, result = proposed
    start, end = _free_span(project, result)
    with pytest.raises(ValueError, match="reason"):
        curation.add_manual(project, result, "S1+S2", start, end, "  ")
    with pytest.raises(ValueError, match="interview"):
        curation.add_manual(project, result, "S9+S8", start, end, "x")

    moment = curation.add_manual(project, result, "S1+S2", start, end, "The teacher joins the pair.", "Ask first")
    items = _list(project, result)
    kept = [item for item in items if item.number is not None]
    assert kept[-1].row.pk == moment.pk
    assert moment.is_manual and moment.status == InterviewMoment.STATUS_ACCEPTED
    assert kept[-1].start_clock is not None

    with pytest.raises(ValueError):
        curation.set_status(moment, InterviewMoment.STATUS_REJECTED)
    curation.remove_manual(moment)
    assert not InterviewMoment.objects.filter(pk=moment.pk).exists()
    with pytest.raises(ValueError):
        curation.remove_manual(items[0].row)


@pytest.mark.django_db
def test_proposing_again_keeps_every_decision_and_the_order(proposed) -> None:
    project, result = proposed
    items = _list(project, result)
    accepted, rejected, noted, moved = (item.row for item in items[:4])
    curation.set_status(accepted, InterviewMoment.STATUS_ACCEPTED)
    curation.set_status(rejected, InterviewMoment.STATUS_REJECTED)
    curation.set_note(noted, "Ask about the worksheet.")
    curation.change_time(moved, result, moved.start_seconds + 2, moved.end_seconds, "Starts with the question.")
    curation.move(noted, 1)
    start, end = _free_span(project, result)
    manual = curation.add_manual(project, result, "S1+S2", start, end, "Laughter in the video.")
    before = [(item.row.pk, item.row.status, item.row.note, item.row.start_seconds) for item in _list(project, result)]

    result = compute_interviews(project)
    after = [(item.row.pk, item.row.status, item.row.note, item.row.start_seconds) for item in _list(project, result)]
    assert after == before
    assert InterviewMoment.objects.filter(pk=manual.pk).exists()
    assert not project.interview_moments.filter(needs_check=True).exists()


def _fake_result(project: Project, moments: list[tuple[int, float, float]], key: str = "A+B",
                 start_clock: str = "2024-05-06T09:00:00") -> AnalysisResult:
    payload = {
        "counts": {"primary": 2, "reserve": 2},
        "lesson": {"start_seconds": 0.0, "end_seconds": 600.0, "start_clock": start_clock},
        "roles": {"video_start_time": "09:00:30"},
        "interviews": [{
            "key": key,
            "moments": [{"number": index + 1, "status": PRIMARY if index < 2 else RESERVE, "step": step,
                         "start_seconds": start, "end_seconds": end, "start_clock": None, "end_clock": None}
                        for index, (step, start, end) in enumerate(moments)],
        }],
    }
    result, _ = AnalysisResult.objects.update_or_create(
        project=project, kind=AnalysisResult.KIND_VSR,
        defaults={"signature": "x", "payload": payload, "computed_at": timezone.now()},
    )
    return result


@pytest.mark.django_db
def test_a_decided_moment_no_longer_proposed_stays_and_is_marked_for_checking() -> None:
    project = Project.objects.create(name="Sync")
    result = _fake_result(project, [(1, 10, 40), (2, 100, 130), (5, 200, 230), (4, 300, 330)])
    curation.sync_interview_moments(project, result)
    rows = {row.start_seconds: row for row in project.interview_moments.all()}
    curation.set_status(rows[100], InterviewMoment.STATUS_ACCEPTED)
    curation.move(rows[300], -1)  # order: 10, 100, 300, 200

    # The engine now proposes 10-40 and 300-330 again, drops 100-130 and 200-230, and adds two new moments.
    result = _fake_result(project, [(1, 10, 40), (3, 150, 180), (4, 300, 330), (5, 400, 430)])
    curation.sync_interview_moments(project, result)
    items = _list(project, result, "A+B")
    assert [item.row.start_seconds for item in items] == [10, 150, 100, 300, 400]
    by_start = {item.row.start_seconds: item.row for item in items}
    assert by_start[100].needs_check and by_start[100].status == InterviewMoment.STATUS_ACCEPTED
    assert not by_start[300].needs_check
    assert 200 not in by_start  # undecided and no longer proposed: replaced

    curation.set_status(by_start[100], InterviewMoment.STATUS_ACCEPTED)
    by_start[100].refresh_from_db()
    assert not by_start[100].needs_check


@pytest.mark.django_db
def test_the_lists_are_created_for_proposals_stored_before_curation() -> None:
    project = Project.objects.create(name="Legacy")
    result = _fake_result(project, [(1, 10, 40), (2, 100, 130)])
    # Proposals stored before the lesson clock was kept carry the clock only on their moments.
    del result.payload["lesson"]["start_clock"]
    for moment in result.payload["interviews"][0]["moments"]:
        moment["start_clock"] = f"09:{int(moment['start_seconds']) // 60:02d}:{int(moment['start_seconds']) % 60:02d}"
    result.save()
    assert not project.interview_moments.exists()
    curation.ensure_synced(project, result)
    items = _list(project, result, "A+B")
    assert [item.row.start_seconds for item in items] == [10, 100]
    # Sensor clock times of a moved moment come from the lesson clock read back from a proposed moment.
    curation.change_time(items[0].row, result, 60, 90, "Later")
    item = _list(project, result, "A+B")[0]
    assert (item.start_clock, item.end_clock) == ("09:01:00", "09:01:30")


@pytest.mark.django_db
def test_a_moved_moment_reads_the_clock_like_the_engine_from_a_sub_second_lesson_start() -> None:
    project = Project.objects.create(name="Clock")
    result = _fake_result(project, [(1, 10, 40)], start_clock="2024-05-06T10:00:00.700000")
    curation.sync_interview_moments(project, result)
    row = _list(project, result, "A+B")[0].row
    curation.change_time(row, result, 10.5, 40, "Earlier")
    item = _list(project, result, "A+B")[0]
    assert (item.start_clock, item.end_clock) == ("10:00:11", "10:00:40")


@pytest.mark.django_db
def test_a_moved_moment_reads_a_lesson_clock_stored_without_a_date() -> None:
    project = Project.objects.create(name="Bare clock")
    result = _fake_result(project, [(1, 10, 40)], start_clock="09:00:00")
    curation.sync_interview_moments(project, result)
    row = _list(project, result, "A+B")[0].row
    curation.change_time(row, result, 60, 90, "Later")
    item = _list(project, result, "A+B")[0]
    assert (item.start_clock, item.end_clock) == ("09:01:00", "09:01:30")


def test_lesson_times_parse_as_minutes_and_seconds() -> None:
    assert curation.parse_lesson_time("12:30") == 750
    assert curation.parse_lesson_time("1:02:03") == 3723
    assert curation.parse_lesson_time("95") == 95
    assert curation.parse_lesson_time("0:07,5") == 7.5
    for text in ("", "abc", "1:75", "-3"):
        with pytest.raises(ValueError):
            curation.parse_lesson_time(text)


@pytest.mark.django_db
def test_card_actions_post_through_htmx_and_without_javascript(client, proposed) -> None:
    project, result = proposed
    row = _list(project, result)[0].row
    url = reverse("gsr:interview_moment", args=[project.pk, row.pk])

    with translation.override("en"):
        response = client.post(url, {"action": "accept", "tab": "interview-1"}, HTTP_ACCEPT_LANGUAGE="en", **HTMX)
    html = response.content.decode()
    assert response.status_code == 200
    assert f'id="moment-{row.pk}"' in html and "Undo acceptance" in html
    row.refresh_from_db()
    assert row.status == InterviewMoment.STATUS_ACCEPTED

    response = client.post(url, {"action": "reject", "tab": "interview-1"})
    assert response.status_code == 302 and response["Location"].endswith(f"?tab=interview-1#moment-{row.pk}")
    row.refresh_from_db()
    assert row.status == InterviewMoment.STATUS_REJECTED

    assert client.post(url, {"action": "explode"}).status_code == 404


@pytest.mark.django_db
def test_a_failed_form_comes_back_open_with_its_error(client, proposed) -> None:
    project, result = proposed
    row = _list(project, result)[0].row
    url = reverse("gsr:interview_moment", args=[project.pk, row.pk])
    with translation.override("en"):
        response = client.post(url, {"action": "time", "start": "0:01", "end": "0:20", "reason": ""},
                               HTTP_ACCEPT_LANGUAGE="en", **HTMX)
    html = response.content.decode()
    assert "Give the reason for the change." in html
    assert 'value="0:01"' in html

    start, end = _free_span(project, result)
    add = reverse("gsr:interview_moment_add", args=[project.pk])
    with translation.override("pl"):
        response = client.post(add, {"interview": "S1+S2", "start": f"{int(start)}", "end": f"{int(end)}",
                                     "reason": ""}, HTTP_ACCEPT_LANGUAGE="pl", **HTMX)
    assert "Podaj powód dodania momentu." in response.content.decode()

    response = client.post(add, {"interview": "S1+S2", "start": f"{int(start)}", "end": f"{int(end)}",
                                 "reason": "Dyskusja o zadaniu"}, HTTP_ACCEPT_LANGUAGE="pl", **HTMX)
    html = response.content.decode()
    assert "Dodany przez badacza:" in html and "Dyskusja o zadaniu" in html


@pytest.mark.django_db
def test_card_actions_need_proposals(client, lesson) -> None:
    url = reverse("gsr:interview_moment_add", args=[lesson.pk])
    assert client.post(url, {"interview": "S1+S2"}).status_code == 404


@pytest.mark.django_db
def test_removed_pairs_archive_decisions_and_restore_them_when_added_again(proposed) -> None:
    project, result = proposed
    moment = _list(project, result)[0].row
    curation.set_status(moment, InterviewMoment.STATUS_ACCEPTED)
    curation.set_note(moment, "Keep this question")
    for pairs in ([["S1", "T"]], [["S1", "S2"]]):
        project.interview_config = apply_interview_config(project, {"student_pairs": pairs})
        project.save()
        result = compute_interviews(project)
        moment.refresh_from_db()
        assert moment.status == InterviewMoment.STATUS_ACCEPTED and moment.note == "Keep this question"
        assert list(curation.curated_interviews(project, result)) == ["+".join(pairs[0])]
    assert any(item.row.pk == moment.pk for item in _list(project, result))


@pytest.mark.django_db
def test_video_migration_preserves_sensor_times_and_researcher_decisions(proposed) -> None:
    from importlib import import_module

    from django.apps import apps

    project, result = proposed
    moment = _list(project, result)[0].row
    curation.set_status(moment, InterviewMoment.STATUS_ACCEPTED)
    project.interview_config["video_start_time"] = "11:59:00"
    project.save()
    result.config["video_start_time"] = "11:59:00"
    result.payload["roles"]["video_start_time"] = "11:59:00"
    result.payload["interviews"][0]["moments"][0]["start_video"] = "0:01:00"
    result.save()
    moment.proposal["start_video"] = "0:01:00"
    moment.proposal["end_video"] = "0:02:00"
    moment.save()
    old_clock = moment.proposal["start_clock"]
    old_time = result.computed_at
    migration = import_module("gsr.migrations.0006_remove_video_times")
    migration.forwards(apps, None)
    project.refresh_from_db()
    result.refresh_from_db()
    moment.refresh_from_db()
    assert "video_start_time" not in project.interview_config
    assert "video_start_time" not in result.config and "video_start_time" not in result.payload["roles"]
    assert "start_video" not in result.payload["interviews"][0]["moments"][0]
    assert "start_video" not in moment.proposal and "end_video" not in moment.proposal
    assert moment.proposal["start_clock"] == old_clock
    assert moment.status == InterviewMoment.STATUS_ACCEPTED
    assert result.computed_at == old_time
