"""VSR interview moments chosen from the Analysis results.

The unit tests feed hand-made Analysis payloads (synchronization subgroup episodes,
co-arousal High / Low / Co-movement episodes) and windowed correlations to the
engine; the integration tests run the real pipeline and Analysis on synthetic
recordings.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from django.utils import translation

from gsr import vsr_text
from gsr.pipeline.coarousal import compute_coarousal
from gsr.pipeline.common import PipelineError, _zscore
from gsr.pipeline.config import PipelineConfig
from gsr.pipeline.preprocessing import run_pipeline
from gsr.pipeline.synchrony import compute_synchrony
from gsr.pipeline.vsr import (
    COAROUSAL,
    COMOVEMENT,
    COMOVEMENT_STATE,
    DECOUPLED,
    DIVERGENT,
    HIGH,
    LOW,
    OPPOSITE,
    OWN,
    PRIMARY,
    RESERVE,
    SHARED_HIGH,
    SHARED_LOW,
    STEP_NUMBERS,
    SYNCHRONY,
    TEACHER_PAIR,
    Candidate,
    Episode,
    Proposal,
    Windows,
    _moment_payload,
    aggregate_student_signal,
    candidates_by_step,
    compute_vsr,
    interview_episodes,
    interview_signals,
    pair_analysis,
    select,
    windowed_correlation,
)

from .esense_csv import build_esense_csv

CONFIG = PipelineConfig()  # window 30 s, step 5 s, episodes at least 30 s, gaps up to 15 s bridged
LESSON = (0.0, 1800.0)


def _sync(start_s: float, end_s: float, members: list[str], strength: float = 0.8) -> dict:
    return {"start_relative": start_s / 60.0, "end_relative": end_s / 60.0, "members": members,
            "size": len(members), "mean_strength": strength, "peak_strength": strength}


def _coarousal(direction: str, start_s: float, end_s: float, contributors: list[str]) -> dict:
    return {"direction": direction, "start_s": start_s, "end_s": end_s, "contributors": contributors}


def _windows(values_by_span: list[tuple[float, float, float]], end: float = 1800.0, default: float = 0.3) -> Windows:
    """Window centres every 5 s from 15 s; r = ``default`` except inside the given (start, end, r) spans."""
    centers = np.arange(15.0, end - 14.0, 5.0)
    values = np.full(len(centers), default)
    for start, stop, value in values_by_span:
        values[(centers >= start) & (centers <= stop)] = value
    return Windows(centers, values)


def _divergent(start_s: float, end_s: float, members: list[str], strength: float = -0.85) -> dict:
    return {**_sync(start_s, end_s, members, strength)}


def _decoupled(start_s: float, end_s: float, members: list[str], r: float = 0.0) -> dict:
    return {"start_relative": start_s / 60.0, "end_relative": end_s / 60.0, "members": members, "size": 2,
            "mean_r": r}


def _steps(synchrony: list[dict], coarousal: list[dict], windows: Windows, divergent: list[dict] | None = None,
           decoupled: list[dict] | None = None, config: PipelineConfig = CONFIG) -> dict[str, list[Candidate]]:
    episodes = interview_episodes(["S1", "S2"], {"episodes": synchrony, "divergent_episodes": divergent or [],
                                                 "decoupled_episodes": decoupled or []},
                                  {"episodes": coarousal}, config, LESSON)
    return candidates_by_step(windows, episodes, config)


def _sentences(candidate: Candidate, kind: str = "dyad") -> tuple[str, str]:
    """The candidate's justification in English and Polish."""
    moment = _moment_payload(Proposal(1, PRIMARY, 1, 1, 1, candidate), None)
    with translation.override("en"):
        english = vsr_text.justification(moment, kind)
    with translation.override("pl"):
        polish = vsr_text.justification(moment, kind)
    return english, polish


# ─── Which Analysis episodes belong to an interview ─────────────────────────


