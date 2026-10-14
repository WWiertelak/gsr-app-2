"""VSR interview moments: the GSR protocol of the "VSR Interview Flow Chart".

Implements the "GSR PARTICIPANTS" half of that document (pages 1, 3 and 4) as a
selection engine over an existing pipeline result: the five synchrony types with
their correlation criteria (page 1), the STEP 1-5 questions asked in document
order, and the STEP 6 loop that repeats those questions until five moments are
selected (pages 3-4).

What the document leaves open is decided here once, and every decision is
recorded in the justification of the moment it produced:
  * a "moment" is a run of consecutive 30 s correlation windows (5 s step) that
    answer the same step - the same windowing the Synchrony analysis uses;
  * an arousal state is the Co-arousal analysis' state (z-scored within the
    analysed period, HIGH above z = 1, LOW below z = -1), and it is "shared"
    when both participants hold it for at least 60% of the window;
  * when several moments answer one step, the engine takes the one that meets
    that step's criterion most strongly and reports how many candidates there
    were;
  * selected moments may not overlap in time; the protocol states no spacing
    rule, so none is imposed.

Justifications are built from document quotes and the measured values only.
They are returned as ordered fragments, each carrying a verbatim quote and its
page, so a user-visible surface can render the surrounding sentence through
Django's gettext while this package stays importable without Django;
``justification_text`` is the reference English rendering of those fragments.

The ESM half of the document (pages 2 and 5-7) is out of scope: the application
analyses GSR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

import numpy as np

from .common import PipelineError, _estimate_step_seconds, _resolve_aliases, _zscore
from .preprocessing import PipelineResult
from .synchrony import _window_correlation

CRITERIA_PAGE = 1  # "Criteria for moments selected for discussion in VSR interviews", GSR PARTICIPANTS
STEP_PAGES = {1: 3, 2: 3, 3: 3, 4: 4, 5: 4, 6: 4}

PRIMARY = "primary"
RESERVE = "reserve"

HIGH = "HIGH"
LOW = "LOW"

OPPOSITE_DIRECTION = "opposite_direction"
NO_RELATION = "no_relation"

NO_CANDIDATES = "no_candidates"
CANDIDATES_EXHAUSTED = "candidates_exhausted"

HIGHEST_CORRELATION = "highest_correlation"
LOWEST_CORRELATION = "lowest_correlation"
LARGEST_SHARED_STATE = "largest_shared_state_share"


@dataclass(frozen=True)
class Quote:
    """One verbatim quote from the document, with the page it was read from."""

    text: str
    page: int

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "page": self.page}


QUOTE_FIVE_MOMENTS = Quote("A total of 5 moments discussed per interview (ideally)", CRITERIA_PAGE)
QUOTE_RESERVE = Quote(
    "Aim to identify more (e.g. 8) just in case the participant has nothing to say "
    "about one of the selected moments",
    CRITERIA_PAGE,
)
QUOTE_DYAD_DATA = Quote("Points for discussion are selected based on the dyad\u2019s GSR data", CRITERIA_PAGE)
QUOTE_TEACHER_DATA = Quote(
    "Points selected for discussion are based on teacher and student aggregated GSR data", CRITERIA_PAGE
)
QUOTE_TEACHER_AGGREGATION = Quote(
    "Student data will be aggregated to create average GSR values for the 6 students", CRITERIA_PAGE
)
QUOTE_SYNCHRONOUS = Quote("Synchronous = above 0.8", CRITERIA_PAGE)
QUOTE_ASYNCHRONOUS = Quote("Asynchronous = below 0.1", CRITERIA_PAGE)

QUOTE_STEP_6_QUESTION = Quote("Have a total of 5 points been selected from STEPS 1 to 5?", STEP_PAGES[6])
QUOTE_STEP_6_YES = Quote("Use these moments for the interview.", STEP_PAGES[6])
QUOTE_STEP_6_NO = Quote(
    "Select additional moments by going through STEPS 1 through 5 again until a total of 5 moments are selected.",
    STEP_PAGES[6],
)


@dataclass(frozen=True)
class Step:
    """One STEP of the GSR protocol: its question, its synchrony type and its criterion."""

    number: int
    question: Quote
    synchrony_type: Quote
    criterion: Quote
    choice_rule: str
    # STEPS 3 and 4 are a shared arousal state WITHOUT co-movement: they cite the
    # same correlation criterion as STEPS 1-2, as the moment that does NOT meet it.
    meets_criterion: bool

    @property
    def select(self) -> Quote:
        return Quote("Select this moment for interview.", self.question.page)

    @property
    def go_to_next(self) -> Quote:
        return Quote(f"Go to STEP {self.number + 1}", self.question.page)


STEPS: dict[int, Step] = {
    1: Step(
        1,
        Quote(
            "Is there at least 1 moment when the pair shares the same arousal state AND co-movement",
            STEP_PAGES[1],
        ),
        Quote("Co-movement AND shared arousal states", CRITERIA_PAGE),
        QUOTE_SYNCHRONOUS,
        HIGHEST_CORRELATION,
        meets_criterion=True,
    ),
    2: Step(
        2,
        Quote("Is there at least 1 moment when the pair has significant co-movement?", STEP_PAGES[2]),
        Quote("Co-movement only", CRITERIA_PAGE),
        QUOTE_SYNCHRONOUS,
        HIGHEST_CORRELATION,
        meets_criterion=True,
    ),
    3: Step(
        3,
        Quote("Is there at least 1 moment when the pair shares a HIGH arousal state?", STEP_PAGES[3]),
        Quote("Shared HIGH arousal", CRITERIA_PAGE),
        QUOTE_SYNCHRONOUS,
        LARGEST_SHARED_STATE,
        meets_criterion=False,
    ),
    4: Step(
        4,
        Quote("Is there at least 1 moment when the pair shares a LOW arousal state?", STEP_PAGES[4]),
        Quote("Shared LOW arousal", CRITERIA_PAGE),
        QUOTE_SYNCHRONOUS,
        LARGEST_SHARED_STATE,
        meets_criterion=False,
    ),
    5: Step(
        5,
        Quote("Is there at least 1 moment when the pair is NOT synchronous?", STEP_PAGES[5]),
        Quote("Asynchronous moments", CRITERIA_PAGE),
        QUOTE_ASYNCHRONOUS,
        LOWEST_CORRELATION,
        meets_criterion=True,
    ),
}


@dataclass(frozen=True)
class VsrConfig:
    """Selection parameters. Every default is the document's own number.

    The windowing and arousal-state parameters repeat the Synchrony and
    Co-arousal defaults, which is what makes a VSR moment readable next to those
    analyses; the document does not define them.
    """

    synchronous_above: float = 0.8  # page 1: "Synchronous = above 0.8"
    asynchronous_below: float = 0.1  # page 1: "Asynchronous = below 0.1"
    window_seconds: float = 30.0
    step_seconds: float = 5.0
    high_z: float = 1.0
    low_z: float = -1.0
    shared_state_min_fraction: float = 0.6
    moments_per_interview: int = 5  # page 1: "A total of 5 moments discussed per interview (ideally)"
    reserve_moments: int = 3  # page 1: "Aim to identify more (e.g. 8)"
    correlation_method: str = "pearson"

    def __post_init__(self) -> None:
        if not -1.0 <= self.asynchronous_below <= self.synchronous_above <= 1.0:
            raise ValueError("asynchronous_below must not exceed synchronous_above, and both must lie in [-1, 1]")
        if self.window_seconds <= 0 or self.step_seconds <= 0:
            raise ValueError("window_seconds and step_seconds must be positive")
        if self.step_seconds > self.window_seconds:
            raise ValueError("step_seconds must not exceed window_seconds")
        if self.low_z >= self.high_z:
            raise ValueError("low_z must be lower than high_z")
        if not 0.0 < self.shared_state_min_fraction <= 1.0:
            raise ValueError("shared_state_min_fraction must lie in (0, 1]")
        if self.moments_per_interview < 1:
            raise ValueError("moments_per_interview must be at least 1")
        if self.reserve_moments < 0:
            raise ValueError("reserve_moments must not be negative")
        if self.correlation_method not in ("pearson", "spearman"):
            raise ValueError("correlation_method must be one of: pearson, spearman")

    def as_dict(self) -> dict[str, Any]:
        return {
            "synchronous_above": float(self.synchronous_above),
            "asynchronous_below": float(self.asynchronous_below),
            "window_seconds": float(self.window_seconds),
            "step_seconds": float(self.step_seconds),
            "high_z": float(self.high_z),
            "low_z": float(self.low_z),
            "shared_state_min_fraction": float(self.shared_state_min_fraction),
            "moments_per_interview": int(self.moments_per_interview),
            "reserve_moments": int(self.reserve_moments),
            "correlation_method": str(self.correlation_method),
        }


@dataclass(frozen=True)
class Window:
    """One correlation window of a pair, and the step its measurements answer."""

    start_seconds: float
    end_seconds: float
    correlation: float
    shared_high_fraction: float
    shared_low_fraction: float
    shared_state: str | None
    step: int | None


@dataclass(frozen=True)
class Moment:
    """A run of windows answering the same step, with that run's mean measurements.

    Every window in the run clears its step's criterion, so the mean does too -
    the value quoted in the justification is a value the whole moment satisfies.
    """

    step: int
    start_seconds: float
    end_seconds: float
    window_count: int
    correlation: float
    shared_state: str | None
    shared_state_fraction: float | None

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def asynchrony_subtype(self) -> str | None:
        """Which kind of "below 0.1" this is; the threshold itself is taken literally."""
        if self.step != 5:
            return None
        return OPPOSITE_DIRECTION if self.correlation < 0.0 else NO_RELATION

    def strength(self) -> float:
        """How strongly the moment meets its step's criterion; higher wins."""
        rule = STEPS[self.step].choice_rule
        if rule == LOWEST_CORRELATION:
            return -self.correlation
        if rule == LARGEST_SHARED_STATE:
            return self.shared_state_fraction or 0.0
        return self.correlation

    def overlaps(self, other: "Moment") -> bool:
        return self.start_seconds < other.end_seconds and other.start_seconds < self.end_seconds


