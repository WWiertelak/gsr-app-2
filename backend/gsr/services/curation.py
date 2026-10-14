"""The researcher's curation of the VSR proposals: accept, reject, change the time, add, reorder, annotate.

Every moment on an interview's list is an ``InterviewMoment`` row. Proposing the
moments again (``sync_interview_moments``) keeps every decision: a proposal the
engine returns again keeps its row, with the decision, the changed time, the note
and the place on the list; a decided moment the engine no longer proposes stays
and is marked for checking; only proposals nobody touched are replaced.

The list order decides the roles: the first moments that are not rejected are
the primary ones (5 by default, as the VSR Interview Flow Chart asks) and the
rest the reserve, so rejecting a primary moment moves the first reserve moment up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from django.db import transaction
from django.utils.translation import gettext

from ..models import AnalysisResult, InterviewMoment, Project
from ..pipeline.vsr import PRIMARY, RESERVE
from .vsr import moment_counts, video_time

MAX_REASON_LENGTH = 1000
MAX_NOTE_LENGTH = 2000
# The forms show whole seconds, so times within half a second count as equal.
TIME_TOLERANCE = 0.5

_LESSON_TIME = re.compile(r"^(?:(\d+):)?(\d+):(\d{1,2}(?:[.,]\d+)?)$")


@dataclass(frozen=True)
class CuratedMoment:
    """One row of an interview's list with what the list order and the lesson clock make of it."""

    row: InterviewMoment
    # 1-based place among the moments that are not rejected; None for a rejected moment.
    number: int | None
    # PRIMARY or RESERVE from the place on the list; None for a rejected moment.
    role: str | None
    start_clock: str | None
    end_clock: str | None
    start_video: str | None
    end_video: str | None

    @property
    def proposed_role(self) -> str | None:
        """The role the engine proposed (PRIMARY or RESERVE); None for a manual moment."""
        return None if self.row.is_manual else self.row.proposal.get("status")


def _rounded(seconds: Any) -> float:
    return round(float(seconds), 1)


def _identity(step: Any, start: Any, end: Any) -> tuple[int, float, float]:
    """What makes two proposals the same moment: the step it answers and its proposed times."""
    return int(step), _rounded(start), _rounded(end)


def _row_identity(row: InterviewMoment) -> tuple[int, float, float] | None:
    if row.is_manual or row.original_start_seconds is None or row.original_end_seconds is None:
        return None
    return _identity(row.proposal.get("step", 0), row.original_start_seconds, row.original_end_seconds)


def is_decided(row: InterviewMoment) -> bool:
    return row.status != InterviewMoment.STATUS_PENDING or bool(row.note.strip()) or row.time_changed


def _new_proposed_row(project: Project, key: str, moment: dict[str, Any]) -> InterviewMoment:
    return InterviewMoment(
        project=project,
        interview_key=key,
        source=InterviewMoment.SOURCE_PROPOSED,
        original_start_seconds=moment["start_seconds"],
        original_end_seconds=moment["end_seconds"],
        start_seconds=moment["start_seconds"],
        end_seconds=moment["end_seconds"],
        proposal=moment,
    )


def _merge_order(survivors: list[InterviewMoment], engine_index: dict[int, int],
                 new_rows: list[tuple[int, InterviewMoment]]) -> list[InterviewMoment]:
    """The survivors in their previous order, each new proposal placed after its nearest engine predecessor.

    ``engine_index`` maps ``id()`` of a row the engine proposed this time to its
    place in the engine's order; ``new_rows`` are the new proposals with theirs.
    """
    ordered = list(survivors)
    for index, row in new_rows:
        insert_at = 0
        for place, existing in enumerate(ordered):
            existing_index = engine_index.get(id(existing))
            if existing_index is not None and existing_index < index:
                insert_at = place + 1
        ordered.insert(insert_at, row)
        engine_index[id(row)] = index
    return ordered