def test_pair_episodes_are_the_analysis_episodes_both_students_are_in() -> None:
    synchrony = [_sync(60, 120, ["S1", "S2", "S3"]), _sync(200, 260, ["S1", "S3"]), _sync(300, 360, ["S2", "S1"])]
    coarousal = [
        _coarousal("High", 400, 460, ["S1", "S2"]),
        _coarousal("Low", 500, 560, ["S2"]),
        _coarousal("Co-movement", 600, 660, ["S1", "S2", "S4"]),
        _coarousal("Low", 700, 760, ["S2", "S1"]),
    ]

    divergent = [_divergent(800, 860, ["S1", "S2"]), _divergent(900, 960, ["S1", "S3"])]
    decoupled = [_decoupled(1000, 1060, ["S2", "S1"]), _decoupled(1100, 1160, ["S1", "S3"])]

    found = interview_episodes(["S1", "S2"], {"episodes": synchrony, "divergent_episodes": divergent,
                                              "decoupled_episodes": decoupled}, {"episodes": coarousal}, CONFIG, LESSON)

    # Numbered as on the Analysis screen: E1, E3 on Synchronization, 3 on Co-arousal; D1 divergent, U1 decoupling.
    assert [(episode.source, episode.kind, episode.number) for episode in found.comovement] == [
        (SYNCHRONY, "synchronization", 1), (SYNCHRONY, "synchronization", 3), (COAROUSAL, "co-movement", 3)]
    assert [(episode.number, episode.state, episode.in_episode, episode.people) for episode in found.shared] == [
        (1, HIGH, 2, 2), (4, LOW, 2, 2)]
    assert [(episode.kind, episode.number) for episode in found.divergent] == [("divergent", 1)]
    assert [(episode.kind, episode.number) for episode in found.decoupled] == [("decoupled", 1)]
    assert found.opposite == []
    assert (found.comovement[0].start_seconds, found.comovement[0].end_seconds) == pytest.approx((60.0, 120.0))


def test_the_teacher_interview_is_analysed_as_the_teacher_and_the_students_average() -> None:
    # The Analysis functions on the pair: a shared wave makes a synchronization and a co-movement episode, a stretch
    # where both are high a shared High episode.
    minutes = np.arange(0, 1200) / 60.0
    rng = np.random.default_rng(3)
    teacher, students = rng.normal(0, 0.3, len(minutes)), rng.normal(0, 0.3, len(minutes))
    wave = np.sin(2 * np.pi * np.arange(120) / 40.0) * 2.0
    teacher[300:420] += wave
    students[300:420] += wave
    teacher[800:900] += 3.0
    students[800:900] += 3.0

    synchrony, coarousal = pair_analysis(_zscore(teacher), _zscore(students), minutes, CONFIG, TEACHER_PAIR)
    found = interview_episodes(TEACHER_PAIR, synchrony, coarousal, CONFIG, (0.0, 1199.0), source=OWN)
    comovement, shared = found.comovement, found.shared

    assert synchrony["aliases"] == list(TEACHER_PAIR) and len(synchrony["windows"]["pairs"]) == 1
    assert comovement and all(episode.source == OWN for episode in comovement + shared)
    # The shared wave: a synchronization episode and a co-arousal co-movement episode around it.
    assert len([episode for episode in comovement
                if episode.overlap(300, 420) and 270 <= episode.start_seconds and episode.end_seconds <= 450]) == 2
    assert [(episode.state, episode.in_episode, episode.people) for episode in shared if episode.overlap(800, 900)] \
        == [(HIGH, 2, 2)]

    # Named as the teacher with the students' average, not as an Analysis episode.
    windows = _windows([(780, 920, 0.1)])
    high = candidates_by_step(windows, found, CONFIG)["3"][0]
    with translation.override("en"):
        sentence = vsr_text.justification(_moment_payload(Proposal(1, PRIMARY, 1, 1, 1, high), None), "teacher")
    assert "co-arousal of the teacher and the students' average" in sentence
    assert "the teacher and the students' average in the High state" in sentence and "of 6" not in sentence