@dataclass(frozen=True)
class Proposal:
    """A moment selected for an interview, with how the protocol arrived at it."""

    number: int
    status: str
    pass_number: int
    after_step_6_no: bool
    rank: int
    candidate_count: int
    moment: Moment


def aggregate_student_signal(signals: Sequence[np.ndarray]) -> np.ndarray:
    """The students' average GSR, used as the teacher interview's second signal.

    Page 1: "Student data will be aggregated to create average GSR values for
    the 6 students". Each student is z-scored before averaging, so a student
    with a wider range cannot dominate the average, and the average is z-scored
    again to sit on the same scale as the teacher's own series.
    """
    if not signals:
        raise PipelineError("The teacher interview needs at least one student signal to aggregate")
    stacked = np.vstack([_zscore(np.asarray(signal, dtype=float)) for signal in signals])
    return _zscore(stacked.mean(axis=0))


def _shared_state(shared_high_fraction: float, shared_low_fraction: float, config: VsrConfig) -> str | None:
    """The arousal state both participants hold for long enough, if there is one.

    They cannot both hold HIGH and LOW for most of the same window, so the two
    fractions cannot clear the minimum together.
    """
    if shared_high_fraction >= config.shared_state_min_fraction:
        return HIGH
    if shared_low_fraction >= config.shared_state_min_fraction:
        return LOW
    return None


