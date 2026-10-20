"""The PDF report's section of VSR interview moments ("Momenty do wywiadów VSR").

Lays out the moments of the Interviews step (``services/vsr.report_data``: the researcher's
list without rejected moments, or the chosen moments while nothing is decided): the Analysis
run and settings they come from, and per interview a table of the moments with lesson,
sensor-clock times, and a card per moment with the same preview chart and the same
one-sentence justification (``vsr_text``) as the Interviews screen. Report strings live here
in Polish and English like the rest of the report; the justification goes through gettext
under the report language.
"""

from __future__ import annotations

from typing import Any

from django.utils import translation
from django.utils.formats import number_format
from django.utils.safestring import SafeString

from .. import params, vsr_text
from ..pipeline.vsr import HIGH, LOW, PRIMARY
from ..templatetags.gsr_tags import seconds_mmss
from .vsr_charts import lesson_timeline_svg, moment_chart_svg

_TEXT: dict[str, dict[str, str]] = {
    "pl": {
        "title": "Momenty do wywiadów VSR",
        "lead": "Momenty do omówienia w wywiadach VSR, wybrane z wyników Analizy: epizody synchronizacji i "
                "ko-pobudzenia danej pary (lub nauczyciela ze średnią uczniów) przypisano do kroków schematu "
                "„VSR Interview Flow Chart”, najsilniejsze najpierw, aż wybrano momenty główne i rezerwowe. To "
                "propozycja dla badacza, nie wynik analizy: o tym, co omówić, decyduje badacz.",
        "stale": "Uczestnicy wywiadów, liczba momentów lub Analiza zmieniły się po wybraniu tych momentów. "
                 "Raport opisuje ostatnio wybrane momenty.",
        "steps_heading": "Kroki schematu VSR",
        "source_heading": "Źródło: Analiza",
        "parameter": "Ustawienie",
        "value": "Wartość",
        "analysis_run": "Przebieg Analizy",
        "moment_counts": "Momenty na wywiad",
        "moment_counts_value": "{primary} głównych + {reserve} rezerwowych",
        "times_note": "Czas lekcji liczy się od wspólnego początku nagrań, jak w Analizie; zegar czujnika to zegar "
                      "zapisany w nagraniach GSR, według którego badacz odnajduje moment.",
        "pair": "Para {left} + {right}",
        "teacher": "Nauczyciel {alias}",
        "teacher_against": "Nauczyciel {alias} względem średniej sygnałów uczniów (każdy uczeń w z-score): "
                           "{students}.",
        "students_average": "Średnia uczniów",
        "step": "Krok",
        "steps_count_heading": "Epizody Analizy w poszczególnych krokach",
        "col_step": "Krok",
        "col_name": "Opis",
        "col_count": "Epizody",
        "timeline_heading": "Oś lekcji",
        "timeline_label": "Oś lekcji z numerami momentów",
        "moments_heading": "Wybrane momenty",
        "col_number": "Nr",
        "col_status": "Status",
        "col_lesson": "Czas lekcji",
        "col_clock": "Zegar czujnika",
        "col_episode": "Epizod",
        "decision": "Decyzja",
        "accepted": "zaakceptowany",
        "undecided": "bez decyzji",
        "manual": "dodany ręcznie",
        "time_changed": "czas zmieniony",
        "synchrony_episode": "synchronizacja E{number}",
        "divergent_episode": "rozbieżna D{number}",
        "decoupled_episode": "rozprzężenie U{number}",
        "coarousal_episode": "ko-pobudzenie {number}",
        "own_whose_dyad": "pary",
        "own_whose_teacher": "nauczyciela i średniej uczniów",
        "own_synchronization": "synchronizacja",
        "own_co-movement": "ko-ruch",
        "own_state": "wspólny stan",
        "own_divergent": "synchronia rozbieżna",
        "own_decoupled": "rozprzężenie",
        "own_opposite": "pobudzenie przeciwstawne",
        "curated_note": "Lista momentów to lista badacza z kroku 4 Wywiady: kolejność decyduje o roli (pierwsze "
                        "momenty niewykluczone są główne, reszta rezerwowa), a uwzględnione są zaakceptowane, "
                        "przesunięte i dodane ręcznie momenty wraz z powodami i notatkami. Momenty odrzucone "
                        "przez badacza pominięto.",
        "rejected": "Odrzucone przez badacza i pominięte w raporcie: {count}.",
        "added_by": "Dodany przez badacza:",
        "retimed": "Zaproponowano {start}–{end}; czas zmieniony przez badacza:",
        "retimed_clock": "Zaproponowano {start}–{end} (zegar czujnika {clock_start}–{clock_end}); czas zmieniony "
                         "przez badacza:",
        "needs_check_rules": "Decyzję podjęto, gdy momenty wybrano według wcześniejszych reguł; aplikacja czyta teraz "
                             "schemat VSR według siły korelacji. Decyzja badacza została zachowana. Sprawdź ją.",
        "stale_rules": "Te momenty wybrano według wcześniejszych reguł. Aplikacja czyta teraz kryteria korelacji "
                       "schematu VSR według siły (krok 4b synchronia rozbieżna; krok 5 pobudzenie przeciwstawne i "
                       "rozprzężenie). Decyzje badacza zostaną zachowane i oznaczone do sprawdzenia.",
        "needs_check": "Ostatni wybór momentów już nie zawiera tego momentu; decyzja badacza została zachowana. "
                       "Sprawdź go.",
        "note_label": "Notatka:",
        "manual_heading": "Dodany ręcznie",
        "primary": "główny",
        "reserve": "rezerwowy",
        "cards_heading": "Podgląd i uzasadnienie każdego momentu",
        "lesson": "Lekcja",
        "chart_note": "Sygnały (z-score) wokół momentu (zacieniony) na zegarze czujnika, jak na kroku Wywiady.",
        "chart_label": "Sygnały wokół momentu {number}",
        "no_moments": "Brak momentów na liście.",
        "incomplete": "Sygnały wywiadu mają epizody tylko dla {count} z {target} momentów głównych.",
        "poor_signal": "Sygnał GSR {alias} ma słabą jakość ({pct}% próbek odrzucono przy czyszczeniu). Sprawdź "
                       "nagranie przed omówieniem tych momentów.",
        "warning": "Uwaga",
        "summary_label": "Wywiady VSR",
        "summary_unit": "momentów",
        "summary_interviews": "wywiadów: {count}",
        "summary_stale": "momenty nieaktualne",
        "method_title": "8. Momenty do wywiadów VSR",
        "method_selection": "Momenty każdego wywiadu pochodzą z analizy jego własnych dwóch sygnałów: tymi samymi "
                            "funkcjami i ustawieniami co przebieg Analizy, zastosowanymi tylko do sygnałów pary "
                            "(schemat VSR: momenty na podstawie danych GSR diady) albo "
                            "nauczyciela i średniej sygnałów uczniów (schemat VSR: momenty na podstawie zagregowanych "
                            "danych nauczyciela i uczniów). Momenty pary nie zależą więc od tego, kto jeszcze jest w "
                            "projekcie; ekran Analizy pokazuje całą klasę. Kryteria schematu VSR („Synchronous = above "
                            "0.8”, „Asynchronous = below 0.1”) czytane są jako siła korelacji, czyli jej wartość "
                            "bezwzględna. Kroki: 1 to epizod ko-ruchu (synchronizacji lub ko-ruchu z zakładki "
                            "Ko-pobudzenie), którego r osiąga próg momentu wywiadu (0,8), nakładający się na wspólny "
                            "stan Wysoki lub Niski; 2 taki epizod ko-ruchu bez wspólnego stanu; 3 i 4 epizod wspólnego "
                            "stanu Wysokiego lub Niskiego bez takiego ko-ruchu; 4b, przechodzony po kroku 5, żeby pierwsze pięć "
                            "momentów zachowało „4 synchronous, 1 not synchronous” schematu, to epizod synchronizacji "
                            "rozbieżnej (sygnały zmieniają się w przeciwnych kierunkach), którego r jest równe lub "
                            "niższe od minus progu momentu wywiadu (za prośbą zespołu Hassana, „ideally above .80 for "
                            "the interviews”); 5 brak synchronii: najpierw pobudzenie przeciwstawne (zakładka Ko-pobudzenie: jedna "
                            "osoba w stanie Wysokim, druga w Niskim), potem odcinek rozprzężenia między epizodami "
                            "synchronizacji i rozbieżności pary, w którym każde okno ma |r| < 0,1 przez co najmniej "
                            "minimalny czas epizodu (reguła rozprzężenia z ustawień Analizy).",
        "method_choice": "Korelacja r momentu to średnia z okien zakładki Synchronizacja (to samo okno, krok i "
                         "metoda) dla sygnałów pary albo nauczyciela i średniej uczniów (każdy uczeń w z-score, "
                         "potem średnia ponownie w z-score). Gdy kilka epizodów spełnia ten sam krok, pierwszy jest "
                         "najsilniejszy: najwyższe r w krokach 1–2, najniższe r w kroku 4b; w kroku 5 pobudzenie "
                         "przeciwstawne przed rozprzężeniem, a z odcinków rozprzężenia r najbliższe zera "
                         "(najwyraźniejszy brak związku); potem dłuższy. Kroki przechodzi się od początku (krok 6), "
                         "aż wybrano momenty główne, potem rezerwowe. Momenty nie mogą się nakładać.",
    },
    "en": {
        "title": "Moments for VSR interviews",
        "lead": "Moments to discuss in the VSR interviews, chosen from the Analysis results: the synchronization "
                "and co-arousal episodes of each pair (or of the teacher with the students' average) were sorted into "
                "the steps of the “VSR Interview Flow Chart”, the strongest first, until the primary and reserve "
                "moments were chosen. This is a proposal for the researcher, not an analysis result: the "
                "researcher decides what to discuss.",
        "stale": "The interview participants, the number of moments or the Analysis changed after these moments "
                 "were chosen. The report describes the last chosen moments.",
        "steps_heading": "Steps of the VSR flow chart",
        "source_heading": "Source: the Analysis",
        "parameter": "Setting",
        "value": "Value",
        "analysis_run": "Analysis run",
        "moment_counts": "Moments per interview",
        "moment_counts_value": "{primary} primary + {reserve} reserve",
        "times_note": "Lesson time counts from the recordings' common start, as in the Analysis; the sensor clock is "
                      "the clock stored in the GSR recordings, used by the researcher to locate the moment.",
        "pair": "Pair {left} + {right}",
        "teacher": "Teacher {alias}",
        "teacher_against": "Teacher {alias} against the average of the students' signals (each student z-scored): "
                           "{students}.",
        "students_average": "Students' average",
        "step": "Step",
        "steps_count_heading": "Analysis episodes per step",
        "col_step": "Step",
        "col_name": "Description",
        "col_count": "Episodes",
        "timeline_heading": "Lesson timeline",
        "timeline_label": "Lesson timeline with the moment numbers",
        "moments_heading": "Chosen moments",
        "col_number": "No.",
        "col_status": "Status",
        "col_lesson": "Lesson time",
        "col_clock": "Sensor clock",
        "col_episode": "Episode",
        "decision": "Decision",
        "accepted": "accepted",
        "undecided": "undecided",
        "manual": "added manually",
        "time_changed": "time changed",
        "synchrony_episode": "synchronization E{number}",
        "divergent_episode": "divergent D{number}",
        "decoupled_episode": "decoupling U{number}",
        "coarousal_episode": "co-arousal {number}",
        "own_whose_dyad": "of the pair",
        "own_whose_teacher": "of the teacher and students' average",
        "own_synchronization": "synchronization",
        "own_co-movement": "co-movement",
        "own_state": "shared state",
        "own_divergent": "divergent synchrony",
        "own_decoupled": "decoupling",
        "own_opposite": "opposite arousal",
        "curated_note": "The list of moments is the researcher's list from the Interviews step: its order decides "
                        "the role (the first moments not rejected are primary, the rest reserve), and it holds the "
                        "accepted, retimed and manually added moments with their reasons and notes. Moments the "
                        "researcher rejected are left out.",
        "rejected": "Rejected by the researcher and left out of the report: {count}.",
        "added_by": "Added by the researcher:",
        "retimed": "Proposed as {start}–{end}; time changed by the researcher:",
        "retimed_clock": "Proposed as {start}–{end} (sensor clock {clock_start}–{clock_end}); time changed by the "
                         "researcher:",
        "needs_check_rules": "The decision was made when the moments were chosen under the earlier rules; the app now "
                             "reads the VSR flow chart by correlation strength. The researcher's decision was kept. "
                             "Check it.",
        "stale_rules": "These moments were chosen under the earlier rules. The app now reads the VSR flow chart's "
                       "correlation criteria by strength (step 4b divergent synchrony; step 5 opposite arousal and "
                       "decoupling). The researcher's decisions will be kept and marked for checking.",
        "needs_check": "The last choice of moments no longer includes this moment; the researcher's decision was "
                       "kept. Check it.",
        "note_label": "Note:",
        "manual_heading": "Added manually",
        "primary": "primary",
        "reserve": "reserve",
        "cards_heading": "Preview and justification of each moment",
        "lesson": "Lesson",
        "chart_note": "Signals (z-scores) around the moment (shaded) on the sensor clock, as on the Interviews step.",
        "chart_label": "Signals around moment {number}",
        "no_moments": "There are no moments on the list.",
        "incomplete": "The interview's signals have episodes for only {count} of {target} primary moments.",
        "poor_signal": "The GSR signal of {alias} has poor quality ({pct}% of its samples were rejected on the "
                       "Cleaning step). Check the recording before discussing these moments.",
        "warning": "Note",
        "summary_label": "VSR interviews",
        "summary_unit": "moments",
        "summary_interviews": "interviews: {count}",
        "summary_stale": "moments outdated",
        "method_title": "8. Moments for VSR interviews",
        "method_selection": "Every interview's moments come from the analysis of its own two signals: the same "
                            "functions and settings as the Analysis run, applied to the pair's two signals alone (the "
                            "VSR flow chart: moments based on the dyad's GSR data) or to "
                            "the teacher and the students' average signal (the VSR flow chart: moments based on the "
                            "teacher and student aggregated data). A pair's moments therefore do not depend on who "
                            "else is in the project; the Analysis screen shows the whole class. The VSR flow chart's "
                            "criteria (“Synchronous = above 0.8”, “Asynchronous = below 0.1”) are read as the strength "
                            "of the correlation, its absolute value. The steps: 1 is a co-movement episode (synchronization, or "
                            "co-movement on the Co-arousal tab) whose r reaches the interview moment threshold (0.8) "
                            "overlapping a shared High or Low state; 2 such a co-movement episode without a shared "
                            "state; 3 and 4 a shared High or Low episode without such co-movement; 4b, walked after step "
                            "5 so the first five moments keep the flow chart's “4 synchronous, 1 not synchronous”, a "
                            "divergent synchronization episode (the signals move in opposite directions) whose r is at "
                            "or below minus the interview moment threshold (as Hassan's team asked, “ideally above .80 "
                            "for the interviews”); 5 no synchrony: opposite arousal first (Co-arousal tab: one person in the "
                            "High state while the other is in the Low state), then a decoupling stretch between the "
                            "pair's synchronization and divergent episodes in which every window has |r| < 0.1 for at "
                            "least the minimum episode duration (the decoupling rule of the Analysis settings).",
        "method_choice": "A moment's correlation r is the mean over the Synchronization tab's windows (same window, "
                         "step and method) of the pair's signals, or of the teacher and the students' average (each "
                         "student z-scored, then the average z-scored again). When several episodes answer the "
                         "same step, the strongest comes first: the highest r in steps 1–2, the lowest r in step 4b; "
                         "in step 5 opposite arousal before decoupling, and of the decoupling stretches the r closest "
                         "to zero (the clearest absence of a relation); then the longer one. "
                         "The steps are walked again (step 6) until the primary moments are chosen, then the reserve. "
                         "Moments may not overlap.",
    },
}