def test_a_single_window_episode_gets_the_window_it_was_measured_over() -> None:
    comovement = interview_episodes(["S1", "S2"], {"episodes": [_sync(100, 100, ["S1", "S2"]),
                                                                 _sync(5, 5, ["S1", "S2"])]},
                                    {"episodes": []}, CONFIG, LESSON).comovement

    assert (comovement[0].start_seconds, comovement[0].end_seconds) == pytest.approx((85.0, 115.0))
    # Clipped to the analysed period.
    assert (comovement[1].start_seconds, comovement[1].end_seconds) == pytest.approx((0.0, 20.0))


# ─── The steps of the VSR flow chart ─────────────────────────────────────────


def test_the_protocol_is_the_documents_steps_read_by_strength_with_divergent_as_4b_after_step_5() -> None:
    # "4 synchronous, 1 not synchronous": STEP 5 comes before the divergent STEP 4b.
    assert STEP_NUMBERS == ("1", "2", "3", "4", "5", "4b")


def test_each_step_takes_its_kind_of_analysis_episode() -> None:
    synchrony = [_sync(100, 160, ["S1", "S2"]), _sync(300, 360, ["S1", "S2"])]
    coarousal = [
        _coarousal("High", 130, 200, ["S1", "S2"]),  # overlaps the first co-movement episode: STEP 1
        _coarousal("High", 500, 560, ["S1", "S2"]),
        _coarousal("Low", 700, 760, ["S1", "S2"]),
    ]
    windows = _windows([(100, 160, 0.9), (300, 360, 0.85), (900, 960, -0.9), (1000, 1100, 0.02)])

    steps = _steps(synchrony, coarousal, windows, divergent=[_divergent(900, 960, ["S1", "S2"])],
                   decoupled=[_decoupled(1000, 1100, ["S1", "S2"], 0.02)])

    step1, = steps["1"]
    assert (step1.start_seconds, step1.end_seconds, step1.shared_state, step1.kind) == (100.0, 160.0, HIGH,
                                                                                        COMOVEMENT_STATE)
    assert step1.episode.number == 1 and step1.shared.number == 1
    assert step1.correlation == pytest.approx(0.9)
    assert [(c.start_seconds, c.shared_state, c.kind) for c in steps["2"]] == [(300.0, None, COMOVEMENT)]
    assert [(c.episode.number, c.shared_state) for c in steps["3"]] == [(2, HIGH)]
    assert [(c.episode.number, c.shared_state) for c in steps["4"]] == [(3, LOW)]
    divergent, = steps["4b"]
    assert (divergent.kind, divergent.episode.kind, divergent.correlation) == (DIVERGENT, "divergent",
                                                                                pytest.approx(-0.9))
    step5, = steps["5"]
    assert (step5.kind, step5.episode.kind) == (DECOUPLED, "decoupled")
    assert step5.start_seconds == pytest.approx(1000.0)
    assert step5.correlation == pytest.approx(0.02)


def test_a_shared_state_overlapping_co_movement_is_step_1_only() -> None:
    steps = _steps([_sync(100, 160, ["S1", "S2"])], [_coarousal("Low", 150, 300, ["S1", "S2"])],
                   _windows([(100, 160, 0.9)]))

    assert [c.shared_state for c in steps["1"]] == [LOW]
    assert steps["3"] == steps["4"] == []


def test_step_1_names_the_shared_state_that_overlaps_most() -> None:
    coarousal = [_coarousal("Low", 90, 105, ["S1", "S2"]), _coarousal("High", 110, 170, ["S1", "S2"])]

    steps = _steps([_sync(100, 160, ["S1", "S2"])], coarousal, _windows([(100, 160, 0.9)]))

    assert steps["1"][0].shared_state == HIGH
    assert steps["1"][0].shared.number == 2


def test_co_movement_episodes_of_the_co_arousal_tab_count_as_co_movement() -> None:
    steps = _steps([], [_coarousal("Co-movement", 400, 520, ["S1", "S2", "S3"])], _windows([(400, 520, 0.85)]))

    candidate, = steps["2"]
    assert (candidate.episode.source, candidate.episode.number) == (COAROUSAL, 1)
    assert candidate.correlation == pytest.approx(0.85)