def _window_step(correlation: float, shared_state: str | None, config: VsrConfig) -> int | None:
    """The step a window answers, tested in document order; the steps exclude each other."""
    if correlation > config.synchronous_above:
        return 1 if shared_state is not None else 2
    if shared_state == HIGH:
        return 3
    if shared_state == LOW:
        return 4
    if correlation < config.asynchronous_below:
        return 5
    return None


def _windows(a: np.ndarray, b: np.ndarray, relative_minutes: np.ndarray, config: VsrConfig) -> list[Window]:
    sample_seconds = _estimate_step_seconds(relative_minutes)
    width = max(2, int(round(config.window_seconds / sample_seconds)))
    stride = max(1, int(round(config.step_seconds / sample_seconds)))
    seconds = relative_minutes * 60.0
    shared_high = (a > config.high_z) & (b > config.high_z)
    shared_low = (a < config.low_z) & (b < config.low_z)

    windows: list[Window] = []
    for start in range(0, len(relative_minutes) - width + 1, stride):
        stop = start + width
        correlation = _window_correlation(a[start:stop], b[start:stop], config.correlation_method)
        high_fraction = float(shared_high[start:stop].mean())
        low_fraction = float(shared_low[start:stop].mean())
        state = _shared_state(high_fraction, low_fraction, config)
        windows.append(
            Window(
                start_seconds=float(seconds[start]),
                end_seconds=float(seconds[stop - 1]),
                correlation=correlation,
                shared_high_fraction=high_fraction,
                shared_low_fraction=low_fraction,
                shared_state=state,
                step=_window_step(correlation, state, config),
            )
        )
    return windows


