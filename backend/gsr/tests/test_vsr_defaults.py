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
    # Page 1 of the document: "Synchronous = above 0.8" for the strength an interview moment needs.
    # Where the document is silent, Hassan's script: co-movement detected at rho >= 0.3 (as divergent synchrony at
    # -0.3, his e-mail's "negative correlations above > .30"), Spearman, whole shared High/Low episodes, and episodes
    # not joined across gaps (his printout keeps episodes 9 s and 11 s apart separate).
    # "Asynchronous = below 0.1" read as strength is the decoupling threshold.
    # The High and Low states (shared states and opposite arousal) at z = +/-1, GSRv2's values.
    assert VSR_DOCUMENT_SETTINGS == {"synchrony_threshold": 0.3, "contagion_comovement_rho": 0.3,
                                     "synchrony_correlation_method": "spearman",
                                     "synchrony_episode_max_gap_seconds": 0.0,
                                     "contagion_min_episode_gap_seconds": 0.0,
                                     "synchrony_interview_threshold": 0.8, "synchrony_divergent_threshold": 0.3,
                                     "synchrony_decoupled_threshold": 0.1, "synchrony_decoupled_rule": "every_window",
                                     "synchrony_interview_shared_without_comovement": False,
                                     "contagion_high_z": 1.0, "contagion_low_z": -1.0}
    assert (PipelineConfig().contagion_high_z, PipelineConfig().contagion_low_z) == (1.0, -1.0)
    assert params.DEFAULTS["synchrony_threshold"] == 0.3 and params.DEFAULTS["contagion_comovement_rho"] == 0.3
    assert params.DEFAULTS["synchrony_correlation_method"] == "spearman"
    # GSRv2's, for projects saved earlier.
    assert PipelineConfig().synchrony_correlation_method == "pearson"
    assert (PipelineConfig().synchrony_episode_max_gap_seconds, PipelineConfig().contagion_min_episode_gap_seconds) \
        == (15.0, 30.0)
    assert PipelineConfig().synchrony_threshold == 0.6 and PipelineConfig().contagion_comovement_rho == 0.3
    # A setting GSRv2 did not have takes the document value everywhere, saved projects included.
    assert PipelineConfig().synchrony_interview_threshold == 0.8


@pytest.mark.django_db
def test_new_projects_start_with_the_document_values(client) -> None:
    client.post(reverse("gsr:projects"), {"name": "Nowy"})
    project = Project.objects.get(name="Nowy")
    assert project.pipeline_config == VSR_DOCUMENT_SETTINGS
    config = resolve_config(project)
    assert config.synchrony_threshold == 0.3 and config.contagion_comovement_rho == 0.3
    assert config.synchrony_correlation_method == "spearman"
    interview = load_interview_config(project)
    # "A total of 5 moments discussed per interview (ideally)", "Aim to identify more (e.g. 8)".
    assert (interview.primary_count, interview.reserve_count) == (5, 3)
    assert project.interview_config == {}


@pytest.mark.django_db
def test_a_saved_project_keeps_its_values_until_reset_defaults(client) -> None:
    project = Project.objects.create(name="Stary")
    Project.objects.filter(pk=project.pk).update(pipeline_config={"median_kernel": 9},
                                                 interview_config={"primary_moment_count": 4})
    project.refresh_from_db()
    assert resolve_config(project).synchrony_threshold == 0.6  # GSRv2's default, as before
    # GSRv2's gap bridging and correlation too: the new defaults do not change a saved project's results.
    saved = resolve_config(project)
    assert (saved.synchrony_episode_max_gap_seconds, saved.contagion_min_episode_gap_seconds) == (15.0, 30.0)
    assert saved.synchrony_correlation_method == "pearson"
    # A setting GSRv2 did not have has only the document value.
    assert resolve_config(project).synchrony_decoupled_threshold == 0.1

    with translation.override("en"):
        page = client.get(reverse("gsr:analysis", args=[project.pk]), HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert ("Defaults follow the VSR Interview Flow Chart (interview moments above 0.8) and, where it is silent, "
            "Hassan's script (Spearman correlation, co-movement detected from 0.3, episodes not joined across "
            "gaps).") in page

    response = client.post(reverse("gsr:analysis_reset", args=[project.pk]), {"tab": "coarousal"})
    assert response.status_code == 302 and response["Location"].endswith("?tab=coarousal")
    project.refresh_from_db()
    assert project.pipeline_config == {"median_kernel": 9, **VSR_DOCUMENT_SETTINGS}
    assert project.interview_config == {"primary_moment_count": 4}
