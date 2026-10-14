"""Report content (HTML page and its PDF): sections, language, embedded charts."""

from __future__ import annotations

import html
import io
import re
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage

from gsr.pipeline.config import PipelineConfig
from gsr.reporting.methodology import METHODOLOGY, METHODS_COMPARISON
from gsr.reporting.report import FONT_DIR, _fmt, render_report_html, render_report_pdf
from gsr.services.analysis import analyze_files_with_result

from .sample_data import Participant, RecordingFormat, write_group


@pytest.fixture(scope="module")
def payload(tmp_path_factory) -> dict[str, Any]:
    directory = tmp_path_factory.mktemp("report")
    files = write_group(
        directory,
        "session",
        {"A": Participant(tonic_us=4.0), "B": Participant(tonic_us=6.0, lag_seconds=1.0)},
        {"B": RecordingFormat(start_time="12:00:02:000")},
        seconds=180.0,
        seed=2,
    )
    config = PipelineConfig(scaling_enabled=True, synchrony_surrogate_count=20, contagion_surrogate_count=20)
    result = analyze_files_with_result(files, config, max_points=500)[1]
    return {
        "project_name": "Sesja łódzka",
        "project_description": "Zajęcia <b>grupowe</b>\ndruga linia",
        "generated_at": "2026-10-12 12:00",
        "computed_at": "2026-10-12 11:59",
        "dataset_aliases": ["A", "B"],
        "datasets": [
            {"alias": alias, "filename": Path(path).name, "sha256": "ab" * 32, "raw_points": 900,
             "in_window_points": 890, "recorded_start": "1900-01-01T12:00:00", "recorded_end": "1900-01-01T12:03:00"}
            for alias, path in files.items()
        ],
        "config": config.model_dump(),
        "preview": result["preview"],
        "synchrony": result["synchrony"],
        "coarousal": result["coarousal"],
        "versions": {"python": "3.12", "numpy": "2.3.2"},
        "chart_images": {},
    }


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.skip = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.skip = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        self.skip = False

    def handle_data(self, data: str) -> None:
        if not self.skip and data.strip():
            self.texts.append(data.strip())


def _texts(page: str) -> list[str]:
    """Text of every element (headings, cells, paragraphs) in document order."""
    collector = _TextCollector()
    collector.feed(page)
    return collector.texts


def _png() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (60, 30), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


def test_polish_report_has_sync_and_coarousal_sections_without_segments(payload) -> None:
    page = render_report_html({**payload, "lang": "pl"})
    texts = _texts(page)
    for heading in (
        "Raport analizy GSR",
        "Podsumowanie wyników",
        "Import",
        "Czyszczenie",
        "Analiza synchronizacji",
        "Epizody synchronizacji podgrup",
        "Metryki par",
        "Analiza zarażania emocjonalnego (ko-pobudzenie)",
        "Epizody ko-pobudzenia",
        "Metodologia analizy",
        "Porównanie metod",
        "Porównanie wyników projektu",
        "Dane do odtworzenia analizy",
    ):
        assert heading in texts, heading
    joined = "\n".join(texts)
    assert "Segmentacja" not in joined and "Korelacje Spearmana" not in joined and "Trendy" not in joined
    # User text is escaped (no markup injection) and keeps its line breaks.
    assert "Zajęcia &lt;b&gt;grupowe&lt;/b&gt;<br>druga linia" in page
    assert "ab" * 32 in texts  # input file checksum in the appendix
    assert '<html lang="pl">' in page and "<script" not in page


def test_english_report_and_methodology_follow_language(payload) -> None:
    texts = _texts(render_report_html({**payload, "lang": "en"}))
    for heading in ("GSR Analysis Report", "Results summary", "Synchronization analysis",
                    "Emotional-contagion (co-arousal) analysis", "Analysis methodology", "Reproducibility data"):
        assert heading in texts, heading
    assert "Raport analizy GSR" not in texts