@transaction.atomic
def sync_interview_moments(project: Project, result: AnalysisResult) -> None:
    """Bring the interview lists in line with the stored proposals, keeping every researcher decision."""
    proposals = {interview["key"]: interview["moments"] for interview in result.payload.get("interviews", [])}
    rows_by_key: dict[str, list[InterviewMoment]] = {}
    for row in project.interview_moments.order_by("position", "created_at"):
        rows_by_key.setdefault(row.interview_key, []).append(row)

    for key in set(proposals) | set(rows_by_key):
        rows = rows_by_key.get(key, [])
        by_identity = {identity: row for row in rows if (identity := _row_identity(row)) is not None}
        engine_index: dict[int, int] = {}
        matched: set[int] = set()
        new_rows: list[tuple[int, InterviewMoment]] = []
        for index, moment in enumerate(proposals.get(key, [])):
            row = by_identity.get(_identity(moment["step"], moment["start_seconds"], moment["end_seconds"]))
            if row is None or id(row) in matched:
                new_rows.append((index, _new_proposed_row(project, key, moment)))
                continue
            matched.add(id(row))
            engine_index[id(row)] = index
            row.proposal = moment
            row.needs_check = False

        survivors: list[InterviewMoment] = []
        for row in rows:
            if row.is_manual or id(row) in matched:
                survivors.append(row)
            elif is_decided(row):
                row.needs_check = True
                survivors.append(row)
            else:
                row.delete()

        for position, row in enumerate(_merge_order(survivors, engine_index, new_rows)):
            row.position = position
            row.save()


def ensure_synced(project: Project, result: AnalysisResult | None) -> None:
    """Create the rows of proposals stored before the researcher's lists existed (or never synced)."""
    if result is None:
        return
    keys = {row["interview_key"] for row in project.interview_moments.values("interview_key")}
    if any(interview["moments"] and interview["key"] not in keys for interview in result.payload.get("interviews", [])):
        sync_interview_moments(project, result)


def lesson_clock_start(payload: dict[str, Any]) -> datetime | None:
    """The sensor clock at lesson time 0, or None when the recordings have no clock."""
    stored = (payload.get("lesson") or {}).get("start_clock")
    if stored:
        # Early proposals stored the lesson clock as a bare HH:MM:SS (to the second).
        return datetime.fromisoformat(stored) if "T" in stored else datetime.combine(date(2000, 1, 2), time.fromisoformat(stored))
    # Proposals stored before the lesson clock was kept: read it back from a proposed moment (to the second).
    for interview in payload.get("interviews", []):
        for moment in interview["moments"]:
            if moment.get("start_clock"):
                clock = datetime.combine(date(2000, 1, 2), time.fromisoformat(moment["start_clock"]))
                return clock - timedelta(seconds=moment["start_seconds"])
    return None


def _clock(clock_start: datetime | None, seconds: float) -> str | None:
    return None if clock_start is None else (clock_start + timedelta(seconds=seconds)).strftime("%H:%M:%S")


def _times(row: InterviewMoment, clock_start: datetime | None, video_start: str | None) -> dict[str, str | None]:
    if not row.is_manual and not row.time_changed:
        start_clock, end_clock = row.proposal.get("start_clock"), row.proposal.get("end_clock")
    else:
        start_clock, end_clock = _clock(clock_start, row.start_seconds), _clock(clock_start, row.end_seconds)
    return {
        "start_clock": start_clock,
        "end_clock": end_clock,
        "start_video": video_time(start_clock, video_start),
        "end_video": video_time(end_clock, video_start),
    }


def primary_target(result: AnalysisResult) -> int:
    return moment_counts(result.payload)[0]


def ordered_rows(rows: Iterable[InterviewMoment]) -> list[InterviewMoment]:
    """The list as shown: the moments that are not rejected in list order, then the rejected ones."""
    rows = sorted(rows, key=lambda row: (row.position, row.created_at))
    return [row for row in rows if row.status != InterviewMoment.STATUS_REJECTED] + [
        row for row in rows if row.status == InterviewMoment.STATUS_REJECTED
    ]