# The Analysis settings the section states, in the Analysis' own labels.
_SETTINGS = (
    "synchrony_threshold", "synchrony_window_seconds", "synchrony_step_seconds",
    "synchrony_episode_min_duration_seconds", "synchrony_episode_max_gap_seconds", "synchrony_interview_threshold",
    "synchrony_divergent_threshold", "synchrony_decoupled_threshold", "contagion_high_z", "contagion_low_z",
)


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


def _clock(moment: dict[str, Any]) -> str:
    return f"{moment['start_clock']}–{moment['end_clock']}" if moment["start_clock"] else "—"


def _lesson(moment: dict[str, Any]) -> str:
    return f"{seconds_mmss(moment['start_seconds'])}–{seconds_mmss(moment['end_seconds'])}"


def _status(lang: str, moment: dict[str, Any]) -> str:
    return _t(lang, "primary" if moment["status"] == PRIMARY else "reserve")


def _step_cell(lang: str, moment: dict[str, Any]) -> str:
    """The step the moment answers; a manual moment answers none."""
    return "—" if not moment["step"] else f"{_t(lang, 'step')} {moment['step']}"


def _episode_cell(lang: str, moment: dict[str, Any], interview_kind: str) -> str:
    """The episode a moment is: of the interview's own analysis, or (moments chosen from the whole-class Analysis)
    its number on the Analysis screen."""
    episode = moment["episode"]
    if not episode:
        return "—"
    if episode["source"] == "own":
        whose = _t(lang, f"own_whose_{'teacher' if interview_kind == 'teacher' else 'dyad'}")
        return f"{_t(lang, 'own_' + (episode.get('kind') or 'synchronization'))} {whose}"
    key = "synchrony_episode" if episode["source"] == "synchrony" else "coarousal_episode"
    if episode["source"] == "synchrony" and episode.get("kind") in ("divergent", "decoupled"):
        key = f"{episode['kind']}_episode"
    return _t(lang, key, number=episode["number"])