def test_report_translates_setting_values(payload) -> None:
    payload = {**payload, "config": {**payload["config"], "synchrony_surrogate_enabled": True,
                                     "clean_filter_iqr": False}}
    pl = _texts(render_report_html({**payload, "lang": "pl"}))
    assert {"tak", "nie", "czas absolutny", "Początek RAW", "Koniec RAW"} <= set(pl)
    en = _texts(render_report_html({**payload, "lang": "en"}))
    assert {"yes", "no", "absolute time", "RAW start", "RAW end"} <= set(en)
    # The appendix keeps raw values on purpose, so they can be copied back into the settings.
    for texts, appendix in ((pl, "Dane do odtworzenia analizy"), (en, "Reproducibility data")):
        body = set(texts[:texts.index(appendix)])
        assert not {"True", "False", "absolute", "circular", "Start RAW", "End RAW"} & body


def test_methodology_drops_segment_analyses() -> None:
    for lang in ("pl", "en"):
        titles = [section["title"] for section in METHODOLOGY[lang]["sections"]]
        assert len(titles) == 8 and titles[-1].startswith("7. ")
        assert not any(word in " ".join(titles) for word in ("Segment", "Korelacje", "Correlations", "Trend"))
        assert [row[0] for row in METHODS_COMPARISON[lang]["rows"]][0] in ("Synchronizacja", "Synchrony")


def test_report_embeds_charts_in_their_sections(payload) -> None:
    page = render_report_html({**payload, "lang": "pl", "chart_images": {
        "sync_matrix": _png(), "raw_overlay": _png(), "sync_signals": _png()}})
    assert page.count('src="data:image/png;base64,') == 3
    texts = _texts(page)
    assert {"Sygnały z zaznaczonymi epizodami", "Macierz synchronizacji", "Sygnały RAW (nałożenie)"} <= set(texts)
    # The signals with marked episodes and the matrix are on the summary page; the others in their sections.
    body = page[page.index("<body"):]
    order = [body.index(marker) for marker in (
        'id="summary"', "chart-sync_signals", "chart-sync_matrix", 'id="import"', "chart-raw_overlay",
        'id="cleaning"', 'id="synchrony"', 'id="coarousal"')]
    assert order == sorted(order)
    assert "chart-sync_signals" not in body[body.index('id="synchrony"'):]


def test_summary_page_has_key_figures_and_contents(payload) -> None:
    sync_stats, coarousal = payload["synchrony"]["stats"], payload["coarousal"]
    page = render_report_html({**payload, "lang": "en"})
    summary = page[page.index('id="summary"'):page.index('id="import"')]
    texts = _texts(summary)
    assert ["Data scope", "Data quality", "Synchronization", "Co-arousal"] == [
        text for text in texts if text in ("Data scope", "Data quality", "Synchronization", "Co-arousal")]
    assert "2" in texts  # participants
    assert _fmt(sync_stats["mean_synchrony"]) in texts and _fmt(coarousal["coarousal_index"]) in texts
    assert f"p={_fmt(sync_stats['mean_synchrony_p_value'])}" in texts
    # The contents list every later section and link to them for the PDF's page numbers.
    titles = ["Import", "Cleaning", "Synchronization analysis", "Emotional-contagion (co-arousal) analysis",
              "Analysis methodology", "Reproducibility data"]
    assert [text for text in texts if text in titles] == titles
    assert 'href="#appendix"' in summary


def test_long_label_value_tables_split_into_two_column_pairs(payload) -> None:
    page = render_report_html({**payload, "lang": "en"})
    appendix = page[page.index('id="appendix"'):]
    settings_table = appendix[appendix.index('<table class="kv settings split">'):]
    settings_table = settings_table[:settings_table.index("</table>")]
    texts = _texts(settings_table)
    assert texts[:4] == ["Field", "Value", "Field", "Value"]
    assert set(payload["config"]) <= set(texts)  # every setting kept


def test_report_prints_large_values_without_scientific_notation(payload) -> None:
    preview = {**payload["preview"], "alignment": {**payload["preview"]["alignment"], "duration_seconds": 5400.0},
               "range": {"from": 0.0, "to": 123.45}}
    texts = _texts(render_report_html({**payload, "lang": "en", "preview": preview}))
    assert "5400" in texts and "123.5" in texts
    assert not any("e+0" in text for text in texts)


