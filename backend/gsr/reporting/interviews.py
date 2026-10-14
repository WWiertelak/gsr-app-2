"""The PDF report's section of VSR interview moments ("Momenty do wywiadów VSR").

Lays out the stored proposals of the Interviews step (``services/vsr.report_data``): per
interview a table of the moments with lesson, sensor-clock and video times, and for every
moment its justification built from document quotes (``vsr_text``) beside a mini-chart of
the two signals. Report strings live here in Polish and English like the rest of the
report; the justification sentences go through gettext under the report language.
"""

from __future__ import annotations

from typing import Any

from django.utils import translation
from django.utils.formats import number_format
from django.utils.html import format_html
from django.utils.safestring import SafeString

from .. import vsr_text
from ..params import display_number
from ..pipeline.vsr import HIGH, PRIMARY, QUOTE_GSR_DISRUPTION, STEPS
from ..templatetags.gsr_tags import seconds_mmss
from ..vsr_params import VSR_PARAMS
from .vsr_charts import lesson_timeline_svg, moment_chart_svg

_TEXT: dict[str, dict[str, str]] = {
    "pl": {
        "title": "Momenty do wywiadów VSR",
        "lead": "Propozycje momentów do omówienia podczas wywiadów VSR, wybrane według protokołu GSR z dokumentu "
                "„VSR Interview Flow Chart”. Kroki 1–5 pytają po kolei o typ synchronii, a krok 6 powtarza je, aż "
                "wybrano 5 momentów. To propozycja dla badacza, nie wynik analizy: o tym, co omówić, decyduje badacz.",
        "stale": "Uczestnicy wywiadów, parametry lub analiza zmieniły się po wyznaczeniu tych propozycji. "
                 "Raport opisuje ostatnio wyznaczone propozycje.",
        "criteria_heading": "Kryteria z dokumentu",
        "settings_heading": "Parametry wyboru",
        "parameter": "Parametr",
        "value": "Wartość",
        "document_value": "Wartość z dokumentu",
        "video_start": "Start nagrania wideo (zegar czujnika)",
        "video_start_missing": "nie podano",
        "times_note": "Czas lekcji liczy się od wspólnego początku nagrań, jak w Analizie; zegar czujnika to zegar "
                      "zapisany w nagraniach; kod wideo wymaga podania startu nagrania.",
        "pair": "Para {left} + {right}",
        "teacher": "Nauczyciel {alias}",
        "data": "Dane",
        "teacher_against": "Nauczyciel {alias} względem średniej sygnałów uczniów: {students}.",
        "students_average": "Średnia uczniów",
        "step": "KROK",
        "steps_heading": "Momenty spełniające poszczególne kroki",
        "col_step": "Krok",
        "col_type": "Typ synchronii (dokument, s. 1)",
        "col_count": "Liczba momentów",
        "timeline_heading": "Oś lekcji",
        "timeline_label": "Oś lekcji z numerami momentów",
        "moments_heading": "Wybrane momenty",
        "col_number": "Nr",
        "col_status": "Status",
        "col_lesson": "Czas lekcji",
        "col_clock": "Zegar czujnika",
        "col_video": "Wideo",
        "col_shared": "Wspólny stan",
        "decision": "Decyzja",
        "accepted": "zaakceptowany",
        "undecided": "bez decyzji",
        "manual": "dodany ręcznie",
        "time_changed": "czas zmieniony",
        "curated_note": "Lista momentów to lista badacza z kroku 4 Wywiady: kolejność decyduje o roli (pierwsze "
                        "momenty niewykluczone są główne, reszta rezerwowa), a uwzględnione są zaakceptowane, "
                        "przesunięte i dodane ręcznie momenty wraz z powodami i notatkami. Momenty odrzucone "
                        "przez badacza pominięto.",
        "rejected": "Odrzucone przez badacza i pominięte w raporcie: {count}.",
        "added_by": "Dodany przez badacza:",
        "retimed": "Zaproponowano {start}–{end}; czas zmieniony przez badacza:",
        "retimed_note": "Uzasadnienie i pomiary r oraz wspólnego stanu dotyczą zaproponowanego czasu.",
        "needs_check": "Ostatnie wyznaczenie propozycji już nie zawiera tego momentu; decyzja badacza została "
                       "zachowana. Sprawdź go.",
        "note_label": "Notatka:",
        "manual_heading": "Dodany ręcznie",
        "primary": "główny",
        "reserve": "rezerwowy",
        "high": "Wysoki",
        "low": "Niski",
        "justification_heading": "Uzasadnienie każdego momentu",
        "pass": "przejście {number}",
        "lesson": "Lekcja",
        "before_video": "przed startem wideo",
        "chart_note": "Sygnały z-score obu osób wokół momentu (zacieniony).",
        "chart_label": "Sygnały obu osób wokół momentu {number}",
        "no_moments": "Brak momentów na liście.",
        "incomplete": "Wybrano tylko {count} z {target} momentów głównych.",
        "poor_signal": "Sygnał GSR {alias} ma słabą jakość ({pct}% próbek odrzucono przy czyszczeniu). "
                       "Dokument (s. 1):",
        "check_recording": "Sprawdź nagranie przed omówieniem tych momentów.",
        "warning": "Uwaga",
        "summary_label": "Wywiady VSR",
        "summary_unit": "momentów",
        "summary_interviews": "wywiadów: {count}",
        "summary_stale": "propozycje nieaktualne",
        "method_title": "8. Momenty do wywiadów VSR",
        "method_selection": "Propozycje wynikają z protokołu GSR dokumentu „VSR Interview Flow Chart”, a nie z "
                            "wnioskowania statystycznego. Dla każdej pary uczniów, a w wywiadzie z nauczycielem dla "
                            "nauczyciela i średniej sygnałów uczniów (każdy uczeń w z-score, potem średnia ponownie "
                            "w z-score), program liczy korelację Pearsona w przesuwanych oknach na sygnałach "
                            "z-score z analizowanego okresu. Każde okno odpowiada na jeden krok protokołu: 1 to "
                            "ko-ruch i wspólny stan pobudzenia, 2 sam ko-ruch, 3 wspólne wysokie pobudzenie, 4 "
                            "wspólne niskie pobudzenie, 5 brak synchronii. Wspólny stan to ten sam stan, Wysoki "
                            "lub Niski, u obu osób przez zadaną część okna; kolejne okna tego samego kroku tworzą "
                            "moment. Progi korelacji, długość okna i krok podano w tabeli parametrów.",
        "method_choice": "Dokument nie rozstrzyga, który moment wybrać, gdy kilka spełnia ten sam krok, ani jak "
                         "długi jest moment. Program bierze moment, który najmocniej spełnia kryterium kroku "
                         "(najwyższa korelacja w krokach 1–2, największy udział wspólnego stanu w krokach 3–4, "
                         "najniższa korelacja w kroku 5) i w uzasadnieniu podaje, ilu było kandydatów. Kroki 1–5 "
                         "przechodzi się od początku (krok 6), aż wybrano pięć momentów, potem rezerwowe. Momenty "
                         "nie mogą się nakładać; innej reguły odstępu protokół nie ma, więc jej nie stosujemy. "
                         "Stany Wysoki i Niski są względne: dotyczą poziomu danej osoby w analizowanym okresie.",
    },
    "en": {
        "title": "Moments for VSR interviews",
        "lead": "Moments proposed for discussion in the VSR interviews, selected by the GSR protocol of the "
                "“VSR Interview Flow Chart”. STEPS 1–5 ask about the types of synchrony in turn, and "
                "STEP 6 repeats them until 5 moments are selected. This is a proposal for the researcher, not an "
                "analysis result: the researcher decides what to discuss.",
        "stale": "The interview participants, the parameters or the analysis changed after these proposals "
                 "were made. The report describes the last proposals.",
        "criteria_heading": "Criteria from the document",
        "settings_heading": "Selection parameters",
        "parameter": "Parameter",
        "value": "Value",
        "document_value": "Document value",
        "video_start": "Video start (sensor clock)",
        "video_start_missing": "not set",
        "times_note": "Lesson time counts from the recordings' common start, as in the Analysis; the sensor clock is "
                      "the clock stored in the recordings; the video timecode needs the video start.",
        "pair": "Pair {left} + {right}",
        "teacher": "Teacher {alias}",
        "data": "Data",
        "teacher_against": "Teacher {alias} against the average of the students' signals: {students}.",
        "students_average": "Students' average",
        "step": "STEP",
        "steps_heading": "Moments meeting each step",
        "col_step": "Step",
        "col_type": "Synchrony type (document, p. 1)",
        "col_count": "Moments",
        "timeline_heading": "Lesson timeline",
        "timeline_label": "Lesson timeline with the moment numbers",
        "moments_heading": "Selected moments",
        "col_number": "No.",
        "col_status": "Status",
        "col_lesson": "Lesson time",
        "col_clock": "Sensor clock",
        "col_video": "Video",
        "col_shared": "Shared state",
        "decision": "Decision",
        "accepted": "accepted",
        "undecided": "undecided",
        "manual": "added manually",
        "time_changed": "time changed",
        "curated_note": "The list of moments is the researcher's list from the Interviews step: its order decides "
                        "the role (the first moments not rejected are primary, the rest reserve), and it holds the "
                        "accepted, retimed and manually added moments with their reasons and notes. Moments the "
                        "researcher rejected are left out.",
        "rejected": "Rejected by the researcher and left out of the report: {count}.",
        "added_by": "Added by the researcher:",
        "retimed": "Proposed as {start}\u2013{end}; time changed by the researcher:",
        "retimed_note": "The justification and the measurements of r and of the shared state describe the "
                        "proposed time.",
        "needs_check": "The last proposal run no longer proposes this moment; the researcher's decision was kept. "
                       "Check it.",
        "note_label": "Note:",
        "manual_heading": "Added manually",
        "primary": "primary",
        "reserve": "reserve",
        "high": "High",
        "low": "Low",
        "justification_heading": "Justification of each moment",
        "pass": "pass {number}",
        "lesson": "Lesson",
        "before_video": "before the video starts",
        "chart_note": "Z-scored signals of both people around the moment (shaded).",
        "chart_label": "Signals of both people around moment {number}",
        "no_moments": "There are no moments on the list.",
        "incomplete": "Only {count} of {target} primary moments were selected.",
        "poor_signal": "The GSR signal of {alias} has poor quality ({pct}% of its samples were rejected on the "
                       "Cleaning step). The document (page 1):",
        "check_recording": "Check the recording before discussing these moments.",
        "warning": "Note",
        "summary_label": "VSR interviews",
        "summary_unit": "moments",
        "summary_interviews": "interviews: {count}",
        "summary_stale": "proposals outdated",
        "method_title": "8. Moments for VSR interviews",
        "method_selection": "The proposals follow the GSR protocol of the “VSR Interview Flow Chart”, not "
                            "statistical inference. For each student pair, and in the teacher interview for the "
                            "teacher and the students' average signal (each student z-scored, then the average "
                            "z-scored again), the program computes the Pearson correlation in sliding windows of the "
                            "z-scored signals of the analysed period. Each window answers one step of the protocol: "
                            "1 is co-movement and a shared arousal state, 2 co-movement only, 3 shared HIGH arousal, "
                            "4 shared LOW arousal, 5 not synchronous. A shared state is the same state, High or Low, "
                            "in both people for a given part of the window; consecutive windows answering the same "
                            "step form a moment. The correlation thresholds, the window length and the step are in "
                            "the parameter table.",
        "method_choice": "The document does not say which moment to take when several meet the same step, nor how "
                         "long a moment is. The program takes the moment that meets the step's criterion most "
                         "strongly (highest correlation in steps 1–2, largest share of the shared state in "
                         "steps 3–4, lowest correlation in step 5) and states in the justification how many "
                         "candidates there were. Steps 1–5 are walked again (step 6) until five moments are "
                         "selected, then the reserve moments. Moments may not overlap; the protocol has no other "
                         "spacing rule, so none is applied. The High and Low states are relative: they refer to a "
                         "person's own level within the analysed period.",
    },
}


