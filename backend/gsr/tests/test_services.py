from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command

from gsr.models import Dataset, Project
from gsr.pipeline.config import PipelineConfig
from gsr.services.analysis import AnalysisError, analyze_files, resolve_config, run_project_analysis
from gsr.services.datasets import add_dataset

from .sample_data import Participant, RecordingFormat, write_group

FAST_ANALYSIS = {"synchrony_surrogate_count": 20, "contagion_surrogate_count": 20}


@pytest.fixture()
def recordings(tmp_path: Path) -> dict[str, Path]:
    return write_group(
        tmp_path,
        "session",
        {"A": Participant(tonic_us=4.0), "B": Participant(tonic_us=6.0, lag_seconds=1.0)},
        {"B": RecordingFormat(start_time="12:00:02:000", polish=True)},
        seconds=180.0,
        seed=11,
    )


def _upload(path: Path) -> SimpleUploadedFile:
    return SimpleUploadedFile(path.name, path.read_bytes(), content_type="text/csv")


@pytest.mark.django_db
def test_add_dataset_stores_file_and_parser_metadata(recordings, isolated_storage) -> None:
    project = Project.objects.create(name="Session")
    dataset = add_dataset(project, _upload(recordings["A"]), alias="  A ")

    assert dataset.alias == "A"
    assert dataset.original_filename == recordings["A"].name
    assert dataset.file_size == recordings["A"].stat().st_size
    assert len(dataset.file_sha256) == 64
    assert dataset.parsed_metadata["time_column"] == "TIMESTAMP"
    stored = Path(dataset.file.path)
    assert stored.read_bytes() == recordings["A"].read_bytes()
    assert Path(isolated_storage) in stored.parents
    assert stored.parent.name == "uploads" and stored.parent.parent.name == str(project.id)

    dataset.delete()
    assert not stored.exists()


@pytest.mark.django_db
def test_add_dataset_defaults_alias_to_file_stem(recordings) -> None:
    project = Project.objects.create(name="Session")
    assert add_dataset(project, _upload(recordings["B"])).alias == "session_B"


@pytest.mark.django_db
def test_add_dataset_rejects_unparseable_file_without_leaving_it_on_disk(isolated_storage) -> None:
    project = Project.objects.create(name="Session")
    upload = SimpleUploadedFile("notes.csv", b"not;an;esense;export\n1;2;3;4\n")

    with pytest.raises(ValidationError, match="Cannot detect data header"):
        add_dataset(project, upload, alias="X")

    assert not Dataset.objects.exists()
    assert not any(path.is_file() for path in Path(isolated_storage).rglob("*"))


@pytest.mark.django_db
def test_add_dataset_rejects_duplicate_alias_and_dataset_limit(recordings, settings) -> None:
    project = Project.objects.create(name="Session")
    add_dataset(project, _upload(recordings["A"]), alias="A")
    with pytest.raises(ValidationError, match="Alias already exists"):
        add_dataset(project, _upload(recordings["B"]), alias="A")

    settings.MAX_DATASETS = 1
    with pytest.raises(ValidationError, match="Maximum 1 datasets"):
        add_dataset(project, _upload(recordings["B"]), alias="B")


@pytest.mark.django_db
def test_run_project_analysis_matches_direct_file_analysis(recordings) -> None:
    project = Project.objects.create(name="Session", pipeline_config={"scaling_enabled": True})
    for alias, path in recordings.items():
        add_dataset(project, _upload(path), alias=alias)

    payload = run_project_analysis(project, config_overrides=FAST_ANALYSIS)

    assert set(payload) == {"preview", "synchrony", "coarousal"}
    assert payload["preview"]["aliases"] == ["A", "B"]
    assert payload["synchrony"]["aliases"] == ["A", "B"]
    assert payload["coarousal"]["aliases"] == ["A", "B"]
    expected = analyze_files(recordings, PipelineConfig(scaling_enabled=True, **FAST_ANALYSIS))
    assert json.dumps(payload["synchrony"]) == json.dumps(expected["synchrony"])
    assert json.dumps(payload["coarousal"]) == json.dumps(expected["coarousal"])


@pytest.mark.django_db
def test_run_project_analysis_selects_datasets_by_id(recordings) -> None:
    project = Project.objects.create(name="Session")
    datasets = [add_dataset(project, _upload(path), alias=alias) for alias, path in recordings.items()]

    payload = run_project_analysis(project, analyses=[], dataset_ids=[str(datasets[1].id)])
    assert payload["preview"]["aliases"] == ["B"]

    with pytest.raises(AnalysisError, match="do not belong"):
        run_project_analysis(project, analyses=[], dataset_ids=["00000000-0000-0000-0000-000000000000"])


@pytest.mark.django_db
def test_run_project_analysis_requires_datasets() -> None:
    with pytest.raises(AnalysisError, match="no uploaded datasets"):
        run_project_analysis(Project.objects.create(name="Empty"))


