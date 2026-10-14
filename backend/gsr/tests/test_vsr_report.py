"""The VSR interview moments in the PDF report, the CSV export and the Export screen."""

from __future__ import annotations

import csv
import io
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from gsr.models import AnalysisResult, Project
from gsr.pipeline.vsr import aggregate_student_signal
from gsr.reporting.report import render_report_html, render_report_pdf
from gsr.reporting.vsr_charts import lesson_timeline_svg, moment_chart_svg
from gsr.services.datasets import add_dataset
from gsr.services.exports import VSR_MOMENT_COLUMNS, vsr_moments_csv
from gsr.services.interview import apply_interview_config
from gsr.services.results import compute_analysis, get_result
from gsr.services.vsr import compute_interviews, report_data
from gsr.views import report_payload

from .sample_data import Participant, write_group
from .test_report import _page_count

FAST = {"synchrony_surrogate_count": 20, "contagion_surrogate_count": 20}


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


@pytest.fixture()
def proposed(lesson: Project) -> Project:
    compute_analysis(lesson)
    lesson.interview_config = apply_interview_config(
        lesson,
        {"teacher_alias": "T", "student_pairs": [["S1", "S2"], ["S3", "S4"]], "video_start_time": "11:59:00"},
    )
    lesson.save()
    compute_interviews(lesson)
    return lesson


class _Texts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.skip = False

    def handle_starttag(self, tag, attrs) -> None:
        self.skip = tag == "style"

    def handle_endtag(self, tag) -> None:
        self.skip = False

    def handle_data(self, data) -> None:
        if not self.skip and data.strip():
            self.texts.append(data.strip())


def _texts(page: str) -> list[str]:
    collector = _Texts()
    collector.feed(page)
    return collector.texts


def _page(project: Project, lang: str) -> str:
    return render_report_html(report_payload(project, get_result(project, AnalysisResult.KIND_ANALYSIS), {}, lang))


def _moment_count(project: Project) -> int:
    return sum(len(interview["moments"]) for interview in report_data(project, with_traces=False)["interviews"])


@pytest.mark.django_db
def test_polish_report_lists_every_interview_with_times_and_document_justifications(proposed) -> None:
    page = _page(proposed, "pl")
    texts = _texts(page)
    for heading in ("Momenty do wywiadów VSR", "Para S1 + S2", "Para S3 + S4", "Nauczyciel T",
                    "Kryteria z dokumentu", "Parametry wyboru", "Oś lekcji", "Wybrane momenty",
                    "Uzasadnienie każdego momentu", "8. Momenty do wywiadów VSR"):
        assert heading in texts, heading

    data = report_data(proposed, with_traces=False)
    first = data["interviews"][0]["moments"][0]
    assert first["start_video"] in texts or f"{first['start_video']}–{first['end_video']}" in texts
    assert f"{first['start_clock']}–{first['end_clock']}" in texts
    # Justifications quote the document verbatim, with its page; page 1 gives the data basis.
    assert "Points for discussion are selected based on the dyad’s GSR data" in page
    assert page.count('<q class="doc-quote" lang="en">') > _moment_count(proposed)
    assert "Select this moment for interview." in page and "KROK 1" in texts
    # The teacher is drawn against the students' average.
    assert "Średnia uczniów" in page
    # One card per moment with its mini-chart, plus one timeline per interview.
    assert page.count('class="moment-card') == _moment_count(proposed)
    assert page.count('class="vsr-chart"') == _moment_count(proposed) + len(data["interviews"])


@pytest.mark.django_db
def test_summary_tile_and_contents_list_the_interview_section(proposed) -> None:
    page = _page(proposed, "pl")
    stats = report_data(proposed, with_traces=False)["stats"]
    texts = _texts(page)
    assert "Wywiady VSR" in texts
    tile = texts[texts.index("Wywiady VSR") + 1]
    assert tile in ("3 × 5 + 3", f"{stats['primary_total']} + {stats['reserve_total']}")
    assert 'href="#interviews"' in page and 'id="interviews"' in page
    assert page.index('id="coarousal"') < page.index('id="interviews"') < page.index('id="methodology"')