def _moment_from_run(step: int, state: str | None, run: list[Window]) -> Moment:
    fractions = [
        window.shared_high_fraction if state == HIGH else window.shared_low_fraction for window in run
    ]
    return Moment(
        step=step,
        start_seconds=run[0].start_seconds,
        end_seconds=run[-1].end_seconds,
        window_count=len(run),
        correlation=float(np.mean([window.correlation for window in run])),
        shared_state=state,
        shared_state_fraction=float(np.mean(fractions)) if state is not None else None,
    )


def _moments_by_step(windows: list[Window]) -> dict[int, list[Moment]]:
    """Group windows into moments: strictly consecutive windows answering the same step.

    A change of shared arousal state splits a moment too, so the state a moment
    reports holds for all of it.
    """
    runs: dict[tuple[int, str | None], list[list[Window]]] = {}
    previous_key: tuple[int, str | None] | None = None
    for window in windows:
        key = (window.step, window.shared_state) if window.step is not None else None
        if key is not None:
            if key == previous_key:
                runs[key][-1].append(window)
            else:
                runs.setdefault(key, []).append([window])
        previous_key = key

    moments: dict[int, list[Moment]] = {step: [] for step in STEPS}
    for (step, state), step_runs in runs.items():
        moments[step].extend(_moment_from_run(step, state, run) for run in step_runs)
    for candidates in moments.values():
        # Strongest first, then longer, then earlier: the document does not say
        # which of several moments answering one step to take.
        candidates.sort(key=lambda moment: (-moment.strength(), -moment.duration_seconds, moment.start_seconds))
    return moments


def _select(moments: dict[int, list[Moment]], config: VsrConfig) -> tuple[list[Proposal], list[dict[str, Any]]]:
    """Walk STEPS 1-5 repeatedly (STEP 6) until five moments plus the reserve are selected."""
    target = config.moments_per_interview + config.reserve_moments
    selected: list[Proposal] = []
    used: set[tuple[int, int]] = set()
    passes: list[dict[str, Any]] = []

    pass_number = 0
    while len(selected) < target:
        pass_number += 1
        purpose = PRIMARY if len(selected) < config.moments_per_interview else RESERVE
        step_records: list[dict[str, Any]] = []
        selected_in_pass = 0

        for number, step in STEPS.items():
            if len(selected) >= target:
                break
            taken = [proposal.moment for proposal in selected]
            choice = next(
                (
                    (rank, moment)
                    for rank, moment in enumerate(moments[number], start=1)
                    if (number, rank) not in used
                    and not any(moment.overlaps(other) for other in taken)
                ),
                None,
            )
            if choice is None:
                step_records.append(
                    _step_record(
                        step,
                        answer="NO",
                        candidate_count=len(moments[number]),
                        reason=NO_CANDIDATES if not moments[number] else CANDIDATES_EXHAUSTED,
                    )
                )
                continue

            rank, moment = choice
            used.add((number, rank))
            selected_in_pass += 1
            proposal = Proposal(
                number=len(selected) + 1,
                status=PRIMARY if len(selected) < config.moments_per_interview else RESERVE,
                pass_number=pass_number,
                after_step_6_no=pass_number > 1 and purpose == PRIMARY,
                rank=rank,
                candidate_count=len(moments[number]),
                moment=moment,
            )
            selected.append(proposal)
            step_records.append(
                _step_record(step, answer="YES", candidate_count=len(moments[number]), moment_number=proposal.number)
            )

        primary_count = min(len(selected), config.moments_per_interview)
        reached_target = primary_count >= config.moments_per_interview
        passes.append(
            {
                "pass_number": pass_number,
                "purpose": purpose,
                "steps": step_records,
                "step_6": {
                    "step": 6,
                    "question": QUOTE_STEP_6_QUESTION.as_dict(),
                    "answer": "YES" if reached_target else "NO",
                    "instruction": (QUOTE_STEP_6_YES if reached_target else QUOTE_STEP_6_NO).as_dict(),
                    "selected_count": primary_count,
                },
            }
        )
        if selected_in_pass == 0:
            break

    return selected, passes


