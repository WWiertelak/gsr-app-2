"""PDF report content: sections, language, embedded charts (ported GSRv2 ReportLab renderer)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage
from reportlab.platypus import Image, KeepTogether, Paragraph, Table

from gsr.pipeline.config import PipelineConfig
from gsr.reporting.methodology import METHODOLOGY, METHODS_COMPARISON
from gsr.reporting.pdf import build_story, render_report_pdf
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


def _flatten(flowables: list[Any]) -> list[Any]:
    found: list[Any] = []
    for flowable in flowables:
        found.append(flowable)
        if isinstance(flowable, KeepTogether):
            found.extend(_flatten(flowable._content))
        elif isinstance(flowable, Table):
            found.extend(_flatten([cell for row in flowable._cellvalues for cell in row]))
    return found


def _texts(story: list[Any]) -> list[str]:
    return [flowable.text for flowable in _flatten(story) if isinstance(flowable, Paragraph)]


def _png() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (60, 30), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


def test_polish_report_has_sync_and_coarousal_sections_without_segments(payload) -> None:
    texts = _texts(build_story({**payload, "lang": "pl"}))
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
    assert "Zajęcia &lt;b&gt;grupowe&lt;/b&gt;<br/>druga linia" in texts
    assert "ab" * 32 in texts  # input file checksum in the appendix


def test_english_report_and_methodology_follow_language(payload) -> None:
    texts = _texts(build_story({**payload, "lang": "en"}))
    for heading in ("GSR Analysis Report", "Results summary", "Synchronization analysis",
                    "Emotional-contagion (co-arousal) analysis", "Analysis methodology", "Reproducibility data"):
        assert heading in texts, heading
    assert "Raport analizy GSR" not in texts


def test_methodology_drops_segment_analyses() -> None:
    for lang in ("pl", "en"):
        titles = [section["title"] for section in METHODOLOGY[lang]["sections"]]
        assert len(titles) == 8 and titles[-1].startswith("7. ")
        assert not any(word in " ".join(titles) for word in ("Segment", "Korelacje", "Correlations", "Trend"))
        assert [row[0] for row in METHODS_COMPARISON[lang]["rows"]][0] in ("Synchronizacja", "Synchrony")


def test_report_embeds_captured_charts_in_their_sections(payload) -> None:
    story = build_story({**payload, "lang": "pl", "chart_images": {"sync_timeline": _png(), "raw_overlay": _png()}})
    flat = _flatten(story)
    images = [flowable for flowable in flat if isinstance(flowable, Image)]
    assert len(images) == 2
    texts = _texts(story)
    assert "Oś czasu synchronizacji" in texts and "Sygnały RAW (nałożenie)" in texts
    assert texts.index("Sygnały RAW (nałożenie)") < texts.index("Czyszczenie") < texts.index("Oś czasu synchronizacji")


def test_report_prints_large_values_without_scientific_notation(payload) -> None:
    preview = {**payload["preview"], "alignment": {**payload["preview"]["alignment"], "duration_seconds": 5400.0},
               "range": {"from": 0.0, "to": 123.45}}
    texts = _texts(build_story({**payload, "lang": "en", "preview": preview}))
    assert "5400" in texts and "123.5" in texts
    assert not any("e+0" in text for text in texts)


def test_report_states_the_analysed_window_not_the_full_span(payload) -> None:
    preview = {**payload["preview"], "range": {"from": 0.0, "to": 9.93}}
    config = {**payload["config"], "time_range_from": 1.0, "time_range_to": 9.0}
    texts = _texts(build_story({**payload, "lang": "en", "preview": preview, "config": config}))
    assert any(text.endswith("1–9 min") for text in texts)
    assert not any("9.93" in text for text in texts)


def test_render_report_pdf_embeds_unicode_font(payload) -> None:
    pdf = render_report_pdf({**payload, "lang": "pl", "stale": True})
    assert pdf.startswith(b"%PDF") and b"DejaVuSans" in pdf
