"""The extracted pipeline must reproduce GSRv2 exactly.

Every scenario runs the frozen GSRv2 pipeline (``legacy_gsrv2``) and the new
pipeline on the same synthetic recordings and requires identical outputs for
parsing, cleaning, synchronization, joint synchrony and co-arousal: same keys,
same types, bit-identical floats (NaN only where GSRv2 has NaN).
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from gsr.pipeline import coarousal as new_coarousal
from gsr.pipeline import config as new_config
from gsr.pipeline import parser as new_parser
from gsr.pipeline import preprocessing as new_pre
from gsr.pipeline import synchrony as new_synchrony
from gsr.pipeline.common import PipelineError

from .legacy_gsrv2 import config as old_config
from .legacy_gsrv2 import core as old_core
from .legacy_gsrv2 import parser as old_parser
from .sample_data import Participant, RecordingFormat, write_esense_csv, write_group

LEGACY_DIR = Path(__file__).parent / "legacy_gsrv2"
# sha256 of GSRv2 backend/gsr/pipeline/{config,parser,core}.py as they were on disk
# (commit e16e2f6 plus uncommitted changes) when the pipeline was extracted.
LEGACY_SHA256 = {
    "config.py": "9a95ea8c30729d76e6829e1c313f8847efa06e02acda8019973cf96e42ad9f1a",
    "parser.py": "5716694c2365a8d3c771878138a8318f138abec4e2168a5d4b04a9a3c6b5326a",
    "core.py": "3dbfce9ca696677e419c522988084dc916d0c469b6c52940c875d5b854c209f7",
}

STAGES = ("raw", "clean", "scaled")


def assert_identical(expected: Any, actual: Any, path: str = "$") -> None:
    if isinstance(expected, pd.DataFrame):
        assert isinstance(actual, pd.DataFrame), path
        pd.testing.assert_frame_equal(actual, expected, check_exact=True, obj=path)
        return
    if isinstance(expected, pd.Series):
        assert isinstance(actual, pd.Series), path
        pd.testing.assert_series_equal(actual, expected, check_exact=True, obj=path)
        return
    if isinstance(expected, np.ndarray):
        assert isinstance(actual, np.ndarray), path
        assert expected.dtype == actual.dtype, f"{path}: dtype {expected.dtype} != {actual.dtype}"
        assert np.array_equal(expected, actual, equal_nan=expected.dtype.kind in "fc"), path
        return
    if dataclasses.is_dataclass(expected) and not isinstance(expected, type):
        assert_identical(dataclasses.asdict(expected), dataclasses.asdict(actual), path)
        return
    assert type(expected) is type(actual), f"{path}: type {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        assert list(expected) == list(actual), f"{path}: keys {list(expected)} != {list(actual)}"
        for key in expected:
            assert_identical(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, (list, tuple)):
        assert len(expected) == len(actual), f"{path}: length {len(expected)} != {len(actual)}"
        for idx, (exp, act) in enumerate(zip(expected, actual)):
            assert_identical(exp, act, f"{path}[{idx}]")
    elif isinstance(expected, float) and math.isnan(expected):
        assert math.isnan(actual), f"{path}: expected NaN, got {actual!r}"
    else:
        assert expected == actual, f"{path}: {expected!r} != {actual!r}"


THREE_PARTICIPANTS = {
    "A": Participant(tonic_us=4.0, drift_us_per_min=0.15),
    "B": Participant(tonic_us=7.5, drift_us_per_min=-0.05, response_us=1.1, lag_seconds=2.0),
    "C": Participant(tonic_us=2.5, coupling=-0.6, own_events=3),
}
FOUR_PARTICIPANTS = {
    **THREE_PARTICIPANTS,
    "D": Participant(tonic_us=11.0, coupling=0.0, own_events=5, noise_us=0.05),
}
STAGGERED_FORMATS = {
    "A": RecordingFormat(start_time="12:00:00:000", pause_rows=(40, 41, 900), artifacts={300: 19.5, 301: 26.0}),
    "B": RecordingFormat(start_time="12:00:03:400", polish=True, artifacts={700: 0.1, 701: 0.1, 702: 0.1}),
    "C": RecordingFormat(start_time="11:59:58:600", paused_label="Paused", markers_without_ms=(120, 121, 122)),
    "D": RecordingFormat(start_time="12:00:01:100", encoding="cp1250", polish=True),
}

# name -> (participants, formats, seconds, seed, config kwargs). Config kwargs only
# use fields both pipelines share; the dict is passed as-is so the explicitly-set
# fields (which select the downsampling mode) are the same on both sides.
SCENARIOS: dict[str, tuple[dict[str, Participant], dict[str, RecordingFormat], float, int, dict[str, Any]]] = {
    # The GSRv2 analysis path: analyses require scaling; 1 s interval downsampling.
    "app_defaults": (THREE_PARTICIPANTS, STAGGERED_FORMATS, 300.0, 1, {"scaling_enabled": True}),
    # Unscaled signals keep the full ~5 Hz merged grid (no downsampling).
    "unscaled_full_rate": (
        THREE_PARTICIPANTS, STAGGERED_FORMATS, 240.0, 2, {"synchrony_surrogate_count": 40, "contagion_surrogate_count": 40}
    ),
    # Only the sample-based factor is set → GSRv2 legacy downsample mode.
    "legacy_downsample_factor": (THREE_PARTICIPANTS, STAGGERED_FORMATS, 300.0, 3, {"scaling_enabled": True, "downsample_factor": 7}),
    "strict_cleaning_time_range": (
        THREE_PARTICIPANTS,
        STAGGERED_FORMATS,
        360.0,
        4,
        {
            "scaling_enabled": True,
            "clean_filter_iqr": True,
            "clean_filter_zscore_threshold": 3.5,
            "clean_filter_jump_sigma": 3.5,
            "clean_filter_physiological_min_us": 1.0,
            "clean_filter_butterworth": False,
            "median_kernel": 0,
            "time_range_from": 0.5,
            "time_range_to": 4.5,
            "downsample_interval_seconds": 2,
        },
    ),
    "unsigned_phase_spearman": (
        FOUR_PARTICIPANTS,
        STAGGERED_FORMATS,
        300.0,
        5,
        {
            "scaling_enabled": True,
            "synchrony_signed": False,
            "synchrony_surrogate_method": "phase",
            "synchrony_correlation_method": "spearman",
            "synchrony_dtw_enabled": False,
            "synchrony_threshold": 0.4,
            "synchrony_window_seconds": 20.0,
            "synchrony_step_seconds": 4.0,
            "synchrony_episode_min_size": 3,
            "synchrony_episode_min_duration_seconds": 10.0,
            "synchrony_cluster_distance_threshold": 0.8,
            "contagion_min_participants": 3,
            "contagion_high_z": 0.8,
            "contagion_low_z": -0.8,
            "contagion_window_seconds": 20.0,
            "contagion_step_seconds": 2.0,
            "contagion_min_agreement_pct": 0.4,
            "contagion_comovement_rho": 0.1,
        },
    ),
    "surrogates_disabled": (
        FOUR_PARTICIPANTS,
        STAGGERED_FORMATS,
        240.0,
        6,
        {"scaling_enabled": True, "synchrony_surrogate_enabled": False, "contagion_surrogate_count": 0},
    ),
    # Recordings crossing midnight, Polish cp1250 exports with Excel-mangled decimals.
    "midnight_polish_excel": (
        {"A": Participant(tonic_us=3.0), "B": Participant(tonic_us=6.0, lag_seconds=1.0)},
        {
            "A": RecordingFormat(
                start_time="23:58:00:000",
                polish=True,
                encoding="cp1250",
                excel_tokens={50: "3.mar", 51: "mar.05", 52: "1.lut"},
            ),
            "B": RecordingFormat(start_time="23:58:01:200", polish=True, markers_without_ms=(10, 11)),
        },
        240.0,
        7,
        {"scaling_enabled": True, "contagion_min_participants": 2},
    ),
}


@pytest.fixture(scope="module")
def scenario_runs(tmp_path_factory) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for name, (participants, formats, seconds, seed, kwargs) in SCENARIOS.items():
        directory = tmp_path_factory.mktemp(name)
        files = write_group(directory, name, participants, formats, seconds=seconds, seed=seed)
        old_cfg = old_config.PipelineConfig(**kwargs)
        new_cfg = new_config.PipelineConfig(**kwargs)
        runs[name] = {
            "files": files,
            "old_config": old_cfg,
            "new_config": new_cfg,
            "old": old_core.run_pipeline(files, old_cfg),
            "new": new_pre.run_pipeline(files, new_cfg),
        }
    return runs


def test_legacy_snapshot_is_unmodified() -> None:
    for name, expected in LEGACY_SHA256.items():
        assert hashlib.sha256((LEGACY_DIR / name).read_bytes()).hexdigest() == expected, name


def test_shared_config_fields_have_identical_defaults() -> None:
    old_defaults = old_config.PipelineConfig().model_dump()
    new_defaults = new_config.PipelineConfig().model_dump()
    # Settings GSRv2 did not have (the VSR flow chart, Hassan's e-mail) take their defaults from the document values.
    added = set(new_defaults) - set(old_defaults)
    assert added <= set(new_config.VSR_DOCUMENT_SETTINGS)
    assert {key: new_defaults[key] for key in added} == {key: new_config.VSR_DOCUMENT_SETTINGS[key] for key in added}
    assert {key: old_defaults[key] for key in new_defaults if key not in added} == {
        key: value for key, value in new_defaults.items() if key not in added}
    assert new_config.MAX_DATASETS == old_config.MAX_DATASETS


@pytest.mark.parametrize("name", SCENARIOS)
def test_parsing_is_identical(scenario_runs, name: str) -> None:
    for path in scenario_runs[name]["files"].values():
        assert_identical(old_parser.load_esense_csv(path), new_parser.load_esense_csv(path))


@pytest.mark.parametrize("name", SCENARIOS)
def test_cleaning_and_synchronization_are_identical(scenario_runs, name: str) -> None:
    old, new = scenario_runs[name]["old"], scenario_runs[name]["new"]
    for field_name in (
        "aliases",
        "dataset_states",
        "cleaning_summary_by_alias",
        "cleaning_summary_totals",
        "merged_scaled",
        "merged_decimated",
        "used_downsample_factor",
        "alignment",
        "identical_signal_groups",
    ):
        assert_identical(getattr(old, field_name), getattr(new, field_name), field_name)

    for max_points in (None, 50):
        assert_identical(old_core.serialize_preview(old, max_points), new_pre.serialize_preview(new, max_points))
        assert_identical(
            old_core.serialize_decimated_traces(old, max_points), new_pre.serialize_decimated_traces(new, max_points)
        )

    old_cfg, new_cfg = scenario_runs[name]["old_config"], scenario_runs[name]["new_config"]
    for stage in STAGES:
        assert_identical(
            old_core.build_stage_export_frame(old, stage, old_cfg),
            new_pre.build_stage_export_frame(new, stage, new_cfg),
            stage,
        )


@pytest.mark.parametrize("name", SCENARIOS)
def test_joint_synchrony_is_identical(scenario_runs, name: str) -> None:
    run = scenario_runs[name]
    assert_identical(
        old_core.compute_synchrony(run["old"], None, run["old_config"]),
        new_synchrony.compute_synchrony(run["new"], None, run["new_config"]),
    )


@pytest.mark.parametrize("name", SCENARIOS)
def test_coarousal_is_identical(scenario_runs, name: str) -> None:
    run = scenario_runs[name]
    assert_identical(
        old_core.compute_contagion(run["old"], None, run["old_config"]),
        new_coarousal.compute_coarousal(run["new"], None, run["new_config"]),
    )


def test_analyses_with_default_config_and_alias_subset_are_identical(scenario_runs) -> None:
    run = scenario_runs["unsigned_phase_spearman"]
    subset = ["D", "B", "A"]
    assert_identical(
        old_core.compute_synchrony(run["old"], subset, None),
        new_synchrony.compute_synchrony(run["new"], subset, None),
    )
    assert_identical(
        old_core.compute_contagion(run["old"], subset, None),
        new_coarousal.compute_coarousal(run["new"], subset, None),
    )
    with pytest.raises(old_core.PipelineError) as old_error:
        old_core.compute_synchrony(run["old"], ["A", "Z"], None)
    with pytest.raises(PipelineError) as new_error:
        new_synchrony.compute_synchrony(run["new"], ["A", "Z"], None)
    assert str(old_error.value) == str(new_error.value)


def test_identical_recordings_are_reported_identically(tmp_path: Path) -> None:
    trace = np.linspace(3.0, 6.0, 600) + np.sin(np.arange(600) / 15.0)
    files = {
        "A": write_esense_csv(tmp_path / "a.csv", trace),
        "A-copy": write_esense_csv(tmp_path / "a_copy.csv", trace, RecordingFormat(polish=True, encoding="cp1250")),
        "B": write_esense_csv(tmp_path / "b.csv", trace[::-1].copy()),
    }
    old = old_core.run_pipeline(files, old_config.PipelineConfig(scaling_enabled=True))
    new = new_pre.run_pipeline(files, new_config.PipelineConfig(scaling_enabled=True))
    assert new.identical_signal_groups == [["A", "A-copy"]]
    assert_identical(old.identical_signal_groups, new.identical_signal_groups)
    assert_identical(old_core.serialize_preview(old), new_pre.serialize_preview(new))


def _error_message(run, *args) -> str:
    with pytest.raises(ValueError) as error:
        run(*args)
    return str(error.value)


@pytest.mark.parametrize(
    "case",
    ["no_overlap", "empty_time_range", "missing_file", "missing_column", "too_many", "too_short"],
)
def test_pipeline_errors_are_identical(tmp_path: Path, case: str) -> None:
    trace = np.full(300, 5.0) + np.sin(np.arange(300) / 10.0)
    kwargs: dict[str, Any] = {"scaling_enabled": True}
    if case == "no_overlap":
        files = {
            "A": write_esense_csv(tmp_path / "a.csv", trace, RecordingFormat(start_time="10:00:00:000")),
            "B": write_esense_csv(tmp_path / "b.csv", trace, RecordingFormat(start_time="11:00:00:000")),
        }
    elif case == "empty_time_range":
        files = {"A": write_esense_csv(tmp_path / "a.csv", trace), "B": write_esense_csv(tmp_path / "b.csv", trace)}
        kwargs["time_range_from"] = 30.0
    elif case == "missing_file":
        files = {"A": write_esense_csv(tmp_path / "a.csv", trace), "B": tmp_path / "missing.csv"}
    elif case == "missing_column":
        broken = tmp_path / "broken.csv"
        broken.write_text("Device;x\nSECOND;MICROSIEMENS;OTHER\n0,0;5,0;x\n", encoding="utf-8")
        files = {"A": write_esense_csv(tmp_path / "a.csv", trace), "B": broken}
    elif case == "too_many":
        files = {f"S{idx:02d}": tmp_path / f"S{idx:02d}.csv" for idx in range(old_config.MAX_DATASETS + 1)}
    else:  # too_short: a single merged point cannot be analysed
        files = {"A": write_esense_csv(tmp_path / "a.csv", trace[:3])}

    old_message = _error_message(old_core.run_pipeline, files, old_config.PipelineConfig(**kwargs))
    new_message = _error_message(new_pre.run_pipeline, files, new_config.PipelineConfig(**kwargs))
    if case == "too_short":
        # The only intentional wording change: GSRv2 named the dropped segmentation step.
        assert old_message == "Not enough points for segmentation"
        assert new_message == "Not enough points for analysis"
    else:
        assert old_message == new_message
