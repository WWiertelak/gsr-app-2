"""VSR interview moment proposals behind the Interviews screen: compute, store, check for staleness.

The proposals are computed from the signals of the stored analysis run (the same
cleaned, scaled ``merged_decimated`` the Synchronization and Co-arousal tabs use),
with the interview roles and VSR parameters stored on the project, and kept as the
project's ``AnalysisResult`` of kind ``vsr``.
"""

from __future__ import annotations

import time
from typing import Any

from django.utils import timezone
from django.utils.translation import gettext

from ..models import AnalysisResult, Project
from ..pipeline.common import PipelineError
from ..pipeline.preprocessing import _signal_quality_level
from ..pipeline.vsr import PRIMARY, _clock_start, compute_vsr, interview_signals
from ..pipeline.vsr_config import VsrConfig
from .analysis import AnalysisError
from .interview import InterviewRoles, compute_vsr_signature, load_interview_config
from .results import FALLBACK_SIGNAL_COLOR, get_result, is_stale, signal_colors, stored_pipeline_result

SECONDS_PER_DAY = 24 * 3600


def interview_students(roles: InterviewRoles, aliases: list[str]) -> list[str]:
    """The students whose average GSR the teacher interview uses.

    Page 1: "Focused on synchronicity of the teacher and all GSR students". The
    students are the recordings in the student pairs; without pairs, every other
    recording of the analysis run.
    """
    paired = [alias for pair in roles.student_pairs for alias in pair]
    return paired or [alias for alias in aliases if alias != roles.teacher_alias]


