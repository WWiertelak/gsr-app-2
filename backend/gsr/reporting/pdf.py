"""Bilingual (PL/EN) PDF report of a synchronization + co-arousal run.

Ported from GSRv2 ``gsr/pipeline/reporting.py`` (ReportLab): title page, results
summary, import, cleaning, synchronization, co-arousal and methodology sections.
The segment analysis, correlation, trend and scaling sections were dropped with
those analyses; a reproducibility appendix (settings, library versions, input
file SHA-256) replaces the segment CSV appendix. Charts are PNGs captured in the
browser from the same ECharts options as the screens.
"""

from __future__ import annotations

import html
import io
import re
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .methodology import METHODOLOGY, METHODS_COMPARISON

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_NAME = "DejaVuSans"
FONT_NAME_BOLD = "DejaVuSans-Bold"
FONT_SIZE_TABLE = 8

_TR: dict[str, dict[str, str]] = {
    "pl": {
        "report_title": "Raport analizy GSR",
        "field": "Pole",
        "value": "Wartość",
        "project": "Projekt",
        "description": "Opis projektu",
        "no_description": "Nie dodano opisu projektu.",
        "generated_at": "Data generacji",
        "computed_at": "Data obliczeń",
        "signals": "Sygnały",
        "executive_summary": "Podsumowanie wyników",
        "summary_scope": "Zakres danych",
        "summary_quality": "Jakość danych",
        "summary_synchrony": "Synchronizacja",
        "summary_contagion": "Ko-pobudzenie",
        "summary_participants": "uczestników",
        "summary_artifacts": "odrzuconych artefaktów",
        "summary_episodes": "epizodów",
        "summary_in_window": "punktów w oknie",
        "summary_max_group": "maks. grupa",
        "summary_high": "wysokie",
        "summary_low": "niskie",
        "summary_comovement": "ko-ruch",
        "summary_mean": "średnia",
        "summary_index": "indeks",
        "interpretation_note": "Uwaga interpretacyjna",
        "summary_interpretation_note": "GSR opisuje poziom i dynamikę pobudzenia fizjologicznego. Nie rozpoznaje konkretnej emocji, nie określa jej znaku i sam nie dowodzi związku przyczynowego między osobami.",
        "stale_warning": "UWAGA: ustawienia lub sygnały projektu zmieniły się po tym obliczeniu. Raport opisuje ostatnie obliczenie.",
        # import
        "import_heading": "Import",
        "alias": "Alias",
        "file": "Plik",
        "raw_points": "Pkt. RAW",
        # cleaning
        "cleaning_heading": "Czyszczenie",
        "cleaning_params": "Parametry czyszczenia",
        "median_kernel": "Filtr med. (okno)",
        "physio_range": "Zakres fizjol.",
        "zscore": "Z-score",
        "jumps": "Nagłe skoki",
        "iqr": "IQR",
        "butterworth": "Butterworth",
        "downsample_s": "Downsamp. (s)",
        "time_range_from_cfg": "Zakres od (ustawienia)",
        "time_range_to_cfg": "Zakres do (ustawienia)",
        "time_range_from_eff": "Zakres od (efektywny, min)",
        "time_range_to_eff": "Zakres do (efektywny, min)",
        "sync_heading": "Synchronizacja sygnałów",
        "alignment_mode": "Tryb wyrównania",
        "alignment_range": "Wspólny czas absolutny",
        "alignment_duration": "Wspólny czas (s)",
        "start_raw": "Start RAW",
        "end_raw": "End RAW",
        "clean_points": "Pkt. CLEAN",
        "signal_stats_raw": "Statystyki sygnału RAW (µS, przed filtrami)",
        "signal_stats_clean": "Statystyki sygnału CLEAN (µS, po filtrach)",
        "stat_n": "n",
        "stat_mean": "Średnia",
        "stat_std": "Odch. std",
        "stat_min": "Min",
        "stat_median": "Mediana",
        "stat_max": "Max",
        "rejection_summary": "Podsumowanie odrzuceń",
        "synced": "Zsync.",
        "artifacts": "Artefakty",
        "signal_quality": "Jakość",
        "quality_good": "dobra",
        "quality_warning": "uwaga",
        "quality_poor": "słaba",
        "identical_signals_warning": "OSTRZEŻENIE: wykryto identyczne sygnały",
        "outside_range": "Poza zakres.",
        "in_window": "W oknie",
        "metric": "Metryka",
        "synced_points_total": "Pkt. zsynchronizowane (suma)",
        "artifacts_total": "Artefakty odrzucone (suma)",
        "outside_range_total": "Poza zakresem czasu (suma)",
        "before_downsample": "Przed downsamplingiem",
        "after_downsample": "Po downsamplingu",
        "dropped_downsample": "Odrzucone przy downsamplingu",
        # synchrony
        "synchrony_heading": "Analiza synchronizacji",
        "no_seg": "Nr",
        "sync_mean": "Średnia synchronizacja",
        "sync_episode_count": "Liczba epizodów",
        "sync_cluster_count": "Liczba klastrów",
        "sync_mode": "Tryb signed",
        "sync_surrogate_enabled": "Test surogatowy włączony",
        "sync_surrogate_method": "Metoda surogatów",
        "sync_surrogate_count": "Liczba surogatów",
        "sync_surrogate_seed": "Seed surogatów",
        "sync_group_p": "p całej grupy",
        "sync_pair_p": "p (surogat)",
        "sync_episodes_heading": "Epizody synchronizacji podgrup",
        "sync_ep_time": "Czas (min)",
        "sync_ep_duration": "Czas trwania",
        "sync_ep_members": "Uczestnicy",
        "sync_ep_size": "Osób",
        "sync_ep_mean": "Śr. siła",
        "sync_ep_peak": "Szczyt. siła",
        "sync_no_episodes": "Nie wykryto epizodów synchronizacji podgrup.",
        "sync_pairs_heading": "Metryki par",
        "sync_pair": "Para",
        "sync_xcorr": "Maks. korel.",
        "sync_lag": "Opóźn. (s)",
        "sync_coherence": "Koherencja",
        "sync_dtw": "DTW",
        "sync_clusters_heading": "Klastry sygnałów",
        "sync_cluster": "Klaster",
        "sync_members": "Sygnały",
        "sync_cluster_mean": "Śr. synchronizacja",
        "sync_charts": "Wykresy synchronizacji",
        # contagion
        "contagion_heading": "Analiza zarażania emocjonalnego (ko-pobudzenie)",
        "contagion_coarousal": "Indeks ko-pobudzenia",
        "contagion_null_mean": "Średnia zerowa",
        "contagion_null_sd": "SD zerowe",
        "contagion_p": "p (surogatowe)",
        "contagion_significance": "Istotność",
        "contagion_significant": "Istotne ponad przypadek (p<0.05)",
        "contagion_not_significant": "Nieistotne (p≥0.05)",
        "contagion_no_surrogate": "Bez testu surogatowego",
        "contagion_episodes_heading": "Epizody ko-pobudzenia",
        "contagion_no_episodes": "Nie wykryto epizodów.",
        "contagion_ep_dir": "Typ",
        "contagion_ep_contrib": "Współtworzący",
        "contagion_ep_strength": "Siła",
        "contagion_ep_noncontrib": "Poza synchronizacją",
        "contagion_dir_high": "Wysoki",
        "contagion_dir_low": "Niski",
        "contagion_dir_comov": "Ko-ruch",
        "contagion_strength_full": "pełna",
        "contagion_strength_strong": "silna",
        "contagion_strength_partial": "częściowa",
        "contagion_primary_heading": "Oś czasu (bez nakładania)",
        "contagion_state_heading": "Udział czasu w stanach (per sygnał)",
        "contagion_state_high": "% Wysoki",
        "contagion_state_neutral": "% Neutralny",
        "contagion_state_low": "% Niski",
        # charts
        "chart_raw_overlay": "Sygnały RAW (nałożenie)",
        "chart_clean_overlay": "Sygnały po czyszczeniu (nałożenie)",
        "chart_sync_timeline": "Oś czasu synchronizacji",
        "chart_sync_signals": "Sygnały z zaznaczonymi epizodami",
        "chart_sync_matrix": "Macierz synchronizacji",
        "chart_coarousal_states": "Mapa stanów uczestników w czasie",
        # methodology
        "results_comparison_heading": "Porównanie wyników projektu",
        "results_comparison_intro": "Poniżej te same wyniki zapisano prostym językiem. Interpretacja opisuje, co mówi liczba i czego sama nie rozstrzyga.",
        "res_method": "Analiza",
        "res_metric": "Miara",
        "res_value": "Wartość",
        "res_reading": "Jak to rozumieć",
        "res_sync": "Synchronizacja",
        "res_sync_metric": "Średnia synchronizacja",
        "res_sync_reading": "Przeciętna zgodność czasowa par po dopuszczeniu niewielkiego opóźnienia; bliżej 1 oznacza większą zgodność.",
        "res_sync_ep": "Epizody / klastry",
        "res_sync_p": "Istotność średniej synchronizacji",
        "res_cont": "Ko-pobudzenie",
        "res_cont_metric": "Indeks ko-pobudzenia",
        "res_cont_ep": "Epizody (High/Low/ko-ruch)",
        "res_cont_p": "Istotność (p surogatowe)",
        "res_episodes_reading": "Liczba wykrytych okresów i grup; ich znaczenie zależy od czasu, uczestników i kontekstu.",
        "res_cont_reading": "Średnia liczba osób w liczniejszym stanie skrajnym High albo Low; zakres 0–{participants}.",
        "res_p_significant": "Wynik przekracza poziom oczekiwany z przypadku przy progu p < 0,05.",
        "res_p_not_significant": "Dane nie dają wystarczających podstaw, aby odróżnić wynik od przypadku.",
        "res_p_unavailable": "Test istotności nie został wykonany; nie oceniaj wyniku wyłącznie po jego wielkości.",
        "comp_headers": "Metoda|Co mierzy i jak czytać|Istotność i ograniczenie",
        "comp_high": "Wysoka wartość",
        "comp_limitation": "Ograniczenie",
        # appendix
        "appendix_heading": "Dane do odtworzenia analizy",
        "appendix_intro": "Ustawienia, wersje bibliotek i sumy kontrolne plików wejściowych pozwalają odbiorcy sprawdzić, co dokładnie policzono.",
        "appendix_settings": "Ustawienia analizy",
        "appendix_versions": "Wersje oprogramowania",
        "appendix_files": "Pliki wejściowe",
        "package": "Pakiet",
        "version": "Wersja",
    },
    "en": {
        "report_title": "GSR Analysis Report",
        "field": "Field",
        "value": "Value",
        "project": "Project",
        "description": "Project description",
        "no_description": "No project description was added.",
        "generated_at": "Generated at",
        "computed_at": "Computed at",
        "signals": "Signals",
        "executive_summary": "Results summary",
        "summary_scope": "Data scope",
        "summary_quality": "Data quality",
        "summary_synchrony": "Synchronization",
        "summary_contagion": "Co-arousal",
        "summary_participants": "participants",
        "summary_artifacts": "rejected artifacts",
        "summary_episodes": "episodes",
        "summary_in_window": "points in range",
        "summary_max_group": "max group",
        "summary_high": "high",
        "summary_low": "low",
        "summary_comovement": "co-movement",
        "summary_mean": "mean",
        "summary_index": "index",
        "interpretation_note": "Interpretation note",
        "summary_interpretation_note": "GSR describes physiological arousal level and dynamics. It does not identify a specific emotion or valence and cannot by itself establish causation between people.",
        "stale_warning": "NOTE: the project settings or signals changed after this run. The report describes the last run.",
        # import
        "import_heading": "Import",
        "alias": "Alias",
        "file": "File",
        "raw_points": "RAW pts.",
        # cleaning
        "cleaning_heading": "Cleaning",
        "cleaning_params": "Cleaning parameters",
        "median_kernel": "Median filter (window)",
        "physio_range": "Physiological range",
        "zscore": "Z-score",
        "jumps": "Abrupt jumps",
        "iqr": "IQR",
        "butterworth": "Butterworth",
        "downsample_s": "Downsamp. (s)",
        "time_range_from_cfg": "Range from (settings)",
        "time_range_to_cfg": "Range to (settings)",
        "time_range_from_eff": "Range from (effective, min)",
        "time_range_to_eff": "Range to (effective, min)",
        "sync_heading": "Signal synchronization",
        "alignment_mode": "Alignment mode",
        "alignment_range": "Shared absolute time",
        "alignment_duration": "Shared duration (s)",
        "start_raw": "Start RAW",
        "end_raw": "End RAW",
        "clean_points": "CLEAN pts.",
        "signal_stats_raw": "RAW signal statistics (µS, before filters)",
        "signal_stats_clean": "CLEAN signal statistics (µS, after filters)",
        "stat_n": "n",
        "stat_mean": "Mean",
        "stat_std": "Std dev",
        "stat_min": "Min",
        "stat_median": "Median",
        "stat_max": "Max",
        "rejection_summary": "Rejection summary",
        "synced": "Synced",
        "artifacts": "Artifacts",
        "signal_quality": "Quality",
        "quality_good": "good",
        "quality_warning": "warning",
        "quality_poor": "poor",
        "identical_signals_warning": "WARNING: identical signals detected",
        "outside_range": "Out of range",
        "in_window": "In window",
        "metric": "Metric",
        "synced_points_total": "Synced points (total)",
        "artifacts_total": "Rejected artifacts (total)",
        "outside_range_total": "Outside time range (total)",
        "before_downsample": "Before downsampling",
        "after_downsample": "After downsampling",
        "dropped_downsample": "Dropped by downsampling",
        # synchrony
        "synchrony_heading": "Synchronization analysis",
        "no_seg": "No.",
        "sync_mean": "Mean synchrony",
        "sync_episode_count": "Episode count",
        "sync_cluster_count": "Cluster count",
        "sync_mode": "Signed mode",
        "sync_surrogate_enabled": "Surrogate test enabled",
        "sync_surrogate_method": "Surrogate method",
        "sync_surrogate_count": "Surrogate count",
        "sync_surrogate_seed": "Surrogate seed",
        "sync_group_p": "Whole-group p",
        "sync_pair_p": "p (surrogate)",
        "sync_episodes_heading": "Subgroup synchronization episodes",
        "sync_ep_time": "Time (min)",
        "sync_ep_duration": "Duration",
        "sync_ep_members": "Participants",
        "sync_ep_size": "People",
        "sync_ep_mean": "Mean strength",
        "sync_ep_peak": "Peak strength",
        "sync_no_episodes": "No subgroup synchronization episodes detected.",
        "sync_pairs_heading": "Pairwise metrics",
        "sync_pair": "Pair",
        "sync_xcorr": "Max corr.",
        "sync_lag": "Lag (s)",
        "sync_coherence": "Coherence",
        "sync_dtw": "DTW",
        "sync_clusters_heading": "Signal clusters",
        "sync_cluster": "Cluster",
        "sync_members": "Signals",
        "sync_cluster_mean": "Mean synchrony",
        "sync_charts": "Synchronization charts",
        # contagion
        "contagion_heading": "Emotional-contagion (co-arousal) analysis",
        "contagion_coarousal": "Co-arousal index",
        "contagion_null_mean": "Null mean",
        "contagion_null_sd": "Null SD",
        "contagion_p": "p (surrogate)",
        "contagion_significance": "Significance",
        "contagion_significant": "Above chance (p<0.05)",
        "contagion_not_significant": "Not significant (p≥0.05)",
        "contagion_no_surrogate": "No surrogate test",
        "contagion_episodes_heading": "Co-arousal episodes",
        "contagion_no_episodes": "No episodes detected.",
        "contagion_ep_dir": "Type",
        "contagion_ep_contrib": "Contributors",
        "contagion_ep_strength": "Strength",
        "contagion_ep_noncontrib": "Not in sync",
        "contagion_dir_high": "High",
        "contagion_dir_low": "Low",
        "contagion_dir_comov": "Co-move",
        "contagion_strength_full": "full",
        "contagion_strength_strong": "strong",
        "contagion_strength_partial": "partial",
        "contagion_primary_heading": "Timeline (non-overlapping)",
        "contagion_state_heading": "Time in states (per signal)",
        "contagion_state_high": "% High",
        "contagion_state_neutral": "% Neutral",
        "contagion_state_low": "% Low",
        # charts
        "chart_raw_overlay": "RAW signals (overlay)",
        "chart_clean_overlay": "Cleaned signals (overlay)",
        "chart_sync_timeline": "Synchronization timeline",
        "chart_sync_signals": "Signals with marked episodes",
        "chart_sync_matrix": "Synchronization matrix",
        "chart_coarousal_states": "Participant state map over time",
        # methodology
        "results_comparison_heading": "Project results comparison",
        "results_comparison_intro": "The same results are restated below in plain language. The interpretation says what each number describes and what it cannot establish by itself.",
        "res_method": "Analysis",
        "res_metric": "Metric",
        "res_value": "Value",
        "res_reading": "How to read it",
        "res_sync": "Synchrony",
        "res_sync_metric": "Mean synchrony",
        "res_sync_reading": "Average temporal pair agreement after allowing a small lag; values nearer 1 indicate closer agreement.",
        "res_sync_ep": "Episodes / clusters",
        "res_sync_p": "Mean-synchrony significance",
        "res_cont": "Co-arousal",
        "res_cont_metric": "Co-arousal index",
        "res_cont_ep": "Episodes (High/Low/co-move)",
        "res_cont_p": "Significance (surrogate p)",
        "res_episodes_reading": "Number of detected periods and groups; meaning depends on timing, participants, and context.",
        "res_cont_reading": "Average number of people in the more populated High or Low state; range 0–{participants}.",
        "res_p_significant": "The result exceeds the chance level at the p < 0.05 threshold.",
        "res_p_not_significant": "The data do not provide enough evidence to distinguish the result from chance.",
        "res_p_unavailable": "No significance test was run; do not judge the result from its size alone.",
        "comp_headers": "Method|What it measures and how to read it|Significance and limitation",
        "comp_high": "High value",
        "comp_limitation": "Limitation",
        # appendix
        "appendix_heading": "Reproducibility data",
        "appendix_intro": "The settings, library versions and input-file checksums let a recipient check exactly what was computed.",
        "appendix_settings": "Analysis settings",
        "appendix_versions": "Software versions",
        "appendix_files": "Input files",
        "package": "Package",
        "version": "Version",
    },
}