def curated_interviews(project: Project, result: AnalysisResult) -> dict[str, list[CuratedMoment]]:
    """Every interview's list (by interview key) with numbers, roles and lesson / clock / video times."""
    payload = result.payload
    target = primary_target(result)
    clock_start = lesson_clock_start(payload)
    video_start = (payload.get("roles") or {}).get("video_start_time")
    rows_by_key: dict[str, list[InterviewMoment]] = {}
    for row in project.interview_moments.all():
        rows_by_key.setdefault(row.interview_key, []).append(row)

    curated: dict[str, list[CuratedMoment]] = {}
    for interview in payload.get("interviews", []):
        items = []
        kept = 0
        for row in ordered_rows(rows_by_key.get(interview["key"], [])):
            rejected = row.status == InterviewMoment.STATUS_REJECTED
            if not rejected:
                kept += 1
            items.append(
                CuratedMoment(
                    row=row,
                    number=None if rejected else kept,
                    role=None if rejected else (PRIMARY if kept <= target else RESERVE),
                    **_times(row, clock_start, video_start),
                )
            )
        curated[interview["key"]] = items
    return curated


# ─── The researcher's actions ────────────────────────────────────────────────


def parse_lesson_time(text: str) -> float:
    """A lesson time typed as m:ss, h:mm:ss or plain seconds, in seconds."""
    value = (text or "").strip()
    match = _LESSON_TIME.match(value)
    if match:
        hours, minutes, seconds = match.groups()
        if float(seconds.replace(",", ".")) >= 60:
            raise ValueError(gettext("Write the lesson time as m:ss, for example 12:30."))
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds.replace(",", "."))
    try:
        seconds = float(value.replace(",", "."))
    except ValueError:
        raise ValueError(gettext("Write the lesson time as m:ss, for example 12:30.")) from None
    if seconds < 0:
        raise ValueError(gettext("Write the lesson time as m:ss, for example 12:30."))
    return seconds


def _clean_text(text: str | None, limit: int, too_long: str) -> str:
    """``text`` stripped; ``too_long`` (a translated message with ``%(count)s``) when it is over ``limit``."""
    value = (text or "").strip()
    if len(value) > limit:
        raise ValueError(too_long % {"count": limit})
    return value


def _reason(text: str | None) -> str:
    return _clean_text(text, MAX_REASON_LENGTH, gettext("The reason may be at most %(count)s characters long."))


def _note(text: str | None) -> str:
    return _clean_text(text, MAX_NOTE_LENGTH, gettext("The note may be at most %(count)s characters long."))


def _validate_times(project: Project, result: AnalysisResult, key: str, start: float, end: float,
                    exclude: InterviewMoment | None = None) -> tuple[float, float]:
    lesson = result.payload.get("lesson") or {}
    lesson_start, lesson_end = lesson.get("start_seconds"), lesson.get("end_seconds")
    start, end = _rounded(start), _rounded(end)
    if end <= start:
        raise ValueError(gettext("The moment must end after it starts."))
    if lesson_start is not None and lesson_end is not None:
        # The form shows whole seconds: half a second past either end is still the end.
        if start < lesson_start - TIME_TOLERANCE or end > lesson_end + TIME_TOLERANCE:
            raise ValueError(gettext("The moment must lie within the analysed period of the lesson."))
        start, end = max(start, _rounded(lesson_start)), min(end, _rounded(lesson_end))
    others = project.interview_moments.filter(interview_key=key).exclude(status=InterviewMoment.STATUS_REJECTED)
    if exclude is not None:
        others = others.exclude(pk=exclude.pk)
    for other in others:
        if start < other.end_seconds - TIME_TOLERANCE and other.start_seconds < end - TIME_TOLERANCE:
            # The engine selects moments that do not overlap; the researcher's moments follow the same rule.
            raise ValueError(gettext("The moment overlaps another moment of this interview; reject or move that one first."))
    return start, end


def interview_keys(result: AnalysisResult) -> list[str]:
    return [interview["key"] for interview in result.payload.get("interviews", [])]


@transaction.atomic
def set_status(moment: InterviewMoment, status: str) -> None:
    if status not in (InterviewMoment.STATUS_PENDING, InterviewMoment.STATUS_ACCEPTED,
                      InterviewMoment.STATUS_REJECTED):
        raise ValueError(f"Unknown moment status: {status}")
    if status == InterviewMoment.STATUS_REJECTED and moment.is_manual:
        raise ValueError(gettext("Remove a moment you added instead of rejecting it."))
    if status != InterviewMoment.STATUS_REJECTED and moment.status == InterviewMoment.STATUS_REJECTED:
        _check_free(moment)
    moment.status = status
    moment.needs_check = False
    moment.save(update_fields=["status", "needs_check", "updated_at"])


