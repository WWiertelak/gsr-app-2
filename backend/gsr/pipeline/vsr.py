"""VSR interview moments chosen from the Analysis results (the "VSR Interview Flow Chart", GSR section).

The Interviews step does not analyse the signals again with settings of its own: it
takes the episodes the Analysis step found, with the Analysis settings they were found
with, and sorts them into the document's steps for every interview. The document's
correlation criteria ("Synchronous = above 0.8", "Asynchronous = below 0.1") are read as
the strength of the correlation, its absolute value; Hassan's e-mail adds the divergent
type. The steps (``PROTOCOL``, the one place their order lives):

  1. co-movement and a shared arousal state - a synchronization episode (Synchronization
     tab) or a co-movement episode (Co-arousal tab) of the interview's people whose r is
     at least the interview threshold (0.8), that overlaps a shared High or Low episode
     (Co-arousal tab) of the same people;
  2. co-movement only - such a co-movement episode that overlaps no shared state;
  3. shared High arousal - a shared High episode that overlaps no such co-movement;
  4. shared Low arousal - a shared Low episode that overlaps no such co-movement;
  5. no synchrony - opposite arousal first (Co-arousal tab: one person High while the
     other is Low, the complement of STEPS 3-4), then a decoupling stretch (Synchronization
     tab: between the pair's synchronization and divergent episodes, its mean r within
     +/-0.1, "Asynchronous = below 0.1");
  4b. divergent synchrony - a divergent synchronization episode (Synchronization tab: the
     signals move in opposite directions) whose r is at or below minus the interview
     threshold ("ideally above .80 for the interviews"), walked after STEP 5 so the first
     five moments keep the document's "4 synchronous, 1 not synchronous".

An episode belongs to a student pair when both students are in it (members of a
synchronization subgroup, contributors of a co-arousal episode). The teacher interview is
the pair of the teacher and the students' average signal (page 1 of the document:
"Student data will be aggregated to create average GSR values for the 6 students",
"Points selected for discussion are based on teacher and student aggregated GSR data"):
its episodes are found by the Analysis' own functions and settings on that pair
(``aggregate_analysis``), not taken from the Analysis episodes the teacher shares with
any one student. The correlation r of a moment is the interview's own, from the
Synchronization tab's windows (window, step and method from the Analysis settings): the
pair's two signals, or the teacher against the students' average.

STEP 6 then walks the steps again until the primary moments are selected, and once more
for the reserve. Of several moments answering one step the strongest comes first: the
highest r in STEPS 1-2, the lowest r in STEP 4b; in STEP 5 opposite arousal before
decoupling, and of the decoupling stretches the r closest to zero (the clearest absence of
a relation); then the longer one, then the earlier one. Selected
moments may not overlap.

The ESM half of the document is out of scope: the application analyses GSR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import numpy as np
import pandas as pd

from .coarousal import compute_coarousal
from .common import PipelineError, _estimate_step_seconds, _resolve_aliases, _zscore, clock_at, clock_start
from .config import PipelineConfig
from .preprocessing import PipelineResult
from .synchrony import _rolling_synchrony, _window_correlation, synchrony_types

PRIMARY = "primary"
RESERVE = "reserve"

HIGH = "HIGH"
LOW = "LOW"

SYNCHRONY = "synchrony"
COAROUSAL = "coarousal"
# An episode of the teacher and the students' average, found for the teacher interview (not on the Analysis screen).
AGGREGATE = "aggregate"

# The two participants of the teacher interview's own analysis.
AGGREGATE_PAIR = ("teacher", "students")

# The moment types.
COMOVEMENT_STATE = "comovement_state"
COMOVEMENT = "comovement"
SHARED_HIGH = "shared_high"
SHARED_LOW = "shared_low"
DIVERGENT = "divergent"
OPPOSITE = "opposite"
DECOUPLED = "decoupled"

# Every moment type with the step of the VSR flow chart it answers, in the order STEP 6 walks them: the document's
# STEPS 1-5 read by correlation strength ("4 synchronous, 1 not synchronous": the first walk yields STEP 5 among the
# five primary moments), with Hassan's e-mail's types where they belong - opposite arousal before decoupling in
# STEP 5 (no synchrony), divergent synchrony as STEP 4b after it. Walked before STEP 5, the many divergent episodes
# would push the no-synchrony moment out of the primary five.
# This is the one place the order lives: moving or renumbering a type is one line.
PROTOCOL = (
    ("1", COMOVEMENT_STATE),
    ("2", COMOVEMENT),
    ("3", SHARED_HIGH),
    ("4", SHARED_LOW),
    ("5", OPPOSITE),
    ("5", DECOUPLED),
    ("4b", DIVERGENT),
)
STEP_NUMBERS = tuple(dict.fromkeys(step for step, _kind in PROTOCOL))
STEP_OF = {kind: step for step, kind in PROTOCOL}
_KIND_ORDER = {kind: index for index, (_step, kind) in enumerate(PROTOCOL)}


def step_label(step: Any) -> str:
    """A step as ``STEP_NUMBERS`` names it; moments stored before STEP 4b existed have whole-number steps."""
    return str(step)


# The document: "A total of 5 moments discussed per interview (ideally)" and "Aim to identify more (e.g. 8)".
DEFAULT_PRIMARY_COUNT = 5
DEFAULT_RESERVE_COUNT = 3

# Samples per interview signal kept for the moment previews (the Analysis charts send 3000 per series).
TRACE_POINTS = 4000

# The Analysis settings a proposal depends on, stated on the Interviews step and in the report.
ANALYSIS_SETTINGS = (
    "synchrony_threshold",
    "synchrony_window_seconds",
    "synchrony_step_seconds",
    "synchrony_correlation_method",
    "synchrony_episode_min_size",
    "synchrony_episode_min_duration_seconds",
    "synchrony_episode_max_gap_seconds",
    "synchrony_interview_threshold",
    "synchrony_divergent_threshold",
    "synchrony_decoupled_threshold",
    "contagion_high_z",
    "contagion_low_z",
    "contagion_min_participants",
    "contagion_comovement_rho",
)


@dataclass(frozen=True)
class Episode:
    """One Analysis episode as an interview sees it."""

    source: str  # SYNCHRONY or COAROUSAL, or AGGREGATE for the teacher interview's own episodes
    # Its number on the Analysis screen: E<number> (D, U for divergent and decoupling) on Synchronization, <number>
    # on Co-arousal.
    number: int
    start_seconds: float
    end_seconds: float
    state: str | None  # HIGH or LOW for a shared-state episode, None otherwise
    # The interview's people in the episode: 2 of 2 for a pair (the teacher and the students' average too); moments
    # chosen before the teacher interview used the aggregated signals counted the students in it with the teacher.
    in_episode: int
    people: int
    # "synchronization", "co-movement", "state", "opposite", "divergent" or "decoupled"; moments chosen before the
    # kinds were stored have none.
    kind: str = ""

    def overlap(self, start: float, end: float) -> float:
        return max(0.0, min(self.end_seconds, end) - max(self.start_seconds, start))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "number": self.number,
            "start_seconds": round(self.start_seconds, 1),
            "end_seconds": round(self.end_seconds, 1),
            "state": self.state,
            "in_episode": self.in_episode,
            "people": self.people,
        }


@dataclass(frozen=True)
class Candidate:
    """A moment of one type (``PROTOCOL``), with what it was made from."""

    kind: str
    start_seconds: float
    end_seconds: float
    correlation: float
    # The Analysis episode the moment is: co-movement (STEPS 1-2), shared state (3-4), divergent (4b), opposite
    # arousal or decoupling (5).
    episode: Episode | None = None
    # STEP 1: the shared-state episode the co-movement episode overlaps most.
    shared: Episode | None = None

    @property
    def step(self) -> str:
        return STEP_OF[self.kind]

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def shared_state(self) -> str | None:
        state_episode = self.shared if self.kind == COMOVEMENT_STATE else self.episode
        return state_episode.state if state_episode is not None else None

    def sort_key(self) -> tuple[float, float, float, float]:
        """In a step, the types in ``PROTOCOL`` order, then the strongest of a type first (r counts to the two
        decimals it is shown with), then the longer, then the earlier."""
        r = round(self.correlation, 2)
        if self.kind in (COMOVEMENT_STATE, COMOVEMENT):
            strength = -r
        elif self.kind in (SHARED_HIGH, SHARED_LOW, OPPOSITE):
            strength = -float(self.episode.in_episode) if self.episode else 0.0
        elif self.kind == DIVERGENT:
            strength = r
        else:  # DECOUPLED: the clearest absence of a relation first
            strength = abs(r)
        return _KIND_ORDER[self.kind], strength, -self.duration_seconds, self.start_seconds

    def overlaps(self, other: "Candidate") -> bool:
        return self.start_seconds < other.end_seconds and other.start_seconds < self.end_seconds


@dataclass(frozen=True)
class Proposal:
    """A moment selected for an interview, with how the protocol arrived at it."""

    number: int
    status: str
    pass_number: int
    rank: int
    candidate_count: int
    candidate: Candidate


@dataclass(frozen=True)
class Windows:
    """The interview's windowed correlation: window centres (lesson seconds) and r per window."""

    centers: np.ndarray
    values: np.ndarray

    def mean(self, start: float, end: float) -> float:
        """Mean r of the windows centred in [start, end]; the nearest window when none is."""
        if not len(self.centers):
            return 0.0
        inside = (self.centers >= start - 1e-9) & (self.centers <= end + 1e-9)
        if inside.any():
            return float(np.mean(self.values[inside]))
        return float(self.values[int(np.argmin(np.abs(self.centers - (start + end) / 2.0)))])