CHART_SECTIONS = {
    "import": ("raw_overlay",),
    "cleaning": ("clean_overlay",),
    "synchrony": ("sync_timeline", "sync_signals", "sync_matrix"),
    "coarousal": ("coarousal_states",),
}

_CONTAGION_DIR_KEYS = {"High": "contagion_dir_high", "Low": "contagion_dir_low", "Co-movement": "contagion_dir_comov"}


def resolve_lang(value: Any) -> str:
    lang = str(value or "pl").split("-")[0].strip().lower()
    return lang if lang in _TR else "pl"


def _tr(lang: str, key: str) -> str:
    return _TR.get(lang, _TR["pl"]).get(key, key)


def _register_fonts() -> None:
    """DejaVu Sans (vendored, covers Polish diacritics); the family mapping makes <b> use the bold face."""
    for font_name, filename in ((FONT_NAME, "DejaVuSans.ttf"), (FONT_NAME_BOLD, "DejaVuSans-Bold.ttf")):
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font_name, str(FONT_DIR / filename)))
    pdfmetrics.registerFontFamily(FONT_NAME, normal=FONT_NAME, bold=FONT_NAME_BOLD, italic=FONT_NAME, boldItalic=FONT_NAME_BOLD)


# ISO datetime: 2023-01-15T14:23:45.123456 → 14:23:45
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})")


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        # Up to 3 significant figures; avoids long decimals in narrow cells.
        if value == 0.0:
            return "0"
        return f"{value:.3g}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        match = _ISO_DATETIME_RE.match(value)
        if match:
            return match.group(1)
        return value
    if isinstance(value, list):
        if not value:
            return "[]"
        if len(value) <= 5:
            return ", ".join(_fmt(v) for v in value)
        return f"[{len(value)} items]"
    return str(value)