def test_steps_1_and_2_need_the_interview_threshold() -> None:
    # "Synchronous = above 0.8": a co-movement episode whose moment r is 0.69 answers no step, and does not keep the
    # shared state it overlaps out of STEP 3.
    synchrony = [_sync(100, 160, ["S1", "S2"]), _sync(300, 360, ["S1", "S2"]), _sync(500, 560, ["S1", "S2"])]
    coarousal = [_coarousal("High", 120, 200, ["S1", "S2"])]
    windows = _windows([(100, 160, 0.69), (300, 360, 0.8), (500, 560, 0.79)])

    steps = _steps(synchrony, coarousal, windows)

    assert CONFIG.synchrony_interview_threshold == 0.8
    assert steps["1"] == [] and [c.start_seconds for c in steps["2"]] == [300.0]
    assert [c.episode.number for c in steps["3"]] == [1]
    assert steps["5"] == []
    lowered = _steps(synchrony, coarousal, windows,
                     config=CONFIG.model_copy(update={"synchrony_interview_threshold": 0.6}))
    assert [c.start_seconds for c in lowered["1"]] == [100.0]
    assert [c.start_seconds for c in lowered["2"]] == pytest.approx([300.0, 500.0])


def test_divergent_synchrony_is_step_4b_from_minus_the_interview_threshold() -> None:
    # Hassan: "negative correlations above > .30 and ideally above .80 for the interviews". The Analysis finds the
    # divergent episodes from -0.3; an interview moment needs -0.8, its own r.
    divergent = [_divergent(100, 160, ["S1", "S2"]), _divergent(300, 360, ["S1", "S2"]),
                 _divergent(500, 560, ["S1", "S2"]), _divergent(700, 760, ["S1", "S2"])]
    windows = _windows([(100, 160, -0.8), (300, 360, -0.79), (500, 560, -0.95), (700, 760, -0.5)])

    steps = _steps([], [], windows, divergent=divergent)

    # The most negative first.
    assert [c.start_seconds for c in steps["4b"]] == pytest.approx([500.0, 100.0])
    english, polish = _sentences(steps["4b"][0])
    assert english.startswith("VSR flow chart step 4b: divergent synchrony – divergent synchronization episode D3, ")
    assert "r = -0.95" in english and "the signals moved in opposite directions" in english
    assert polish.startswith("Krok 4b schematu VSR: ") and "przeciwnych kierunkach" in polish


def test_an_opposite_direction_co_movement_episode_is_no_synchrony_moment() -> None:
    # An unsigned-synchrony Analysis puts an anti-phase pair into a synchronization episode: with r = -0.9 it is not
    # co-movement, and it is no STEP 5 either (that strength is the opposite of no relation).
    windows = _windows([(100, 160, -0.9), (600, 700, 0.85)])
    synchrony = [_sync(100, 160, ["S1", "S2"])]
    coarousal = [_coarousal("High", 130, 200, ["S1", "S2"]), _coarousal("Co-movement", 600, 700, ["S1", "S2"])]

    steps = _steps(synchrony, coarousal, windows)

    assert [(c.episode.source, c.episode.number) for c in steps["1"] + steps["2"]] == [(COAROUSAL, 2)]
    assert steps["4b"] == steps["5"] == []
    # It does not hide the shared High state it overlaps.
    assert [c.episode.number for c in steps["3"]] == [1]


def test_no_synchrony_is_the_analysis_decoupling_closest_to_zero_first() -> None:
    # "Asynchronous = below 0.1" read as strength: the Analysis' decoupling stretches (|mean r| < 0.1). The clearest
    # absence of a relation comes first, not the strongest anticorrelation.
    decoupled = [_decoupled(100, 160, ["S1", "S2"], -0.08), _decoupled(300, 400, ["S1", "S2"], 0.01),
                 _decoupled(500, 560, ["S1", "S2"], 0.05), _decoupled(700, 760, ["S1", "S3"], 0.0)]
    windows = _windows([(100, 160, -0.08), (300, 400, 0.01), (500, 560, 0.05)])

    steps = _steps([], [], windows, decoupled=decoupled)

    assert [c.start_seconds for c in steps["5"]] == pytest.approx([300.0, 500.0, 100.0])
    english, polish = _sentences(steps["5"][0])
    assert english.startswith("VSR flow chart step 5: no synchrony – decoupling stretch U2, 5:00–6:40")
    assert "r = 0.01" in english and "the signals were not related" in english
    assert "opposite" not in english
    assert "sygnały nie były powiązane" in polish