def _source_blocks(vsr: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    settings = vsr["analysis_settings"]
    labels = {param.name: str(param.label) for param in params.ANALYSIS_PARAMS}
    rows = [[labels.get(name, name), params.display_number(settings[name])] for name in _SETTINGS if name in settings]
    computed = (vsr.get("analysis_computed_at") or "")[:16].replace("T", " ")
    counts = vsr["counts"]
    rows = [
        [_t(lang, "analysis_run"), computed or "—"],
        *rows,
        [_t(lang, "moment_counts"), _t(lang, "moment_counts_value", primary=counts["primary"],
                                       reserve=counts["reserve"])],
    ]
    return [
        {"type": "h2", "text": _t(lang, "source_heading")},
        _table([_t(lang, "parameter"), _t(lang, "value")], rows, numeric=(1,)),
    ]


def _warnings(vsr: dict[str, Any], interview: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    blocks = []
    for alias in interview["participants"]:
        quality = vsr["quality"].get(alias) or {}
        if quality.get("level") == "poor":
            blocks.append({"type": "note", "label": _t(lang, "warning"), "style": "warning",
                           "text": _t(lang, "poor_signal", alias=alias, pct=number_format(quality["rejected_pct"], 0))})
    stats = interview["stats"]
    if not stats["complete"]:
        blocks.append({"type": "note", "label": _t(lang, "warning"), "style": "warning",
                       "text": _t(lang, "incomplete", count=stats["primary_count"], target=stats["primary_target"])})
    return blocks


def _step_blocks(interview: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    counts = interview.get("candidate_counts", {})
    rows = [[str(step["number"]), step["name"], str(counts.get(str(step["number"]), 0))] for step in vsr_text.steps()]
    return [
        {"type": "h3", "text": _t(lang, "steps_count_heading")},
        _table([_t(lang, "col_step"), _t(lang, "col_name"), _t(lang, "col_count")], rows, numeric=(0, 2)),
    ]


def _names(interview: dict[str, Any], lang: str) -> list[str]:
    first = interview["participants"][0]
    return [first, _t(lang, "students_average") if interview["kind"] == "teacher" else interview["participants"][1]]


def _sentence(lang: str, moment: dict[str, Any], kind: str) -> str:
    """The proposed moment's justification from the Analysis; a manual moment's is the researcher's reason."""
    if moment["source"] == "manual":
        return f"{_t(lang, 'added_by')} {moment['reason']}"
    return vsr_text.justification(moment["proposal"], kind)


def _change(lang: str, moment: dict[str, Any]) -> list[str]:
    """What the researcher changed about a proposed moment's time, and why."""
    if not moment["time_changed"]:
        return []
    times = {"start": seconds_mmss(moment["original_start_seconds"]), "end": seconds_mmss(moment["original_end_seconds"])}
    if moment.get("original_start_clock") and moment.get("original_end_clock"):
        proposed = _t(lang, "retimed_clock", **times, clock_start=moment["original_start_clock"],
                      clock_end=moment["original_end_clock"])
    else:
        proposed = _t(lang, "retimed", **times)
    return [f"{proposed} {moment['reason']}"]


def _chart(vsr: dict[str, Any], interview: dict[str, Any], moment: dict[str, Any], lang: str) -> SafeString | None:
    if not moment.get("preview"):
        return None
    settings = vsr["analysis_settings"]
    state = moment["shared_state"] if moment["source"] != "manual" else None
    opposite = moment["source"] != "manual" and moment.get("type") == "opposite"
    svg = moment_chart_svg(
        moment["preview"], _names(interview, lang), interview["colors"],
        _t(lang, "chart_label", number=moment["number"]), moment["number"],
        high=settings.get("contagion_high_z") if state == HIGH or opposite else None,
        low=settings.get("contagion_low_z") if state == LOW or opposite else None,
        clock=vsr.get("clock_start"),
    )
    return SafeString(svg) if svg else None


def _moment_cards(interview: dict[str, Any], vsr: dict[str, Any], lang: str) -> dict[str, Any]:
    cards = []
    for moment in interview["moments"]:
        manual = moment["source"] == "manual"
        cards.append({
            "number": moment["number"],
            "primary": moment["status"] == PRIMARY,
            "status": _status(lang, moment),
            "step": _t(lang, "manual_heading") if manual else vsr_text.step_title(moment["step"]),
            "decision": _decision(lang, moment),
            "check": "" if not moment["needs_check"] else _t(
                lang, "needs_check_rules" if moment["check_reason"] == "rules" else "needs_check"),
            "times": [
                (_t(lang, "lesson"), _lesson(moment)),
                (_t(lang, "col_clock"), _clock(moment)),
            ],
            "chart": _chart(vsr, interview, moment, lang),
            "chart_note": _t(lang, "chart_note"),
            "sentence": _sentence(lang, moment, interview["kind"]),
            "change": _change(lang, moment),
            "note": moment["note"],
            "note_label": _t(lang, "note_label"),
        })
    return {"type": "moment_cards", "items": cards}


def _interview_blocks(interview: dict[str, Any], vsr: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    lesson = vsr["lesson"]
    moments = interview["moments"]
    blocks: list[dict[str, Any]] = [{"type": "h2", "text": _interview_label(lang, interview)}]
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
            moments, lesson["start_seconds"], lesson["end_seconds"], _t(lang, "timeline_label"),
            vsr.get("clock_start")))},
        {"type": "h3", "text": _t(lang, "moments_heading")},
        _table(
            [_t(lang, key) for key in ("col_number", "col_status", "decision", "col_step", "col_lesson", "col_clock",
                                       "col_episode")]
            + ["r"],
            [[str(moment["number"]), _status(lang, moment), _decision(lang, moment), _step_cell(lang, moment),
              _lesson(moment), _clock(moment), _episode_cell(lang, moment, interview["kind"]),
              vsr_text.correlation(moment["correlation"])] for moment in moments],
            numeric=(0, 7),
        ),
        {"type": "h3", "text": _t(lang, "cards_heading")},
        _moment_cards(interview, vsr, lang),
    ]
    return blocks


def build_section(vsr: dict[str, Any] | None, lang: str) -> dict[str, Any] | None:
    """The report section for the stored moments (``services/vsr.report_data``), or None without any."""
    if not vsr:
        return None
    with translation.override(lang):
        blocks: list[dict[str, Any]] = []
        if vsr["stale"]:
            blocks.append({"type": "note", "label": _t(lang, "warning"), "style": "warning",
                           "text": _t(lang, "stale_rules" if vsr.get("predates_rules") else "stale")})
        blocks += [
            _p(_t(lang, "lead"), style="lead"),
            *([_p(_t(lang, "curated_note"))] if vsr["curated"] else []),
            {"type": "h2", "text": _t(lang, "steps_heading")},
            {"type": "list", "items": [vsr_text.step_title(step["number"]) for step in vsr_text.steps()]},
            *_source_blocks(vsr, lang),
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
    """The short note on how the moments are chosen, appended to the Methodology."""
    return [
        {"type": "h2", "text": _t(lang, "method_title")},
        _p(_t(lang, "method_selection")),
        _p(_t(lang, "method_choice")),
    ]
