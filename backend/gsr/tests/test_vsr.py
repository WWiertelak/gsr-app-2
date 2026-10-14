"""VSR interview moments chosen from the Analysis results.

The unit tests feed hand-made Analysis payloads (synchronization subgroup episodes,
co-arousal High / Low / Co-movement episodes) and windowed correlations to the
engine; the integration tests run the real pipeline and Analysis on synthetic
recordings.
"""

from __future__ import annotations

from pathlib import Path

from django.utils import translation

import numpy as np
import pytest

from gsr.pipeline.coarousal import compute_coarousal
from gsr.pipeline.common import PipelineError
from gsr.pipeline.config import PipelineConfig
from gsr.pipeline.preprocessing import run_pipeline
from gsr.pipeline.synchrony import compute_synchrony
from gsr import vsr_text
from gsr.pipeline.vsr import (
    COAROUSAL,
    HIGH,
    LOW,
    PRIMARY,
    RESERVE,
    SYNCHRONY,
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


def _steps(synchrony: list[dict], coarousal: list[dict], windows: Windows, kind: str = "dyad",
           participants: list[str] | None = None) -> dict[int, list[Candidate]]:
    comovement, shared = interview_episodes(kind, participants or ["S1", "S2"], {"episodes": synchrony},
                                            {"episodes": coarousal}, CONFIG, LESSON)
    return candidates_by_step(windows, comovement, shared, CONFIG, LESSON)


# ─── Which Analysis episodes belong to an interview ─────────────────────────


def test_pair_episodes_are_the_analysis_episodes_both_students_are_in() -> None:
    synchrony = [_sync(60, 120, ["S1", "S2", "S3"]), _sync(200, 260, ["S1", "S3"]), _sync(300, 360, ["S2", "S1"])]
    coarousal = [
        _coarousal("High", 400, 460, ["S1", "S2"]),
        _coarousal("Low", 500, 560, ["S2"]),
        _coarousal("Co-movement", 600, 660, ["S1", "S2", "S4"]),
        _coarousal("Low", 700, 760, ["S2", "S1"]),
    ]

    comovement, shared = interview_episodes("dyad", ["S1", "S2"], {"episodes": synchrony}, {"episodes": coarousal},
                                            CONFIG, LESSON)

    # Numbered as on the Analysis screen: E1, E3 on Synchronization, 3 on Co-arousal.
    assert [(episode.source, episode.number) for episode in comovement] == [
        (SYNCHRONY, 1), (SYNCHRONY, 3), (COAROUSAL, 3)]
    assert [(episode.number, episode.state, episode.in_episode, episode.people) for episode in shared] == [
        (1, HIGH, 2, 2), (4, LOW, 2, 2)]
    assert (comovement[0].start_seconds, comovement[0].end_seconds) == pytest.approx((60.0, 120.0))


def test_teacher_episodes_need_the_teacher_and_at_least_one_student() -> None:
    synchrony = [_sync(60, 120, ["T", "S1"]), _sync(200, 260, ["S1", "S2"]), _sync(300, 360, ["T", "X"])]
    coarousal = [_coarousal("High", 400, 460, ["T", "S1", "S2", "S3"]), _coarousal("High", 500, 560, ["S1", "S2"])]

    comovement, shared = interview_episodes("teacher", ["T", "S1", "S2", "S3", "S4", "S5", "S6"],
                                            {"episodes": synchrony}, {"episodes": coarousal}, CONFIG, LESSON)

    assert [episode.number for episode in comovement] == [1]
    assert [(episode.number, episode.in_episode, episode.people) for episode in shared] == [(1, 3, 6)]


def test_a_single_window_episode_gets_the_window_it_was_measured_over() -> None:
    comovement, _shared = interview_episodes("dyad", ["S1", "S2"], {"episodes": [_sync(100, 100, ["S1", "S2"]),
                                                                                  _sync(5, 5, ["S1", "S2"])]},
                                             {"episodes": []}, CONFIG, LESSON)

    assert (comovement[0].start_seconds, comovement[0].end_seconds) == pytest.approx((85.0, 115.0))
    # Clipped to the analysed period.
    assert (comovement[1].start_seconds, comovement[1].end_seconds) == pytest.approx((0.0, 20.0))


# ─── The steps of the VSR flow chart ─────────────────────────────────────────


def test_each_step_takes_its_kind_of_analysis_episode() -> None:
    synchrony = [_sync(100, 160, ["S1", "S2"]), _sync(300, 360, ["S1", "S2"])]
    coarousal = [
        _coarousal("High", 130, 200, ["S1", "S2"]),  # overlaps the first co-movement episode: STEP 1
        _coarousal("High", 500, 560, ["S1", "S2"]),
        _coarousal("Low", 700, 760, ["S1", "S2"]),
    ]
    windows = _windows([(100, 160, 0.9), (300, 360, 0.7), (1000, 1100, -0.5)])

    steps = _steps(synchrony, coarousal, windows)

    step1, = steps[1]
    assert (step1.start_seconds, step1.end_seconds, step1.shared_state) == (100.0, 160.0, HIGH)
    assert step1.episode.number == 1 and step1.shared.number == 1
    assert step1.correlation == pytest.approx(0.9)
    assert [(c.start_seconds, c.shared_state) for c in steps[2]] == [(300.0, None)]
    assert [(c.episode.number, c.shared_state) for c in steps[3]] == [(2, HIGH)]
    assert [(c.episode.number, c.shared_state) for c in steps[4]] == [(3, LOW)]
    step5, = steps[5]
    assert step5.correlation == pytest.approx(-0.5)
    assert step5.episode is None


def test_a_shared_state_overlapping_co_movement_is_step_1_only() -> None:
    steps = _steps([_sync(100, 160, ["S1", "S2"])], [_coarousal("Low", 150, 300, ["S1", "S2"])], _windows([]))

    assert [c.shared_state for c in steps[1]] == [LOW]
    assert steps[3] == steps[4] == []


def test_step_1_names_the_shared_state_that_overlaps_most() -> None:
    coarousal = [_coarousal("Low", 90, 105, ["S1", "S2"]), _coarousal("High", 110, 170, ["S1", "S2"])]

    steps = _steps([_sync(100, 160, ["S1", "S2"])], coarousal, _windows([]))

    assert steps[1][0].shared_state == HIGH
    assert steps[1][0].shared.number == 2


def test_co_movement_episodes_of_the_co_arousal_tab_count_as_co_movement() -> None:
    steps = _steps([], [_coarousal("Co-movement", 400, 520, ["S1", "S2", "S3"])], _windows([(400, 520, 0.6)]))

    candidate, = steps[2]
    assert (candidate.episode.source, candidate.episode.number) == (COAROUSAL, 1)
    assert candidate.correlation == pytest.approx(0.6)


def test_no_synchrony_needs_a_negative_correlation_for_the_minimum_episode_duration() -> None:
    windows = _windows([(200, 215, -0.4), (400, 460, -0.6), (600, 640, -0.3), (650, 690, -0.3)])

    steps = _steps([], [], windows)

    # 200-215 is shorter than 30 s; 600-640 and 650-690 are 10 s apart and bridged into one moment.
    assert [(c.start_seconds, c.end_seconds) for c in steps[5]] == [(400.0, 460.0), (600.0, 690.0)]


def test_no_synchrony_stays_outside_the_interviews_episodes() -> None:
    windows = _windows([(300, 500, -0.5)])

    steps = _steps([], [_coarousal("High", 380, 420, ["S1", "S2"])], windows)

    # The run breaks at the episode instead of bridging across it.
    assert [(c.start_seconds, c.end_seconds) for c in steps[5]] == [(300.0, 375.0), (425.0, 500.0)]


def test_an_episode_whose_signals_moved_in_opposite_directions_is_no_synchrony() -> None:
    # An unsigned-synchrony Analysis finds an anti-phase subgroup; it is no co-movement.
    windows = _windows([(100, 160, -0.9), (600, 700, 0.6)])
    synchrony = [_sync(100, 160, ["S1", "S2"])]
    coarousal = [_coarousal("High", 130, 200, ["S1", "S2"]), _coarousal("Co-movement", 600, 700, ["S1", "S2"])]

    steps = _steps(synchrony, coarousal, windows)

    assert [(c.episode.source, c.episode.number) for c in steps[1] + steps[2]] == [(COAROUSAL, 2)]
    step5, = steps[5]
    assert (step5.episode.source, step5.episode.number) == (SYNCHRONY, 1)
    assert step5.correlation == pytest.approx(-0.9)
    # The opposite-direction episode does not hide the shared High state it overlaps.
    assert [c.episode.number for c in steps[3]] == [1]

    moment = _moment_payload(Proposal(1, PRIMARY, 1, 1, 1, step5), None)
    with translation.override("en"):
        english = vsr_text.justification(moment, "dyad")
    with translation.override("pl"):
        polish = vsr_text.justification(moment, "dyad")
    assert "synchronization episode E1" in english and "opposite direction" in english
    assert "co-movement" not in english
    assert "przeciwnym kierunku" in polish


def test_candidates_of_one_step_are_ranked_strongest_first() -> None:
    synchrony = [_sync(100, 140, ["S1", "S2"]), _sync(300, 400, ["S1", "S2"]), _sync(600, 640, ["S1", "S2"])]
    windows = _windows([(100, 140, 0.7), (300, 400, 0.7), (600, 640, 0.95), (900, 960, -0.2), (1200, 1240, -0.8)])
    coarousal = [_coarousal("High", 1400, 1440, ["T", "S1"]), _coarousal("High", 1500, 1600, ["T", "S1", "S2"])]

    steps = _steps(synchrony, coarousal, windows)
    teacher = _steps([], coarousal, _windows([]), kind="teacher", participants=["T", "S1", "S2"])

    # Highest r first, then the longer one.
    assert [c.start_seconds for c in steps[2]] == [600.0, 300.0, 100.0]
    # Lowest r first.
    assert [c.start_seconds for c in steps[5]] == [1200.0, 900.0]
    # The most people in the shared state first.
    assert [(c.episode.in_episode, c.start_seconds) for c in teacher[3]] == [(2, 1500.0), (1, 1400.0)]


# ─── STEP 6: walking the steps until the moments are chosen ────────────────


def _candidate(step: int, start: float, end: float, correlation: float = 0.8) -> Candidate:
    return Candidate(step, start, end, correlation)


def test_step_6_walks_the_steps_again_until_the_primary_and_reserve_moments_are_chosen() -> None:
    steps = {
        1: [_candidate(1, 0, 30), _candidate(1, 100, 130)],
        2: [_candidate(2, 200, 230), _candidate(2, 300, 330), _candidate(2, 400, 430)],
        3: [],
        4: [],
        5: [_candidate(5, 500, 530, -0.5), _candidate(5, 600, 630, -0.4), _candidate(5, 700, 730, -0.3)],
    }

    proposals = select(steps, primary_count=5, reserve_count=3)

    assert [(p.number, p.candidate.step, p.pass_number) for p in proposals] == [
        (1, 1, 1), (2, 2, 1), (3, 5, 1), (4, 1, 2), (5, 2, 2), (6, 5, 2), (7, 2, 3), (8, 5, 3)]
    assert [p.status for p in proposals] == [PRIMARY] * 5 + [RESERVE] * 3
    assert proposals[3].rank == 2 and proposals[3].candidate_count == 2


def test_selected_moments_never_overlap() -> None:
    steps = {1: [_candidate(1, 100, 200)], 2: [_candidate(2, 150, 250), _candidate(2, 300, 330)], 3: [], 4: [],
             5: [_candidate(5, 190, 210, -0.6)]}

    proposals = select(steps, primary_count=5, reserve_count=0)

    assert [(p.candidate.step, p.candidate.start_seconds) for p in proposals] == [(1, 100), (2, 300)]


def test_without_candidates_nothing_is_selected() -> None:
    assert select({number: [] for number in range(1, 6)}, 5, 3) == []


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

    payload = compute_vsr(result, analysis, config, dyads=[("S1", "S2")], teacher_alias="T",
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
            assert moment["step"] in range(1, 6)
            assert (moment["episode"] is None) == (moment["step"] == 5)


def test_compute_vsr_reports_moment_times_on_the_sensor_clock(tmp_path: Path) -> None:
    result, analysis, config = _analysed(tmp_path)

    moments = compute_vsr(result, analysis, config, dyads=[("S1", "S2")])["interviews"][0]["moments"]

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
        compute_vsr(result, analysis, config, dyads=[("S1", "S9")])
    with pytest.raises(PipelineError, match="two different participants"):
        compute_vsr(result, analysis, config, dyads=[("S1", "S1")])
    with pytest.raises(PipelineError, match="at least one student recording"):
        compute_vsr(result, analysis, config, dyads=[], teacher_alias="T", student_aliases=["T"])


def test_episode_overlap_is_measured_in_lesson_seconds() -> None:
    episode = Episode(SYNCHRONY, 1, 100.0, 160.0, None, 2, 2)

    assert episode.overlap(150.0, 300.0) == 10.0
    assert episode.overlap(160.0, 300.0) == 0.0