def _opposite(start_s: float, end_s: float, high: list[str], low: list[str]) -> dict:
    return {"direction": "Opposite", "start_s": start_s, "end_s": end_s, "contributors": high + low,
            "high_contributors": high, "low_contributors": low}


def test_opposite_arousal_is_step_5_before_decoupling() -> None:
    # Hassan: "opposite shared arousal (one is high, one is low)". One of the pair on each side; not both on one side.
    coarousal = [_opposite(900, 960, ["S2"], ["S1"]), _opposite(1100, 1200, ["S1", "S3"], ["S2"]),
                 _opposite(1300, 1360, ["S1", "S2"], ["S3"]), _opposite(1400, 1460, ["S1"], ["S3"])]
    decoupled = [_decoupled(100, 160, ["S1", "S2"], 0.0)]

    steps = _steps([], coarousal, _windows([(100, 160, 0.0)]), decoupled=decoupled)

    assert [(c.kind, c.start_seconds) for c in steps["5"]] == [(OPPOSITE, 1100.0), (OPPOSITE, 900.0),
                                                               (DECOUPLED, pytest.approx(100.0))]
    assert steps["3"] == steps["4"] == []
    english, polish = _sentences(steps["5"][1])
    assert english.startswith("VSR flow chart step 5: no synchrony – co-arousal episode 1, 15:00–16:00, "
                              "one person in the High state, the other in the Low state, r = ")
    assert "jedna osoba w stanie Wysokim, druga w stanie Niskim" in polish
    teacher, _polish = _sentences(steps["5"][1], "teacher")
    assert "the teacher or the students' average in the High state, the other in the Low state" in teacher


def test_a_moment_stored_before_the_strength_reading_keeps_its_sentence() -> None:
    # Moments chosen when STEP 5 took a correlation below 0.1 with its sign have whole-number steps and no type.
    stored = {"step": 5, "start_seconds": 4480.0, "end_seconds": 4510.0, "correlation": -0.82, "episode": None,
              "shared_state": None}
    with translation.override("en"):
        sentence = vsr_text.justification(stored, "dyad")
    assert sentence.startswith("VSR flow chart step 5: no synchrony – 74:40–75:10 outside the synchronization")
    assert "the signals moved in the opposite direction" in sentence
    with translation.override("en"):
        assert vsr_text.step_title(5) == "Step 5 · No synchrony" and vsr_text.step_title("4b").startswith("Step 4b")


def test_candidates_of_one_step_are_ranked_strongest_first() -> None:
    synchrony = [_sync(100, 140, ["S1", "S2"]), _sync(300, 400, ["S1", "S2"]), _sync(600, 640, ["S1", "S2"])]
    windows = _windows([(100, 140, 0.85), (300, 400, 0.85), (600, 640, 0.95)])
    coarousal = [_coarousal("High", 1400, 1440, ["S1", "S2"]), _coarousal("High", 1500, 1600, ["S1", "S2"])]

    steps = _steps(synchrony, coarousal, windows)

    # Highest r first, then the longer one.
    assert [c.start_seconds for c in steps["2"]] == [600.0, 300.0, 100.0]
    # Both people are in every shared state of a pair: the longer one first.
    assert [c.start_seconds for c in steps["3"]] == [1500.0, 1400.0]


# ─── STEP 6: walking the steps until the moments are chosen ────────────────


def _candidate(kind: str, start: float, end: float, correlation: float = 0.8) -> Candidate:
    return Candidate(kind, start, end, correlation)


def _empty_steps() -> dict[str, list[Candidate]]:
    return {step: [] for step in STEP_NUMBERS}