def _step_record(
    step: Step,
    *,
    answer: str,
    candidate_count: int,
    reason: str | None = None,
    moment_number: int | None = None,
) -> dict[str, Any]:
    fragments = [
        {
            "kind": "step_question",
            "step": step.number,
            "quote": step.question.as_dict(),
            "answer": answer,
            "candidate_count": candidate_count,
            "reason": reason,
            "outcome": (step.select if answer == "YES" else step.go_to_next).as_dict(),
        }
    ]
    return {
        "step": step.number,
        "answer": answer,
        "candidate_count": candidate_count,
        "reason": reason,
        "moment_number": moment_number,
        "fragments": fragments,
        "text": justification_text(fragments),
    }


def justification_fragments(proposal: Proposal, data_basis: Quote, config: VsrConfig) -> list[dict[str, Any]]:
    """The justification of one proposal: document quotes plus the measured values."""
    moment = proposal.moment
    minimum_shared_fraction = config.shared_state_min_fraction
    step = STEPS[moment.step]
    fragments: list[dict[str, Any]] = [
        {"kind": "data_basis", "quote": data_basis.as_dict()},
        {
            "kind": "step_question",
            "step": step.number,
            "quote": step.question.as_dict(),
            "answer": "YES",
            "candidate_count": proposal.candidate_count,
            "reason": None,
            "outcome": step.select.as_dict(),
        },
        {"kind": "synchrony_type", "quote": step.synchrony_type.as_dict()},
        {
            "kind": "criterion",
            "quote": step.criterion.as_dict(),
            "correlation": moment.correlation,
            "meets_threshold": step.meets_criterion,
        },
    ]
    if moment.shared_state is not None:
        fragments.append(
            {
                "kind": "shared_state",
                "state": moment.shared_state,
                "fraction": moment.shared_state_fraction,
                "minimum_fraction": minimum_shared_fraction,
            }
        )
    elif moment.step == 2:
        fragments.append({"kind": "no_shared_state"})
    if moment.asynchrony_subtype is not None:
        fragments.append(
            {
                "kind": "asynchrony_direction",
                "subtype": moment.asynchrony_subtype,
                "correlation": moment.correlation,
            }
        )
    fragments.append(
        {
            "kind": "choice",
            "step": step.number,
            "rank": proposal.rank,
            "candidate_count": proposal.candidate_count,
            "rule": step.choice_rule,
        }
    )
    if proposal.after_step_6_no:
        fragments.append(
            {
                "kind": "repeat_pass",
                "pass_number": proposal.pass_number,
                "quote": QUOTE_STEP_6_QUESTION.as_dict(),
                "answer": "NO",
                "instruction": QUOTE_STEP_6_NO.as_dict(),
            }
        )
    if proposal.status == RESERVE:
        fragments.append({"kind": "reserve", "quote": QUOTE_RESERVE.as_dict()})
    return fragments


_CHOICE_RULE_PHRASES = {
    HIGHEST_CORRELATION: "the highest correlation",
    LOWEST_CORRELATION: "the lowest correlation",
    LARGEST_SHARED_STATE: "the largest shared-state share",
}


def _quote(fragment: dict[str, Any], key: str = "quote") -> str:
    quote = fragment[key]
    return f'(p. {quote["page"]}) \u201c{quote["text"]}\u201d'


def _moments_counted(count: int) -> str:
    return "1 moment" if count == 1 else f"{count} moments"


