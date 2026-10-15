"""Interview roles, the number of moments and the asynchrony threshold, stored on ``Project.interview_config``.

The interview moments come from the Analysis results with the Analysis settings, so
the Interviews step keeps only what is its own: the teacher, the student pairs, the
number of moments to propose and the r below which a moment answers STEP 5. They stay outside ``pipeline_config``
and the analysis result signature, so assigning a teacher or pairs does not mark the
synchronization / co-arousal run stale.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext

from ..models import AnalysisResult, Project
from ..pipeline.vsr import DEFAULT_PRIMARY_COUNT, DEFAULT_RESERVE_COUNT
from .results import included_datasets

_ROLE_KEYS = ("teacher_alias", "student_pairs")
_COUNT_KEYS = ("primary_moment_count", "reserve_moment_count")
# (lowest, highest) of each count; the document: "A total of 5 moments ... (ideally)", "(e.g. 8)" with the reserve.
COUNT_LIMITS = {"primary_moment_count": (1, 20), "reserve_moment_count": (0, 20)}
ASYNCHRONY_KEY = "asynchrony_threshold"
ASYNCHRONY_LIMITS = (-1.0, 1.0)
# Projects saved before the document's "below 0.1" became the default keep STEP 5 at r < 0 (new projects store 0.1).
SAVED_ASYNCHRONY_THRESHOLD = 0.0


@dataclass(frozen=True)
class InterviewRoles:
    teacher_alias: str | None
    student_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class InterviewConfig:
    roles: InterviewRoles
    primary_count: int = DEFAULT_PRIMARY_COUNT
    reserve_count: int = DEFAULT_RESERVE_COUNT
    asynchrony_threshold: float = SAVED_ASYNCHRONY_THRESHOLD

    def to_storage(self) -> dict[str, Any]:
        return {
            "teacher_alias": self.roles.teacher_alias,
            "student_pairs": [[left, right] for left, right in self.roles.student_pairs],
            "primary_moment_count": self.primary_count,
            "reserve_moment_count": self.reserve_count,
            ASYNCHRONY_KEY: self.asynchrony_threshold,
        }


def validate_roles(roles: dict[str, Any], aliases: set[str]) -> InterviewRoles:
    teacher = roles.get("teacher_alias")
    if teacher is not None:
        teacher = str(teacher).strip() or None
    if teacher is not None and teacher not in aliases:
        raise ValueError(gettext("Unknown teacher recording: %(alias)s") % {"alias": teacher})

    raw_pairs = roles.get("student_pairs") or []
    if not isinstance(raw_pairs, list):
        raise ValueError(gettext("student_pairs must be a list of pairs"))
    seen_students: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(raw_pairs):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(gettext("Each student pair must list two recording aliases"))
        left, right = str(item[0]).strip(), str(item[1]).strip()
        if not left or not right:
            raise ValueError(gettext("Each student pair must list two recording aliases"))
        if left == right:
            raise ValueError(gettext("A student pair must use two different recordings"))
        for alias in (left, right):
            if alias not in aliases:
                raise ValueError(gettext("Unknown recording in pair %(index)d: %(alias)s") % {
                    "index": index + 1,
                    "alias": alias,
                })
        if left in seen_students or right in seen_students:
            raise ValueError(gettext("Each student recording may appear in only one pair"))
        seen_students.update((left, right))
        if teacher is not None and teacher in (left, right):
            raise ValueError(gettext("The teacher recording cannot be in a student pair"))
        pairs.append((left, right))

    return InterviewRoles(teacher_alias=teacher, student_pairs=tuple(pairs))


def validate_count(name: str, value: Any) -> int:
    """A number of moments within ``COUNT_LIMITS``; raises ``ValueError`` with a translated message."""
    low, high = COUNT_LIMITS[name]
    try:
        number = float(str(value).strip().replace(",", "."))
    except ValueError:
        number = float("nan")
    if isinstance(value, bool) or not number.is_integer() or not low <= number <= high:
        message = (gettext("The number of primary moments must be a whole number from %(low)s to %(high)s.")
                   if name == "primary_moment_count"
                   else gettext("The number of reserve moments must be a whole number from %(low)s to %(high)s."))
        raise ValueError(message % {"low": low, "high": high})
    return int(number)


def validate_asynchrony(value: Any) -> float:
    """An asynchrony threshold (a correlation r) within ``ASYNCHRONY_LIMITS``; raises ``ValueError`` when not."""
    low, high = ASYNCHRONY_LIMITS
    try:
        number = float(str(value).strip().replace(",", "."))
    except ValueError:
        number = float("nan")
    if isinstance(value, bool) or not low <= number <= high:
        raise ValueError(gettext("The asynchrony threshold must be a number from %(low)s to %(high)s.")
                         % {"low": low, "high": high})
    return number


def _config(stored: dict[str, Any], aliases: set[str]) -> InterviewConfig:
    defaults = {"primary_moment_count": DEFAULT_PRIMARY_COUNT, "reserve_moment_count": DEFAULT_RESERVE_COUNT}
    counts = {name: validate_count(name, stored.get(name, defaults[name])) for name in _COUNT_KEYS}
    return InterviewConfig(roles=validate_roles(stored, aliases), primary_count=counts["primary_moment_count"],
                           reserve_count=counts["reserve_moment_count"],
                           asynchrony_threshold=validate_asynchrony(stored.get(ASYNCHRONY_KEY,
                                                                               SAVED_ASYNCHRONY_THRESHOLD)))


def load_interview_config(project: Project) -> InterviewConfig:
    return _config(project.interview_config or {}, {dataset.alias for dataset in project.datasets.all()})


def apply_interview_config(project: Project, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into stored interview settings; raises ``ValueError`` when invalid."""
    stored = project.interview_config or {}
    keys = (*_ROLE_KEYS, *_COUNT_KEYS, ASYNCHRONY_KEY)
    merged = {**stored, **{key: updates[key] for key in keys if key in updates}}
    return _config(merged, {dataset.alias for dataset in project.datasets.all()}).to_storage()


