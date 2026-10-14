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
from ..pipeline.vsr import _clock_start, compute_vsr
from .analysis import AnalysisError
from .interview import InterviewRoles, compute_vsr_signature, load_interview_config
from .results import get_result, stored_pipeline_result

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
            "start_clock": clock_start.strftime("%H:%M:%S") if (clock_start := _clock_start(result)) else None,
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