def _fmt_min(value: Any) -> str:
    """Relative minutes (float) → m:ss, matching the screens' time labels."""
    if not isinstance(value, (int, float)):
        return "-"
    total_seconds = max(0, round(value * 60))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def _escaped_multiline(value: Any, fallback: str) -> str:
    """Escape user-authored project text while preserving its paragraph breaks."""
    text = str(value or "").strip() or fallback
    return html.escape(text, quote=True).replace("\n", "<br/>")


def _build_styles() -> dict[str, Any]:
    styles = getSampleStyleSheet()
    for name in ("Title", "Heading1", "Heading2", "Heading3"):
        styles[name].fontName = FONT_NAME_BOLD
    styles["BodyText"].fontName = FONT_NAME
    styles["BodyText"].fontSize = 10
    styles["BodyText"].leading = 13
    muted = ParagraphStyle("Muted", parent=styles["BodyText"], textColor=colors.HexColor("#5f6c7b"))
    cell_style = ParagraphStyle(
        "TableCell", fontName=FONT_NAME, fontSize=FONT_SIZE_TABLE, leading=FONT_SIZE_TABLE + 3, wordWrap="CJK"
    )
    cell_header_style = ParagraphStyle(
        "TableCellHeader", fontName=FONT_NAME_BOLD, fontSize=FONT_SIZE_TABLE, leading=FONT_SIZE_TABLE + 3,
        wordWrap="CJK",
    )
    return {"base": styles, "muted": muted, "cell": cell_style, "cell_header": cell_header_style}