def test_step_6_walks_the_steps_again_until_the_primary_and_reserve_moments_are_chosen() -> None:
    steps = {
        **_empty_steps(),
        "1": [_candidate(COMOVEMENT_STATE, 0, 30), _candidate(COMOVEMENT_STATE, 100, 130)],
        "2": [_candidate(COMOVEMENT, 200, 230), _candidate(COMOVEMENT, 300, 330), _candidate(COMOVEMENT, 400, 430)],
        "4b": [_candidate(DIVERGENT, 450, 480, -0.9)],
        "5": [_candidate(DECOUPLED, 500, 530, 0.0), _candidate(DECOUPLED, 600, 630, 0.02),
              _candidate(DECOUPLED, 700, 730, 0.05)],
    }

    proposals = select(steps, primary_count=5, reserve_count=3)

    assert [(p.number, p.candidate.step, p.pass_number) for p in proposals] == [
        (1, "1", 1), (2, "2", 1), (3, "5", 1), (4, "4b", 1), (5, "1", 2), (6, "2", 2), (7, "5", 2), (8, "2", 3)]
    assert [p.status for p in proposals] == [PRIMARY] * 5 + [RESERVE] * 3
    assert proposals[4].rank == 2 and proposals[4].candidate_count == 2


def test_the_primary_five_keep_a_no_synchrony_moment_among_many_divergent_ones() -> None:
    # Every step has candidates and divergent ones are many (32 on the trial replica): the five primary moments are
    # STEPS 1-5, with divergent synchrony first in the reserve.
    steps = {
        "1": [_candidate(COMOVEMENT_STATE, 0, 30)],
        "2": [_candidate(COMOVEMENT, 100, 130)],
        "3": [_candidate(SHARED_HIGH, 200, 230)],
        "4": [_candidate(SHARED_LOW, 300, 330)],
        "5": [_candidate(DECOUPLED, 400, 430, 0.0)],
        "4b": [_candidate(DIVERGENT, 500 + 40 * index, 530 + 40 * index, -0.9) for index in range(10)],
    }

    proposals = select(steps, primary_count=5, reserve_count=3)

    assert [p.candidate.step for p in proposals if p.status == PRIMARY] == ["1", "2", "3", "4", "5"]
    assert proposals[5].candidate.step == "4b" and proposals[5].status == RESERVE


def test_selected_moments_never_overlap() -> None:
    steps = {**_empty_steps(), "1": [_candidate(COMOVEMENT_STATE, 100, 200)],
             "2": [_candidate(COMOVEMENT, 150, 250), _candidate(COMOVEMENT, 300, 330)],
             "5": [_candidate(DECOUPLED, 190, 210, 0.0)]}

    proposals = select(steps, primary_count=5, reserve_count=0)

    assert [(p.candidate.step, p.candidate.start_seconds) for p in proposals] == [("1", 100), ("2", 300)]


def test_without_candidates_nothing_is_selected() -> None:
    assert select(_empty_steps(), 5, 3) == []


# ─── Signals and the whole engine ───────────────────────────────────────────


def test_student_average_z_scores_each_student_before_averaging() -> None:
    minutes = np.arange(600) / 60.0
    quiet = np.sin(2 * np.pi * minutes)
    loud = 20.0 * np.cos(2 * np.pi * minutes) + 100.0

    average = aggregate_student_signal([quiet, loud])

    # The louder student cannot dominate: scaling and shifting one student's
    # recording leaves the aggregate unchanged.
    np.testing.assert_allclose(average, aggregate_student_signal([quiet, 3.0 * loud - 7.0]), atol=1e-12)
    assert np.mean(average) == pytest.approx(0.0, abs=1e-12)
    assert np.std(average) == pytest.approx(1.0)


def test_student_average_needs_at_least_one_student() -> None:
    with pytest.raises(PipelineError, match="at least one student signal"):
        aggregate_student_signal([])


def _analysed(tmp_path: Path) -> tuple:
    files = {}
    for index, alias in enumerate(["T", "S1", "S2", "S3"]):
        path = tmp_path / f"{alias}.csv"
        build_esense_csv(path, rows=1800, seed_offset=index * 0.4)
        files[alias] = path
    config = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=1, synchrony_surrogate_count=0,
                            contagion_surrogate_count=0)
    result = run_pipeline(files, config)
    analysis = {"synchrony": compute_synchrony(result, None, config), "coarousal": compute_coarousal(result, None, config)}
    return result, analysis, config