def rename_interview_alias(project: Project, old: str, new: str) -> None:
    """Keep the stored roles pointing at a recording after it is renamed."""
    stored = project.interview_config or {}
    if not stored or old == new:
        return
    teacher = stored.get("teacher_alias")
    pairs = stored.get("student_pairs") or []
    project.interview_config = {
        **stored,
        "teacher_alias": new if teacher == old else teacher,
        "student_pairs": [[new if alias == old else alias for alias in pair] for pair in pairs],
    }
    project.save(update_fields=["interview_config", "updated_at"])


def forget_interview_alias(project: Project, alias: str) -> None:
    """Drop a deleted recording from the teacher role and from every pair that contains it."""
    stored = project.interview_config or {}
    if not stored:
        return
    teacher = stored.get("teacher_alias")
    pairs = stored.get("student_pairs") or []
    project.interview_config = {
        **stored,
        "teacher_alias": None if teacher == alias else teacher,
        "student_pairs": [pair for pair in pairs if alias not in pair],
    }
    project.save(update_fields=["interview_config", "updated_at"])


def compute_vsr_signature(project: Project) -> str:
    """Hash of datasets, interview settings and the analysis run (for ``AnalysisResult`` kind ``vsr``).

    The moments are chosen from the stored analysis run, so running the analysis with
    other settings makes them stale too. The asynchrony threshold of a saved project
    (r < 0) is left out, so its moments chosen before the setting existed stay up to date.
    """
    datasets = included_datasets(project)
    stored = load_interview_config(project).to_storage()
    if stored[ASYNCHRONY_KEY] == SAVED_ASYNCHRONY_THRESHOLD:
        del stored[ASYNCHRONY_KEY]
    analysis = project.results.filter(kind=AnalysisResult.KIND_ANALYSIS).first()
    document = {
        "analysis": analysis.signature if analysis is not None else None,
        "datasets": [[str(dataset.id), dataset.alias, dataset.file_sha256] for dataset in datasets],
        **stored,
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()