def _table(story: list[Any], rows: list[list[Any]], widths: list[float], styles: dict[str, Any]) -> None:
    """Render a table with word-wrapped cells and consistent styling."""
    wrapped = [
        [
            Paragraph(html.escape(cell if isinstance(cell, str) else _fmt(cell), quote=True),
                      styles["cell_header"] if row_index == 0 else styles["cell"])
            for cell in row
        ]
        for row_index, row in enumerate(rows)
    ]
    table = Table(wrapped, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf3ff")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#ccd6e5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 0.35 * cm))


def _embed_charts(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], section: str, lang: str) -> None:
    images = payload.get("chart_images") or {}
    for key in CHART_SECTIONS[section]:
        image_bytes = images.get(key)
        if not image_bytes:
            continue
        image = Image(io.BytesIO(image_bytes))
        image._restrictSize(17.2 * cm, 22.0 * cm)
        story.append(
            KeepTogether(
                [Paragraph(html.escape(_tr(lang, f"chart_{key}")), styles["base"]["Heading3"]), image,
                 Spacer(1, 0.4 * cm)]
            )
        )


def _heading(story: list[Any], styles: dict[str, Any], text: str) -> None:
    story.append(PageBreak())
    story.append(Paragraph(html.escape(text), styles["base"]["Heading1"]))
    story.append(Spacer(1, 0.2 * cm))


def _significance(lang: str, coarousal: dict[str, Any]) -> str:
    if coarousal.get("coarousal_p_value") is None:
        return _tr(lang, "contagion_no_surrogate")
    stats = coarousal.get("stats") or {}
    return _tr(lang, "contagion_significant" if stats.get("coarousal_significant") else "contagion_not_significant")


# ─── Sections ────────────────────────────────────────────────────────────────


def _section_title(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    base = styles["base"]
    story.append(Paragraph(_tr(lang, "report_title"), base["Title"]))
    story.append(Spacer(1, 0.3 * cm))
    _table(
        story,
        [
            [_tr(lang, "field"), _tr(lang, "value")],
            [_tr(lang, "project"), payload.get("project_name") or "-"],
            [_tr(lang, "generated_at"), payload.get("generated_at") or "-"],
            [_tr(lang, "computed_at"), payload.get("computed_at") or "-"],
            [_tr(lang, "signals"), ", ".join(payload.get("dataset_aliases") or []) or "-"],
        ],
        [5.0 * cm, 11.6 * cm],
        styles,
    )
    if payload.get("stale"):
        story.append(Paragraph(f"<b>{html.escape(_tr(lang, 'stale_warning'))}</b>", base["BodyText"]))
        story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(_tr(lang, "description"), base["Heading2"]))
    story.append(
        Paragraph(_escaped_multiline(payload.get("project_description"), _tr(lang, "no_description")), base["BodyText"])
    )