def test_the_pair_correlation_is_the_synchronization_tabs_windows(tmp_path: Path) -> None:
    result, analysis, config = _analysed(tmp_path)
    frame = result.merged_decimated

    windows = windowed_correlation(*interview_signals(frame, "dyad", ["S1", "S2"]),
                                   frame["time"].to_numpy(dtype=float), config)

    stored = analysis["synchrony"]["windows"]
    pair = next(row for row in stored["pairs"] if (row["alias_a"], row["alias_b"]) == ("S1", "S2"))
    np.testing.assert_allclose(windows.centers, np.asarray(stored["centers"]) * 60.0)
    np.testing.assert_allclose(windows.values, pair["values"], atol=1e-9)


def test_compute_vsr_builds_one_interview_per_pair_and_one_for_the_teacher(tmp_path: Path) -> None:
    result, analysis, config = _analysed(tmp_path)

    payload = compute_vsr(result, config, dyads=[("S1", "S2")], teacher_alias="T",
                          student_aliases=["S1", "S2", "S3"], primary_count=4, reserve_count=2)

    assert [interview["key"] for interview in payload["interviews"]] == ["S1+S2", "T"]
    dyad, teacher = payload["interviews"]
    assert (dyad["kind"], dyad["participants"]) == ("dyad", ["S1", "S2"])
    assert (teacher["kind"], teacher["participants"]) == ("teacher", ["T", "S1", "S2", "S3"])
    assert payload["counts"] == {"primary": 4, "reserve": 2}
    assert payload["analysis_settings"]["synchrony_threshold"] == config.synchrony_threshold
    assert payload["stats"]["interview_count"] == 2
    for interview in payload["interviews"]:
        assert len(interview["trace"]["a"]) == len(interview["trace"]["b"]) == len(payload["trace_seconds"])
        assert interview["stats"]["primary_count"] + interview["stats"]["reserve_count"] == len(interview["moments"])
        assert len(interview["moments"]) <= 6
        for moment in interview["moments"]:
            assert moment["status"] in {PRIMARY, RESERVE}
            assert moment["start_seconds"] < moment["end_seconds"]
            assert moment["step"] in STEP_NUMBERS and moment["type"]
            assert moment["episode"]["source"] == OWN  # every moment is an episode of the interview's own signals


def _class_recordings(tmp_path: Path, together: dict[str, tuple[float, float]], seconds: float = 900.0) -> dict:
    """A teacher T and students S1-S6 with independent responses; the aliases in ``together`` share a strong wave
    in the given (start, end) seconds."""
    from .sample_data import SAMPLE_PERIOD_MS, _scr, write_esense_csv

    rng = np.random.default_rng(11)
    t = np.arange(int(seconds * 1000 / SAMPLE_PERIOD_MS)) * SAMPLE_PERIOD_MS / 1000.0
    files = {}
    for alias in ["T", "S1", "S2", "S3", "S4", "S5", "S6"]:
        signal = 5.0 + 0.3 * np.sin(t / 300.0 + rng.uniform(0, 6))
        for onset in rng.uniform(30, seconds - 30, size=15):
            signal = signal + 0.5 * rng.uniform(0.6, 1.3) * _scr(t, onset)
        for start, end in [span for name, span in together.items() if alias in name.split("+")]:
            signal = signal + 0.8 * np.sin(2 * np.pi * (t - start) / 18.0) * ((t > start) & (t < end))
        files[alias] = write_esense_csv(tmp_path / f"{alias}.csv", signal + rng.normal(0, 0.02, len(t)))
    return files