def _t(lang: str, key: str, **values: Any) -> str:
    text = _TEXT[lang][key]
    return text.format(**values) if values else text


def _p(content: Any, style: str = "") -> dict[str, Any]:
    return {"type": "p", "text": content, "style": style}


def _table(header: list[str], rows: list[list[Any]], numeric: tuple[int, ...] = (), kind: str = "grid") -> dict[str, Any]:
    return {"type": "table", "kind": kind, "header": header, "rows": rows, "numeric": numeric}


def _interview_label(lang: str, interview: dict[str, Any]) -> str:
    participants = interview["participants"]
    if interview["kind"] == "teacher":
        return _t(lang, "teacher", alias=participants[0])
    return _t(lang, "pair", left=participants[0], right=participants[1])


def _decision(lang: str, moment: dict[str, Any]) -> str:
    text = _t(lang, moment["decision"])
    return f"{text}, {_t(lang, 'time_changed')}" if moment["time_changed"] else text


def _correlation(moment: dict[str, Any]) -> str:
    return "\u2014" if moment["correlation"] is None else vsr_text.correlation(moment["correlation"])


def _shared_state(lang: str, moment: dict[str, Any]) -> str:
    if not moment["shared_state"]:
        return "—"
    name = _t(lang, "high" if moment["shared_state"] == HIGH else "low")
    return f"{name} {vsr_text.percent(moment['shared_state_fraction'])}%"