def _section_executive_summary(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    preview = payload.get("preview") or {}
    totals = (preview.get("cleaning_summary") or {}).get("totals") or {}
    preview_range = preview.get("range") or {}
    aliases = payload.get("dataset_aliases") or []
    synchrony = payload.get("synchrony") or {}
    sync_stats = synchrony.get("stats") or {}
    coarousal = payload.get("coarousal") or {}
    coarousal_stats = coarousal.get("stats") or {}

    rows = [
        [_tr(lang, "metric"), _tr(lang, "value")],
        [
            _tr(lang, "summary_scope"),
            f"{len(aliases)} {_tr(lang, 'summary_participants')}; "
            f"{_fmt(preview_range.get('from'))}–{_fmt(preview_range.get('to'))} min",
        ],
        [
            _tr(lang, "summary_quality"),
            f"{_fmt(totals.get('in_time_window_points'))} {_tr(lang, 'summary_in_window')}; "
            f"{_fmt(totals.get('artifact_rejected_points'))} {_tr(lang, 'summary_artifacts')}",
        ],
        [
            _tr(lang, "summary_synchrony"),
            (
                f"{_tr(lang, 'summary_mean')}={_fmt(sync_stats.get('mean_synchrony'))}; "
                f"{_fmt(sync_stats.get('episode_count'))} {_tr(lang, 'summary_episodes')}; "
                f"{_tr(lang, 'summary_max_group')}={_fmt(sync_stats.get('max_episode_size'))}; "
                f"p={_fmt(sync_stats.get('mean_synchrony_p_value'))}"
                if synchrony
                else "—"
            ),
        ],
        [
            _tr(lang, "summary_contagion"),
            (
                f"{_tr(lang, 'summary_index')}={_fmt(coarousal.get('coarousal_index'))}; "
                f"{_fmt(coarousal_stats.get('episode_count'))} {_tr(lang, 'summary_episodes')}; "
                f"{_tr(lang, 'summary_high')}={_fmt(coarousal_stats.get('high_episode_count'))}; "
                f"{_tr(lang, 'summary_low')}={_fmt(coarousal_stats.get('low_episode_count'))}; "
                f"{_tr(lang, 'summary_comovement')}={_fmt(coarousal_stats.get('comovement_episode_count'))}; "
                f"p={_fmt(coarousal.get('coarousal_p_value'))}; {_significance(lang, coarousal)}"
                if coarousal
                else "—"
            ),
        ],
    ]
    _heading(story, styles, _tr(lang, "executive_summary"))
    _table(story, rows, [5.0 * cm, 11.6 * cm], styles)
    story.append(Spacer(1, 0.15 * cm))
    story.append(
        Paragraph(
            f"<b>{_tr(lang, 'interpretation_note')}:</b> {_tr(lang, 'summary_interpretation_note')}",
            styles["base"]["BodyText"],
        )
    )


def _section_import(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    _heading(story, styles, _tr(lang, "import_heading"))
    datasets = payload.get("datasets") or []
    if datasets:
        rows = [[_tr(lang, "alias"), _tr(lang, "file"), _tr(lang, "raw_points"), _tr(lang, "start_raw"),
                 _tr(lang, "end_raw")]]
        for dataset in datasets:
            rows.append([
                dataset.get("alias") or "-",
                dataset.get("filename") or "-",
                _fmt(dataset.get("raw_points")),
                _fmt(dataset.get("recorded_start")),
                _fmt(dataset.get("recorded_end")),
            ])
        _table(story, rows, [3.2 * cm, 6.2 * cm, 2.4 * cm, 2.4 * cm, 2.4 * cm], styles)
    _embed_charts(story, styles, payload, "import", lang)


def _signal_statistics_tables(story: list[Any], styles: dict[str, Any], cleaning: dict[str, Any], lang: str) -> None:
    """RAW and CLEAN descriptive statistics per signal (mean, std, min, median, max in µS)."""
    statistics = cleaning.get("signal_statistics") or []
    stat_keys = ("count", "mean", "std", "min", "median", "max")
    header = [_tr(lang, key) for key in ("alias", "stat_n", "stat_mean", "stat_std", "stat_min", "stat_median",
                                         "stat_max")]
    widths = [3.2 * cm, 2.0 * cm, 2.3 * cm, 2.3 * cm, 2.2 * cm, 2.3 * cm, 2.3 * cm]
    for variant, heading_key in (("raw", "signal_stats_raw"), ("clean", "signal_stats_clean")):
        rows = [header]
        for entry in statistics:
            stats = entry.get(variant)
            if isinstance(stats, dict):
                rows.append([entry.get("alias") or "-"] + [_fmt(stats.get(key)) for key in stat_keys])
        if len(rows) > 1:
            story.append(Paragraph(_tr(lang, heading_key), styles["base"]["Heading2"]))
            _table(story, rows, widths, styles)


def _section_cleaning(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    base = styles["base"]
    _heading(story, styles, _tr(lang, "cleaning_heading"))
    preview = payload.get("preview") or {}
    cfg = payload.get("config") or {}
    preview_range = preview.get("range") or {}
    alignment = preview.get("alignment") or {}

    story.append(Paragraph(_tr(lang, "cleaning_params"), base["Heading2"]))
    mapping = [
        (_tr(lang, "median_kernel"), cfg.get("median_kernel")),
        (_tr(lang, "physio_range"), cfg.get("clean_filter_physiological_range")),
        (_tr(lang, "zscore"), cfg.get("clean_filter_zscore")),
        (_tr(lang, "jumps"), cfg.get("clean_filter_jump")),
        (_tr(lang, "iqr"), cfg.get("clean_filter_iqr")),
        (_tr(lang, "butterworth"), cfg.get("clean_filter_butterworth")),
        (_tr(lang, "downsample_s"), cfg.get("downsample_interval_seconds")),
        (_tr(lang, "time_range_from_cfg"), cfg.get("time_range_from")),
        (_tr(lang, "time_range_to_cfg"), cfg.get("time_range_to")),
        (_tr(lang, "time_range_from_eff"), preview_range.get("from")),
        (_tr(lang, "time_range_to_eff"), preview_range.get("to")),
        (_tr(lang, "alignment_mode"), alignment.get("mode")),
        (_tr(lang, "alignment_range"), f"{_fmt(alignment.get('start'))}–{_fmt(alignment.get('end'))}"),
        (_tr(lang, "alignment_duration"), alignment.get("duration_seconds")),
    ]
    _table(story, [[_tr(lang, "field"), _tr(lang, "value")], *[[label, _fmt(value)] for label, value in mapping]],
           [9.0 * cm, 7.6 * cm], styles)

    story.append(Paragraph(_tr(lang, "sync_heading"), base["Heading2"]))
    sync_rows = [[_tr(lang, key) for key in ("alias", "start_raw", "end_raw", "raw_points", "clean_points")]]
    for dataset in payload.get("datasets") or []:
        sync_rows.append([
            dataset.get("alias") or "-",
            _fmt(dataset.get("recorded_start")),
            _fmt(dataset.get("recorded_end")),
            _fmt(dataset.get("raw_points")),
            _fmt(dataset.get("in_window_points")),
        ])
    _table(story, sync_rows, [3.2 * cm, 3.5 * cm, 3.5 * cm, 3.0 * cm, 3.4 * cm], styles)

    cleaning = preview.get("cleaning_summary") or {}
    if cleaning:
        _signal_statistics_tables(story, styles, cleaning, lang)
        story.append(Paragraph(_tr(lang, "rejection_summary"), base["Heading2"]))
        per_alias = cleaning.get("per_alias") or []
        if per_alias:
            rows = [[_tr(lang, key) for key in ("alias", "synced", "artifacts", "signal_quality", "outside_range",
                                                 "in_window")]]
            for entry in per_alias:
                rows.append([
                    entry.get("alias") or "-",
                    _fmt(entry.get("synced_points")),
                    f"{_fmt(entry.get('artifact_rejected_points'))} ({_fmt(entry.get('artifact_rejected_pct'))}%)",
                    _tr(lang, f"quality_{entry.get('signal_quality', 'poor')}"),
                    _fmt(entry.get("outside_time_window_points")),
                    _fmt(entry.get("in_time_window_points")),
                ])
            _table(story, rows, [3.2 * cm, 2.5 * cm, 2.5 * cm, 2.3 * cm, 3.0 * cm, 3.1 * cm], styles)

        identical_groups = cleaning.get("identical_signal_groups") or []
        if identical_groups:
            groups_text = "; ".join(" = ".join(str(alias) for alias in group) for group in identical_groups)
            story.append(
                Paragraph(
                    f"<b>{html.escape(_tr(lang, 'identical_signals_warning'))}:</b> {html.escape(groups_text)}",
                    styles["muted"],
                )
            )
            story.append(Spacer(1, 0.15 * cm))

        totals = cleaning.get("totals")
        if isinstance(totals, dict):
            _table(
                story,
                [
                    [_tr(lang, "metric"), _tr(lang, "value")],
                    [_tr(lang, "synced_points_total"), _fmt(totals.get("synced_points"))],
                    [_tr(lang, "artifacts_total"), _fmt(totals.get("artifact_rejected_points"))],
                    [_tr(lang, "outside_range_total"), _fmt(totals.get("outside_time_window_points"))],
                    [_tr(lang, "before_downsample"), _fmt(totals.get("merged_points_before_downsampling"))],
                    [_tr(lang, "after_downsample"), _fmt(totals.get("merged_points_after_downsampling"))],
                    [_tr(lang, "dropped_downsample"), _fmt(totals.get("downsampled_points"))],
                ],
                [10.0 * cm, 6.6 * cm],
                styles,
            )
    _embed_charts(story, styles, payload, "cleaning", lang)


def _section_synchrony(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    synchrony = payload.get("synchrony")
    if not isinstance(synchrony, dict):
        return
    base = styles["base"]
    _heading(story, styles, _tr(lang, "synchrony_heading"))
    stats = synchrony.get("stats") or {}
    config = payload.get("config") or {}
    _table(
        story,
        [
            [_tr(lang, "metric"), _tr(lang, "value")],
            [_tr(lang, "sync_mean"), _fmt(stats.get("mean_synchrony"))],
            [_tr(lang, "sync_episode_count"), _fmt(stats.get("episode_count"))],
            [_tr(lang, "sync_cluster_count"), _fmt(stats.get("cluster_count"))],
            [_tr(lang, "sync_mode"), _fmt(config.get("synchrony_signed"))],
            [_tr(lang, "sync_surrogate_enabled"), _fmt(config.get("synchrony_surrogate_enabled"))],
            [_tr(lang, "sync_surrogate_method"),
             _fmt(stats.get("surrogate_method") or config.get("synchrony_surrogate_method"))],
            [_tr(lang, "sync_surrogate_count"),
             _fmt(stats.get("surrogate_count") or config.get("synchrony_surrogate_count"))],
            [_tr(lang, "sync_surrogate_seed"), _fmt(config.get("synchrony_surrogate_seed"))],
            [_tr(lang, "sync_group_p"), _fmt(stats.get("mean_synchrony_p_value"))],
        ],
        [8.0 * cm, 8.6 * cm],
        styles,
    )

    story.append(Paragraph(_tr(lang, "sync_episodes_heading"), base["Heading2"]))
    episodes = synchrony.get("episodes") or []
    if episodes:
        rows = [[_tr(lang, key) for key in ("no_seg", "sync_ep_time", "sync_ep_duration", "sync_ep_members",
                                             "sync_ep_size", "sync_ep_mean", "sync_ep_peak")]]
        for index, episode in enumerate(episodes):
            start, end = episode.get("start_relative"), episode.get("end_relative")
            duration = end - start if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None
            rows.append([
                _fmt(index + 1),
                f"{_fmt_min(start)}–{_fmt_min(end)}",
                _fmt_min(duration),
                ", ".join(str(member) for member in episode.get("members") or []),
                _fmt(episode.get("size")),
                _fmt(episode.get("mean_strength")),
                _fmt(episode.get("peak_strength")),
            ])
        _table(story, rows, [1.0 * cm, 2.6 * cm, 2.2 * cm, 5.0 * cm, 1.4 * cm, 2.2 * cm, 2.2 * cm], styles)
    else:
        story.append(Paragraph(_tr(lang, "sync_no_episodes"), base["BodyText"]))
        story.append(Spacer(1, 0.15 * cm))

    pairs = synchrony.get("pairs") or []
    if pairs:
        story.append(Paragraph(_tr(lang, "sync_pairs_heading"), base["Heading2"]))
        has_pair_p = any("p_value" in pair for pair in pairs)
        rows = [[
            _tr(lang, "sync_pair"),
            _tr(lang, "sync_xcorr"),
            *([_tr(lang, "sync_pair_p")] if has_pair_p else []),
            _tr(lang, "sync_lag"),
            _tr(lang, "sync_coherence"),
            _tr(lang, "sync_dtw"),
        ]]
        for pair in pairs:
            rows.append([
                f"{pair.get('alias_a')} · {pair.get('alias_b')}",
                _fmt(pair.get("max_xcorr")),
                *([_fmt(pair.get("p_value"))] if has_pair_p else []),
                _fmt(pair.get("lag_seconds")),
                _fmt(pair.get("coherence")),
                _fmt(pair.get("dtw_distance")),
            ])
        column_count = 6 if has_pair_p else 5
        _table(story, rows, [16.6 * cm / column_count] * column_count, styles)

    clusters = (synchrony.get("clusters") or {}).get("clusters") or []
    if clusters:
        story.append(Paragraph(_tr(lang, "sync_clusters_heading"), base["Heading2"]))
        rows = [[_tr(lang, "sync_cluster"), _tr(lang, "sync_members"), _tr(lang, "sync_cluster_mean")]]
        for cluster in clusters:
            label = cluster.get("cluster")
            rows.append([
                _fmt((label + 1) if isinstance(label, int) else label),
                ", ".join(str(member) for member in cluster.get("members") or []),
                _fmt(cluster.get("mean_synchrony")),
            ])
        _table(story, rows, [3.0 * cm, 10.6 * cm, 3.0 * cm], styles)

    if any((payload.get("chart_images") or {}).get(key) for key in CHART_SECTIONS["synchrony"]):
        story.append(Paragraph(_tr(lang, "sync_charts"), base["Heading2"]))
        _embed_charts(story, styles, payload, "synchrony", lang)


def _section_coarousal(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    coarousal = payload.get("coarousal")
    if not isinstance(coarousal, dict):
        return
    base = styles["base"]
    _heading(story, styles, _tr(lang, "contagion_heading"))
    p_value = coarousal.get("coarousal_p_value")
    stats = coarousal.get("stats") or {}
    config = payload.get("config") or {}
    _table(
        story,
        [
            [_tr(lang, "metric"), _tr(lang, "value")],
            [_tr(lang, "contagion_coarousal"), _fmt(coarousal.get("coarousal_index"))],
            [_tr(lang, "contagion_null_mean"), _fmt(coarousal.get("coarousal_null_mean"))],
            [_tr(lang, "contagion_null_sd"), _fmt(coarousal.get("coarousal_null_sd"))],
            [_tr(lang, "sync_surrogate_count"), _fmt(config.get("contagion_surrogate_count"))],
            [_tr(lang, "sync_surrogate_seed"), _fmt(config.get("contagion_surrogate_seed"))],
            [_tr(lang, "contagion_p"), _fmt(p_value) if p_value is not None else "—"],
            [_tr(lang, "sync_episode_count"), _fmt(stats.get("episode_count"))],
            [_tr(lang, "contagion_significance"), _significance(lang, coarousal)],
        ],
        [8.0 * cm, 8.6 * cm],
        styles,
    )

    story.append(Paragraph(_tr(lang, "contagion_episodes_heading"), base["Heading2"]))
    episodes = coarousal.get("episodes") or []
    if episodes:
        rows = [[_tr(lang, key) for key in ("no_seg", "contagion_ep_dir", "sync_ep_time", "sync_ep_duration",
                                             "contagion_ep_contrib", "contagion_ep_strength",
                                             "contagion_ep_noncontrib")]]
        for index, episode in enumerate(episodes):
            contributors = episode.get("contributors") or []
            non_contributors = episode.get("non_contributors") or []
            rows.append([
                _fmt(index + 1),
                _tr(lang, _CONTAGION_DIR_KEYS.get(str(episode.get("direction")), "contagion_ep_dir")),
                f"{_fmt_min((episode.get('start_s') or 0) / 60)}–{_fmt_min((episode.get('end_s') or 0) / 60)}",
                _fmt_min((episode.get("duration_s") or 0) / 60),
                ", ".join(str(member) for member in contributors) if contributors else "—",
                _tr(lang, f"contagion_strength_{episode.get('strength')}"),
                ", ".join(str(member) for member in non_contributors) if non_contributors else "—",
            ])
        _table(story, rows, [1.0 * cm, 2.0 * cm, 2.4 * cm, 2.0 * cm, 3.6 * cm, 1.8 * cm, 3.4 * cm], styles)
    else:
        story.append(Paragraph(_tr(lang, "contagion_no_episodes"), base["BodyText"]))
        story.append(Spacer(1, 0.15 * cm))

    primary = coarousal.get("primary_timeline") or []
    if primary:
        story.append(Paragraph(_tr(lang, "contagion_primary_heading"), base["Heading2"]))
        rows = [[_tr(lang, key) for key in ("no_seg", "contagion_ep_dir", "sync_ep_time", "sync_ep_duration")]]
        for index, segment in enumerate(primary):
            rows.append([
                _fmt(index + 1),
                _tr(lang, _CONTAGION_DIR_KEYS.get(str(segment.get("label")), "contagion_ep_dir")),
                f"{_fmt_min(segment.get('start_min'))}–{_fmt_min(segment.get('end_min'))}",
                _fmt_min((segment.get("duration_s") or 0) / 60),
            ])
        _table(story, rows, [1.5 * cm, 4.0 * cm, 5.5 * cm, 5.6 * cm], styles)

    state_percentages = coarousal.get("state_percentages") or []
    if state_percentages:
        story.append(Paragraph(_tr(lang, "contagion_state_heading"), base["Heading2"]))
        rows = [[_tr(lang, key) for key in ("alias", "contagion_state_high", "contagion_state_neutral",
                                             "contagion_state_low")]]
        for row in state_percentages:
            rows.append([row.get("alias") or "-", _fmt(row.get("high_pct")), _fmt(row.get("neutral_pct")),
                         _fmt(row.get("low_pct"))])
        _table(story, rows, [5.0 * cm, 3.8 * cm, 3.8 * cm, 3.8 * cm], styles)
    _embed_charts(story, styles, payload, "coarousal", lang)


def _section_methodology(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    base = styles["base"]
    content = METHODOLOGY.get(lang, METHODOLOGY["pl"])
    _heading(story, styles, content["title"])
    story.append(Paragraph(content["intro"], base["BodyText"]))
    story.append(Spacer(1, 0.2 * cm))

    def block_flowables(block: tuple) -> list[Any]:
        kind = block[0]
        if kind == "p":
            return [Paragraph(block[1], base["BodyText"])]
        if kind == "formula":
            return [Paragraph(block[1], styles["muted"])]
        if kind == "note":
            return [Paragraph(f"<b>{block[1]}:</b> {block[2]}", base["BodyText"])]
        if kind == "list":
            return [Paragraph(f"• {item}", base["BodyText"]) for item in block[1]]
        if kind == "params":
            return [Paragraph(f"<b>{term}</b> — {definition}", base["BodyText"]) for term, definition in block[1]]
        return []

    for section in content["sections"]:
        items: list[Any] = [Paragraph(section["title"], base["Heading2"])]
        for block in section["blocks"]:
            items.extend(block_flowables(block))
        story.append(KeepTogether(items[:2]))
        story.extend(items[2:])
        story.append(Spacer(1, 0.15 * cm))

    comparison = METHODS_COMPARISON.get(lang, METHODS_COMPARISON["pl"])
    story.append(KeepTogether([Paragraph(comparison["title"], base["Heading2"]),
                               Paragraph(comparison["intro"], base["BodyText"])]))
    rows: list[list[str]] = [_tr(lang, "comp_headers").split("|")]
    for row in comparison["rows"]:
        rows.append([
            str(row[0]),
            f"{row[1]}. {row[2]}. {_tr(lang, 'comp_high')}: {row[3]}.",
            f"{row[4]}. {_tr(lang, 'comp_limitation')}: {row[5]}.",
        ])
    _table(story, rows, [3.0 * cm, 7.0 * cm, 6.6 * cm], styles)

    story.append(KeepTogether([Paragraph(_tr(lang, "results_comparison_heading"), base["Heading2"]),
                               Paragraph(_tr(lang, "results_comparison_intro"), base["BodyText"])]))
    participant_count = len(payload.get("dataset_aliases") or [])

    def p_reading(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return _tr(lang, "res_p_unavailable")
        return _tr(lang, "res_p_significant" if float(value) < 0.05 else "res_p_not_significant")

    result_rows: list[list[str]] = [[_tr(lang, key) for key in ("res_method", "res_metric", "res_value",
                                                                 "res_reading")]]
    synchrony = payload.get("synchrony")
    if isinstance(synchrony, dict):
        sstats = synchrony.get("stats") or {}
        result_rows += [
            [_tr(lang, "res_sync"), _tr(lang, "res_sync_metric"), _fmt(sstats.get("mean_synchrony")),
             _tr(lang, "res_sync_reading")],
            [_tr(lang, "res_sync"), _tr(lang, "res_sync_ep"),
             f"{_fmt(sstats.get('episode_count'))} / {_fmt(sstats.get('cluster_count'))}",
             _tr(lang, "res_episodes_reading")],
            [_tr(lang, "res_sync"), _tr(lang, "res_sync_p"), _fmt(sstats.get("mean_synchrony_p_value")),
             p_reading(sstats.get("mean_synchrony_p_value"))],
        ]
    coarousal = payload.get("coarousal")
    if isinstance(coarousal, dict):
        cstats = coarousal.get("stats") or {}
        c_p = coarousal.get("coarousal_p_value")
        result_rows += [
            [_tr(lang, "res_cont"), _tr(lang, "res_cont_metric"), _fmt(coarousal.get("coarousal_index")),
             _tr(lang, "res_cont_reading").format(participants=participant_count or "N")],
            [_tr(lang, "res_cont"), _tr(lang, "res_cont_ep"),
             f"{_fmt(cstats.get('high_episode_count'))} / {_fmt(cstats.get('low_episode_count'))} / "
             f"{_fmt(cstats.get('comovement_episode_count'))}",
             _tr(lang, "res_episodes_reading")],
            [_tr(lang, "res_cont"), _tr(lang, "res_cont_p"), _fmt(c_p) if c_p is not None else "—", p_reading(c_p)],
        ]
    _table(story, result_rows, [2.8 * cm, 3.7 * cm, 2.1 * cm, 8.0 * cm], styles)


def _section_appendix(story: list[Any], styles: dict[str, Any], payload: dict[str, Any], lang: str) -> None:
    base = styles["base"]
    _heading(story, styles, _tr(lang, "appendix_heading"))
    story.append(Paragraph(_tr(lang, "appendix_intro"), base["BodyText"]))
    story.append(Spacer(1, 0.2 * cm))

    story.append(Paragraph(_tr(lang, "appendix_files"), base["Heading2"]))
    rows = [[_tr(lang, "alias"), _tr(lang, "file"), "SHA-256"]]
    for dataset in payload.get("datasets") or []:
        rows.append([dataset.get("alias") or "-", dataset.get("filename") or "-", dataset.get("sha256") or "-"])
    _table(story, rows, [2.6 * cm, 5.0 * cm, 9.0 * cm], styles)

    story.append(Paragraph(_tr(lang, "appendix_versions"), base["Heading2"]))
    rows = [[_tr(lang, "package"), _tr(lang, "version")]]
    rows += [[package, version] for package, version in (payload.get("versions") or {}).items()]
    _table(story, rows, [8.0 * cm, 8.6 * cm], styles)

    story.append(Paragraph(_tr(lang, "appendix_settings"), base["Heading2"]))
    config = payload.get("config") or {}
    # Raw values here (not _fmt) so the table can be copied back into the settings exactly.
    rows = [[_tr(lang, "field"), _tr(lang, "value")]]
    rows += [[name, "-" if value is None else str(value)] for name, value in config.items()]
    _table(story, rows, [9.0 * cm, 7.6 * cm], styles)


def build_story(payload: dict[str, Any]) -> list[Any]:
    """The report flowables; ``payload['lang']`` selects Polish or English."""
    _register_fonts()
    styles = _build_styles()
    lang = resolve_lang(payload.get("lang"))
    story: list[Any] = []
    _section_title(story, styles, payload, lang)
    _section_executive_summary(story, styles, payload, lang)
    _section_import(story, styles, payload, lang)
    _section_cleaning(story, styles, payload, lang)
    _section_synchrony(story, styles, payload, lang)
    _section_coarousal(story, styles, payload, lang)
    _section_methodology(story, styles, payload, lang)
    _section_appendix(story, styles, payload, lang)
    return story


def render_report_pdf(payload: dict[str, Any]) -> bytes:
    lang = resolve_lang(payload.get("lang"))
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.2 * cm,
        leftMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
        title=f"{_tr(lang, 'report_title')} {payload.get('project_name', '')}".strip(),
    )
    doc.build(build_story(payload))
    return buffer.getvalue()