def test_report_states_the_analysed_window_not_the_full_span(payload) -> None:
    preview = {**payload["preview"], "range": {"from": 0.0, "to": 9.93}}
    config = {**payload["config"], "time_range_from": 1.0, "time_range_to": 9.0}
    texts = _texts(render_report_html({**payload, "lang": "en", "preview": preview, "config": config}))
    assert any(html.unescape(text).endswith("1–9 min") for text in texts)
    assert not any("9.93" in text for text in texts)


def _pdf_objects(pdf: bytes) -> bytes:
    """The PDF with its compressed streams (object streams included) inflated, for searching."""
    parts = [pdf]
    for stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.DOTALL):
        try:
            parts.append(zlib.decompress(stream))
        except zlib.error:
            continue
    return b"\n".join(parts)


def _page_count(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", _pdf_objects(pdf)))


def test_render_report_pdf_embeds_plex_fonts_and_charts(payload) -> None:
    pdf = render_report_pdf({**payload, "lang": "pl", "stale": True})
    fonts = set(re.findall(rb"/BaseFont\s*/[A-Z]{6}\+([\w-]+)", _pdf_objects(pdf)))
    # The app's IBM Plex; Polish letters come from the Latin Extended file, which needs its own family name.
    assert pdf.startswith(b"%PDF") and {b"Plex-Sans-Latin", b"Plex-Sans-Latin-Semi-Bold", b"Plex-Sans-Ext"} <= fonts
    assert fonts <= {b"Plex-Sans-Latin", b"Plex-Sans-Latin-Semi-Bold", b"Plex-Sans-Ext", b"Plex-Sans-Ext-Semi-Bold",
                     b"Plex-Mono-Latin", b"Plex-Mono-Ext", b"DejaVu-Sans", b"DejaVu-Sans-Bold"}
    with_chart = render_report_pdf({**payload, "lang": "pl", "chart_images": {"sync_signals": _png()}})
    assert b"/Subtype /Image" in _pdf_objects(with_chart) and b"/Subtype /Image" not in _pdf_objects(pdf)


def test_report_sections_flow_on_after_the_summary_page(payload) -> None:
    charts = {key: _png() for key in ("raw_overlay", "clean_overlay", "sync_signals", "sync_matrix",
                                      "coarousal_states")}
    pages = _page_count(render_report_pdf({**payload, "lang": "en", "chart_images": charts}))
    # With every section on a new page this payload took 15 pages; the summary page plus flowing sections take 8.
    assert pages <= 10, pages


def _section_starts(payload: dict[str, Any]) -> tuple[int, dict[str, tuple[int, float]]]:
    """Page count of the PDF and where each section starts: its page (1-based) and its top on that page."""
    from weasyprint import HTML

    document = HTML(string=render_report_html(payload), base_url=str(FONT_DIR)).render()
    starts: dict[str, tuple[int, float]] = {}
    for number, page in enumerate(document.pages, start=1):
        for anchor, position in page.anchors.items():
            starts.setdefault(anchor, (number, position[1]))
    return len(document.pages), starts


def test_outdated_run_with_a_long_description_does_not_leave_an_empty_page(payload) -> None:
    charts = {key: _png() for key in ("raw_overlay", "clean_overlay", "sync_signals", "sync_matrix",
                                      "coarousal_states")}
    def notes(count: int) -> str:
        return "\n".join(f"Session note {line}: the group worked on a shared task in the lab." for line in range(count))

    description = notes(8)
    long_payload = {**payload, "lang": "en", "chart_images": charts, "stale": True, "project_description": description}
    plain_pages, _ = _section_starts({**payload, "lang": "en", "chart_images": charts})
    pages, starts = _section_starts(long_payload)
    _, three_line_starts = _section_starts({**long_payload, "project_description": notes(3)})
    # Page 1 clamps the description to three lines, so a longer one does not push the summary down.
    assert starts["summary"] == three_line_starts["summary"] and starts["summary"][0] == 1, starts
    # The summary flows on and Import follows it on page 2.
    assert starts["import"][0] == 2, starts
    assert pages <= plain_pages + 1, (pages, plain_pages)
    page = render_report_html(long_payload)
    assert description in html.unescape(page[page.index('id="import"'):page.index('id="cleaning"')])