def _clock_seconds(text: str | None) -> int | None:
    if not text:
        return None
    hours, minutes, seconds = (int(part) for part in text.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def video_time(clock: str | None, video_start: str | None) -> str | None:
    """A sensor clock time as a position in the video (H:MM:SS), or None before the video starts or without its start.

    A moment up to 12 hours before the video start is before the video; an earlier
    one is read as after midnight.
    """
    moment, start = _clock_seconds(clock), _clock_seconds(video_start)
    if moment is None or start is None:
        return None
    offset = moment - start
    if offset < -SECONDS_PER_DAY // 2:
        offset += SECONDS_PER_DAY
    if offset < 0:
        return None
    return f"{offset // 3600}:{offset // 60 % 60:02d}:{offset % 60:02d}"


def _signal_quality(result: Any, aliases: list[str]) -> dict[str, dict[str, Any]]:
    quality = {}
    for alias in aliases:
        summary = result.cleaning_summary_by_alias[alias]
        rejected_pct, level = _signal_quality_level(summary.artifact_rejected_points, summary.synced_points)
        quality[alias] = {"level": level, "rejected_pct": rejected_pct}
    return quality


def compute_interviews(project: Project) -> AnalysisResult:
    """Propose the moments for every student pair and the teacher, and store them.

    The researcher's decisions on the interview lists are kept (``curation.sync_interview_moments``).
    """
    started = time.perf_counter()
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    if analysis is None:
        raise AnalysisError(gettext("Run the analysis before proposing interview moments."), code="needs_analysis")
    try:
        config = load_interview_config(project)
    except ValueError as exc:
        raise AnalysisError(str(exc)) from exc
    roles = config.roles
    if not roles.student_pairs and roles.teacher_alias is None:
        raise AnalysisError(gettext("Choose the teacher or at least one student pair."), code="no_interviews")

    result, _pipeline_config = stored_pipeline_result(project, analysis)
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
        payload = compute_vsr(result, roles.student_pairs, roles.teacher_alias, students, config.vsr)
    except PipelineError as exc:
        raise AnalysisError.from_pipeline(exc) from exc

    for interview in payload["interviews"]:
        for moment in interview["moments"]:
            moment["start_video"] = video_time(moment["start_clock"], roles.video_start_time)
            moment["end_video"] = video_time(moment["end_clock"], roles.video_start_time)
    times = result.merged_decimated["time"]
    payload.update(
        roles={
            "teacher_alias": roles.teacher_alias,
            "student_pairs": [list(pair) for pair in roles.student_pairs],
            "students": students,
            "video_start_time": roles.video_start_time,
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


PROPOSAL_FIELDS = (
    "step", "synchrony_type", "pass_number", "correlation", "shared_state", "shared_state_fraction",
    "asynchrony_subtype", "candidate_rank", "candidate_count", "justification", "justification_text",
)


def _report_moment(number: int, role: str, decision: str, source: dict[str, Any]) -> dict[str, Any]:
    """One moment as the report and the CSV read it; ``source`` holds the fields the moment's origin knows."""
    base = {field: None for field in PROPOSAL_FIELDS}
    base.update(
        number=number, status=role, decision=decision, source="proposed", time_changed=False, reason="", note="",
        needs_check=False, original_start_seconds=None, original_end_seconds=None,
    )
    return {**base, **source}


def _proposal_moments(interview: dict[str, Any]) -> list[dict[str, Any]]:
    """The engine's proposals in their order, none of them decided."""
    return [
        _report_moment(moment["number"], moment["status"], "undecided", moment)
        for moment in interview["moments"]
    ]


def _curated_moments(items: list[Any]) -> list[dict[str, Any]]:
    """The researcher's list without its rejected moments: roles and numbers from the list order, the researcher's
    times, reasons and notes, and each proposed moment's measurements and justification."""
    moments = []
    for item in items:
        row = item.row
        if item.number is None:  # rejected
            continue
        decision = "manual" if row.is_manual else ("accepted" if row.status == row.STATUS_ACCEPTED else "undecided")
        source = {
            **({} if row.is_manual else {field: row.proposal.get(field) for field in PROPOSAL_FIELDS}),
            "source": row.source,
            "start_seconds": row.start_seconds,
            "end_seconds": row.end_seconds,
            "duration_seconds": row.end_seconds - row.start_seconds,
            "start_clock": item.start_clock,
            "end_clock": item.end_clock,
            "start_video": item.start_video,
            "end_video": item.end_video,
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


def _interview_traces(project: Project, vsr: AnalysisResult, analysis: AnalysisResult) -> dict[str, dict[str, Any]]:
    """Per interview key the two z-scored signals the proposals compare, for the report's mini-charts.

    Empty when the proposals were computed from another analysis run than the stored one (the signals would not
    match the moments) or when the run's recordings are gone.
    """
    if vsr.payload.get("analysis_computed_at") != analysis.computed_at.isoformat():
        return {}
    try:
        result, _config = stored_pipeline_result(project, analysis)
    except AnalysisError:
        return {}
    frame = result.merged_decimated
    seconds = frame["time"].to_numpy(dtype=float) * 60.0
    traces = {}
    for interview in vsr.payload.get("interviews", []):
        a, b = interview_signals(frame, interview["kind"], interview["participants"])
        traces[interview["key"]] = {"seconds": seconds, "a": a, "b": b}
    return traces


def report_data(project: Project, with_traces: bool = True) -> dict[str, Any] | None:
    """What the PDF report and the exports need of the stored VSR proposals, or None when none were proposed.

    ``with_traces`` adds the signals for the report's mini-charts, which re-runs the analysis run's pipeline;
    the CSV does not need them.
    """
    vsr = get_result(project, AnalysisResult.KIND_VSR)
    analysis = get_result(project, AnalysisResult.KIND_ANALYSIS)
    if vsr is None or analysis is None or not vsr.payload.get("interviews"):
        return None
    payload = vsr.payload
    colors = signal_colors(project)
    traces = _interview_traces(project, vsr, analysis) if with_traces else {}
    from .curation import curated_interviews, is_decided  # curation imports this module

    curated = curated_interviews(project, vsr)
    config = VsrConfig.from_dict(payload["config"])
    interviews = []
    for interview in payload["interviews"]:
        participants = interview["participants"]
        items = curated.get(interview["key"])
        moments = moments_for_report(interview, items)
        interviews.append({
            **interview,
            "moments": moments,
            "curated": any(is_decided(item.row) for item in items or []),
            "rejected_count": sum(1 for item in items or [] if item.number is None),
            "stats": {**interview["stats"], **_stats(moments, config.vsr_primary_moment_count,
                                                     config.vsr_reserve_moment_count)},
            "trace": traces.get(interview["key"]),
            "colors": [colors.get(participants[0], FALLBACK_SIGNAL_COLOR),
                       colors.get(participants[1], FALLBACK_SIGNAL_COLOR) if interview["kind"] == "dyad" else None],
        })
    return {
        "config": payload["config"],
        "criteria": payload["criteria"],
        "roles": payload["roles"],
        "lesson": payload["lesson"],
        "quality": payload["quality"],
        "stats": {
            **payload["stats"],
            "primary_total": sum(item["stats"]["primary_count"] for item in interviews),
            "reserve_total": sum(item["stats"]["reserve_count"] for item in interviews),
        },
        "curated": any(item["curated"] for item in interviews),
        "stale": is_stale(project, vsr),
        "interviews": interviews,
    }