def justification_text(fragments: Sequence[dict[str, Any]]) -> str:
    """Reference English rendering of justification fragments.

    Localized surfaces render the same fragments through Django's gettext; the
    quotes stay in the document's English either way.
    """
    sentences: list[str] = []
    for fragment in fragments:
        kind = fragment["kind"]
        if kind == "data_basis":
            sentences.append(f"Document {_quote(fragment)}.")
        elif kind == "step_question":
            answer = fragment["answer"]
            detail = ""
            if answer == "NO":
                counted = _moments_counted(fragment["candidate_count"])
                detail = (
                    f" ({counted} answer the criterion, all already selected or overlapping a selected moment)"
                    if fragment["reason"] == CANDIDATES_EXHAUSTED
                    else " (no moment answers the criterion)"
                )
            sentences.append(
                f"STEP {fragment['step']} {_quote(fragment)} - {answer}{detail} - {_quote(fragment, 'outcome')}."
            )
        elif kind == "synchrony_type":
            sentences.append(f"Synchrony type {_quote(fragment)}.")
        elif kind == "criterion":
            measured = f"r = {fragment['correlation']:.2f}"
            sentences.append(
                f"Criterion {_quote(fragment)}: {measured}."
                if fragment["meets_threshold"]
                else f"Criterion {_quote(fragment)} is not met ({measured}), which is what separates this type"
                " from co-movement."
            )
        elif kind == "shared_state":
            sentences.append(
                f"Both participants in the {fragment['state']} state for {fragment['fraction'] * 100:.0f}%"
                f" of the moment, against the {fragment['minimum_fraction'] * 100:.0f}% a shared state requires."
            )
        elif kind == "no_shared_state":
            sentences.append("No shared arousal state.")
        elif kind == "asynchrony_direction":
            sentences.append(
                "The signals moved in opposite directions."
                if fragment["subtype"] == OPPOSITE_DIRECTION
                else "The signals were unrelated rather than opposed."
            )
        elif kind == "choice":
            phrase = _CHOICE_RULE_PHRASES[fragment["rule"]]
            counted = _moments_counted(fragment["candidate_count"])
            if fragment["rank"] == 1:
                sentences.append(f"Of {counted} answering STEP {fragment['step']} this one has {phrase}.")
            else:
                sentences.append(
                    f"Ranked {fragment['rank']} of {counted} answering STEP {fragment['step']} by {phrase}"
                    " (the stronger ones were selected earlier or overlap a selected moment)."
                )
        elif kind == "repeat_pass":
            sentences.append(
                f"STEP 6 {_quote(fragment)} - NO, so {_quote(fragment, 'instruction')}"
                f" Selected in pass {fragment['pass_number']}."
            )
        elif kind == "reserve":
            sentences.append(f"Additional moment {_quote(fragment)}.")
        else:  # pragma: no cover - fragment kinds are a closed set
            raise ValueError(f"Unknown justification fragment: {kind}")
    return " ".join(sentences)


def _clock_start(result: PipelineResult) -> datetime | None:
    try:
        return datetime.fromisoformat(str(result.alignment["start"]))
    except (KeyError, TypeError, ValueError):
        return None


def _proposal_payload(
    proposal: Proposal, data_basis: Quote, clock_start: datetime | None, config: VsrConfig
) -> dict[str, Any]:
    moment = proposal.moment
    fragments = justification_fragments(proposal, data_basis, config)
    payload: dict[str, Any] = {
        "number": proposal.number,
        "status": proposal.status,
        "pass_number": proposal.pass_number,
        "step": moment.step,
        "synchrony_type": STEPS[moment.step].synchrony_type.as_dict(),
        "start_seconds": round(moment.start_seconds, 1),
        "end_seconds": round(moment.end_seconds, 1),
        "duration_seconds": round(moment.duration_seconds, 1),
        "window_count": moment.window_count,
        "correlation": moment.correlation,
        "shared_state": moment.shared_state,
        "shared_state_fraction": moment.shared_state_fraction,
        "asynchrony_subtype": moment.asynchrony_subtype,
        "candidate_rank": proposal.rank,
        "candidate_count": proposal.candidate_count,
        "start_clock": None,
        "end_clock": None,
        "justification": fragments,
        "justification_text": justification_text(fragments),
    }
    if clock_start is not None:
        payload["start_clock"] = (clock_start + timedelta(seconds=moment.start_seconds)).strftime("%H:%M:%S")
        payload["end_clock"] = (clock_start + timedelta(seconds=moment.end_seconds)).strftime("%H:%M:%S")
    return payload


@dataclass(frozen=True)
class PairSelection:
    """What the protocol did for one pair: its outcome and the record of getting there."""

    proposals: list[Proposal]
    passes: list[dict[str, Any]]
    moments_by_step: dict[int, list[Moment]]
    window_count: int


def select_pair_moments(
    signal_a: np.ndarray,
    signal_b: np.ndarray,
    relative_minutes: np.ndarray,
    config: VsrConfig | None = None,
) -> PairSelection:
    """Run the GSR protocol on one pair of already-z-scored signals."""
    cfg = config if config is not None else VsrConfig()
    windows = _windows(signal_a, signal_b, relative_minutes, cfg)
    moments = _moments_by_step(windows)
    proposals, passes = _select(moments, cfg)
    return PairSelection(proposals=proposals, passes=passes, moments_by_step=moments, window_count=len(windows))