@pytest.mark.django_db
def test_english_report_uses_english_sentences_and_headings(proposed) -> None:
    page = _page(proposed, "en")
    texts = _texts(page)
    for heading in ("Moments for VSR interviews", "Pair S1 + S2", "Teacher T", "Criteria from the document",
                    "Lesson timeline", "Selected moments", "8. Moments for VSR interviews", "VSR interviews"):
        assert heading in texts, heading
    assert "STEP 1" in texts and "Students' average" in page
    assert "Momenty do wywiadów" not in page and "KROK" not in page


@pytest.mark.django_db
def test_report_without_proposals_has_no_interview_section(lesson) -> None:
    compute_analysis(lesson)
    assert report_data(lesson) is None
    page = _page(lesson, "pl")
    assert 'id="interviews"' not in page and "Wywiady VSR" not in page and "8. Momenty" not in page


@pytest.mark.django_db
def test_report_warns_when_the_proposals_are_outdated(proposed) -> None:
    proposed.interview_config = apply_interview_config(proposed, {"vsr": {"vsr_sync_threshold": 0.7}})
    proposed.save()
    texts = _texts(_page(proposed, "pl"))
    assert any(text.startswith("UWAGA: uczestnicy wywiadów") for text in texts)
    assert "propozycje nieaktualne" in texts


@pytest.mark.django_db
def test_charts_are_left_out_when_the_analysis_was_rerun_after_the_proposals(proposed) -> None:
    compute_analysis(proposed)  # a new run: the proposals were computed from another one
    data = report_data(proposed)
    assert all(interview["trace"] is None for interview in data["interviews"])
    page = _page(proposed, "pl")
    assert page.count('class="vsr-chart"') == len(data["interviews"])  # the timelines remain
    assert page.count('class="moment-card') == _moment_count(proposed)


@pytest.mark.django_db
def test_traces_are_the_signals_the_proposals_were_computed_from(proposed) -> None:
    data = report_data(proposed)
    teacher = next(interview for interview in data["interviews"] if interview["kind"] == "teacher")
    pair = next(interview for interview in data["interviews"] if interview["key"] == "S1+S2")
    assert teacher["colors"][1] is None and all(pair["colors"])
    for interview in (teacher, pair):
        trace = interview["trace"]
        assert len(trace["seconds"]) == len(trace["a"]) == len(trace["b"]) > 100
        assert abs(float(np.nanmean(trace["a"]))) < 1e-9 and abs(float(np.nanstd(trace["a"])) - 1) < 1e-6
    students = [next(i for i in data["interviews"] if i["key"] == key)["trace"] for key in ("S1+S2", "S3+S4")]
    expected = aggregate_student_signal([students[0]["a"], students[0]["b"], students[1]["a"], students[1]["b"]])
    assert np.allclose(teacher["trace"]["b"], expected)


def test_lesson_timeline_numbers_the_moments_and_marks_reserves() -> None:
    moments = [
        {"number": 1, "status": "primary", "start_seconds": 60.0, "end_seconds": 100.0},
        {"number": 2, "status": "reserve", "start_seconds": 300.0, "end_seconds": 330.0},
    ]
    svg = lesson_timeline_svg(moments, 0.0, 600.0, 'Oś "lekcji"')
    assert svg.startswith("<svg") and svg.count("<rect") == 3 and ">1</text>" in svg and ">2</text>" in svg
    assert "0:00" in svg and "10:00" in svg and "&quot;lekcji&quot;" in svg


