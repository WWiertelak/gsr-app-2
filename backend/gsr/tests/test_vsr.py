"""VSR interview moment selection, on synthetic lessons built in z units.

A synthetic lesson is a sequence of stretches; each stretch puts both
participants in one arousal band and makes the second signal either follow the
first (r = 1) or mirror it (r = -1), which is enough to produce every one of the
document's five synchrony types on demand.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from gsr.pipeline.common import PipelineError
from gsr.pipeline.config import PipelineConfig
from gsr.pipeline.preprocessing import run_pipeline
from gsr.pipeline.vsr import (
    CANDIDATES_EXHAUSTED,
    HIGH,
    LOW,
    NO_CANDIDATES,
    NO_RELATION,
    OPPOSITE_DIRECTION,
    PRIMARY,
    QUOTE_ASYNCHRONOUS,
    QUOTE_DYAD_DATA,
    QUOTE_FIVE_MOMENTS,
    QUOTE_RESERVE,
    QUOTE_SYNCHRONOUS,
    QUOTE_TEACHER_AGGREGATION,
    QUOTE_TEACHER_DATA,
    RESERVE,
    STEP_PAGES,
    STEPS,
    Moment,
    Window,
    _moments_by_step,
    _select,
    _shared_state,
    _window_step,
    aggregate_student_signal,
    compute_vsr,
    justification_fragments,
    justification_text,
    select_pair_moments,
)
from gsr.pipeline.vsr_config import VsrConfig

from .esense_csv import build_esense_csv

HIGH_BAND = 1.5  # both participants above z = 1
LOW_BAND = -1.5  # both participants below z = -1
NEUTRAL_BAND = 0.0


def _stretch(seconds: int, band: float, *, together: bool) -> tuple[np.ndarray, np.ndarray]:
    """One stretch of a synthetic lesson, sampled at 1 Hz, in z units."""
    slope = np.linspace(-0.3, 0.3, seconds)
    return band + slope, band + (slope if together else -slope)


def _lesson(*stretches: tuple[np.ndarray, np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    first = np.concatenate([stretch[0] for stretch in stretches])
    second = np.concatenate([stretch[1] for stretch in stretches])
    return first, second, np.arange(len(first)) / 60.0


def _moment(step: int, start: float, end: float, correlation: float, state_fraction: float | None = None) -> Moment:
    return Moment(
        step=step,
        start_seconds=start,
        end_seconds=end,
        window_count=1,
        correlation=correlation,
        shared_state=HIGH if state_fraction is not None else None,
        shared_state_fraction=state_fraction,
    )


def test_document_quotes_carry_the_numbers_the_defaults_use() -> None:
    config = VsrConfig()

    # A default drifting away from the quoted criterion would make every
    # justification cite a threshold the engine did not apply.
    assert f"above {config.vsr_sync_threshold}" in QUOTE_SYNCHRONOUS.text
    assert f"below {config.vsr_async_threshold}" in QUOTE_ASYNCHRONOUS.text
    assert f"A total of {config.vsr_primary_moment_count} moments" in QUOTE_FIVE_MOMENTS.text
    assert f"(e.g. {config.vsr_primary_moment_count + config.vsr_reserve_moment_count})" in QUOTE_RESERVE.text
    assert [step.question.page for step in STEPS.values()] == [STEP_PAGES[number] for number in STEPS]
    assert {step.criterion for step in STEPS.values()} == {QUOTE_SYNCHRONOUS, QUOTE_ASYNCHRONOUS}


@pytest.mark.parametrize(
    ("correlation", "shared_high", "shared_low", "expected_state", "expected_step"),
    [
        (0.95, 1.0, 0.0, HIGH, 1),  # co-movement AND shared arousal states
        (0.95, 0.0, 1.0, LOW, 1),  # the shared state may be either band
        (0.95, 0.3, 0.0, None, 2),  # co-movement only: no state held long enough
        (0.5, 0.8, 0.0, HIGH, 3),  # shared HIGH arousal without co-movement
        (0.5, 0.0, 0.8, LOW, 4),  # shared LOW arousal without co-movement
        (0.05, 0.0, 0.0, None, 5),  # asynchronous: literally below 0.1
        (-0.9, 0.0, 0.0, None, 5),  # a negative correlation is below 0.1 too
        (-0.9, 0.0, 0.8, LOW, 4),  # a shared state is answered before asynchrony
        (0.5, 0.0, 0.0, None, None),  # between the two criteria: answers no step
        (0.8, 1.0, 0.0, HIGH, 3),  # "above 0.8" is strict, so 0.8 exactly is not co-movement
        (0.95, 0.59, 0.41, None, 2),  # neither band is held for the required 60%
    ],
)
def test_window_answers_the_first_step_it_satisfies(
    correlation: float,
    shared_high: float,
    shared_low: float,
    expected_state: str | None,
    expected_step: int | None,
) -> None:
    config = VsrConfig()
    state = _shared_state(shared_high, shared_low, config)

    assert state == expected_state
    assert _window_step(correlation, state, config) == expected_step


@pytest.mark.parametrize(
    ("correlation", "expected_subtype"),
    [(-0.9, OPPOSITE_DIRECTION), (-0.01, OPPOSITE_DIRECTION), (0.0, NO_RELATION), (0.05, NO_RELATION)],
)
def test_asynchrony_subtype_separates_opposite_direction_from_no_relation(
    correlation: float, expected_subtype: str
) -> None:
    assert _moment(5, 0.0, 30.0, correlation).asynchrony_subtype == expected_subtype
    assert _moment(1, 0.0, 30.0, 0.9, state_fraction=1.0).asynchrony_subtype is None


def _window(index: int, step: int | None, state: str | None = HIGH) -> Window:
    return Window(
        start_seconds=float(index * 5),
        end_seconds=float(index * 5 + 29),
        correlation=0.9,
        shared_high_fraction=1.0 if state == HIGH else 0.0,
        shared_low_fraction=1.0 if state == LOW else 0.0,
        shared_state=state,
        step=step,
    )


def test_a_moment_is_a_run_of_strictly_consecutive_windows_of_one_step() -> None:
    windows = [_window(index, step) for index, step in enumerate([1, 1, 2, 1, None, 1, 1])]

    moments = _moments_by_step(windows)

    # A window answering another step or no step ends the moment, so no moment
    # spans a window that fails its step's criterion.
    assert sorted((moment.start_seconds, moment.end_seconds) for moment in moments[1]) == [
        (0.0, 34.0),
        (15.0, 44.0),
        (25.0, 59.0),
    ]
    assert sorted(moment.window_count for moment in moments[1]) == [1, 2, 2]
    assert [(moment.start_seconds, moment.end_seconds) for moment in moments[2]] == [(10.0, 39.0)]


def test_a_change_of_shared_state_starts_a_new_moment() -> None:
    windows = [_window(index, 1, state) for index, state in enumerate([HIGH, HIGH, LOW, LOW])]

    moments = _moments_by_step(windows)[1]

    # Both runs answer STEP 1, but one is shared HIGH and the other shared LOW,
    # so the share each moment reports holds for the whole moment.
    assert [moment.shared_state for moment in moments] == [HIGH, LOW]
    assert [(moment.start_seconds, moment.end_seconds) for moment in moments] == [(0.0, 34.0), (10.0, 44.0)]
    assert all(moment.shared_state_fraction == 1.0 for moment in moments)


# The document's five synchrony types (page 1), each as the lesson that produces it.
SYNCHRONY_TYPES = [
    pytest.param(HIGH_BAND, True, 1, HIGH, id="co-movement-and-shared-arousal-states"),
    pytest.param(NEUTRAL_BAND, True, 2, None, id="co-movement-only"),
    pytest.param(HIGH_BAND, False, 3, HIGH, id="shared-high-arousal"),
    pytest.param(LOW_BAND, False, 4, LOW, id="shared-low-arousal"),
    pytest.param(NEUTRAL_BAND, False, 5, None, id="asynchronous-moments"),
]


@pytest.mark.parametrize(("band", "together", "expected_step", "expected_state"), SYNCHRONY_TYPES)
def test_each_synchrony_type_of_the_document_is_found(
    band: float, together: bool, expected_step: int, expected_state: str | None
) -> None:
    first, second, minutes = _lesson(_stretch(300, band, together=together))

    selection = select_pair_moments(first, second, minutes)
    moment = selection.proposals[0].moment

    assert [proposal.moment.step for proposal in selection.proposals] == [expected_step]
    assert moment.shared_state == expected_state
    assert selection.moments_by_step[expected_step] == [moment]
    assert [number for number, found in selection.moments_by_step.items() if found] == [expected_step]
    assert moment.start_seconds == 0.0
    assert moment.end_seconds == 299.0


def test_selected_moments_never_overlap_and_block_the_steps_they_cover() -> None:
    # Where the arousal band changes, both participants step up together, so
    # windows straddling a band change read as co-movement and overlap the
    # moments on either side.
    first, second, minutes = _lesson(
        _stretch(150, HIGH_BAND, together=True),
        _stretch(150, NEUTRAL_BAND, together=True),
        _stretch(150, HIGH_BAND, together=False),
        _stretch(150, LOW_BAND, together=False),
        _stretch(150, NEUTRAL_BAND, together=False),
    )

    selection = select_pair_moments(first, second, minutes)

    spans = sorted((proposal.moment.start_seconds, proposal.moment.end_seconds) for proposal in selection.proposals)
    for (_start, end), (next_start, _next_end) in zip(spans, spans[1:]):
        assert end <= next_start
    # Candidates the selection had to pass over are reported as such, not as a
    # step that found nothing.
    skipped = [
        record
        for pass_record in selection.passes
        for record in pass_record["steps"]
        if record["reason"] == CANDIDATES_EXHAUSTED
    ]
    assert skipped, "no step had to pass over an overlapping candidate"
    assert all(record["candidate_count"] > 0 for record in skipped)


def test_empty_step_is_recorded_with_the_documents_go_to_step_quote() -> None:
    # One long asynchronous stretch: only STEP 5 can be answered.
    first, second, minutes = _lesson(_stretch(300, NEUTRAL_BAND, together=False))

    selection = select_pair_moments(first, second, minutes)
    first_pass = selection.passes[0]
    empty_steps = {record["step"]: record for record in first_pass["steps"] if record["answer"] == "NO"}

    assert sorted(empty_steps) == [1, 2, 3, 4]
    assert [proposal.moment.step for proposal in selection.proposals] == [5]
    for number, record in empty_steps.items():
        assert record["reason"] == NO_CANDIDATES
        assert record["candidate_count"] == 0
        fragment = record["fragments"][0]
        assert fragment["quote"] == STEPS[number].question.as_dict()
        assert fragment["outcome"] == {"text": f"Go to STEP {number + 1}", "page": STEP_PAGES[number]}
        assert f"Go to STEP {number + 1}" in record["text"]
        assert "no moment answers the criterion" in record["text"]

    # STEP 6 is answered NO and the second pass finds nothing left to add.
    assert first_pass["step_6"]["answer"] == "NO"
    assert first_pass["step_6"]["selected_count"] == 1
    assert len(selection.passes) == 2
    assert all(record["reason"] == CANDIDATES_EXHAUSTED for record in selection.passes[1]["steps"] if record["step"] == 5)


def test_step_6_repeats_the_steps_until_five_moments_are_selected() -> None:
    moments = {
        1: [_moment(1, 0.0, 30.0, 0.99, 1.0), _moment(1, 100.0, 130.0, 0.95, 0.9), _moment(1, 200.0, 230.0, 0.9, 0.8)],
        2: [],
        3: [],
        4: [],
        5: [_moment(5, 300.0, 330.0, -0.9), _moment(5, 400.0, 430.0, -0.5), _moment(5, 500.0, 530.0, 0.0)],
    }

    proposals, passes = _select(moments, VsrConfig())

    # Each pass takes at most one moment per step, so five moments need three passes.
    assert [proposal.pass_number for proposal in proposals] == [1, 1, 2, 2, 3, 3]
    assert [proposal.moment.step for proposal in proposals] == [1, 5, 1, 5, 1, 5]
    assert [proposal.status for proposal in proposals] == [PRIMARY] * 5 + [RESERVE]
    # STEP 6 is answered after every pass, and the last pass runs out of candidates.
    assert [pass_record["step_6"]["answer"] for pass_record in passes] == ["NO", "NO", "YES", "YES"]
    assert [pass_record["step_6"]["selected_count"] for pass_record in passes] == [2, 4, 5, 5]
    assert [pass_record["purpose"] for pass_record in passes] == [PRIMARY, PRIMARY, PRIMARY, RESERVE]
    assert all(record["answer"] == "NO" for record in passes[3]["steps"])
    # The strongest candidate of a step goes first, and its rank is reported.
    assert [proposal.rank for proposal in proposals if proposal.moment.step == 1] == [1, 2, 3]
    assert all(proposal.candidate_count == 3 for proposal in proposals)
    # Passes 2 and 3 ran because STEP 6 answered NO; the reserve #6 was picked
    # after five moments were selected, so it does not claim a NO.
    assert [proposal.after_step_6_no for proposal in proposals] == [False, False, True, True, True, False]


def test_a_candidate_overlapping_a_selected_moment_is_skipped() -> None:
    moments = {
        1: [_moment(1, 0.0, 100.0, 0.99, 1.0)],
        2: [_moment(2, 50.0, 150.0, 0.98), _moment(2, 200.0, 300.0, 0.9)],
        3: [],
        4: [],
        5: [_moment(5, 60.0, 90.0, -0.9)],  # inside the STEP 1 moment
    }

    proposals, passes = _select(moments, VsrConfig())
    answers = {record["step"]: record for record in passes[0]["steps"]}

    assert [(proposal.moment.start_seconds, proposal.moment.end_seconds) for proposal in proposals] == [
        (0.0, 100.0),
        (200.0, 300.0),
    ]
    # The stronger STEP 2 candidate overlaps the STEP 1 moment, so the weaker one
    # is taken and the justification says which rank was used.
    assert answers[2]["answer"] == "YES"
    assert proposals[1].rank == 2
    # STEP 5's only candidate is inside the selected moment, so the step answers NO.
    assert answers[5]["answer"] == "NO"
    assert answers[5]["reason"] == CANDIDATES_EXHAUSTED
    assert "all already selected or overlapping a selected moment" in answers[5]["text"]


def test_reserve_moments_are_marked_and_justified_as_additional() -> None:
    moments = {number: [] for number in STEPS}
    moments[5] = [_moment(5, index * 100.0, index * 100.0 + 30.0, -0.9 + index * 0.1) for index in range(8)]

    proposals, _passes = _select(moments, VsrConfig())
    kinds = [
        [fragment["kind"] for fragment in justification_fragments(proposal, QUOTE_DYAD_DATA, VsrConfig())]
        for proposal in proposals
    ]

    assert [proposal.status for proposal in proposals] == [PRIMARY] * 5 + [RESERVE] * 3
    assert all("reserve" not in kind_list for kind_list in kinds[:5])
    assert all("reserve" in kind_list for kind_list in kinds[5:])
    # Passes 2-5 ran because STEP 6 answered NO; the reserves come from passes
    # run after STEP 6 answered YES, so they do not claim a NO.
    assert [pass_record["step_6"]["answer"] for pass_record in _passes[:5]] == ["NO"] * 4 + ["YES"]
    assert ["repeat_pass" in kind_list for kind_list in kinds] == [False] + [True] * 4 + [False] * 3
    reserve_text = justification_text(justification_fragments(proposals[5], QUOTE_DYAD_DATA, VsrConfig()))
    assert QUOTE_RESERVE.text in reserve_text


@pytest.mark.parametrize(("band", "together", "expected_step", "expected_state"), SYNCHRONY_TYPES)
def test_justification_cites_the_document_and_the_measured_values(
    band: float, together: bool, expected_step: int, expected_state: str | None
) -> None:
    first, second, minutes = _lesson(_stretch(300, band, together=together))
    config = VsrConfig()

    selection = select_pair_moments(first, second, minutes, config)
    proposal = selection.proposals[0]
    fragments = justification_fragments(proposal, QUOTE_DYAD_DATA, config)
    by_kind = {fragment["kind"]: fragment for fragment in fragments}
    step = STEPS[expected_step]

    assert {"data_basis", "step_question", "synchrony_type", "criterion", "choice"} <= set(by_kind)
    assert by_kind["data_basis"]["quote"] == QUOTE_DYAD_DATA.as_dict()
    assert by_kind["step_question"]["quote"] == step.question.as_dict()
    assert by_kind["synchrony_type"]["quote"] == step.synchrony_type.as_dict()
    assert by_kind["criterion"]["quote"] == step.criterion.as_dict()
    assert by_kind["criterion"]["correlation"] == proposal.moment.correlation
    assert by_kind["choice"]["candidate_count"] == 1

    text = justification_text(fragments)
    assert QUOTE_DYAD_DATA.text in text
    assert step.question.text in text
    assert step.synchrony_type.text in text
    assert step.criterion.text in text
    assert f"r = {proposal.moment.correlation:.2f}" in text
    assert f"p. {step.question.page}" in text
    if expected_state is not None:
        assert by_kind["shared_state"]["state"] == expected_state
        assert f"{expected_state} state for 100% of the moment, against the 60%" in text
    elif expected_step == 2:
        assert "No shared arousal state." in text
    else:
        assert by_kind["asynchrony_direction"]["subtype"] == OPPOSITE_DIRECTION


def test_justification_reports_the_shared_state_share_against_its_threshold() -> None:
    config = VsrConfig()
    proposals, _passes = _select(
        {1: [_moment(1, 0.0, 60.0, 0.97, 0.88)], 2: [], 3: [], 4: [], 5: []},
        config,
    )

    text = justification_text(justification_fragments(proposals[0], QUOTE_DYAD_DATA, config))

    assert f"{HIGH} state for 88% of the moment, against the 60%" in text
    assert "Select this moment for interview." in text


def test_justification_marks_the_shared_arousal_steps_as_not_meeting_the_correlation_criterion() -> None:
    config = VsrConfig()
    proposals, _passes = _select(
        {1: [], 2: [], 3: [], 4: [_moment(4, 0.0, 60.0, 0.36, 1.0)], 5: []},
        config,
    )
    fragments = justification_fragments(proposals[0], QUOTE_DYAD_DATA, config)
    criterion = next(fragment for fragment in fragments if fragment["kind"] == "criterion")

    assert criterion["meets_threshold"] is False
    assert "is not met (r = 0.36)" in justification_text(fragments)


def test_justification_of_a_later_pass_quotes_the_step_6_instruction() -> None:
    config = VsrConfig()
    moments = {number: [] for number in STEPS}
    moments[1] = [_moment(1, 0.0, 30.0, 0.99, 1.0), _moment(1, 100.0, 130.0, 0.9, 0.9)]

    proposals, _passes = _select(moments, config)
    text = justification_text(justification_fragments(proposals[1], QUOTE_DYAD_DATA, config))

    assert "Have a total of 5 points been selected from STEPS 1 to 5?" in text
    assert "Select additional moments by going through STEPS 1 through 5 again" in text
    assert "Selected in pass 2" in text
    assert "Ranked 2 of 2 moments answering STEP 1" in text


def test_asynchronous_moment_states_the_signals_moved_in_opposite_directions() -> None:
    first, second, minutes = _lesson(_stretch(300, NEUTRAL_BAND, together=False))
    config = VsrConfig()

    selection = select_pair_moments(first, second, minutes, config)
    proposal = selection.proposals[0]
    fragments = justification_fragments(proposal, QUOTE_DYAD_DATA, config)
    direction = next(fragment for fragment in fragments if fragment["kind"] == "asynchrony_direction")

    assert proposal.moment.correlation == pytest.approx(-1.0)
    assert direction["subtype"] == OPPOSITE_DIRECTION
    assert QUOTE_ASYNCHRONOUS.text in justification_text(fragments)
    assert "The signals moved in opposite directions." in justification_text(fragments)


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


def _pipeline_result(tmp_path: Path):
    files = {}
    for index, alias in enumerate(["T", "S1", "S2", "S3"]):
        path = tmp_path / f"{alias}.csv"
        build_esense_csv(path, rows=1800, seed_offset=index * 0.4)
        files[alias] = path
    config = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=1)
    return run_pipeline(files, config)


def test_compute_vsr_builds_one_interview_per_dyad_and_one_for_the_teacher(tmp_path: Path) -> None:
    result = _pipeline_result(tmp_path)

    payload = compute_vsr(
        result,
        dyads=[("S1", "S2")],
        teacher_alias="T",
        student_aliases=["S1", "S2", "S3"],
        config=VsrConfig(),
    )

    assert [interview["key"] for interview in payload["interviews"]] == ["S1+S2", "T"]
    dyad, teacher = payload["interviews"]
    assert dyad["kind"] == "dyad"
    assert dyad["participants"] == ["S1", "S2"]
    assert dyad["data_basis"] == QUOTE_DYAD_DATA.as_dict()
    assert dyad["basis_notes"] == []
    assert teacher["kind"] == "teacher"
    assert teacher["participants"] == ["T", "S1", "S2", "S3"]
    assert teacher["data_basis"] == QUOTE_TEACHER_DATA.as_dict()
    assert teacher["basis_notes"] == [QUOTE_TEACHER_AGGREGATION.as_dict()]
    assert payload["config"]["vsr_sync_threshold"] == 0.8
    assert payload["stats"]["interview_count"] == 2

    for interview in payload["interviews"]:
        assert interview["window_count"] > 0
        assert sum(interview["candidate_counts"].values()) > 0
        assert interview["stats"]["primary_count"] + interview["stats"]["reserve_count"] == len(interview["moments"])
        for moment in interview["moments"]:
            assert moment["status"] in {PRIMARY, RESERVE}
            assert moment["start_seconds"] < moment["end_seconds"]
            assert moment["duration_seconds"] > 0
            assert interview["data_basis"]["text"] in moment["justification_text"]
            assert {fragment["kind"] for fragment in moment["justification"]} >= {"data_basis", "criterion"}


def test_compute_vsr_reports_moment_times_on_the_sensor_clock(tmp_path: Path) -> None:
    result = _pipeline_result(tmp_path)

    payload = compute_vsr(result, dyads=[("S1", "S2")])
    moments = payload["interviews"][0]["moments"]

    assert moments, "the synthetic recording produced no moment to place on the clock"
    for moment in moments:
        # build_esense_csv records from 12:00:00, so a moment's clock time is the
        # recording start plus its lesson time.
        hours, minutes_text, seconds_text = moment["start_clock"].split(":")
        offset = (int(hours) - 12) * 3600 + int(minutes_text) * 60 + int(seconds_text)
        assert offset == pytest.approx(moment["start_seconds"], abs=1.0)


def test_compute_vsr_rejects_unusable_interview_participants(tmp_path: Path) -> None:
    result = _pipeline_result(tmp_path)

    with pytest.raises(PipelineError, match="Unknown aliases requested: S9"):
        compute_vsr(result, dyads=[("S1", "S9")])
    with pytest.raises(PipelineError, match="two different participants"):
        compute_vsr(result, dyads=[("S1", "S1")])
    with pytest.raises(PipelineError, match="at least one student recording"):
        compute_vsr(result, dyads=[], teacher_alias="T", student_aliases=["T"])


def test_config_can_follow_an_edited_asynchrony_threshold() -> None:
    first, second, minutes = _lesson(_stretch(300, NEUTRAL_BAND, together=False))

    strict = select_pair_moments(first, second, minutes, VsrConfig(vsr_async_threshold=-1.0))
    literal = select_pair_moments(first, second, minutes, VsrConfig())

    assert not strict.moments_by_step[5]
    assert literal.moments_by_step[5]


def test_lesson_without_a_full_window_answers_no_step() -> None:
    first, second, minutes = _lesson(_stretch(20, NEUTRAL_BAND, together=False))

    selection = select_pair_moments(first, second, minutes)

    assert selection.window_count == 0
    assert selection.proposals == []
    assert len(selection.passes) == 1
    assert {record["reason"] for record in selection.passes[0]["steps"]} == {NO_CANDIDATES}