def _interview_payload(
    key: str,
    kind: str,
    participants: list[str],
    data_basis: Quote,
    basis_notes: list[Quote],
    signal_a: np.ndarray,
    signal_b: np.ndarray,
    relative_minutes: np.ndarray,
    clock_start: datetime | None,
    config: VsrConfig,
) -> dict[str, Any]:
    selection = select_pair_moments(signal_a, signal_b, relative_minutes, config)
    primary = [proposal for proposal in selection.proposals if proposal.status == PRIMARY]
    return {
        "key": key,
        "kind": kind,
        "participants": participants,
        "data_basis": data_basis.as_dict(),
        "basis_notes": [quote.as_dict() for quote in basis_notes],
        "window_count": selection.window_count,
        "candidate_counts": {
            str(step): len(candidates) for step, candidates in selection.moments_by_step.items()
        },
        "passes": selection.passes,
        "moments": [
            _proposal_payload(proposal, data_basis, clock_start, config) for proposal in selection.proposals
        ],
        "stats": {
            "primary_count": len(primary),
            "reserve_count": len(selection.proposals) - len(primary),
            "primary_target": config.moments_per_interview,
            "reserve_target": config.reserve_moments,
            "pass_count": len(selection.passes),
            "complete": len(primary) >= config.moments_per_interview,
        },
    }


def compute_vsr(
    result: PipelineResult,
    dyads: Sequence[Sequence[str]],
    teacher_alias: str | None = None,
    student_aliases: Sequence[str] | None = None,
    config: VsrConfig | None = None,
) -> dict[str, Any]:
    """Interview moment proposals for every student dyad and for the teacher.

    ``dyads`` are the student pairs working together (page 1: "Focused on
    synchronicity of student dyads working together"); ``teacher_alias`` with
    ``student_aliases`` adds the teacher interview against the students' average
    GSR (page 1: "Focused on synchronicity of the teacher and all GSR students").
    """
    cfg = config if config is not None else VsrConfig()
    frame = result.merged_decimated
    relative_minutes = frame["time"].to_numpy(dtype=float)
    clock_start = _clock_start(result)

    requested = [alias for dyad in dyads for alias in dyad]
    if teacher_alias is not None:
        requested += [teacher_alias, *(student_aliases or ())]
    _resolve_aliases(result.aliases, requested or None)

    def signal(alias: str) -> np.ndarray:
        return _zscore(frame[f"microsiemens_{alias}"].to_numpy(dtype=float))

    interviews: list[dict[str, Any]] = []
    for dyad in dyads:
        if len(dyad) != 2 or dyad[0] == dyad[1]:
            raise PipelineError(f"A dyad must name two different participants, got: {', '.join(dyad) or 'none'}")
        alias_a, alias_b = dyad[0], dyad[1]
        interviews.append(
            _interview_payload(
                key=f"{alias_a}+{alias_b}",
                kind="dyad",
                participants=[alias_a, alias_b],
                data_basis=QUOTE_DYAD_DATA,
                basis_notes=[],
                signal_a=signal(alias_a),
                signal_b=signal(alias_b),
                relative_minutes=relative_minutes,
                clock_start=clock_start,
                config=cfg,
            )
        )

    if teacher_alias is not None:
        students = [alias for alias in (student_aliases or ()) if alias != teacher_alias]
        if not students:
            raise PipelineError("The teacher interview needs at least one student recording to aggregate")
        interviews.append(
            _interview_payload(
                key=teacher_alias,
                kind="teacher",
                participants=[teacher_alias, *students],
                data_basis=QUOTE_TEACHER_DATA,
                basis_notes=[QUOTE_TEACHER_AGGREGATION],
                signal_a=signal(teacher_alias),
                signal_b=aggregate_student_signal([signal(alias) for alias in students]),
                relative_minutes=relative_minutes,
                clock_start=clock_start,
                config=cfg,
            )
        )

    return {
        "config": cfg.as_dict(),
        "criteria": {
            "moments_per_interview": QUOTE_FIVE_MOMENTS.as_dict(),
            "reserve": QUOTE_RESERVE.as_dict(),
            "synchronous": QUOTE_SYNCHRONOUS.as_dict(),
            "asynchronous": QUOTE_ASYNCHRONOUS.as_dict(),
        },
        "interviews": interviews,
        "stats": {
            "interview_count": len(interviews),
            "primary_total": sum(interview["stats"]["primary_count"] for interview in interviews),
            "reserve_total": sum(interview["stats"]["reserve_count"] for interview in interviews),
            "incomplete_interviews": [
                interview["key"] for interview in interviews if not interview["stats"]["complete"]
            ],
        },
    }