def _video(lang: str, moment: dict[str, Any], video_start: str | None) -> str:
    if moment["start_video"]:
        return f"{moment['start_video']}–{moment['end_video'] or '…'}"
    return _t(lang, "before_video") if video_start else "—"


def _clock(moment: dict[str, Any]) -> str:
    return f"{moment['start_clock']}–{moment['end_clock']}" if moment["start_clock"] else "—"


def _lesson(moment: dict[str, Any]) -> str:
    return f"{seconds_mmss(moment['start_seconds'])}–{seconds_mmss(moment['end_seconds'])}"


def _status(lang: str, moment: dict[str, Any]) -> str:
    return _t(lang, "primary" if moment["status"] == PRIMARY else "reserve")


def _step_label(lang: str, number: int) -> str:
    return f"{_t(lang, 'step')} {number}"


def _step_heading(lang: str, moment: dict[str, Any]) -> str:
    """The step the moment answers; a manual moment answers none."""
    return "\u2014" if moment["step"] is None else _step_label(lang, moment["step"])


def _settings_blocks(vsr: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    config = vsr["config"]
    rows = [[str(param.label), display_number(config[param.name]), display_number(param.default)]
            for param in VSR_PARAMS]
    video_start = (vsr["roles"] or {}).get("video_start_time") or _t(lang, "video_start_missing")
    rows.append([_t(lang, "video_start"), video_start, "—"])
    return [
        {"type": "h2", "text": _t(lang, "settings_heading")},
        _table([_t(lang, "parameter"), _t(lang, "value"), _t(lang, "document_value")], rows, numeric=(1, 2)),
    ]


def _warnings(vsr: dict[str, Any], interview: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    blocks = []
    for alias in interview["participants"]:
        quality = vsr["quality"].get(alias) or {}
        if quality.get("level") != "poor":
            continue
        blocks.append({
            "type": "note",
            "label": _t(lang, "warning"),
            "text": format_html(
                "{} {} {}",
                _t(lang, "poor_signal", alias=alias, pct=number_format(quality["rejected_pct"], 0)),
                vsr_text.quote(QUOTE_GSR_DISRUPTION.as_dict(), ellipsis=True),
                _t(lang, "check_recording"),
            ),
            "style": "warning",
        })
    stats = interview["stats"]
    if not stats["complete"]:
        blocks.append({
            "type": "note",
            "label": _t(lang, "warning"),
            "text": _t(lang, "incomplete", count=stats["primary_count"], target=stats["primary_target"]),
            "style": "warning",
        })
    return blocks


def _step_blocks(interview: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    rows = [
        [_step_label(lang, number), vsr_text.quote(step.synchrony_type.as_dict()),
         str(interview["candidate_counts"].get(str(number), 0))]
        for number, step in STEPS.items()
    ]
    first_pass = interview["passes"][0]["steps"] if interview["passes"] else []
    empty = [vsr_text.step_sentence(record["fragments"][0]) for record in first_pass if record["answer"] == "NO"]
    blocks: list[dict[str, Any]] = [
        {"type": "h3", "text": _t(lang, "steps_heading")},
        _table([_t(lang, "col_step"), _t(lang, "col_type"), _t(lang, "col_count")], rows, numeric=(2,)),
    ]
    if empty:
        blocks.append({"type": "list", "items": empty})
    return blocks


def _names(interview: dict[str, Any], lang: str) -> list[str]:
    first = interview["participants"][0]
    return [first, _t(lang, "students_average") if interview["kind"] == "teacher" else interview["participants"][1]]


def _sentences(lang: str, moment: dict[str, Any]) -> list[Any]:
    """A proposed moment's justification from the document; a manual moment's is the researcher's reason."""
    if moment["source"] == "manual":
        return [f"{_t(lang, 'added_by')} {moment['reason']}"]
    return vsr_text.justification(moment["justification"])


def _change(lang: str, moment: dict[str, Any]) -> list[str]:
    """What the researcher changed about a proposed moment's time, and why."""
    if not moment["time_changed"]:
        return []
    proposed = _t(lang, "retimed", start=seconds_mmss(moment["original_start_seconds"]),
                  end=seconds_mmss(moment["original_end_seconds"]))
    return [f"{proposed} {moment['reason']}", _t(lang, "retimed_note")]


def _moment_cards(interview: dict[str, Any], vsr: dict[str, Any], lang: str) -> dict[str, Any]:
    video_start = (vsr["roles"] or {}).get("video_start_time")
    names, colors, trace = _names(interview, lang), interview["colors"], interview["trace"]
    cards = []
    for moment in interview["moments"]:
        chart = None
        if trace is not None:
            svg = moment_chart_svg(trace, moment, names, colors,
                                   _t(lang, "chart_label", number=moment["number"]))
            chart = SafeString(svg) if svg else None
        cards.append({
            "number": moment["number"],
            "primary": moment["status"] == PRIMARY,
            "status": _status(lang, moment),
            "step": _t(lang, "manual_heading") if moment["source"] == "manual" else _step_heading(lang, moment),
            "type": vsr_text.quote(moment["synchrony_type"]) if moment["synchrony_type"] else "",
            "pass": _t(lang, "pass", number=moment["pass_number"]) if (moment["pass_number"] or 0) > 1 else "",
            "decision": _decision(lang, moment),
            "check": _t(lang, "needs_check") if moment["needs_check"] else "",
            "times": [
                (_t(lang, "lesson"), _lesson(moment)),
                (_t(lang, "col_clock"), _clock(moment)),
                (_t(lang, "col_video"), _video(lang, moment, video_start)),
                *((("r", _correlation(moment)), (_t(lang, "col_shared"), _shared_state(lang, moment)))
                  if moment["source"] != "manual" else ()),
            ],
            "chart": chart,
            "chart_note": _t(lang, "chart_note"),
            "sentences": _sentences(lang, moment),
            "change": _change(lang, moment),
            "note": moment["note"],
            "note_label": _t(lang, "note_label"),
        })
    return {"type": "moment_cards", "items": cards}


def _interview_blocks(interview: dict[str, Any], vsr: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    video_start = (vsr["roles"] or {}).get("video_start_time")
    lesson = vsr["lesson"]
    moments = interview["moments"]
    basis = [vsr_text.quote(interview["data_basis"]), *(vsr_text.quote(note) for note in interview["basis_notes"])]
    blocks: list[dict[str, Any]] = [
        {"type": "h2", "text": _interview_label(lang, interview)},
        _p(format_html("<strong>{}:</strong> {}", _t(lang, "data"), SafeString(" · ".join(basis)))),
    ]
    if interview["kind"] == "teacher":
        blocks.append(_p(_t(lang, "teacher_against", alias=interview["participants"][0],
                            students=", ".join(interview["participants"][1:])), style="muted"))
    blocks += _warnings(vsr, interview, lang)
    blocks += _step_blocks(interview, lang)
    if interview["rejected_count"]:
        blocks.append(_p(_t(lang, "rejected", count=interview["rejected_count"]), style="muted"))
    if not moments:
        return blocks + [_p(_t(lang, "no_moments"), style="muted")]
    blocks += [
        {"type": "h3", "text": _t(lang, "timeline_heading")},
        {"type": "svg", "markup": SafeString(lesson_timeline_svg(
            moments, lesson["start_seconds"], lesson["end_seconds"], _t(lang, "timeline_label")))},
        {"type": "h3", "text": _t(lang, "moments_heading")},
        _table(
            [_t(lang, key) for key in ("col_number", "col_status", "decision", "col_step", "col_lesson", "col_clock",
                                       "col_video")]
            + ["r", _t(lang, "col_shared")],
            [[str(moment["number"]), _status(lang, moment), _decision(lang, moment), _step_heading(lang, moment),
              _lesson(moment), _clock(moment), _video(lang, moment, video_start), _correlation(moment),
              _shared_state(lang, moment)] for moment in moments],
            numeric=(0, 7),
        ),
        {"type": "h3", "text": _t(lang, "justification_heading")},
        _moment_cards(interview, vsr, lang),
    ]
    return blocks


def build_section(vsr: dict[str, Any] | None, lang: str) -> dict[str, Any] | None:
    """The report section for the stored proposals (``services/vsr.report_data``), or None without any."""
    if not vsr:
        return None
    with translation.override(lang):
        blocks: list[dict[str, Any]] = []
        if vsr["stale"]:
            blocks.append({"type": "note", "label": _t(lang, "warning"), "text": _t(lang, "stale"), "style": "warning"})
        blocks += [
            _p(_t(lang, "lead"), style="lead"),
            *([_p(_t(lang, "curated_note"))] if vsr["curated"] else []),
            {"type": "h2", "text": _t(lang, "criteria_heading")},
            {"type": "list", "items": [vsr_text.quote(item) for item in vsr["criteria"].values()]},
            *_settings_blocks(vsr, lang),
            _p(_t(lang, "times_note"), style="muted"),
        ]
        for interview in vsr["interviews"]:
            blocks += _interview_blocks(interview, vsr, lang)
    return {"id": "interviews", "title": _t(lang, "title"), "blocks": blocks}


def summary_kpi(vsr: dict[str, Any] | None, lang: str) -> dict[str, Any] | None:
    """The summary page's tile: interviews × primary + reserve moments."""
    if not vsr:
        return None
    interviews = vsr["interviews"]
    counts = {(item["stats"]["primary_count"], item["stats"]["reserve_count"]) for item in interviews}
    stats = vsr["stats"]
    if len(counts) == 1:
        primary, reserve = counts.pop()
        value = f"{len(interviews)} × {primary} + {reserve}"
    else:
        value = f"{stats['primary_total']} + {stats['reserve_total']}"
    details = [_t(lang, "summary_interviews", count=len(interviews))]
    if vsr["stale"]:
        details.append(_t(lang, "summary_stale"))
    return {"label": _t(lang, "summary_label"), "value": value, "unit": _t(lang, "summary_unit"), "details": details}


def methodology_blocks(lang: str) -> list[dict[str, Any]]:
    """The short note on how the proposals are made, appended to the Methodology."""
    return [
        {"type": "h2", "text": _t(lang, "method_title")},
        _p(_t(lang, "method_selection")),
        _p(_t(lang, "method_choice")),
    ]