def _check_free(moment: InterviewMoment) -> None:
    """A rejected moment comes back only if no kept moment took its place in time."""
    overlapping = moment.project.interview_moments.filter(
        interview_key=moment.interview_key,
        start_seconds__lt=moment.end_seconds - TIME_TOLERANCE,
        end_seconds__gt=moment.start_seconds + TIME_TOLERANCE,
    ).exclude(status=InterviewMoment.STATUS_REJECTED).exclude(pk=moment.pk)
    if overlapping.exists():
        raise ValueError(gettext("The moment overlaps another moment of this interview; reject or move that one first."))


def _renumber(rows: list[InterviewMoment]) -> None:
    for position, row in enumerate(rows):
        if row.position != position:
            row.position = position
            row.save(update_fields=["position", "updated_at"])


@transaction.atomic
def move(moment: InterviewMoment, offset: int) -> None:
    """Move a kept moment one place up (-1) or down (+1) among the interview's kept moments."""
    rows = ordered_rows(moment.project.interview_moments.filter(interview_key=moment.interview_key))
    kept = [row for row in rows if row.status != InterviewMoment.STATUS_REJECTED]
    index = next((place for place, row in enumerate(kept) if row.pk == moment.pk), None)
    if index is None:
        raise ValueError(gettext("Restore a rejected moment before moving it."))
    target = index + offset
    if 0 <= target < len(kept):
        kept[index], kept[target] = kept[target], kept[index]
    _renumber(kept + [row for row in rows if row.status == InterviewMoment.STATUS_REJECTED])


@transaction.atomic
def change_time(moment: InterviewMoment, result: AnalysisResult, start: float, end: float, reason: str) -> None:
    """Set the researcher's time of a moment; a time other than the proposed one needs a reason."""
    reason = _reason(reason)
    start, end = _validate_times(moment.project, result, moment.interview_key, start, end, exclude=moment)
    back_to_proposal = not moment.is_manual and (
        abs(start - moment.original_start_seconds) < TIME_TOLERANCE
        and abs(end - moment.original_end_seconds) < TIME_TOLERANCE
    )
    if back_to_proposal:
        start, end, reason = moment.original_start_seconds, moment.original_end_seconds, ""
    elif not reason:
        raise ValueError(gettext("Give the reason for the change."))
    moment.start_seconds, moment.end_seconds, moment.reason = start, end, reason
    moment.needs_check = False
    moment.save(update_fields=["start_seconds", "end_seconds", "reason", "needs_check", "updated_at"])


def set_note(moment: InterviewMoment, note: str) -> None:
    moment.note = _note(note)
    moment.save(update_fields=["note", "updated_at"])


@transaction.atomic
def add_manual(project: Project, result: AnalysisResult, key: str, start: float, end: float, reason: str,
               note: str = "") -> InterviewMoment:
    """A moment the researcher adds; the reason is required and is the moment's justification."""
    if key not in interview_keys(result):
        raise ValueError(gettext("Choose an interview of the current proposals."))
    reason = _reason(reason)
    if not reason:
        raise ValueError(gettext("Give the reason for adding the moment."))
    note = _note(note)
    start, end = _validate_times(project, result, key, start, end)
    rows = ordered_rows(project.interview_moments.filter(interview_key=key))
    kept = [row for row in rows if row.status != InterviewMoment.STATUS_REJECTED]
    moment = InterviewMoment.objects.create(
        project=project,
        interview_key=key,
        source=InterviewMoment.SOURCE_MANUAL,
        status=InterviewMoment.STATUS_ACCEPTED,
        start_seconds=start,
        end_seconds=end,
        reason=reason,
        note=note,
        position=len(kept),
    )
    _renumber(kept + [moment] + [row for row in rows if row.status == InterviewMoment.STATUS_REJECTED])
    return moment


@transaction.atomic
def remove_manual(moment: InterviewMoment) -> None:
    if not moment.is_manual:
        raise ValueError(gettext("Reject a proposed moment instead of removing it."))
    project, key = moment.project, moment.interview_key
    moment.delete()
    _renumber(ordered_rows(project.interview_moments.filter(interview_key=key)))
