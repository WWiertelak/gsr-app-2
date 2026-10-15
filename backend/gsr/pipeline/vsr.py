"""VSR interview moments chosen from the Analysis results (the "VSR Interview Flow Chart", GSR section).

The Interviews step does not analyse the signals again with settings of its own: it
takes the episodes the Analysis step found, with the Analysis settings they were found
with, and sorts them into the document's STEPS 1-5 for every interview:

  1. co-movement and a shared arousal state - a synchronization episode (Synchronization
     tab) or a co-movement episode (Co-arousal tab) of the interview's people, with a
     positive r, that overlaps a shared High or Low episode (Co-arousal tab) of the same people;
  2. co-movement only - such a co-movement episode that overlaps no shared state;
  3. shared High arousal - a shared High episode that overlaps no co-movement;
  4. shared Low arousal - a shared Low episode that overlaps no co-movement;
  5. no synchrony - a co-movement episode whose r is below the asynchrony threshold (the
     document: "Asynchronous = below 0.1"; below zero the signals moved in the opposite
     direction, from zero up they were not related), or a stretch outside all of those
     episodes where the interview's windowed correlation stays below that threshold, at
     least as long as the Analysis' minimum episode duration (gaps up to its maximum
     episode gap are bridged).

An episode belongs to a student pair when both students are in it (members of a
synchronization subgroup, contributors of a co-arousal episode), and to the teacher
interview when the teacher and at least one student are. The correlation r of a moment
is the interview's own, from the Synchronization tab's windows (window, step and method
from the Analysis settings): the pair's two signals, or the teacher against the students'
average signal (page 1 of the document: "Student data will be aggregated to create
average GSR values").

STEP 6 then walks STEPS 1-5 again until the primary moments are selected, and once more
for the reserve. Of several moments answering one step the strongest comes first: the
highest r in STEPS 1-2, the most people in the shared state in STEPS 3-4, the lowest r
in STEP 5; then the longer one, then the earlier one. Selected moments may not overlap.

The ESM half of the document is out of scope: the application analyses GSR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

import numpy as np

from .common import PipelineError, _estimate_step_seconds, _resolve_aliases, _zscore
from .config import PipelineConfig
from .preprocessing import PipelineResult
from .synchrony import _window_correlation

STEP_NUMBERS = (1, 2, 3, 4, 5)

PRIMARY = "primary"
RESERVE = "reserve"

HIGH = "HIGH"
LOW = "LOW"

SYNCHRONY = "synchrony"
COAROUSAL = "coarousal"

# The document: "A total of 5 moments discussed per interview (ideally)" and "Aim to identify more (e.g. 8)".
DEFAULT_PRIMARY_COUNT = 5
DEFAULT_RESERVE_COUNT = 3
# Page 1 of the document: "Asynchronous = below 0.1"; STEP 5 takes moments whose r is below it.
DEFAULT_ASYNCHRONY_THRESHOLD = 0.1

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
    "contagion_high_z",
    "contagion_low_z",
    "contagion_min_participants",
    "contagion_comovement_rho",
)


@dataclass(frozen=True)
class Episode:
    """One Analysis episode as an interview sees it."""

    source: str  # SYNCHRONY or COAROUSAL
    number: int  # its number on the Analysis screen: E<number> on Synchronization, <number> on Co-arousal
    start_seconds: float
    end_seconds: float
    state: str | None  # HIGH or LOW for a shared-state episode, None for co-movement
    # The interview's people in the episode: 2 of 2 for a pair, the students with the teacher for the teacher.
    in_episode: int
    people: int

    def overlap(self, start: float, end: float) -> float:
        return max(0.0, min(self.end_seconds, end) - max(self.start_seconds, start))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "number": self.number,
            "start_seconds": round(self.start_seconds, 1),
            "end_seconds": round(self.end_seconds, 1),
            "state": self.state,
            "in_episode": self.in_episode,
            "people": self.people,
        }


@dataclass(frozen=True)
class Candidate:
    """A moment that answers one step, with what it was made from."""

    step: int
    start_seconds: float
    end_seconds: float
    correlation: float
    # The co-movement episode (STEPS 1-2 and an opposite-direction one in STEP 5) or the shared-state
    # episode (STEPS 3-4); None for a STEP 5 stretch outside the episodes.
    episode: Episode | None = None
    # STEP 1: the shared-state episode the co-movement episode overlaps most.
    shared: Episode | None = None

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    @property
    def shared_state(self) -> str | None:
        state_episode = self.shared if self.step == 1 else self.episode
        return state_episode.state if state_episode is not None else None

    def sort_key(self) -> tuple[float, float, float]:
        """Strongest first, then the longer, then the earlier; r counts to the two decimals it is shown with."""
        if self.step in (1, 2):
            strength = -round(self.correlation, 2)
        elif self.step in (3, 4):
            strength = -float(self.episode.in_episode) if self.episode else 0.0
        else:
            strength = round(self.correlation, 2)
        return strength, -self.duration_seconds, self.start_seconds

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


def interview_episodes(kind: str, participants: Sequence[str], synchrony: dict[str, Any],
                       coarousal: dict[str, Any], config: PipelineConfig,
                       lesson: tuple[float, float]) -> tuple[list[Episode], list[Episode]]:
    """The interview's co-movement episodes and shared-state episodes among the Analysis episodes.

    ``lesson`` is the analysed period in lesson seconds.
    """
    half_window = config.synchrony_window_seconds / 2.0
    if kind == "teacher":
        teacher, students = participants[0], set(participants[1:])

        def people_in(group: Sequence[str]) -> int | None:
            count = len(students.intersection(group))
            return count if teacher in group and count else None

        people = len(students)
    else:
        pair = set(participants)

        def people_in(group: Sequence[str]) -> int | None:
            return 2 if pair.issubset(group) else None

        people = 2

    comovement: list[Episode] = []
    shared: list[Episode] = []
    for index, episode in enumerate(synchrony.get("episodes", [])):
        count = people_in(episode["members"])
        if count is not None:
            start, end = _span(episode["start_relative"] * 60.0, episode["end_relative"] * 60.0, half_window, lesson)
            comovement.append(Episode(SYNCHRONY, index + 1, start, end, None, count, people))
    for index, episode in enumerate(coarousal.get("episodes", [])):
        count = people_in(episode.get("contributors", []))
        if count is None:
            continue
        start, end = _span(float(episode["start_s"]), float(episode["end_s"]), half_window, lesson)
        direction = episode["direction"]
        if direction == "Co-movement":
            comovement.append(Episode(COAROUSAL, index + 1, start, end, None, count, people))
        else:
            state = HIGH if direction == "High" else LOW
            shared.append(Episode(COAROUSAL, index + 1, start, end, state, count, people))
    return comovement, shared


def _overlapping(episode: Episode, others: list[Episode]) -> Episode | None:
    """The episode of ``others`` that overlaps ``episode`` most (the earlier one on a tie), if any does."""
    best: Episode | None = None
    best_overlap = 0.0
    for other in others:
        overlap = other.overlap(episode.start_seconds, episode.end_seconds)
        if overlap > best_overlap:
            best, best_overlap = other, overlap
    return best


def _no_synchrony(windows: Windows, episodes: list[Episode], config: PipelineConfig,
                  lesson: tuple[float, float], asynchrony_threshold: float) -> list[Candidate]:
    """STEP 5: runs of windows outside the interview's episodes whose r stays below ``asynchrony_threshold``.

    The runs follow the Analysis' episode rules: gaps up to the maximum episode gap are
    bridged (never across an episode), and a run shorter than the minimum episode
    duration is dropped.
    """
    centers, values = windows.centers, windows.values
    if not len(centers):
        return []
    step = float(np.median(np.diff(centers))) if len(centers) > 1 else 0.0
    allowed_gap = step + config.synchrony_episode_max_gap_seconds + 1e-9
    min_duration = config.synchrony_episode_min_duration_seconds
    half_window = config.synchrony_window_seconds / 2.0

    def inside_episode(center: float) -> bool:
        return any(episode.start_seconds - 1e-9 <= center <= episode.end_seconds + 1e-9 for episode in episodes)

    runs: list[list[float]] = []
    current: list[float] = []
    for center, value in zip(centers, values):
        if inside_episode(center):
            if current:
                runs.append(current)
            current = []
            continue
        if value >= asynchrony_threshold:
            continue
        if current and center - current[-1] > allowed_gap:
            runs.append(current)
            current = []
        current.append(float(center))
    if current:
        runs.append(current)

    candidates = []
    for run in runs:
        if run[-1] - run[0] + 1e-9 < min_duration:
            continue
        start, end = _span(run[0], run[-1], half_window, lesson)
        correlation = windows.mean(run[0], run[-1])
        if correlation < asynchrony_threshold:
            candidates.append(Candidate(5, start, end, correlation))
    return candidates


def candidates_by_step(windows: Windows, comovement: list[Episode], shared: list[Episode],
                       config: PipelineConfig, lesson: tuple[float, float],
                       asynchrony_threshold: float = DEFAULT_ASYNCHRONY_THRESHOLD) -> dict[int, list[Candidate]]:
    """Every Analysis episode of the interview as the step it answers, strongest first within a step.

    A co-movement episode answers STEPS 1-2 only with a positive r at or above ``asynchrony_threshold``;
    otherwise it answers STEP 5.
    """
    steps: dict[int, list[Candidate]] = {number: [] for number in STEP_NUMBERS}
    same_direction: list[Episode] = []
    opposite: list[Candidate] = []
    for episode in comovement:
        correlation = windows.mean(episode.start_seconds, episode.end_seconds)
        if correlation <= 0.0 or correlation < asynchrony_threshold:
            opposite.append(Candidate(5, episode.start_seconds, episode.end_seconds, correlation, episode=episode))
            continue
        same_direction.append(episode)
        state = _overlapping(episode, shared)
        steps[1 if state else 2].append(
            Candidate(1 if state else 2, episode.start_seconds, episode.end_seconds, correlation,
                      episode=episode, shared=state)
        )
    for episode in shared:
        if _overlapping(episode, same_direction) is None:
            steps[3 if episode.state == HIGH else 4].append(
                Candidate(3 if episode.state == HIGH else 4, episode.start_seconds, episode.end_seconds,
                          windows.mean(episode.start_seconds, episode.end_seconds), episode=episode)
            )
    steps[5] = opposite + _no_synchrony(windows, comovement + shared, config, lesson, asynchrony_threshold)
    for candidates in steps.values():
        candidates.sort(key=Candidate.sort_key)
    return steps


def select(steps: dict[int, list[Candidate]], primary_count: int, reserve_count: int) -> list[Proposal]:
    """Walk STEPS 1-5 repeatedly (STEP 6) until the primary moments and the reserve are selected."""
    target = primary_count + reserve_count
    selected: list[Proposal] = []
    used: set[tuple[int, int]] = set()
    pass_number = 0
    while len(selected) < target:
        pass_number += 1
        selected_in_pass = 0
        for number in STEP_NUMBERS:
            if len(selected) >= target:
                break
            taken = [proposal.candidate for proposal in selected]
            choice = next(
                ((rank, candidate) for rank, candidate in enumerate(steps[number], start=1)
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


def _clock_start(result: PipelineResult) -> datetime | None:
    try:
        return datetime.fromisoformat(str(result.alignment["start"]))
    except (KeyError, TypeError, ValueError):
        return None


def _candidate_payload(candidate: Candidate, rank: int, count: int, clock_start: datetime | None) -> dict[str, Any]:
    """A candidate moment as stored: its step, times, correlation and the Analysis episodes it was made from."""
    state_episode = candidate.shared if candidate.step == 1 else candidate.episode
    payload: dict[str, Any] = {
        "step": candidate.step,
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
        "start_clock": None,
        "end_clock": None,
    }
    if clock_start is not None:
        payload["start_clock"] = (clock_start + timedelta(seconds=candidate.start_seconds)).strftime("%H:%M:%S")
        payload["end_clock"] = (clock_start + timedelta(seconds=candidate.end_seconds)).strftime("%H:%M:%S")
    return payload


def _moment_payload(proposal: Proposal, clock_start: datetime | None) -> dict[str, Any]:
    return {
        "number": proposal.number,
        "status": proposal.status,
        "pass_number": proposal.pass_number,
        **_candidate_payload(proposal.candidate, proposal.rank, proposal.candidate_count, clock_start),
    }


def _trace(values: np.ndarray, stride: int) -> list[float]:
    return [round(float(value), 3) for value in values[::stride]]


def _interview_payload(key: str, kind: str, participants: list[str], frame: Any, relative_minutes: np.ndarray,
                       synchrony: dict[str, Any], coarousal: dict[str, Any], config: PipelineConfig,
                       primary_count: int, reserve_count: int, asynchrony_threshold: float,
                       clock_start: datetime | None, stride: int) -> dict[str, Any]:
    signal_a, signal_b = interview_signals(frame, kind, participants)
    windows = windowed_correlation(signal_a, signal_b, relative_minutes, config)
    lesson = (float(relative_minutes[0]) * 60.0, float(relative_minutes[-1]) * 60.0)
    comovement, shared = interview_episodes(kind, participants, synchrony, coarousal, config, lesson)
    steps = candidates_by_step(windows, comovement, shared, config, lesson, asynchrony_threshold)
    proposals = select(steps, primary_count, reserve_count)
    primary = sum(1 for proposal in proposals if proposal.status == PRIMARY)
    return {
        "key": key,
        "kind": kind,
        "participants": participants,
        "candidate_counts": {str(number): len(candidates) for number, candidates in steps.items()},
        # Every moment that answers a step, selected or not, for the lesson timeline (STEPS 1-5, strongest first).
        "candidates": [
            _candidate_payload(candidate, rank, len(candidates), clock_start)
            for candidates in steps.values() for rank, candidate in enumerate(candidates, start=1)
        ],
        "moments": [_moment_payload(proposal, clock_start) for proposal in proposals],
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
    asynchrony_threshold: float = DEFAULT_ASYNCHRONY_THRESHOLD,
) -> dict[str, Any]:
    """Interview moments for every student pair and the teacher, from a stored Analysis run.

    ``result`` is the pipeline run behind ``analysis`` (the stored payload with its
    ``synchrony`` and ``coarousal``) and ``config`` the settings it ran with.
    """
    frame = result.merged_decimated
    relative_minutes = frame["time"].to_numpy(dtype=float)
    clock_start = _clock_start(result)
    synchrony = analysis.get("synchrony") or {}
    coarousal = analysis.get("coarousal") or {}
    stride = max(1, int(np.ceil(len(relative_minutes) / TRACE_POINTS)))

    requested = [alias for dyad in dyads for alias in dyad]
    if teacher_alias is not None:
        requested += [teacher_alias, *(student_aliases or ())]
    _resolve_aliases(result.aliases, requested or None)

    def payload(key: str, kind: str, participants: list[str]) -> dict[str, Any]:
        return _interview_payload(key, kind, participants, frame, relative_minutes, synchrony, coarousal, config,
                                  primary_count, reserve_count, asynchrony_threshold, clock_start, stride)

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
        "asynchrony_threshold": asynchrony_threshold,
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