def test_the_teacher_moments_come_from_the_aggregated_data_not_from_one_student(tmp_path: Path) -> None:
    # The scouting report's E5: the teacher co-moves with S1 alone at 300-380 s (the Analysis has a T + S1
    # synchronization episode there), and with every student at 600-680 s.
    files = _class_recordings(tmp_path, {"T+S1": (300.0, 380.0), "T+S1+S2+S3+S4+S5+S6": (600.0, 680.0)})
    config = PipelineConfig(scaling_enabled=True, synchrony_threshold=0.8, contagion_comovement_rho=0.8,
                            synchrony_surrogate_count=0, contagion_surrogate_count=0)
    result = run_pipeline(files, config)
    analysis = {"synchrony": compute_synchrony(result, None, config), "coarousal": compute_coarousal(result, None, config)}
    assert any({"T", "S1"} <= set(episode["members"]) and episode["start_relative"] * 60 < 380
               and episode["end_relative"] * 60 > 300 for episode in analysis["synchrony"]["episodes"])

    teacher = compute_vsr(result, config, dyads=[], teacher_alias="T",
                          student_aliases=["S1", "S2", "S3", "S4", "S5", "S6"])["interviews"][0]

    synchronous = [c for c in teacher["candidates"] if c["step"] in ("1", "2")]
    assert not [c for c in synchronous if c["start_seconds"] < 380 and c["end_seconds"] > 300]
    assert [c for c in synchronous if c["start_seconds"] < 680 and c["end_seconds"] > 600]
    assert all(c["episode"]["source"] == OWN for c in teacher["candidates"] if c["episode"])


def test_a_pairs_moments_do_not_depend_on_who_else_is_in_the_project(tmp_path: Path) -> None:
    # The flow chart: "Points for discussion are selected based on the dyad's GSR data". S1 and S2 share a wave at
    # 300-380 s, S3 joins them at 600-680 s; the pair's moments are the same with or without the rest of the class.
    files = _class_recordings(tmp_path, {"S1+S2": (300.0, 380.0), "S1+S2+S3": (600.0, 680.0)})
    config = PipelineConfig(scaling_enabled=True, synchrony_surrogate_count=0, contagion_surrogate_count=0)
    whole_class = run_pipeline(files, config)
    pair_alone = run_pipeline({alias: files[alias] for alias in ("S1", "S2")}, config)

    def interview(result: object) -> dict:
        return compute_vsr(result, config, dyads=[("S1", "S2")])["interviews"][0]

    in_class, alone = interview(whole_class), interview(pair_alone)

    assert in_class["candidates"] and in_class["candidates"] == alone["candidates"]
    assert in_class["moments"] == alone["moments"]
    assert all(c["episode"]["source"] == OWN for c in in_class["candidates"] if c["episode"])
    assert [c for c in in_class["candidates"] if c["step"] in ("1", "2") and c["start_seconds"] < 380
            and c["end_seconds"] > 300]


def test_compute_vsr_reports_moment_times_on_the_sensor_clock(tmp_path: Path) -> None:
    result, analysis, config = _analysed(tmp_path)

    moments = compute_vsr(result, config, dyads=[("S1", "S2")])["interviews"][0]["moments"]

    assert moments, "the synthetic recording produced no moment to place on the clock"
    for moment in moments:
        # build_esense_csv records from 12:00:00, so a moment's clock time is the
        # recording start plus its lesson time.
        hours, minutes_text, seconds_text = moment["start_clock"].split(":")
        offset = (int(hours) - 12) * 3600 + int(minutes_text) * 60 + int(seconds_text)
        assert offset == pytest.approx(moment["start_seconds"], abs=1.0)


def test_compute_vsr_rejects_unusable_interview_participants(tmp_path: Path) -> None:
    result, analysis, config = _analysed(tmp_path)

    with pytest.raises(PipelineError, match="Unknown aliases requested: S9"):
        compute_vsr(result, config, dyads=[("S1", "S9")])
    with pytest.raises(PipelineError, match="two different participants"):
        compute_vsr(result, config, dyads=[("S1", "S1")])
    with pytest.raises(PipelineError, match="at least one student recording"):
        compute_vsr(result, config, dyads=[], teacher_alias="T", student_aliases=["T"])


def test_episode_overlap_is_measured_in_lesson_seconds() -> None:
    episode = Episode(SYNCHRONY, 1, 100.0, 160.0, None, 2, 2)

    assert episode.overlap(150.0, 300.0) == 10.0
    assert episode.overlap(160.0, 300.0) == 0.0
