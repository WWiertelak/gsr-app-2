"""VSR interview moments behind the Interviews screen: choose them from the Analysis, store, check for staleness.

The moments are the stored analysis run's episodes (``pipeline/vsr.py``), with the
Analysis settings they were found with and the interview roles and moment counts
stored on the project, kept as the project's ``AnalysisResult`` of kind ``vsr``. An
outdated or missing analysis is not worked around: the Analysis has to run first.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from django.utils import timezone
from django.utils.translation import gettext

from ..models import AnalysisResult, Project
from ..pipeline.common import PipelineError
from ..pipeline.preprocessing import _signal_quality_level
from ..pipeline.vsr import DEFAULT_PRIMARY_COUNT, DEFAULT_RESERVE_COUNT, PRIMARY, _clock_start, compute_vsr
from .analysis import AnalysisError
from .interview import SAVED_ASYNCHRONY_THRESHOLD, InterviewRoles, compute_vsr_signature, load_interview_config
from .results import FALLBACK_SIGNAL_COLOR, get_result, is_stale, signal_colors, stored_pipeline_result

# The signals a moment preview shows on each side of the moment: at least this, or half the moment if it is longer.
PREVIEW_CONTEXT_SECONDS = 30.0


def interview_students(roles: InterviewRoles, aliases: list[str]) -> list[str]:
    """The students whose average GSR the teacher interview uses.

    The students are the recordings in the student pairs; without pairs, every other
    recording of the analysis run.
    """
    paired = [alias for pair in roles.student_pairs for alias in pair]
    return paired or [alias for alias in aliases if alias != roles.teacher_alias]


def _signal_quality(result: Any, aliases: list[str]) -> dict[str, dict[str, Any]]:
    quality = {}
    for alias in aliases:
        summary = result.cleaning_summary_by_alias[alias]
        rejected_pct, level = _signal_quality_level(summary.artifact_rejected_points, summary.synced_points)
        quality[alias] = {"level": level, "rejected_pct": rejected_pct}
    return quality


def analysis_ready(project: Project) -> AnalysisResult | None:
    """The stored analysis run when the moments can come from it: run, and not outdated."""
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    return analysis if analysis is not None and not is_stale(project, analysis) else None


def compute_interviews(project: Project) -> AnalysisResult:
    """Choose the moments of every student pair and the teacher from the Analysis, and store them.

    The researcher's decisions on the interview lists are kept (``curation.sync_interview_moments``).
    """
    started = time.perf_counter()
    analysis = analysis_ready(project)
    if analysis is None:
        raise AnalysisError(gettext("Run the Analysis first: the interview moments come from its results."),
                            code="needs_analysis")
    try:
        config = load_interview_config(project)
    except ValueError as exc:
        raise AnalysisError(str(exc)) from exc
    roles = config.roles
    if not roles.student_pairs and roles.teacher_alias is None:
        raise AnalysisError(gettext("Choose the teacher or at least one student pair."), code="no_interviews")

    result, pipeline_config = stored_pipeline_result(project, analysis)
    missing = [
        alias
        for alias in (roles.teacher_alias, *(a for pair in roles.student_pairs for a in pair))
        if alias is not None and alias not in result.aliases
    ]
    if missing:
        raise AnalysisError(
            gettext(
                "%(aliases)s was not in the last analysis run. Include it on the Cleaning step and run the "
                "analysis again."
            )
            % {"aliases": ", ".join(missing)},
            code="role_not_analysed",
        )
    students = interview_students(roles, result.aliases) if roles.teacher_alias is not None else []
    try:
        payload = compute_vsr(result, analysis.payload, pipeline_config, roles.student_pairs, roles.teacher_alias,
                              students, config.primary_count, config.reserve_count, config.asynchrony_threshold)
    except PipelineError as exc:
        raise AnalysisError.from_pipeline(exc) from exc

    times = result.merged_decimated["time"]
    payload.update(
        roles={
            "teacher_alias": roles.teacher_alias,
            "student_pairs": [list(pair) for pair in roles.student_pairs],
            "students": students,
        },
        # The analysed period on the moments' time axis (relative seconds), for the lesson timeline, and the
        # sensor clock at lesson time 0, for the clock times of moments the researcher adds or moves.
        lesson={
            "start_seconds": float(times.min()) * 60.0,
            "end_seconds": float(times.max()) * 60.0,
            "start_clock": clock_start.isoformat() if (clock_start := _clock_start(result)) else None,
        },
        quality=_signal_quality(result, result.aliases),
        analysis_computed_at=analysis.computed_at.isoformat(),
    )
    stored, _ = AnalysisResult.objects.update_or_create(
        project=project,
        kind=AnalysisResult.KIND_VSR,
        defaults={
            "signature": compute_vsr_signature(project),
            "config": config.to_storage(),
            "dataset_ids": list(analysis.dataset_ids),
            "payload": payload,
            "duration_seconds": round(time.perf_counter() - started, 2),
            "computed_at": timezone.now(),
        },
    )
    from .curation import sync_interview_moments  # curation imports this module

    sync_interview_moments(project, stored)
    return stored


def moment_counts(payload: dict[str, Any]) -> tuple[int, int]:
    """The (primary, reserve) numbers of moments the stored moments were chosen for."""
    stored = payload.get("counts") or {}
    return int(stored.get("primary", DEFAULT_PRIMARY_COUNT)), int(stored.get("reserve", DEFAULT_RESERVE_COUNT))


def preview(payload: dict[str, Any], interview: dict[str, Any], start: float, end: float) -> dict[str, Any] | None:
    """The interview's two signals around a moment (z-scores on lesson seconds), or None without them.

    The window reaches ``PREVIEW_CONTEXT_SECONDS`` (or half the moment, if longer) before
    and after the moment, within the analysed period.
    """
    seconds = np.asarray(payload.get("trace_seconds") or [], dtype=float)
    trace = interview.get("trace") or {}
    if len(seconds) < 2 or len(trace.get("a") or []) != len(seconds) or len(trace.get("b") or []) != len(seconds):
        return None
    context = max(PREVIEW_CONTEXT_SECONDS, (end - start) / 2.0)
    low, high = max(float(seconds[0]), start - context), min(float(seconds[-1]), end + context)
    window = np.flatnonzero((seconds >= low) & (seconds <= high))
    if len(window) < 2:
        return None
    first, last = int(window[0]), int(window[-1]) + 1
    return {
        "seconds": seconds[first:last].tolist(),
        "a": trace["a"][first:last],
        "b": trace["b"][first:last],
        "start": float(start),
        "end": float(end),
        "low": low,
        "high": high,
    }


PROPOSAL_FIELDS = (
    "step", "pass_number", "correlation", "episode", "shared_episode", "shared_state", "in_state", "people",
    "candidate_rank", "candidate_count",
)


def _report_moment(number: int, role: str, decision: str, source: dict[str, Any]) -> dict[str, Any]:
    """One moment as the report and the CSV read it; ``source`` holds the fields the moment's origin knows."""
    base = {field: None for field in PROPOSAL_FIELDS}
    base.update(
        number=number, status=role, decision=decision, source="proposed", time_changed=False, reason="", note="",
        needs_check=False, original_start_seconds=None, original_end_seconds=None, proposal={},
    )
    return {**base, **source}


