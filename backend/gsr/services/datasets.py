"""Upload path: store an eSense CSV, validate it with the pipeline parser, record a Dataset."""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction

from ..models import Dataset, Project
from ..pipeline.parser import ParseError, load_esense_csv

logger = logging.getLogger(__name__)


def _normalize_label(value: str, *, field: str, max_length: int) -> str:
    label = unicodedata.normalize("NFC", value).strip()
    if not label:
        raise ValidationError(f"{field} cannot be empty")
    if len(label) > max_length:
        raise ValidationError(f"{field} cannot exceed {max_length} characters")
    if any(unicodedata.category(character).startswith("C") for character in label):
        raise ValidationError(f"{field} cannot contain control characters")
    return label


def normalize_alias(value: str) -> str:
    return _normalize_label(value, field="Alias", max_length=120)


_LEADING_TOKEN = re.compile(r"[^\W_]+")


def propose_alias(project: Project, filename: str) -> str:
    """Default alias for an upload: the leading word or number of the file name stem.

    "12_anna_session.csv" -> "12", "anna-2024.csv" -> "anna". When that token is empty or
    already taken in the project, fall back to the full stem, then to numbered stems.
    """
    stem = Path(filename).stem.strip() or "dataset"
    match = _LEADING_TOKEN.match(unicodedata.normalize("NFC", stem))
    candidates = [match.group()] if match else []
    candidates.append(stem)
    taken = set(project.datasets.values_list("alias", flat=True))
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    suffix = 2
    while f"{stem}-{suffix}" in taken:
        suffix += 1
    return f"{stem}-{suffix}"


def normalize_original_filename(value: str) -> str:
    return _normalize_label(value, field="Filename", max_length=512)


def recording_range(frame) -> dict[str, str]:
    """Absolute start/end of a parsed recording (ISO), shown on the Upload screen."""
    if frame.empty:
        return {}
    return {"recorded_start": frame["time"].min().isoformat(), "recorded_end": frame["time"].max().isoformat()}


def add_dataset(project: Project, upload: UploadedFile, alias: str | None = None) -> Dataset:
    """Persist one uploaded recording; rejects files the pipeline parser cannot read."""
    original_filename = normalize_original_filename(Path(upload.name or "upload.csv").name)
    alias = normalize_alias(alias if alias is not None else propose_alias(project, original_filename))

    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if upload.size is not None and upload.size > max_bytes:
        raise ValidationError(f"Uploaded file exceeds {settings.MAX_UPLOAD_MB} MB")
    if project.datasets.count() >= settings.MAX_DATASETS:
        raise ValidationError(f"Maximum {settings.MAX_DATASETS} datasets per project")
    if project.datasets.filter(alias=alias).exists():
        raise ValidationError(f"Alias already exists in project: {alias}")

    digest = hashlib.sha256()
    size = 0
    for chunk in upload.chunks():
        digest.update(chunk)
        size += len(chunk)

    dataset = Dataset(
        project=project,
        alias=alias,
        original_filename=original_filename,
        file_sha256=digest.hexdigest(),
        file_size=size,
    )
    dataset.file.save(original_filename, upload, save=False)
    try:
        frame, metadata = load_esense_csv(Path(dataset.file.path))
        dataset.parsed_metadata = {**metadata, **recording_range(frame)}
        with transaction.atomic():
            dataset.save()
    except ParseError as exc:
        dataset.file.delete(save=False)
        logger.warning(
            "Dataset upload parse failed: project_id=%s alias=%s filename=%s error=%s",
            project.id, alias, original_filename, exc,
        )
        raise ValidationError(str(exc)) from exc
    except IntegrityError as exc:
        dataset.file.delete(save=False)
        raise ValidationError(f"Could not save dataset {alias}") from exc
    except Exception:
        dataset.file.delete(save=False)
        raise
    return dataset
