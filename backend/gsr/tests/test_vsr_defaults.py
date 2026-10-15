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

HTMX = {"HTTP_HX_REQUEST": "true"}


def test_the_document_values_are_the_application_defaults_and_the_pipeline_keeps_gsrv2s() -> None:
    # Page 1 of the document: "Synchronous = above 0.8" for both co-movement thresholds behind steps 1-2.
    assert VSR_DOCUMENT_SETTINGS == {"synchrony_threshold": 0.8, "contagion_comovement_rho": 0.8}
    assert params.DEFAULTS["synchrony_threshold"] == 0.8 and params.DEFAULTS["contagion_comovement_rho"] == 0.8
    # "A total of 5 moments discussed per interview (ideally)", "Aim to identify more (e.g. 8)".
    assert params.VSR_DOCUMENT_COUNTS == {"primary_moment_count": 5, "reserve_moment_count": 3}
    assert PipelineConfig().synchrony_threshold == 0.6 and PipelineConfig().contagion_comovement_rho == 0.3


@pytest.mark.django_db
def test_new_projects_start_with_the_document_values(client) -> None:
    client.post(reverse("gsr:projects"), {"name": "Nowy"})
    project = Project.objects.get(name="Nowy")
    assert project.pipeline_config == VSR_DOCUMENT_SETTINGS
    config = resolve_config(project)
    assert config.synchrony_threshold == 0.8 and config.contagion_comovement_rho == 0.8
    counts = load_interview_config(project)
    assert (counts.primary_count, counts.reserve_count) == (5, 3)


@pytest.mark.django_db
def test_a_saved_project_keeps_its_values_until_the_researcher_uses_the_document_ones(client) -> None:
    project = Project.objects.create(name="Stary", pipeline_config={})
    Project.objects.filter(pk=project.pk).update(pipeline_config={"median_kernel": 9},
                                                 interview_config={"primary_moment_count": 4})
    project.refresh_from_db()
    assert resolve_config(project).synchrony_threshold == 0.6  # GSRv2's default, as before

    with translation.override("en"):
        page = client.get(reverse("gsr:analysis", args=[project.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert 'class="vsr-document differs"' in page
    assert "<span>Synchronization threshold</span> <span class=\"value\">0.6 → <b>0.8</b></span>" in page
    assert "<span>Primary moments per interview</span> <span class=\"value\">4 → <b>5</b></span>" in page
    action = reverse("gsr:vsr_defaults", args=[project.pk])
    assert f'formaction="{action}"' in page  # inside the drawer form, next to Reset defaults

    response = client.post(action, {"tab": "coarousal"})
    assert response.status_code == 302 and response["Location"].endswith("?tab=coarousal")
    project.refresh_from_db()
    assert project.pipeline_config == {"median_kernel": 9, **VSR_DOCUMENT_SETTINGS}
    assert project.interview_config == {"primary_moment_count": 5, "reserve_moment_count": 3}
    page = client.get(reverse("gsr:analysis", args=[project.pk])).content.decode()
    assert 'class="vsr-document"' in page and f'formaction="{action}"' not in page


@pytest.mark.django_db
def test_the_interviews_step_offers_the_document_values_too(client) -> None:
    project = Project.objects.create(name="Wywiady", pipeline_config={})
    page = client.get(reverse("gsr:interviews", args=[project.pk])).content.decode()
    action = reverse("gsr:vsr_defaults", args=[project.pk])
    assert f'<form method="post" action="{action}"' in page and "Przywróć wartości z dokumentu VSR" in page
    assert '<input type="hidden" name="page" value="interviews">' in page

    fragment = client.post(action, {"page": "interviews", "tab": ""}, **HTMX).content.decode()
    assert 'class="vsr-document"' in fragment and f'action="{action}"' not in fragment
    project.refresh_from_db()
    assert project.pipeline_config == VSR_DOCUMENT_SETTINGS
    assert client.post(action, {"page": "interviews"})["Location"] == reverse("gsr:interviews", args=[project.pk])
