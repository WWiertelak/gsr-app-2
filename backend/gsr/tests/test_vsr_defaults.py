"""The values the VSR Interview Flow Chart suggests: new projects start with them, saved projects keep their own."""

from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import translation

from gsr import params
from gsr.models import Project
from gsr.pipeline.config import VSR_DOCUMENT_SETTINGS, PipelineConfig
from gsr.services.analysis import resolve_config
from gsr.services.interview import load_interview_config


def test_the_document_values_are_the_application_defaults_and_the_pipeline_keeps_gsrv2s() -> None:
    # Page 1 of the document: "Synchronous = above 0.8" for both co-movement thresholds behind steps 1-2.
    assert VSR_DOCUMENT_SETTINGS == {"synchrony_threshold": 0.8, "contagion_comovement_rho": 0.8}
    assert params.DEFAULTS["synchrony_threshold"] == 0.8 and params.DEFAULTS["contagion_comovement_rho"] == 0.8
    assert PipelineConfig().synchrony_threshold == 0.6 and PipelineConfig().contagion_comovement_rho == 0.3


@pytest.mark.django_db
def test_new_projects_start_with_the_document_values(client) -> None:
    client.post(reverse("gsr:projects"), {"name": "Nowy"})
    project = Project.objects.get(name="Nowy")
    assert project.pipeline_config == VSR_DOCUMENT_SETTINGS
    config = resolve_config(project)
    assert config.synchrony_threshold == 0.8 and config.contagion_comovement_rho == 0.8
    interview = load_interview_config(project)
    # "A total of 5 moments discussed per interview (ideally)", "Aim to identify more (e.g. 8)";
    # "Asynchronous = below 0.1".
    assert (interview.primary_count, interview.reserve_count) == (5, 3)
    assert interview.asynchrony_threshold == 0.1


@pytest.mark.django_db
def test_a_saved_project_keeps_its_values_until_reset_defaults(client) -> None:
    project = Project.objects.create(name="Stary")
    Project.objects.filter(pk=project.pk).update(pipeline_config={"median_kernel": 9},
                                                 interview_config={"primary_moment_count": 4})
    project.refresh_from_db()
    assert resolve_config(project).synchrony_threshold == 0.6  # GSRv2's default, as before
    assert load_interview_config(project).asynchrony_threshold == 0.0  # STEP 5 below zero, as before

    with translation.override("en"):
        page = client.get(reverse("gsr:analysis", args=[project.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert "Defaults follow the VSR Interview Flow Chart: synchrony above 0.8." in page

    response = client.post(reverse("gsr:analysis_reset", args=[project.pk]), {"tab": "coarousal"})
    assert response.status_code == 302 and response["Location"].endswith("?tab=coarousal")
    project.refresh_from_db()
    assert project.pipeline_config == {"median_kernel": 9, **VSR_DOCUMENT_SETTINGS}
    assert project.interview_config == {"primary_moment_count": 4, "asynchrony_threshold": 0.1}
