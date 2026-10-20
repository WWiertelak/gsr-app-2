"""The VSR interview moments in the PDF report, the CSV export and the Export screen."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import translation

from gsr.models import AnalysisResult, InterviewMoment, Project
from gsr.pipeline.vsr import aggregate_student_signal
from gsr.reporting import interviews
from gsr.reporting.report import render_report_html, render_report_pdf
from gsr.reporting.vsr_charts import lesson_timeline_svg, moment_chart_svg
from gsr.services import curation
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
    return sum(len(interview["moments"]) for interview in report_data(project)["interviews"])


@pytest.mark.django_db
def test_polish_report_lists_every_interview_with_times_previews_and_one_sentence_each(proposed) -> None:
    page = _page(proposed, "pl")
    texts = _texts(page)
    assert "Wideo" not in texts and "Start nagrania wideo (zegar czujnika)" not in texts
    for heading in ("Momenty do wywiadów VSR", "Para S1 + S2", "Para S3 + S4", "Nauczyciel T", "Kroki schematu VSR",
                    "Źródło: Analiza", "Oś lekcji", "Wybrane momenty", "Podgląd i uzasadnienie każdego momentu",
                    "8. Momenty do wywiadów VSR"):
        assert heading in texts, heading

    data = report_data(proposed)
    first = data["interviews"][0]["moments"][0]
    assert f"{first['start_clock']}–{first['end_clock']}" in texts
    # One plain Polish sentence per moment, built from the Analysis values; no English quotes of the document.
    sentences = [text for text in texts if text.startswith("Krok ") and "schematu VSR:" in text]
    assert len(sentences) == _moment_count(proposed)
    assert all("r = " in sentence for sentence in sentences)
    assert "doc-quote" not in page and "Points for discussion" not in page and "Select this moment" not in page
    # The source table states the Analysis settings in the Analysis' own labels.
    assert texts[texts.index("Próg synchronizacji") + 1] == "0,6" and "Okno (s)" in texts
    # The teacher is drawn against the students' average.
    assert "Średnia uczniów" in page
    # One card per moment with its preview, plus one timeline per interview.
    assert page.count('class="moment-card') == _moment_count(proposed)
    assert page.count('class="vsr-chart"') == _moment_count(proposed) + len(data["interviews"])


@pytest.mark.django_db
def test_summary_tile_and_contents_list_the_interview_section(proposed) -> None:
    page = _page(proposed, "pl")
    data = report_data(proposed)
    stats = data["stats"]
    counts = {(item["stats"]["primary_count"], item["stats"]["reserve_count"]) for item in data["interviews"]}
    texts = _texts(page)
    assert "Wywiady VSR" in texts
    tile = texts[texts.index("Wywiady VSR") + 1]
    # Interviews × primary + reserve when every interview has the same numbers, the totals otherwise.
    expected = (f"3 × {next(iter(counts))[0]} + {next(iter(counts))[1]}" if len(counts) == 1
                else f"{stats['primary_total']} + {stats['reserve_total']}")
    assert tile == expected
    assert 'href="#interviews"' in page and 'id="interviews"' in page
    assert page.index('id="coarousal"') < page.index('id="interviews"') < page.index('id="methodology"')


@pytest.mark.django_db
def test_english_report_uses_english_sentences_and_headings(proposed) -> None:
    page = _page(proposed, "en")
    texts = _texts(page)
    for heading in ("Moments for VSR interviews", "Pair S1 + S2", "Teacher T", "Steps of the VSR flow chart",
                    "Source: the Analysis", "Lesson timeline", "Chosen moments", "8. Moments for VSR interviews",
                    "VSR interviews"):
        assert heading in texts, heading
    assert "VSR flow chart step" in page and "Students' average" in page
    assert "Momenty do wywiadów" not in page and "schematu VSR" not in page
    # The source table states the correlation thresholds the moments were chosen with: Analysis settings.
    assert texts[texts.index("Interview moment correlation (|r| at least)") + 1] == "0.8"
    assert texts[texts.index("Decoupling threshold (|r| <)") + 1] == "0.1"
    assert texts[texts.index("Divergent synchrony threshold (r ≤ −)") + 1] == "0.3"


@pytest.mark.django_db
def test_report_without_proposals_has_no_interview_section(lesson) -> None:
    compute_analysis(lesson)
    assert report_data(lesson) is None
    page = _page(lesson, "pl")
    assert 'id="interviews"' not in page and "Wywiady VSR" not in page and "8. Momenty" not in page


@pytest.mark.django_db
def test_report_warns_when_the_proposals_are_outdated(proposed) -> None:
    proposed.interview_config = apply_interview_config(proposed, {"primary_moment_count": 4})
    proposed.save()
    texts = _texts(_page(proposed, "pl"))
    assert any(text.startswith("Uczestnicy wywiadów, liczba momentów lub Analiza") for text in texts)
    assert any("UWAGA" in text for text in texts)  # the whole export is now outdated too
    assert "momenty nieaktualne" in texts


@pytest.mark.django_db
def test_previews_come_from_the_signals_stored_with_the_moments(proposed) -> None:
    compute_analysis(proposed)  # a new run with the same settings: the stored signals still match the moments
    data = report_data(proposed)
    assert all(moment["preview"] for interview in data["interviews"] for moment in interview["moments"])
    page = _page(proposed, "pl")
    assert page.count('class="vsr-chart"') == _moment_count(proposed) + len(data["interviews"])


@pytest.mark.django_db
def test_stored_signals_are_the_ones_the_interviews_compare(proposed) -> None:
    payload = get_result(proposed, AnalysisResult.KIND_VSR).payload
    data = report_data(proposed)
    teacher = next(interview for interview in data["interviews"] if interview["kind"] == "teacher")
    pair = next(interview for interview in data["interviews"] if interview["key"] == "S1+S2")
    assert teacher["colors"][1] is None and all(pair["colors"])
    for interview in (teacher, pair):
        trace = interview["trace"]
        assert len(payload["trace_seconds"]) == len(trace["a"]) == len(trace["b"]) > 100
        assert abs(float(np.mean(trace["a"]))) < 1e-2 and abs(float(np.std(trace["a"])) - 1) < 1e-2
    students = [next(i for i in data["interviews"] if i["key"] == key)["trace"] for key in ("S1+S2", "S3+S4")]
    expected = aggregate_student_signal([students[0]["a"], students[0]["b"], students[1]["a"], students[1]["b"]])
    assert np.allclose(teacher["trace"]["b"], expected, atol=1e-2)
    moment = pair["moments"][0]
    preview = moment["preview"]
    assert preview["low"] <= moment["start_seconds"] < moment["end_seconds"] <= preview["high"]
    assert preview["seconds"][0] >= preview["low"] and preview["seconds"][-1] <= preview["high"]


def test_lesson_timeline_numbers_the_moments_and_marks_reserves() -> None:
    moments = [
        {"number": 1, "status": "primary", "start_seconds": 60.0, "end_seconds": 100.0},
        {"number": 2, "status": "reserve", "start_seconds": 300.0, "end_seconds": 330.0},
    ]
    svg = lesson_timeline_svg(moments, 0.0, 600.0, 'Oś "lekcji"')
    assert svg.startswith("<svg") and svg.count("<rect") == 3 and ">1</text>" in svg and ">2</text>" in svg
    assert "0:00" in svg and "10:00" in svg and "&quot;lekcji&quot;" in svg
    # With the recordings' clock every tick also gives the clock time.
    clocked = lesson_timeline_svg(moments, 0.0, 600.0, "Oś", datetime(1900, 1, 1, 10, 8, 26, 296000))
    assert ">10:08:26</text>" in clocked and ">10:18:26</text>" in clocked and ">10:00</text>" in clocked


def test_moment_chart_draws_both_signals_around_the_moment() -> None:
    seconds = np.arange(70.0, 170.0, 1.0)
    window = {"seconds": seconds, "a": np.sin(seconds / 20.0), "b": np.cos(seconds / 20.0), "start": 100.0,
              "end": 140.0, "low": 70.0, "high": 169.0}
    svg = moment_chart_svg(window, ["S1", "Średnia uczniów"], ["#1b9e77", None], "label", 3, high=1.0)
    assert svg is not None and svg.count("<polyline") == 2 and "S1" in svg and "Średnia uczniów" in svg
    assert "1:40" in svg and "2:20" in svg  # the moment's own start and end
    assert ">3</text>" in svg  # the moment's number on its shading
    assert 'stroke="#b2182b"' in svg  # the High threshold
    assert moment_chart_svg({**window, "seconds": seconds[:1]}, ["a", "b"], ["#000000", "#000000"], "x", 1) is None
    # On the recordings' clock, as the Interviews step's chart.
    clocked = moment_chart_svg(window, ["S1", "S2"], ["#1b9e77", "#d95f02"], "label", 3,
                               clock=datetime(1900, 1, 1, 10, 0, 0))
    assert ">10:01:40</text>" in clocked and ">10:02:20</text>" in clocked and ">1:40</text>" not in clocked


@pytest.mark.django_db
def test_vsr_moments_csv_has_one_row_per_moment_with_times_and_justification(proposed) -> None:
    rows = list(csv.DictReader(io.StringIO(vsr_moments_csv(report_data(proposed)).decode())))
    assert tuple(rows[0]) == VSR_MOMENT_COLUMNS
    assert len(rows) == _moment_count(proposed)
    assert {row["interview"] for row in rows} == {"S1+S2", "S3+S4", "T"}
    first = rows[0]
    assert first["interview_type"] == "dyad" and first["participants"] == "S1; S2" and first["number"] == "1"
    assert first["status"] in ("primary", "reserve") and first["clock_start"]
    assert "video_start" not in first and "video_end" not in first
    assert float(first["lesson_end_s"]) > float(first["lesson_start_s"])
    assert first["justification"].startswith(f"VSR flow chart step {first['step']}: ")
    assert first["step_name"] and (first["analysis_episode"] or first["step"] == "5")
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
def test_outdated_proposals_are_marked_in_the_csv_and_on_the_export_screen(client, proposed) -> None:
    def csv_stale() -> set[str]:
        content = client.get(reverse("gsr:export_file", args=[proposed.pk, "vsr_moments.csv"])).content.decode()
        return {row["stale"] for row in csv.DictReader(io.StringIO(content))}

    def vsr_row_marker() -> str:
        texts = _texts(client.get(reverse("gsr:export", args=[proposed.pk])).content.decode())
        return texts[texts.index("Momenty do wywiadów VSR", texts.index("Pobierz wszystko (ZIP)")) + 1]

    assert csv_stale() == {"False"}
    assert vsr_row_marker() != "nieaktualne"

    proposed.interview_config = apply_interview_config(
        proposed, {"teacher_alias": "T", "student_pairs": [["S1", "S3"]], "video_start_time": "11:59:00"},
    )
    proposed.save()
    assert csv_stale() == {"True"}
    archive = zipfile.ZipFile(io.BytesIO(client.get(reverse("gsr:export_zip", args=[proposed.pk])).content))
    assert {row["stale"] for row in csv.DictReader(io.StringIO(archive.read("vsr_moments.csv").decode()))} == {"True"}
    assert vsr_row_marker() == "nieaktualne"


@pytest.mark.django_db
def test_moments_of_an_outdated_analysis_are_marked_outdated(proposed) -> None:
    assert not report_data(proposed)["stale"]
    proposed.pipeline_config = {**proposed.pipeline_config, "synchrony_threshold": 0.5}
    proposed.save()
    assert report_data(proposed)["stale"]


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


def _free_window(rows, length: float = 20.0) -> tuple[float, float]:
    """A stretch of the lesson no kept moment of the interview covers."""
    kept = [row for row in rows if row.status != InterviewMoment.STATUS_REJECTED]
    for start in np.arange(10.0, 560.0, 10.0):
        if all(start + length <= row.start_seconds or row.end_seconds <= start for row in kept):
            return float(start), float(start + length)
    raise AssertionError("the lesson has no free stretch")


@pytest.fixture()
def curated(proposed: Project) -> Project:
    """S1+S2 after the researcher rejected a moment, accepted one, retimed one, added one and wrote a note."""
    rows = list(proposed.interview_moments.filter(interview_key="S1+S2").order_by("position"))
    assert len(rows) >= 3
    curation.set_status(rows[0], InterviewMoment.STATUS_REJECTED)
    curation.set_status(rows[1], InterviewMoment.STATUS_ACCEPTED)
    result = get_result(proposed, AnalysisResult.KIND_VSR)
    start, end = rows[2].start_seconds, rows[2].end_seconds
    curation.change_time(rows[2], result, start + 5.0, end - 2.0, "the exchange starts a little later")
    curation.set_note(rows[2], "Ask about the group task")
    window = _free_window(proposed.interview_moments.filter(interview_key="S1+S2"))
    curation.add_manual(proposed, result, "S1+S2", *window, "the teacher stood next to the pair")
    return proposed


def _s1_s2(project: Project) -> list[dict]:
    interview = next(i for i in report_data(project)["interviews"] if i["key"] == "S1+S2")
    return interview["moments"]


@pytest.mark.django_db
def test_report_data_follows_the_researchers_list_without_rejected_moments(curated) -> None:
    proposals = next(i for i in get_result(curated, AnalysisResult.KIND_VSR).payload["interviews"]
                     if i["key"] == "S1+S2")["moments"]
    moments = _s1_s2(curated)
    assert len(moments) == len(proposals)  # one rejected, one added
    assert [moment["number"] for moment in moments] == list(range(1, len(moments) + 1))
    assert [moment["status"] for moment in moments] == ["primary"] * min(5, len(moments)) + ["reserve"] * max(
        len(moments) - 5, 0)
    assert proposals[0]["start_seconds"] not in [m["start_seconds"] for m in moments if m["source"] == "proposed"]
    by_decision = {moment["decision"]: moment for moment in moments if moment["source"] == "proposed"}
    assert by_decision["accepted"]["start_seconds"] == proposals[1]["start_seconds"]
    retimed = next(moment for moment in moments if moment["time_changed"])
    assert retimed["reason"] == "the exchange starts a little later" and retimed["note"] == "Ask about the group task"
    assert retimed["original_start_seconds"] == proposals[2]["start_seconds"]
    assert retimed["start_seconds"] == proposals[2]["start_seconds"] + 5.0
    assert retimed["start_clock"] and retimed["proposal"]["start_seconds"] == proposals[2]["start_seconds"]
    manual = next(moment for moment in moments if moment["source"] == "manual")
    assert manual["decision"] == "manual" and manual["step"] is None and manual["correlation"] is None
    assert manual["reason"] == "the teacher stood next to the pair" and manual["start_clock"]
    interview = next(i for i in report_data(curated)["interviews"] if i["key"] == "S1+S2")
    assert interview["rejected_count"] == 1 and interview["stats"]["primary_count"] == min(5, len(moments))


@pytest.mark.django_db
def test_csv_lists_accepted_retimed_and_manual_moments_with_reasons_and_notes(curated) -> None:
    rows = list(csv.DictReader(io.StringIO(vsr_moments_csv(report_data(curated)).decode())))
    own = [row for row in rows if row["interview"] == "S1+S2"]
    assert [row["number"] for row in own] == [str(n) for n in range(1, len(own) + 1)]
    assert {row["decision"] for row in own} == {"undecided", "accepted", "manual"}
    retimed = next(row for row in own if row["time_changed"] == "True")
    assert retimed["reason"] == "the exchange starts a little later" and retimed["note"] == "Ask about the group task"
    assert float(retimed["lesson_start_s"]) == float(retimed["proposed_start_s"]) + 5.0
    # The proposed times on the sensor clock too, as the proposal stored them.
    proposal = next(moment for interview in report_data(curated)["interviews"] if interview["key"] == "S1+S2"
                    for moment in interview["moments"] if moment["time_changed"])["proposal"]
    assert (retimed["proposed_clock_start"], retimed["proposed_clock_end"]) == (proposal["start_clock"],
                                                                                proposal["end_clock"])
    manual = next(row for row in own if row["source"] == "manual")
    assert manual["justification"] == "Added by the researcher: the teacher stood next to the pair"
    assert manual["step"] == "" and manual["correlation"] == "" and manual["proposed_start_s"] == ""
    assert manual["proposed_clock_start"] == ""
    assert manual["clock_start"]
    # A proposed moment's step stays a whole number although a manual row leaves the column empty there.
    assert {row["step"] for row in own if row["source"] == "proposed"} <= {"1", "2", "3", "4", "5"}
    assert {row["interview"] for row in rows} == {"S1+S2", "S3+S4", "T"}


@pytest.mark.django_db
def test_report_shows_the_curated_list_in_both_languages(curated) -> None:
    pl = _page(curated, "pl")
    texts = _texts(pl)
    assert "the teacher stood next to the pair" in pl and "Ask about the group task" in pl
    assert "Dodany ręcznie" in texts and "Dodany przez badacza:" in pl
    assert "Odrzucone przez badacza i pominięte w raporcie: 1." in texts
    assert any(text.startswith("Zaproponowano ") and "(zegar czujnika " in text
               and "czas zmieniony przez badacza:" in text for text in texts)
    assert any(text.startswith("Lista momentów to lista badacza") for text in texts)
    assert "zaakceptowany" in texts and "zaakceptowany, czas zmieniony" not in texts
    assert "zaakceptowany" in pl and "czas zmieniony" in pl
    en = _page(curated, "en")
    assert "Added by the researcher:" in en and "Rejected by the researcher and left out of the report: 1." in _texts(en)
    assert pl.count('class="moment-card') == _moment_count(curated)


@pytest.mark.django_db
def test_undecided_lists_are_not_called_the_researchers_list(proposed) -> None:
    assert proposed.interview_moments.exists()
    data = report_data(proposed)
    assert not data["curated"] and not any(interview["curated"] for interview in data["interviews"])
    for interview, original in zip(data["interviews"], get_result(proposed, AnalysisResult.KIND_VSR).payload["interviews"]):
        assert [(m["start_seconds"], m["end_seconds"], m["status"]) for m in interview["moments"]] == [
            (m["start_seconds"], m["end_seconds"], m["status"]) for m in original["moments"]]
        assert {m["decision"] for m in interview["moments"]} == {"undecided"} and interview["rejected_count"] == 0
    assert "Lista momentów to lista badacza" not in "".join(_texts(_page(proposed, "pl")))
    assert "researcher's list" not in "".join(_texts(_page(proposed, "en")))


@pytest.mark.django_db
def test_only_interviews_with_a_decision_count_as_curated(curated) -> None:
    data = report_data(curated)
    assert data["curated"]
    assert {interview["key"]: interview["curated"] for interview in data["interviews"]} == {
        "S1+S2": True, "S3+S4": False, "T": False}


@pytest.mark.django_db
def test_a_note_alone_makes_the_list_the_researchers(proposed) -> None:
    row = proposed.interview_moments.filter(interview_key="T").order_by("position").first()
    curation.set_note(row, "Ask what happened here")
    data = report_data(proposed)
    assert data["curated"]
    assert [interview["key"] for interview in data["interviews"] if interview["curated"]] == ["T"]
    assert any(text.startswith("Lista momentów to lista badacza") for text in _texts(_page(proposed, "pl")))


@pytest.mark.django_db
def test_report_falls_back_to_the_proposals_without_any_list(proposed) -> None:
    InterviewMoment.objects.filter(project=proposed).delete()
    data = report_data(proposed)
    assert not data["curated"]
    for interview, original in zip(data["interviews"], get_result(proposed, AnalysisResult.KIND_VSR).payload["interviews"]):
        assert [(m["start_seconds"], m["end_seconds"], m["status"]) for m in interview["moments"]] == [
            (m["start_seconds"], m["end_seconds"], m["status"]) for m in original["moments"]]


@pytest.mark.django_db
def test_summary_tile_counts_the_researchers_list(curated) -> None:
    stats = report_data(curated)["stats"]
    own = next(i for i in report_data(curated)["interviews"] if i["key"] == "S1+S2")
    assert stats["primary_total"] + stats["reserve_total"] == sum(
        len(i["moments"]) for i in report_data(curated)["interviews"])
    assert own["stats"]["primary_count"] + own["stats"]["reserve_count"] == len(own["moments"])


@pytest.mark.parametrize("lang, interview, decoupling", [
    ("pl", "próg momentu wywiadu (0,7)", "bez okna o sile rozbieżności (|r| ≥ 0,4)"),
    ("en", "interview moment threshold (0.7)", "without a window at the divergent strength (|r| ≥ 0.4)"),
])
def test_method_text_states_the_settings_the_moments_were_chosen_with(lang, interview, decoupling) -> None:
    vsr = {"analysis_settings": {"synchrony_interview_threshold": 0.7, "synchrony_divergent_threshold": 0.4,
                                 "synchrony_decoupled_threshold": 0.15,
                                 "synchrony_decoupled_rule": "stretch_correlation"}}
    selection = interviews.methodology_blocks(vsr, lang)[1]["text"]
    assert interview in selection and decoupling in selection
    assert ("±0,15" if lang == "pl" else "±0.15") in selection
    assert "every window" not in selection and "każde okno" not in selection
    # The flow chart's own words stay quoted as written.
    assert "“Synchronous = above 0.8”" in selection or "„Synchronous = above 0.8”" in selection


@pytest.mark.parametrize("lang, every_window", [
    ("pl", "każde okno ma |r| < 0,1 przez co najmniej"),
    ("en", "every window has |r| < 0.1 for at least"),
])
def test_method_text_of_moments_stored_without_a_decoupling_rule_reads_every_window(lang, every_window) -> None:
    vsr = {"analysis_settings": {"synchrony_interview_threshold": 0.8, "synchrony_divergent_threshold": 0.3,
                                 "synchrony_decoupled_threshold": 0.1}}
    assert every_window in interviews.methodology_blocks(vsr, lang)[1]["text"]


@pytest.mark.django_db
def test_source_table_names_the_decoupling_rule(proposed) -> None:
    texts = _texts(_page(proposed, "en"))
    assert texts[texts.index("Decoupling rule") + 1] == "every window"


@pytest.mark.parametrize("lang, dyad, gap, yes", [
    ("pl", "Diady: bez łączenia przez przerwy", "Maks. przerwa (s)", "tak"),
    ("en", "Dyads: no joining across gaps", "Max gap (s)", "yes"),
])
@pytest.mark.parametrize("joining", [True, False, None])
def test_source_table_shows_the_dyad_gap_setting_the_interviews_used(lang, dyad, gap, yes, joining) -> None:
    settings = {"synchrony_episode_max_gap_seconds": 15}
    if joining is not None:
        settings["synchrony_dyad_without_gap_joining"] = joining
    vsr = {"analysis_settings": settings, "counts": {"primary": 5, "reserve": 3}}
    with translation.override(lang):
        rows = {row[0]: row[1] for row in interviews._source_blocks(vsr, lang)[1]["rows"]}
    if joining:
        assert rows[dyad] == yes and gap not in rows
    else:
        assert rows[gap] == "15" and dyad not in rows