def _proposal_moments(interview: dict[str, Any]) -> list[dict[str, Any]]:
    """The engine's proposals in their order, none of them decided."""
    return [
        _report_moment(moment["number"], moment["status"], "undecided", {**moment, "proposal": moment})
        for moment in interview["moments"]
    ]


def _curated_moments(items: list[Any]) -> list[dict[str, Any]]:
    """The researcher's list without its rejected moments: roles and numbers from the list order, the researcher's
    times, reasons and notes, and each proposed moment's measurements."""
    moments = []
    for item in items:
        row = item.row
        if item.number is None:  # rejected
            continue
        decision = "manual" if row.is_manual else ("accepted" if row.status == row.STATUS_ACCEPTED else "undecided")
        source = {
            **({} if row.is_manual else {field: row.proposal.get(field) for field in PROPOSAL_FIELDS}),
            "proposal": {} if row.is_manual else row.proposal,
            "source": row.source,
            "start_seconds": row.start_seconds,
            "end_seconds": row.end_seconds,
            "duration_seconds": row.end_seconds - row.start_seconds,
            "start_clock": item.start_clock,
            "end_clock": item.end_clock,
            "time_changed": row.time_changed,
            "original_start_seconds": row.original_start_seconds,
            "original_end_seconds": row.original_end_seconds,
            "reason": row.reason,
            "note": row.note,
            "needs_check": row.needs_check,
        }
        moments.append(_report_moment(item.number, item.role, decision, source))
    return moments


