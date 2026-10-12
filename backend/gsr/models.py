from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

SAFE_FILENAME = re.compile(r"[^a-zA-Z0-9._-]+")


def sanitize_filename(filename: str) -> str:
    sanitized = SAFE_FILENAME.sub("_", filename.strip())
    # The stored name also receives a timestamp and UUID prefix. Keep the
    # sanitized suffix comfortably below the common 255-byte filesystem limit;
    # the complete original name remains available in Dataset.original_filename.
    return sanitized[:160] or f"upload_{uuid4().hex}.csv"


def dataset_upload_path(instance: "Dataset", filename: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"projects/{instance.project_id}/uploads/{timestamp}_{uuid4().hex}_{sanitize_filename(filename)}"


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    # Stored PipelineConfig overrides (gsr.pipeline.config field names).
    pipeline_config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class Dataset(models.Model):
    """One uploaded GSR recording (eSense CSV export) of one participant."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="datasets")
    alias = models.CharField(max_length=120)
    original_filename = models.CharField(max_length=512)
    file = models.FileField(upload_to=dataset_upload_path, max_length=1024)
    file_sha256 = models.CharField(max_length=64, blank=True, default="")
    file_size = models.BigIntegerField(null=True, blank=True)
    parsed_metadata = models.JSONField(default=dict, blank=True)
    # Unchecked on the Cleaning screen: kept in the project, left out of every run.
    included = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["project", "alias"], name="uq_project_alias"),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}/{self.alias}"


class AnalysisResult(models.Model):
    """The last computed result of one UI stage, so reloading a page does not recompute.

    ``signature`` hashes the datasets and the settings the result was computed
    from; a project whose current signature differs shows the result as stale.
    """

    KIND_CLEANING = "cleaning"
    KIND_ANALYSIS = "analysis"
    KIND_CHOICES = [(KIND_CLEANING, "Cleaning preview"), (KIND_ANALYSIS, "Synchronization and co-arousal")]

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="results")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    signature = models.CharField(max_length=64)
    # Resolved PipelineConfig values and the ordered dataset ids the result was computed from.
    config = models.JSONField(default=dict)
    dataset_ids = models.JSONField(default=list)
    payload = models.JSONField(default=dict)
    duration_seconds = models.FloatField(default=0.0)
    computed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "kind"], name="uq_project_result_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}/{self.kind}"


@receiver(post_delete, sender=Dataset)
def _delete_dataset_file(sender, instance: Dataset, **kwargs) -> None:
    if instance.file:
        instance.file.delete(save=False)