def aggregate_student_signal(signals: Sequence[np.ndarray]) -> np.ndarray:
    """The students' average GSR, the teacher interview's second signal.

    Each student is z-scored before averaging, so a student with a wider range cannot
    dominate the average, and the average is z-scored again to sit on the same scale
    as the teacher's own series.
    """
    if not signals:
        raise PipelineError("The teacher interview needs at least one student signal to aggregate")
    stacked = np.vstack([_zscore(np.asarray(signal, dtype=float)) for signal in signals])
    return _zscore(stacked.mean(axis=0))


def interview_signals(frame: Any, kind: str, participants: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
    """The two z-scored signals an interview compares: a pair's two, or the teacher and the students' average.

    ``participants`` is the interview's list: the two pair members, or the teacher followed by the students.
    """

    def signal(alias: str) -> np.ndarray:
        return _zscore(frame[f"microsiemens_{alias}"].to_numpy(dtype=float))

    if kind == "teacher":
        return signal(participants[0]), aggregate_student_signal([signal(alias) for alias in participants[1:]])
    return signal(participants[0]), signal(participants[1])


def windowed_correlation(a: np.ndarray, b: np.ndarray, relative_minutes: np.ndarray,
                         config: PipelineConfig) -> Windows:
    """The Synchronization tab's rolling windows for two signals: same window, step, centres and method."""
    step_seconds = _estimate_step_seconds(relative_minutes)
    fs = 1.0 / step_seconds if step_seconds > 0 else 1.0
    width = max(2, int(round(config.synchrony_window_seconds * fs)))
    stride = max(1, int(round(config.synchrony_step_seconds * fs)))
    centers: list[float] = []
    values: list[float] = []
    for start in range(0, len(relative_minutes) - width + 1, stride):
        stop = start + width
        centers.append(float(relative_minutes[start + width // 2]) * 60.0)
        values.append(_window_correlation(a[start:stop], b[start:stop], config.synchrony_correlation_method))
    return Windows(np.asarray(centers, dtype=float), np.asarray(values, dtype=float))


def _span(start: float, end: float, half_window: float, lesson: tuple[float, float]) -> tuple[float, float]:
    """An episode's times; a single-window episode (start = end) gets the window it was measured over."""
    if end > start:
        return start, end
    return max(lesson[0], start - half_window), min(lesson[1], end + half_window)


@dataclass(frozen=True)
class _PairInput:
    """The part of a ``PipelineResult`` the co-arousal analysis reads, for a pair of signals of the interview's own."""

    aliases: list[str]
    merged_decimated: pd.DataFrame


def aggregate_analysis(teacher: np.ndarray, students: np.ndarray, relative_minutes: np.ndarray,
                       config: PipelineConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """The Analysis of the teacher and the students' average as a pair of participants (``AGGREGATE_PAIR``).

    The Analysis step's own functions and settings, with the pair as the only subgroup (two people make an
    episode): the synchronization windows with their synchronization, divergent and decoupling episodes, and the
    co-arousal High, Low, co-movement and opposite episodes, shaped like the Analysis payload so ``interview_episodes``
    reads them as it reads a student pair's. The co-arousal surrogate test is skipped: the moments do not use it.
    """
    pair_config = config.model_copy(update={"synchrony_episode_min_size": 2, "contagion_min_participants": 2,
                                            "contagion_surrogate_count": 0})
    aliases = list(AGGREGATE_PAIR)
    signals = dict(zip(aliases, (np.asarray(teacher, dtype=float), np.asarray(students, dtype=float))))
    step_seconds = _estimate_step_seconds(relative_minutes)
    fs = 1.0 / step_seconds if step_seconds > 0 else 1.0
    windows, episodes = _rolling_synchrony(signals, aliases, relative_minutes, fs, pair_config)
    frame = pd.DataFrame({"time": relative_minutes, **{f"microsiemens_{alias}": signals[alias] for alias in aliases}})
    coarousal = compute_coarousal(_PairInput(aliases, frame), aliases, pair_config,  # type: ignore[arg-type]
                                  opposite=True)
    synchrony = synchrony_types({"aliases": aliases, "windows": windows, "episodes": episodes, "stats": {}}, pair_config)
    return synchrony, coarousal


@dataclass(frozen=True)
class InterviewEpisodes:
    """The Analysis episodes of one interview's pair, by kind."""

    comovement: list[Episode]
    shared: list[Episode]
    divergent: list[Episode]
    opposite: list[Episode]
    decoupled: list[Episode]


def interview_episodes(pair: Sequence[str], synchrony: dict[str, Any], coarousal: dict[str, Any],
                       config: PipelineConfig, lesson: tuple[float, float],
                       source: str | None = None) -> InterviewEpisodes:
    """The pair's episodes: the Analysis episodes both of them are in.

    ``synchrony`` and ``coarousal`` are the Analysis payloads, or the teacher interview's own (``aggregate_analysis``,
    with ``source`` AGGREGATE); ``lesson`` is the analysed period in lesson seconds.
    """
    half_window = config.synchrony_window_seconds / 2.0
    members = set(pair)
    found = InterviewEpisodes([], [], [], [], [])

    def episode(default_source: str, kind: str, index: int, start: float, end: float,
                state: str | None = None) -> Episode:
        return Episode(source or default_source, index + 1, *_span(start, end, half_window, lesson), state, 2, 2, kind)

    for key, kind, target in (("episodes", "synchronization", found.comovement),
                              ("divergent_episodes", "divergent", found.divergent),
                              ("decoupled_episodes", "decoupled", found.decoupled)):
        for index, item in enumerate(synchrony.get(key, [])):
            if members.issubset(item["members"]):
                target.append(episode(SYNCHRONY, kind, index, item["start_relative"] * 60.0,
                                      item["end_relative"] * 60.0))
    for index, item in enumerate(coarousal.get("episodes", [])):
        start, end = float(item["start_s"]), float(item["end_s"])
        if item["direction"] == "Opposite":
            # One of the pair on the High side, the other on the Low side.
            high, low = set(item.get("high_contributors", [])), set(item.get("low_contributors", []))
            if any(first in high and second in low for first, second in (pair, tuple(reversed(pair)))):
                found.opposite.append(episode(COAROUSAL, "opposite", index, start, end))
            continue
        if not members.issubset(item.get("contributors", [])):
            continue
        if item["direction"] == "Co-movement":
            found.comovement.append(episode(COAROUSAL, "co-movement", index, start, end))
        elif item["direction"] in ("High", "Low"):
            found.shared.append(episode(COAROUSAL, "state", index, start, end,
                                        HIGH if item["direction"] == "High" else LOW))
    return found


def _overlapping(episode: Episode, others: list[Episode]) -> Episode | None:
    """The episode of ``others`` that overlaps ``episode`` most (the earlier one on a tie), if any does."""
    best: Episode | None = None
    best_overlap = 0.0
    for other in others:
        overlap = other.overlap(episode.start_seconds, episode.end_seconds)
        if overlap > best_overlap:
            best, best_overlap = other, overlap
    return best


def candidates_by_step(windows: Windows, episodes: InterviewEpisodes,
                       config: PipelineConfig) -> dict[str, list[Candidate]]:
    """Every Analysis episode of the interview as the step it answers (``PROTOCOL``), strongest first within a step.

    A co-movement episode is synchronous (STEPS 1-2) when its r reaches the interview threshold (the document:
    "Synchronous = above 0.8"), a divergent one (STEP 4b) when its r reaches minus it; in between neither
    (the document's criteria leave that band neither synchronous nor asynchronous). A decoupling stretch is STEP 5
    as the Analysis found it (its mean r within the decoupling threshold), after opposite arousal (one of the pair
    High, the other Low), which has no correlation criterion, as shared High and Low have none.
    """
    steps: dict[str, list[Candidate]] = {step: [] for step in STEP_NUMBERS}
    threshold = config.synchrony_interview_threshold

    def add(kind: str, episode: Episode, correlation: float, shared: Episode | None = None) -> None:
        if kind in STEP_OF:
            steps[STEP_OF[kind]].append(Candidate(kind, episode.start_seconds, episode.end_seconds, correlation,
                                                  episode=episode, shared=shared))

    def r(episode: Episode) -> float:
        return windows.mean(episode.start_seconds, episode.end_seconds)

    synchronous: list[Episode] = []
    for episode in episodes.comovement:
        correlation = r(episode)
        if round(correlation, 2) >= threshold:
            synchronous.append(episode)
            state = _overlapping(episode, episodes.shared)
            add(COMOVEMENT_STATE if state else COMOVEMENT, episode, correlation, state)
    for episode in episodes.shared:
        if _overlapping(episode, synchronous) is None:
            add(SHARED_HIGH if episode.state == HIGH else SHARED_LOW, episode, r(episode))
    for episode in episodes.divergent:
        correlation = r(episode)
        if round(correlation, 2) <= -threshold:
            add(DIVERGENT, episode, correlation)
    for episode in episodes.opposite:
        add(OPPOSITE, episode, r(episode))
    for episode in episodes.decoupled:
        add(DECOUPLED, episode, r(episode))
    for candidates in steps.values():
        candidates.sort(key=Candidate.sort_key)
    return steps


def select(steps: dict[str, list[Candidate]], primary_count: int, reserve_count: int) -> list[Proposal]:
    """Walk the steps repeatedly (STEP 6) until the primary moments and the reserve are selected."""
    target = primary_count + reserve_count
    selected: list[Proposal] = []
    used: set[tuple[str, int]] = set()
    pass_number = 0
    while len(selected) < target:
        pass_number += 1
        selected_in_pass = 0
        for number in STEP_NUMBERS:
            if len(selected) >= target:
                break
            taken = [proposal.candidate for proposal in selected]
            choice = next(
                ((rank, candidate) for rank, candidate in enumerate(steps.get(number, []), start=1)
                 if (number, rank) not in used and not any(candidate.overlaps(other) for other in taken)),
                None,
            )
            if choice is None:
                continue
            rank, candidate = choice
            used.add((number, rank))
            selected_in_pass += 1
            selected.append(Proposal(
                number=len(selected) + 1,
                status=PRIMARY if len(selected) < primary_count else RESERVE,
                pass_number=pass_number,
                rank=rank,
                candidate_count=len(steps[number]),
                candidate=candidate,
            ))
        if selected_in_pass == 0:
            break
    return selected


def _candidate_payload(candidate: Candidate, rank: int, count: int, lesson_clock: datetime | None) -> dict[str, Any]:
    """A candidate moment as stored: its step, times, correlation and the Analysis episodes it was made from."""
    state_episode = candidate.shared if candidate.kind == COMOVEMENT_STATE else candidate.episode
    payload: dict[str, Any] = {
        "step": candidate.step,
        "type": candidate.kind,
        "start_seconds": round(candidate.start_seconds, 1),
        "end_seconds": round(candidate.end_seconds, 1),
        "duration_seconds": round(candidate.duration_seconds, 1),
        "correlation": candidate.correlation,
        "episode": candidate.episode.as_dict() if candidate.episode else None,
        "shared_episode": candidate.shared.as_dict() if candidate.shared else None,
        "shared_state": candidate.shared_state,
        # The interview's people in the shared state: 2 of 2 in a pair; for the teacher, the students with them.
        "in_state": state_episode.in_episode if state_episode and state_episode.state else None,
        "people": state_episode.people if state_episode and state_episode.state else None,
        "candidate_rank": rank,
        "candidate_count": count,
        "start_clock": clock_at(lesson_clock, candidate.start_seconds),
        "end_clock": clock_at(lesson_clock, candidate.end_seconds),
    }
    return payload


def _moment_payload(proposal: Proposal, lesson_clock: datetime | None) -> dict[str, Any]:
    return {
        "number": proposal.number,
        "status": proposal.status,
        "pass_number": proposal.pass_number,
        **_candidate_payload(proposal.candidate, proposal.rank, proposal.candidate_count, lesson_clock),
    }


def _trace(values: np.ndarray, stride: int) -> list[float]:
    return [round(float(value), 3) for value in values[::stride]]


def _interview_payload(key: str, kind: str, participants: list[str], frame: Any, relative_minutes: np.ndarray,
                       synchrony: dict[str, Any], coarousal: dict[str, Any], config: PipelineConfig,
                       primary_count: int, reserve_count: int,
                       lesson_clock: datetime | None, stride: int) -> dict[str, Any]:
    signal_a, signal_b = interview_signals(frame, kind, participants)
    windows = windowed_correlation(signal_a, signal_b, relative_minutes, config)
    lesson = (float(relative_minutes[0]) * 60.0, float(relative_minutes[-1]) * 60.0)
    if kind == "teacher":
        own_synchrony, own_coarousal = aggregate_analysis(signal_a, signal_b, relative_minutes, config)
        episodes = interview_episodes(AGGREGATE_PAIR, own_synchrony, own_coarousal, config, lesson, source=AGGREGATE)
    else:
        episodes = interview_episodes(participants, synchrony, coarousal, config, lesson)
    steps = candidates_by_step(windows, episodes, config)
    proposals = select(steps, primary_count, reserve_count)
    primary = sum(1 for proposal in proposals if proposal.status == PRIMARY)
    return {
        "key": key,
        "kind": kind,
        "participants": participants,
        "candidate_counts": {step: len(candidates) for step, candidates in steps.items()},
        # Every moment that answers a step, selected or not, for the lesson timeline (in step order, strongest first).
        "candidates": [
            _candidate_payload(candidate, rank, len(candidates), lesson_clock)
            for candidates in steps.values() for rank, candidate in enumerate(candidates, start=1)
        ],
        "moments": [_moment_payload(proposal, lesson_clock) for proposal in proposals],
        "stats": {
            "primary_count": primary,
            "reserve_count": len(proposals) - primary,
            "primary_target": primary_count,
            "reserve_target": reserve_count,
            "complete": primary >= primary_count,
        },
        # The two compared signals (z-scores) for the moment previews, on ``trace_seconds``.
        "trace": {"a": _trace(signal_a, stride), "b": _trace(signal_b, stride)},
    }


def compute_vsr(
    result: PipelineResult,
    analysis: dict[str, Any],
    config: PipelineConfig,
    dyads: Sequence[Sequence[str]],
    teacher_alias: str | None = None,
    student_aliases: Sequence[str] | None = None,
    primary_count: int = DEFAULT_PRIMARY_COUNT,
    reserve_count: int = DEFAULT_RESERVE_COUNT,
) -> dict[str, Any]:
    """Interview moments for every student pair and the teacher, from a stored Analysis run.

    ``result`` is the pipeline run behind ``analysis`` (the stored payload with its
    ``synchrony`` and ``coarousal``) and ``config`` the settings it ran with.
    """
    frame = result.merged_decimated
    relative_minutes = frame["time"].to_numpy(dtype=float)
    lesson_clock = clock_start(result.alignment)
    synchrony = analysis.get("synchrony") or {}
    coarousal = analysis.get("coarousal") or {}
    stride = max(1, int(np.ceil(len(relative_minutes) / TRACE_POINTS)))

    requested = [alias for dyad in dyads for alias in dyad]
    if teacher_alias is not None:
        requested += [teacher_alias, *(student_aliases or ())]
    _resolve_aliases(result.aliases, requested or None)

    def payload(key: str, kind: str, participants: list[str]) -> dict[str, Any]:
        return _interview_payload(key, kind, participants, frame, relative_minutes, synchrony, coarousal, config,
                                  primary_count, reserve_count, lesson_clock, stride)

    interviews: list[dict[str, Any]] = []
    for dyad in dyads:
        if len(dyad) != 2 or dyad[0] == dyad[1]:
            raise PipelineError(f"A dyad must name two different participants, got: {', '.join(dyad) or 'none'}")
        interviews.append(payload(f"{dyad[0]}+{dyad[1]}", "dyad", [dyad[0], dyad[1]]))
    if teacher_alias is not None:
        students = [alias for alias in (student_aliases or ()) if alias != teacher_alias]
        if not students:
            raise PipelineError("The teacher interview needs at least one student recording to aggregate")
        interviews.append(payload(teacher_alias, "teacher", [teacher_alias, *students]))

    settings = config.model_dump()
    return {
        "analysis_settings": {name: settings[name] for name in ANALYSIS_SETTINGS},
        "counts": {"primary": primary_count, "reserve": reserve_count},
        "trace_seconds": [round(float(value) * 60.0, 2) for value in relative_minutes[::stride]],
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