def test_moment_chart_draws_both_signals_around_the_moment() -> None:
    seconds = np.arange(0.0, 300.0, 1.0)
    trace = {"seconds": seconds, "a": np.sin(seconds / 20.0), "b": np.cos(seconds / 20.0)}
    moment = {"number": 3, "start_seconds": 100.0, "end_seconds": 140.0}
    svg = moment_chart_svg(trace, moment, ["S1", "Średnia uczniów"], ["#1b9e77", None], "label")
    assert svg is not None and svg.count("<polyline") == 2 and "S1" in svg and "Średnia uczniów" in svg
    assert "1:40" in svg and "2:20" in svg  # the moment's own start and end
    assert moment_chart_svg(trace, {"number": 4, "start_seconds": 900.0, "end_seconds": 940.0}, ["a", "b"],
                            ["#000000", "#000000"], "label") is None


@pytest.mark.django_db
def test_vsr_moments_csv_has_one_row_per_moment_with_times_and_justification(proposed) -> None:
    rows = list(csv.DictReader(io.StringIO(vsr_moments_csv(report_data(proposed, with_traces=False)).decode())))
    assert tuple(rows[0]) == VSR_MOMENT_COLUMNS
    assert len(rows) == _moment_count(proposed)
    assert {row["interview"] for row in rows} == {"S1+S2", "S3+S4", "T"}
    first = rows[0]
    assert first["interview_type"] == "dyad" and first["participants"] == "S1; S2" and first["number"] == "1"
    assert first["status"] in ("primary", "reserve") and first["video_start"] and first["clock_start"]
    assert float(first["lesson_end_s"]) > float(first["lesson_start_s"])
    assert first["justification"].startswith("Document (p. 1)") and "STEP" in first["justification"]
    teacher_rows = [row for row in rows if row["interview"] == "T"]
    assert teacher_rows[0]["interview_type"] == "teacher" and teacher_rows[0]["participants"].startswith("T; S1")


@pytest.mark.django_db
def test_export_offers_the_csv_and_the_zip_bundles_it(client, proposed) -> None:
    response = client.get(reverse("gsr:export_file", args=[proposed.pk, "vsr_moments.csv"]))
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert "lekcja_vsr_moments.csv" in response["Content-Disposition"]
    assert len(list(csv.DictReader(io.StringIO(response.content.decode())))) == _moment_count(proposed)

    archive = zipfile.ZipFile(io.BytesIO(client.get(reverse("gsr:export_zip", args=[proposed.pk])).content))
    assert "vsr_moments.csv" in archive.namelist()
    assert archive.read("vsr_moments.csv").decode().startswith(",".join(VSR_MOMENT_COLUMNS))

    page = client.get(reverse("gsr:export", args=[proposed.pk])).content.decode()
    assert "lekcja_vsr_moments.csv" in page and "Momenty do wywiadów VSR" in page


@pytest.mark.django_db
def test_export_without_proposals_has_no_vsr_file(client, lesson) -> None:
    compute_analysis(lesson)
    assert client.get(reverse("gsr:export_file", args=[lesson.pk, "vsr_moments.csv"])).status_code == 404
    archive = zipfile.ZipFile(io.BytesIO(client.get(reverse("gsr:export_zip", args=[lesson.pk])).content))
    assert "vsr_moments.csv" not in archive.namelist()
    page = client.get(reverse("gsr:export", args=[lesson.pk])).content.decode()
    assert "vsr_moments.csv" not in page


@pytest.mark.django_db
def test_pdf_with_the_interview_section_renders_in_both_languages(client, proposed) -> None:
    url = reverse("gsr:export_pdf", args=[proposed.pk])
    for response in (client.get(url), client.get(url, {"lang": "en"})):
        assert response.status_code == 200 and response.content.startswith(b"%PDF")

    payload = report_payload(proposed, get_result(proposed, AnalysisResult.KIND_ANALYSIS), {}, "pl")
    assert _page_count(render_report_pdf(payload)) > _page_count(render_report_pdf({**payload, "vsr": None}))