def moments_for_report(interview: dict[str, Any], curated: list[Any] | None) -> list[dict[str, Any]]:
    """The moments the report and the CSV list for one interview.

    The researcher's list from the Interviews step (accepted, undecided, retimed and manually added moments, with
    their notes and reasons; rejected ones left out) once the interview has one, otherwise the engine's proposals.
    """
    return _curated_moments(curated) if curated else _proposal_moments(interview)


def _stats(moments: list[dict[str, Any]], target: int, reserve_target: int) -> dict[str, Any]:
    primary = sum(1 for moment in moments if moment["status"] == PRIMARY)
    return {
        "primary_count": primary,
        "reserve_count": len(moments) - primary,
        "primary_target": target,
        "reserve_target": reserve_target,
        "complete": primary >= target,
    }


def interview_colors(colors: dict[str, str], interview: dict[str, Any]) -> list[str | None]:
    """The line colours of an interview's two signals; the students' average has no recording colour (None)."""
    participants = interview["participants"]
    return [colors.get(participants[0], FALLBACK_SIGNAL_COLOR),
            colors.get(participants[1], FALLBACK_SIGNAL_COLOR) if interview["kind"] == "dyad" else None]


def report_data(project: Project) -> dict[str, Any] | None:
    """What the PDF report and the exports need of the stored interview moments, or None when none were chosen."""
    vsr = get_result(project, AnalysisResult.KIND_VSR)
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    if vsr is None or analysis is None or not vsr.payload.get("interviews"):
        return None
    payload = vsr.payload
    colors = signal_colors(project)
    from .curation import curated_interviews, is_decided  # curation imports this module

    curated = curated_interviews(project, vsr)
    primary_target, reserve_target = moment_counts(payload)
    interviews = []
    for interview in payload["interviews"]:
        items = curated.get(interview["key"])
        moments = moments_for_report(interview, items)
        for moment in moments:
            moment["preview"] = preview(payload, interview, moment["start_seconds"], moment["end_seconds"])
        interviews.append({
            **interview,
            "moments": moments,
            "curated": any(is_decided(item.row) for item in items or []),
            "rejected_count": sum(1 for item in items or [] if item.number is None),
            "stats": {**interview["stats"], **_stats(moments, primary_target, reserve_target)},
            "colors": interview_colors(colors, interview),
        })
    return {
        "analysis_settings": payload.get("analysis_settings") or {},
        "analysis_computed_at": payload.get("analysis_computed_at"),
        "counts": {"primary": primary_target, "reserve": reserve_target},
        "asynchrony_threshold": payload.get("asynchrony_threshold", SAVED_ASYNCHRONY_THRESHOLD),
        "roles": payload["roles"],
        "lesson": payload["lesson"],
        "quality": payload["quality"],
        "stats": {
            **payload["stats"],
            "primary_total": sum(item["stats"]["primary_count"] for item in interviews),
            "reserve_total": sum(item["stats"]["reserve_count"] for item in interviews),
        },
        "curated": any(item["curated"] for item in interviews),
        # The moments come from the analysis: an outdated analysis makes them outdated too.
        "stale": is_stale(project, vsr) or is_stale(project, analysis),
        "interviews": interviews,
    }