@pytest.mark.parametrize(
    ("overrides", "aliases", "message"),
    [
        ({}, ["A", "B"], "Enable scaling"),
        ({"scaling_enabled": True}, ["A"], "at least two distinct datasets"),
        ({"scaling_enabled": True, "synchrony_episode_min_size": 3}, ["A", "B"], "synchrony_episode_min_size"),
        ({"scaling_enabled": True, "contagion_min_participants": 3}, ["A", "B"], "contagion_min_participants"),
    ],
)
def test_analyze_files_guards_mirror_gsrv2(recordings, overrides, aliases, message) -> None:
    files = {alias: recordings[alias] for alias in aliases}
    with pytest.raises(AnalysisError, match=message):
        analyze_files(files, PipelineConfig(**overrides))


def test_analyze_files_reports_pipeline_errors_and_unknown_analyses(recordings, tmp_path: Path) -> None:
    config = PipelineConfig(scaling_enabled=True)
    with pytest.raises(AnalysisError, match="Unsupported analysis: segments"):
        analyze_files(recordings, config, ["segments"])
    with pytest.raises(AnalysisError, match="missing on disk"):
        analyze_files({**recordings, "C": tmp_path / "missing.csv"}, config, [])


def test_preview_only_runs_without_scaling(recordings) -> None:
    payload = analyze_files(recordings, PipelineConfig(), [])
    assert set(payload) == {"preview"}
    assert payload["preview"]["alignment"]["mode"] == "absolute"


def test_gsr_analyze_command_writes_json(recordings, tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    config = json.dumps({"scaling_enabled": True, **FAST_ANALYSIS})
    call_command(
        "gsr_analyze",
        *(f"{alias}={path}" for alias, path in recordings.items()),
        "--config",
        config,
        "--output",
        str(output),
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) == {"preview", "synchrony", "coarousal"}
    assert payload["synchrony"]["aliases"] == ["A", "B"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["A"], "Expected ALIAS=PATH"),
        (["A=a.csv", "A=b.csv"], "Duplicate alias"),
        (["A=a.csv", "--config", "[1]"], "JSON object"),
        (["A=a.csv", "--config", '{"segment_width": 5}'], "Unknown PipelineConfig fields"),
        (["A=a.csv", "--config", '{"synchrony_window_seconds": "abc"}'], "synchrony_window_seconds"),
        (["A=a.csv", "--config", '{"synchrony_surrogate_count": 1000000}'], "synchrony_surrogate_count"),
    ],
)
def test_gsr_analyze_command_rejects_bad_arguments(args, message) -> None:
    with pytest.raises(CommandError, match=message):
        call_command("gsr_analyze", *args)


@pytest.mark.parametrize(
    ("stored", "message"),
    [
        ({"synchrony_surrogate_count": 1000000}, "synchrony_surrogate_count"),
        ({"synchrony_surrogate_method": "foo"}, "synchrony_surrogate_method"),
        ({"synchrony_window_seconds": "abc"}, "synchrony_window_seconds"),
        ({"synchrony_window_seconds": 10, "synchrony_step_seconds": 20}, "must not exceed"),
        ({"contagion_low_z": 0.0, "contagion_high_z": 0.0}, "contagion_low_z must be lower"),
        ({"clean_filter_physiological_min_us": 30.0}, "clean_filter_physiological_min_us"),
        ({"synchrony_episode_min_size": 1}, "synchrony_episode_min_size"),
        ({"median_kernel": 2.5}, "median_kernel"),
        ({"scaling_enabled": "maybe"}, "scaling_enabled"),
        ({"synchrony_threshold": None}, "synchrony_threshold"),
    ],
)
@pytest.mark.django_db
def test_run_project_analysis_rejects_invalid_config(recordings, stored, message) -> None:
    project = Project.objects.create(name="Session", pipeline_config=stored)
    for path in recordings.values():
        add_dataset(project, _upload(path))
    with pytest.raises(AnalysisError, match=message):
        run_project_analysis(project, analyses=[])


@pytest.mark.django_db
def test_resolve_config_coerces_values_gsrv2_accepted() -> None:
    project = Project.objects.create(
        name="Session",
        pipeline_config={
            "median_kernel": "7",
            "downsample_factor": 8.0,
            "scaling_enabled": "true",
            "synchrony_dtw_enabled": 0,
            "synchrony_window_seconds": "45",
            "synchrony_step_seconds": 45,
            "synchrony_surrogate_count": 1000,
            "synchrony_surrogate_method": "phase",
            "time_range_from": None,
            "segment_width": 5,
        },
    )
    config = resolve_config(project, {"time_range_to": "12.5"})
    assert config.median_kernel == 7 and config.downsample_factor == 8
    assert config.scaling_enabled is True and config.synchrony_dtw_enabled is False
    assert config.synchrony_window_seconds == 45.0 and isinstance(config.synchrony_window_seconds, float)
    assert config.synchrony_surrogate_count == 1000 and config.synchrony_surrogate_method == "phase"
    assert config.time_range_from is None and config.time_range_to == 12.5
    assert config.model_fields_set == {
        "median_kernel",
        "downsample_factor",
        "scaling_enabled",
        "synchrony_dtw_enabled",
        "synchrony_window_seconds",
        "synchrony_step_seconds",
        "synchrony_surrogate_count",
        "synchrony_surrogate_method",
        "time_range_from",
        "time_range_to",
    }


@pytest.mark.django_db
def test_admin_cannot_add_datasets_directly(admin_client) -> None:
    assert admin_client.get("/admin/gsr/dataset/add/").status_code == 403
    assert admin_client.get("/admin/gsr/dataset/").status_code == 200
