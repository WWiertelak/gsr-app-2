"""Interview roles and VSR parameters stored on ``Project.interview_config``.

Role and VSR settings are intentionally outside ``pipeline_config`` and the
analysis result signature so assigning a teacher or dyads does not mark
synchronization / co-arousal runs stale.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext

from ..models import Project
from ..pipeline.vsr_config import VsrConfig, parse_video_start_time
from .results import included_datasets

VSR_PARAM_NAMES = tuple(f.name for f in dataclasses.fields(VsrConfig))


@dataclass(frozen=True)
class InterviewRoles:
    teacher_alias: str | None
    student_pairs: tuple[tuple[str, str], ...]
    video_start_time: str | None


@dataclass(frozen=True)
class InterviewConfig:
    roles: InterviewRoles
    vsr: VsrConfig

    def to_storage(self) -> dict[str, Any]:
        pairs = [[left, right] for left, right in self.roles.student_pairs]
        return {
            "teacher_alias": self.roles.teacher_alias,
            "student_pairs": pairs,
            "video_start_time": self.roles.video_start_time,
            "vsr": self.vsr.model_dump(),
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

    video_start = parse_video_start_time(roles.get("video_start_time"))
    return InterviewRoles(teacher_alias=teacher, student_pairs=tuple(pairs), video_start_time=video_start)


def load_interview_config(project: Project) -> InterviewConfig:
    stored = project.interview_config or {}
    aliases = {dataset.alias for dataset in project.datasets.all()}
    roles = validate_roles(stored, aliases)
    vsr = VsrConfig.from_dict(stored.get("vsr"))
    return InterviewConfig(roles=roles, vsr=vsr)


def apply_interview_config(project: Project, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into stored interview settings; raises ``ValueError`` when invalid."""
    stored = dict(project.interview_config or {})
    aliases = {dataset.alias for dataset in project.datasets.all()}

    if "teacher_alias" in updates or "student_pairs" in updates or "video_start_time" in updates:
        role_updates = {
            "teacher_alias": updates.get("teacher_alias", stored.get("teacher_alias")),
            "student_pairs": updates.get("student_pairs", stored.get("student_pairs", [])),
            "video_start_time": updates.get("video_start_time", stored.get("video_start_time")),
        }
        roles = validate_roles(role_updates, aliases)
        stored["teacher_alias"] = roles.teacher_alias
        stored["student_pairs"] = [[left, right] for left, right in roles.student_pairs]
        stored["video_start_time"] = roles.video_start_time

    if "vsr" in updates:
        vsr_payload = {**(stored.get("vsr") or {}), **(updates["vsr"] or {})}
        stored["vsr"] = VsrConfig.from_dict(vsr_payload).model_dump()
    elif any(key in updates for key in VSR_PARAM_NAMES):
        vsr_payload = {**(stored.get("vsr") or {}), **{k: updates[k] for k in VSR_PARAM_NAMES if k in updates}}
        stored["vsr"] = VsrConfig.from_dict(vsr_payload).model_dump()

    # Re-validate roles when only aliases might have changed (caller passes full updates).
    roles = validate_roles(stored, aliases)
    stored["teacher_alias"] = roles.teacher_alias
    stored["student_pairs"] = [[left, right] for left, right in roles.student_pairs]
    stored["video_start_time"] = roles.video_start_time
    return stored


def vsr_field_rows(values: dict[str, Any]) -> list[dict[str, Any]]:
    from ..vsr_params import DEFAULTS, VSR_PARAMS

    return [
        {
            "param": param,
            "value": values.get(param.name),
            "modified": values.get(param.name) != DEFAULTS[param.name],
        }
        for param in VSR_PARAMS
    ]


def vsr_changed_label(values: dict[str, Any]) -> str | None:
    from django.utils.translation import ngettext

    from ..vsr_params import DEFAULTS, VSR_PARAMS

    changed = sum(1 for param in VSR_PARAMS if values.get(param.name) != DEFAULTS[param.name])
    if not changed:
        return None
    return ngettext("%(count)d changed", "%(count)d changed", changed) % {"count": changed}


def compute_vsr_signature(project: Project) -> str:
    """Hash of datasets plus interview roles and VSR parameters (for ``AnalysisResult`` kind ``vsr``)."""
    datasets = included_datasets(project)
    config = load_interview_config(project)
    document = {
        "datasets": [[str(dataset.id), dataset.alias, dataset.file_sha256] for dataset in datasets],
        "teacher_alias": config.roles.teacher_alias,
        "student_pairs": [list(pair) for pair in config.roles.student_pairs],
        "video_start_time": config.roles.video_start_time,
        "vsr": config.vsr.model_dump(),
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def dyad_labels(pairs: tuple[tuple[str, str], ...]) -> list[str]:
    return [gettext("Pair %(left)s + %(right)s") % {"left": left, "right": right} for left, right in pairs]
