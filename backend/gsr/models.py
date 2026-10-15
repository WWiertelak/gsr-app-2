from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .pipeline.config import VSR_DOCUMENT_SETTINGS

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


def default_pipeline_config() -> dict:
    """A new project's settings: the values the VSR Interview Flow Chart suggests over GSRv2's defaults."""
    return dict(VSR_DOCUMENT_SETTINGS)


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    # Stored PipelineConfig overrides (gsr.pipeline.config field names); projects saved before the VSR document
    # values became the defaults keep their stored (possibly empty) overrides, i.e. GSRv2's defaults.
    pipeline_config = models.JSONField(default=default_pipeline_config, blank=True)
    # Interview roles (teacher, student pairs) and the number of interview moments.
    # Outside pipeline_config so role changes do not invalidate analysis results.
    interview_config = models.JSONField(default=dict, blank=True)
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
    KIND_VSR = "vsr"
    KIND_CHOICES = [
        (KIND_CLEANING, "Cleaning preview"),
        (KIND_ANALYSIS, "Synchronization and co-arousal"),
        (KIND_VSR, "VSR interview moments"),
    ]

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


class InterviewMoment(models.Model):
    """One moment on an interview's list on the Interviews step, and the researcher's decision about it.

    A ``proposed`` moment comes from the moments chosen from the Analysis (``AnalysisResult``
    kind ``vsr``) and keeps a copy of that proposal and its original times; a ``manual``
    moment is added by the researcher with a required reason, which is its justification.
    Choosing the moments again keeps every decision (see
    ``services.curation.sync_interview_moments``).
    """

    SOURCE_PROPOSED = "proposed"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [(SOURCE_PROPOSED, "Proposed"), (SOURCE_MANUAL, "Added manually")]

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [(STATUS_PENDING, "Not decided"), (STATUS_ACCEPTED, "Accepted"), (STATUS_REJECTED, "Rejected")]

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="interview_moments")
    # The interview's key in the proposals: "S1+S2" for a student pair, the teacher's alias for the teacher.
    interview_key = models.CharField(max_length=255)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # Order on the interview's list; the first moments not rejected are the primary ones, the rest the reserve.
    position = models.PositiveIntegerField(default=0)
    # Lesson times in seconds from the recordings' common start: as proposed, and as the researcher set them.
    original_start_seconds = models.FloatField(null=True, blank=True)
    original_end_seconds = models.FloatField(null=True, blank=True)
    start_seconds = models.FloatField()
    end_seconds = models.FloatField()
    # Why the researcher changed the time or added the moment.
    reason = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    # The proposed moment as it was chosen (step, Analysis episode, correlation, shared state).
    proposal = models.JSONField(default=dict, blank=True)
    # A decided moment the last choice of moments no longer includes: the researcher should check it.
    needs_check = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["interview_key", "position", "created_at"]
        indexes = [models.Index(fields=["project", "interview_key"], name="gsr_moment_interview_idx")]

    def __str__(self) -> str:
        return f"{self.project_id}/{self.interview_key}/{self.position}"

    @property
    def is_manual(self) -> bool:
        return self.source == self.SOURCE_MANUAL

    @property
    def time_changed(self) -> bool:
        return not self.is_manual and (
            self.start_seconds != self.original_start_seconds or self.end_seconds != self.original_end_seconds
        )


@receiver(post_delete, sender=Dataset)
def _delete_dataset_file(sender, instance: Dataset, **kwargs) -> None:
    if instance.file:
        instance.file.delete(save=False)
