"""Behavioral tests ported from GSRv2 ``gsr/tests/test_pipeline.py``.

Segment, correlation, trend and report tests were dropped together with those analyses.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from gsr.pipeline.coarousal import compute_coarousal
from gsr.pipeline.common import PipelineError, _make_surrogate
from gsr.pipeline.config import PipelineConfig
from gsr.pipeline.preprocessing import (
    IQR_FACTOR,
    JUMP_SIGMA,
    PHYSIO_MAX_US,
    PHYSIO_MIN_US,
    ZSCORE_THRESHOLD,
    _detect_outlier_masks_pilot,
    _pilot_clean_signal,
    _signal_quality_level,
    build_stage_export_frame,
    run_pipeline,
    serialize_preview,
)
from gsr.pipeline.synchrony import (
    _cluster_signals,
    _detect_sync_episodes,
    _lagged_xcorr,
    _surrogate_xcorr_pvalue,
    _window_active_groups,
    compute_synchrony,
)

from .esense_csv import build_esense_csv


def test_pipeline_rejects_more_than_twenty_datasets(tmp_path: Path) -> None:
    dataset_files = {f"S{i:02d}": tmp_path / f"S{i:02d}.csv" for i in range(21)}

    with pytest.raises(PipelineError, match="Maximum number of datasets per analysis is 20"):
        run_pipeline(dataset_files, PipelineConfig())


def test_pipeline_end_to_end(tmp_path: Path) -> None:
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"
    file_c = tmp_path / "c.csv"

    build_esense_csv(file_a, time_column="TIMESTAMP", rows=240, seed_offset=0.0)
    build_esense_csv(file_b, time_column="ZNACZNIKI CZASU", rows=240, seed_offset=0.7)
    build_esense_csv(file_c, time_column="TIMESTAMP", rows=240, seed_offset=1.3)

    config = PipelineConfig(median_kernel=9, downsample_factor=8, scaling_enabled=True)
    result = run_pipeline({"A": file_a, "B": file_b, "C": file_c}, config)

    assert result.aliases == ["A", "B", "C"]
    assert result.merged_scaled.shape[0] > 0
    assert result.used_downsample_factor == 8
    assert list(result.merged_decimated.columns) == ["time", "marker", "microsiemens_A", "microsiemens_B", "microsiemens_C"]

    synchrony = compute_synchrony(result, None, config)
    assert synchrony["aliases"] == ["A", "B", "C"]
    coarousal = compute_coarousal(result, None, config)
    assert coarousal["aliases"] == ["A", "B", "C"]

    preview = serialize_preview(result)
    assert preview["alignment"]["mode"] == "absolute"
    assert preview["alignment"]["duration_seconds"] > 0
    assert preview["cleaning_summary"]["identical_signal_groups"] == []
    for entry in preview["cleaning_summary"]["per_alias"]:
        assert 0.0 <= entry["artifact_rejected_pct"] <= 100.0
        assert entry["signal_quality"] in {"good", "warning", "poor"}


def test_pipeline_rejects_datasets_without_absolute_time_overlap(tmp_path: Path) -> None:
    file_a = tmp_path / "non-overlap-a.csv"
    file_b = tmp_path / "non-overlap-b.csv"

    build_esense_csv(file_a, rows=240, seed_offset=0.0, start_time="12:00:00:000")
    build_esense_csv(file_b, rows=240, seed_offset=0.7, start_time="14:30:00:000")

    config = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=1)
    with pytest.raises(PipelineError, match="do not share an absolute-time overlap") as exc_info:
        run_pipeline({"A": file_a, "B": file_b}, config)

    assert "A:" in str(exc_info.value)
    assert "B:" in str(exc_info.value)
    # Structured for the UI: the recorded clock range of every participant.
    assert exc_info.value.code == "no_overlap"
    ranges = exc_info.value.details["ranges"]
    assert [start.strftime("%H:%M:%S") for start, _end in ranges.values()] == ["12:00:00", "14:30:00"]


def test_pipeline_warns_about_identical_signals_without_excluding_them(tmp_path: Path) -> None:
    shared_file = tmp_path / "shared-signal.csv"
    build_esense_csv(shared_file, rows=240, seed_offset=0.0)

    result = run_pipeline({"A": shared_file, "A-copy": shared_file}, PipelineConfig(scaling_enabled=True))
    preview = serialize_preview(result)

    assert result.aliases == ["A", "A-copy"]
    assert result.identical_signal_groups == [["A", "A-copy"]]
    assert preview["cleaning_summary"]["identical_signal_groups"] == [["A", "A-copy"]]
    assert len(preview["cleaning_summary"]["per_alias"]) == 2


@pytest.mark.parametrize(
    ("rejected_points", "synced_points", "expected_pct", "expected_quality"),
    [
        (9, 100, 9.0, "good"),
        (10, 100, 10.0, "warning"),
        (29, 100, 29.0, "warning"),
        (30, 100, 30.0, "poor"),
    ],
)
def test_signal_quality_thresholds_are_informational(
    rejected_points: int,
    synced_points: int,
    expected_pct: float,
    expected_quality: str,
) -> None:
    assert _signal_quality_level(rejected_points, synced_points) == (expected_pct, expected_quality)


def test_downsampling_preserves_scaled_samples(tmp_path: Path) -> None:
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"

    build_esense_csv(file_a, rows=240, seed_offset=0.0)
    build_esense_csv(file_b, rows=240, seed_offset=0.5)

    config = PipelineConfig(scaling_enabled=True, downsample_factor=8)
    result = run_pipeline({"A": file_a, "B": file_b}, config)

    assert result.used_downsample_factor == 8

    expected_indexes = list(range(0, len(result.merged_scaled), result.used_downsample_factor))
    if expected_indexes[-1] != len(result.merged_scaled) - 1:
        expected_indexes.append(len(result.merged_scaled) - 1)

    expected = result.merged_scaled.iloc[expected_indexes].reset_index(drop=True)

    np.testing.assert_allclose(
        result.merged_decimated["time"].to_numpy(dtype=float),
        expected["relative_time"].to_numpy(dtype=float),
    )
    assert result.merged_decimated["marker"].astype(str).tolist() == expected["marker"].astype(str).tolist()

    for alias in result.aliases:
        np.testing.assert_allclose(
            result.merged_decimated[f"microsiemens_{alias}"].to_numpy(dtype=float),
            expected[f"microsiemens_{alias}"].to_numpy(dtype=float),
        )


def test_downsampling_interval_seconds_uses_time_based_sampling(tmp_path: Path) -> None:
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"

    build_esense_csv(file_a, rows=240, seed_offset=0.0)
    build_esense_csv(file_b, rows=240, seed_offset=0.5)

    one_hz = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=1)
    two_seconds = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=2)

    result_1s = run_pipeline({"A": file_a, "B": file_b}, one_hz)
    result_2s = run_pipeline({"A": file_a, "B": file_b}, two_seconds)

    assert result_1s.used_downsample_factor == 5
    assert result_2s.used_downsample_factor == 10


def test_downsampling_interval_aggregates_points_within_time_bucket(tmp_path: Path) -> None:
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"

    build_esense_csv(file_a, rows=300, seed_offset=0.0)
    build_esense_csv(file_b, rows=300, seed_offset=0.5)

    config = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=2, median_kernel=0)
    result = run_pipeline({"A": file_a, "B": file_b}, config)

    merged = result.merged_scaled
    interval_minutes = 2.0 / 60.0
    base_time = float(merged["relative_time"].iloc[0])
    bucket = np.floor(((merged["relative_time"].to_numpy(dtype=float) - base_time) / interval_minutes) + 1e-9).astype(int)

    expected_time: list[float] = []
    expected_signal: list[float] = []
    for bucket_id in sorted(set(bucket.tolist())):
        mask = bucket == bucket_id
        expected_time.append(float(np.mean(merged.loc[mask, "relative_time"].to_numpy(dtype=float))))
        expected_signal.append(float(np.mean(merged.loc[mask, "microsiemens_A"].to_numpy(dtype=float))))

    np.testing.assert_allclose(result.merged_decimated["time"].to_numpy(dtype=float), np.asarray(expected_time))
    np.testing.assert_allclose(result.merged_decimated["microsiemens_A"].to_numpy(dtype=float), np.asarray(expected_signal))


def test_default_downsample_uses_interval_mode_for_partial_config(tmp_path: Path) -> None:
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"

    build_esense_csv(file_a, rows=1200, seed_offset=0.0)
    build_esense_csv(file_b, rows=1200, seed_offset=0.5)

    # Simulates API payload that only sets scaling_enabled.
    config = PipelineConfig(scaling_enabled=True)
    result = run_pipeline({"A": file_a, "B": file_b}, config)

    # 1s interval default over 5Hz source should result in ~factor 5.
    assert result.used_downsample_factor == 5


def test_time_range_does_not_recompute_clean_signal_preview(tmp_path: Path) -> None:
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"

    build_esense_csv(file_a, rows=240, seed_offset=0.0)
    build_esense_csv(file_b, rows=240, seed_offset=0.7)

    base_config = PipelineConfig(median_kernel=9)
    ranged_config = PipelineConfig(median_kernel=9, time_range_from=0.5, time_range_to=0.7)

    full_result = run_pipeline({"A": file_a, "B": file_b}, base_config)
    ranged_result = run_pipeline({"A": file_a, "B": file_b}, ranged_config)

    assert np.allclose(
        full_result.dataset_states["A"].clean["microsiemens"].to_numpy(),
        ranged_result.dataset_states["A"].clean["microsiemens"].to_numpy(),
    )

    full_preview = serialize_preview(full_result)
    ranged_preview = serialize_preview(ranged_result)
    assert full_preview["range"] == ranged_preview["range"]

    assert float(ranged_result.merged_scaled["relative_time"].min()) >= 0.5
    assert float(ranged_result.merged_scaled["relative_time"].max()) <= 0.7


def test_pipeline_replaces_outliers_before_analysis(tmp_path: Path) -> None:
    file_a = tmp_path / "outlier_a.csv"
    file_b = tmp_path / "outlier_b.csv"

    build_esense_csv(file_a, rows=220, seed_offset=0.0)
    build_esense_csv(file_b, rows=220, seed_offset=0.4)

    lines = file_a.read_text(encoding="utf-8").splitlines()
    data_start_index = next(index for index, line in enumerate(lines) if "MICROSIEMENS" in line and ";" in line) + 1
    outlier_row = data_start_index + 40
    parts = lines[outlier_row].split(";")
    parts[1] = "99,99"
    lines[outlier_row] = ";".join(parts)
    file_a.write_text("\n".join(lines), encoding="utf-8")

    config = PipelineConfig(median_kernel=0, scaling_enabled=False)
    result = run_pipeline({"A": file_a, "B": file_b}, config)

    clean_max = float(result.dataset_states["A"].clean["microsiemens"].max())
    assert clean_max < 20.0


def test_pipeline_can_disable_artifact_filters(tmp_path: Path) -> None:
    file_a = tmp_path / "toggle_a.csv"
    file_b = tmp_path / "toggle_b.csv"

    build_esense_csv(file_a, rows=220, seed_offset=0.0)
    build_esense_csv(file_b, rows=220, seed_offset=0.4)

    lines = file_a.read_text(encoding="utf-8").splitlines()
    data_start_index = next(index for index, line in enumerate(lines) if "MICROSIEMENS" in line and ";" in line) + 1
    outlier_row = data_start_index + 40
    parts = lines[outlier_row].split(";")
    parts[1] = "99,99"
    lines[outlier_row] = ";".join(parts)
    file_a.write_text("\n".join(lines), encoding="utf-8")

    default_config = PipelineConfig(median_kernel=0, scaling_enabled=False, clean_filter_butterworth=False)
    all_disabled_config = PipelineConfig(
        median_kernel=0,
        scaling_enabled=False,
        clean_filter_physiological_range=False,
        clean_filter_zscore=False,
        clean_filter_jump=False,
        clean_filter_iqr=False,
        clean_filter_butterworth=False,
    )

    default_result = run_pipeline({"A": file_a, "B": file_b}, default_config)
    disabled_result = run_pipeline({"A": file_a, "B": file_b}, all_disabled_config)

    default_artifacts = default_result.cleaning_summary_by_alias["A"].artifact_rejected_points
    disabled_artifacts = disabled_result.cleaning_summary_by_alias["A"].artifact_rejected_points
    assert default_artifacts > disabled_artifacts

    default_max = float(default_result.dataset_states["A"].clean["microsiemens"].max())
    disabled_max = float(disabled_result.dataset_states["A"].clean["microsiemens"].max())
    assert default_max < 20.0
    assert disabled_max > 20.0


def test_cleaning_defaults_match_standalone_script_profile() -> None:
    config = PipelineConfig()
    assert config.median_kernel == 5
    assert config.clean_filter_iqr is False
    assert config.clean_filter_physiological_min_us == PHYSIO_MIN_US
    assert config.clean_filter_physiological_max_us == PHYSIO_MAX_US
    assert config.clean_filter_zscore_threshold == ZSCORE_THRESHOLD
    assert config.clean_filter_jump_sigma == JUMP_SIGMA
    assert config.clean_filter_iqr_factor == IQR_FACTOR

    rng = np.random.default_rng(7)
    values = np.concatenate([rng.normal(5.0, 0.5, 200), [30.0, 0.1]])
    default_masks = _detect_outlier_masks_pilot(values, PipelineConfig())
    legacy_masks = _detect_outlier_masks_pilot(values, None)
    for key in default_masks:
        assert np.array_equal(default_masks[key], legacy_masks[key]), key


def test_from_dict_legacy_payload_fills_threshold_defaults() -> None:
    legacy_payload = {"median_kernel": 5, "clean_filter_zscore": False, "unknown_key": 1}
    config = PipelineConfig.from_dict(legacy_payload)
    assert config.clean_filter_physiological_min_us == PHYSIO_MIN_US
    assert config.clean_filter_zscore_threshold == ZSCORE_THRESHOLD
    assert config.clean_filter_butterworth_cutoff_hz == 0.05
    assert config.clean_filter_butterworth_order == 4


def test_custom_thresholds_change_outlier_detection() -> None:
    rng = np.random.default_rng(11)
    values = rng.normal(5.0, 0.5, 300)
    values[50] = 8.0
    values[120] = 19.0
    values[200] = 2.2

    default_masks = _detect_outlier_masks_pilot(values, PipelineConfig())

    narrow_physio = _detect_outlier_masks_pilot(
        values, PipelineConfig(clean_filter_physiological_min_us=4.0, clean_filter_physiological_max_us=6.0)
    )
    assert narrow_physio["physical"].sum() > default_masks["physical"].sum()

    strict_zscore = _detect_outlier_masks_pilot(values, PipelineConfig(clean_filter_zscore_threshold=1.0))
    assert strict_zscore["zscore"].sum() > default_masks["zscore"].sum()

    strict_jump = _detect_outlier_masks_pilot(values, PipelineConfig(clean_filter_jump_sigma=0.5))
    assert strict_jump["jump"].sum() > default_masks["jump"].sum()

    loose_iqr = _detect_outlier_masks_pilot(values, PipelineConfig(clean_filter_iqr_factor=9.0))
    strict_iqr = _detect_outlier_masks_pilot(values, PipelineConfig(clean_filter_iqr_factor=0.5))
    assert strict_iqr["iqr"].sum() > loose_iqr["iqr"].sum()


def test_butterworth_thresholds_change_smoothing() -> None:
    rng = np.random.default_rng(13)
    values = 5.0 + 0.5 * np.sin(np.linspace(0, 20, 400)) + rng.normal(0.0, 0.2, 400)

    base_config = PipelineConfig(scaling_enabled=False)
    default_cleaned, _ = _pilot_clean_signal(values, base_config)
    softer_cleaned, _ = _pilot_clean_signal(
        values, PipelineConfig(scaling_enabled=False, clean_filter_butterworth_cutoff_hz=1.0)
    )
    assert not np.allclose(default_cleaned, softer_cleaned)
    # A higher cutoff keeps more of the fast variation, so it tracks the raw signal closer.
    assert np.abs(softer_cleaned - values).mean() < np.abs(default_cleaned - values).mean()

    higher_order, _ = _pilot_clean_signal(
        values, PipelineConfig(scaling_enabled=False, clean_filter_butterworth_order=8)
    )
    assert not np.allclose(default_cleaned, higher_order)


def test_preview_serialization_contains_cleaning_summary(tmp_path: Path) -> None:
    file_a = tmp_path / "summary_a.csv"
    file_b = tmp_path / "summary_b.csv"

    build_esense_csv(file_a, rows=240, seed_offset=0.0)
    build_esense_csv(file_b, rows=240, seed_offset=0.5)

    config = PipelineConfig(
        median_kernel=5,
        scaling_enabled=True,
        downsample_factor=8,
        time_range_from=0.4,
        time_range_to=0.9,
    )
    result = run_pipeline({"A": file_a, "B": file_b}, config)
    preview = serialize_preview(result)

    assert "cleaning_summary" in preview
    summary = preview["cleaning_summary"]
    assert {"per_alias", "totals"}.issubset(summary.keys())
    assert len(summary["per_alias"]) == 2

    totals = summary["totals"]
    assert totals["synced_points"] >= totals["in_time_window_points"]
    assert totals["synced_points"] >= totals["outside_time_window_points"]
    assert totals["merged_points_before_downsampling"] >= totals["merged_points_after_downsampling"]
    assert (
        totals["merged_points_before_downsampling"] - totals["merged_points_after_downsampling"]
        == totals["downsampled_points"]
    )


def test_stage_export_frame_uses_wide_columns_and_unique_alias_suffixes(tmp_path: Path) -> None:
    file_a = tmp_path / "stage_a.csv"
    file_b = tmp_path / "stage_b.csv"

    build_esense_csv(file_a, rows=240, seed_offset=0.0)
    build_esense_csv(file_b, rows=220, seed_offset=0.4)

    config = PipelineConfig(scaling_enabled=True)
    result = run_pipeline({"Pacjent 1": file_a, "Pacjent-1": file_b}, config)

    export_frame = build_stage_export_frame(result, "raw", config)

    assert len(export_frame) == max(len(result.dataset_states["Pacjent 1"].raw), len(result.dataset_states["Pacjent-1"].raw))
    assert list(export_frame.columns) == [
        "row_index",
        "timestamp_Pacjent_1",
        "relative_time_minutes_Pacjent_1",
        "microsiemens_Pacjent_1",
        "timestamp_Pacjent_1_2",
        "relative_time_minutes_Pacjent_1_2",
        "microsiemens_Pacjent_1_2",
    ]
    assert export_frame["row_index"].iloc[0] == 0
    assert export_frame["relative_time_minutes_Pacjent_1"].iloc[0] == 0.0


def test_stage_export_clean_and_scaled_respect_time_range_without_downsampling(tmp_path: Path) -> None:
    file_a = tmp_path / "range_a.csv"
    file_b = tmp_path / "range_b.csv"

    build_esense_csv(file_a, rows=600, seed_offset=0.0)
    build_esense_csv(file_b, rows=600, seed_offset=0.5)

    config = PipelineConfig(
        scaling_enabled=True,
        downsample_interval_seconds=10,
        time_range_from=0.4,
        time_range_to=1.2,
    )
    result = run_pipeline({"A": file_a, "B": file_b}, config)

    clean_export = build_stage_export_frame(result, "clean", config)
    scaled_export = build_stage_export_frame(result, "scaled", config)

    clean_relative = clean_export["relative_time_minutes_A"].dropna()
    scaled_relative = scaled_export["relative_time_minutes_A"].dropna()

    assert float(clean_relative.min()) >= 0.4
    assert float(clean_relative.max()) <= 1.2
    assert float(scaled_relative.min()) >= 0.4
    assert float(scaled_relative.max()) <= 1.2

    expected_scaled_rows = max(len(result.dataset_states[alias].scaled) for alias in result.aliases)
    assert len(scaled_export) == expected_scaled_rows
    assert len(scaled_export) > len(result.merged_decimated)


def _analysis_result(tmp_path: Path):
    files = {}
    for idx, alias in enumerate(["A", "B", "C"]):
        path = tmp_path / f"{alias}.csv"
        build_esense_csv(path, rows=360, seed_offset=idx * 0.6)
        files[alias] = path
    config = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=1)
    return run_pipeline(files, config), config


def test_compute_synchrony_structure_and_lag_detection(tmp_path: Path) -> None:
    result, config = _analysis_result(tmp_path)
    payload = compute_synchrony(result, None, config)

    assert set(payload) >= {"aliases", "fs", "pairs", "sync_matrix", "windows", "episodes", "clusters", "stats"}
    assert "onsets" not in payload and "onset_count" not in payload["stats"]
    n = len(payload["aliases"])
    assert len(payload["sync_matrix"]) == n
    assert len(payload["pairs"]) == n * (n - 1) // 2
    pair = payload["pairs"][0]
    assert {"alias_a", "alias_b", "max_xcorr", "lag_seconds", "coherence", "dtw_distance"} <= set(pair)
    assert -1.0 <= pair["max_xcorr"] <= 1.0
    assert pair["dtw_distance"] is not None  # DTW enabled by default
    assert payload["clusters"]["cluster_count"] >= 1
    assert set(payload["windows"]) == {"centers", "group_mean", "threshold", "active_counts", "pairs"}
    assert len(payload["windows"]["active_counts"]) == len(payload["windows"]["centers"])
    assert isinstance(payload["episodes"], list)
    assert payload["stats"]["episode_count"] == len(payload["episodes"])
    assert payload["stats"]["max_episode_size"] >= 0
    for episode in payload["episodes"]:
        assert set(episode) == {
            "start_relative", "end_relative", "members", "size", "mean_strength", "peak_strength"
        }
        assert episode["size"] == len(episode["members"]) >= 2


STEP_MINUTES = 5.0 / 60.0  # 5-second windows step used by the episode unit tests


def _episode_inputs(
    active: dict[tuple[int, int], list[tuple[int, int]]],
    n_windows: int,
    n_pairs_aliases: list[str],
    high: float = 0.9,
    low: float = 0.1,
) -> tuple[dict[tuple[int, int], list[float]], list[float]]:
    """Build pair_series where each pair is `high` inside its active window ranges
    (inclusive) and `low` elsewhere."""
    n = len(n_pairs_aliases)
    centers = [idx * STEP_MINUTES for idx in range(n_windows)]
    pair_series: dict[tuple[int, int], list[float]] = {}
    for i in range(n):
        for j in range(i + 1, n):
            ranges = active.get((i, j), [])
            pair_series[(i, j)] = [
                high if any(start <= idx <= end for start, end in ranges) else low
                for idx in range(n_windows)
            ]
    return pair_series, centers


def test_sync_episodes_detects_subgroup_missed_by_group_mean() -> None:
    aliases = ["A", "B", "C"]
    # Only A-B synchronize (windows 12..36); group mean over all 3 pairs stays
    # at ~0.37 < 0.6, so the legacy group-mean onset would miss this stretch.
    pair_series, centers = _episode_inputs({(0, 1): [(12, 36)]}, 50, aliases)
    config = PipelineConfig(synchrony_threshold=0.6)

    episodes, active_counts = _detect_sync_episodes(pair_series, centers, aliases, config)

    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["members"] == ["A", "B"]
    assert episode["size"] == 2
    assert episode["start_relative"] == pytest.approx(centers[12])
    assert episode["end_relative"] == pytest.approx(centers[36])
    assert episode["mean_strength"] == pytest.approx(0.9)
    assert episode["peak_strength"] == pytest.approx(0.9)
    assert active_counts[12] == 2 and active_counts[36] == 2
    assert active_counts[0] == 0 and active_counts[40] == 0
    group_mean = np.mean([abs(pair_series[pair][20]) for pair in pair_series])
    assert group_mean < 0.6


def test_sync_episodes_bridges_short_gaps_only() -> None:
    aliases = ["A", "B"]
    active = {(0, 1): [(0, 10), (12, 22)]}  # window 11 dips below threshold
    config_bridge = PipelineConfig(synchrony_threshold=0.6, synchrony_episode_max_gap_seconds=5.0)
    config_strict = PipelineConfig(synchrony_threshold=0.6, synchrony_episode_max_gap_seconds=0.0)
    pair_series, centers = _episode_inputs(active, 30, aliases)

    bridged, _ = _detect_sync_episodes(pair_series, centers, aliases, config_bridge)
    split, _ = _detect_sync_episodes(pair_series, centers, aliases, config_strict)

    assert len(bridged) == 1
    assert bridged[0]["start_relative"] == pytest.approx(centers[0])
    assert bridged[0]["end_relative"] == pytest.approx(centers[22])
    assert len(split) == 2
    assert split[0]["end_relative"] == pytest.approx(centers[10])
    assert split[1]["start_relative"] == pytest.approx(centers[12])


def test_sync_episodes_split_on_membership_change() -> None:
    aliases = ["A", "B", "C"]
    # A+B synchronized throughout; C joins from window 21 (all pairs active).
    active = {
        (0, 1): [(0, 40)],
        (0, 2): [(21, 40)],
        (1, 2): [(21, 40)],
    }
    pair_series, centers = _episode_inputs(active, 41, aliases)
    config = PipelineConfig(synchrony_threshold=0.6)

    episodes, active_counts = _detect_sync_episodes(pair_series, centers, aliases, config)

    assert [episode["members"] for episode in episodes] == [["A", "B"], ["A", "B", "C"]]
    assert episodes[0]["end_relative"] == pytest.approx(centers[20])
    assert episodes[1]["start_relative"] == pytest.approx(centers[21])
    assert active_counts[20] == 2 and active_counts[21] == 3


def test_sync_episodes_filters_by_min_duration_and_min_size() -> None:
    aliases = ["A", "B", "C"]
    active = {
        (0, 1): [(0, 1)],     # ~5 s span < 10 s minimum -> dropped
        (0, 2): [(10, 30)],   # long enough, but pair episodes dropped at min_size=3
    }
    pair_series, centers = _episode_inputs(active, 40, aliases)

    default_config = PipelineConfig(synchrony_threshold=0.6)
    episodes, _ = _detect_sync_episodes(pair_series, centers, aliases, default_config)
    assert [episode["members"] for episode in episodes] == [["A", "C"]]

    trio_config = PipelineConfig(synchrony_threshold=0.6, synchrony_episode_min_size=3)
    episodes_trio, active_counts_trio = _detect_sync_episodes(pair_series, centers, aliases, trio_config)
    assert episodes_trio == []
    assert max(active_counts_trio) == 0


def test_compute_synchrony_clusters_identical_signals_together(tmp_path: Path) -> None:
    # Two aliases from the same source file should be near-perfectly synchronized.
    shared = tmp_path / "shared.csv"
    other = tmp_path / "other.csv"
    build_esense_csv(shared, rows=360, seed_offset=0.0)
    build_esense_csv(other, rows=360, seed_offset=3.0)
    config = PipelineConfig(scaling_enabled=True, synchrony_cluster_distance_threshold=0.3)
    result = run_pipeline({"A": shared, "Acopy": shared, "B": other}, config)

    payload = compute_synchrony(result, None, config)
    labels = payload["clusters"]["labels"]
    assert labels["A"] == labels["Acopy"]


def test_lagged_xcorr_reports_zero_lag_alongside_max() -> None:
    rng = np.random.default_rng(3)
    x = np.cumsum(rng.normal(0, 1, 200))
    x = (x - x.mean()) / (x.std() + 1e-12)
    max_xcorr, lag_seconds, zero_lag = _lagged_xcorr(x, x, fs=1.0, max_lag_seconds=10.0)
    # A signal against itself peaks at lag 0 with correlation ~1.
    assert max_xcorr == pytest.approx(1.0, abs=1e-6)
    assert zero_lag == pytest.approx(1.0, abs=1e-6)
    assert lag_seconds == pytest.approx(0.0, abs=1e-9)


def test_lagged_xcorr_magnitude_mode_detects_antiphase_signal() -> None:
    rng = np.random.default_rng(31)
    x = rng.normal(size=200)
    x = (x - x.mean()) / x.std()

    value, lag_seconds, _ = _lagged_xcorr(x, -x, fs=1.0, max_lag_seconds=0.0, magnitude=True)

    assert value == pytest.approx(-1.0, abs=1e-6)
    assert lag_seconds == 0.0


def test_make_surrogate_preserves_distribution_and_length() -> None:
    rng = np.random.default_rng(5)
    series = np.cumsum(rng.normal(0, 1, 128))
    circular = _make_surrogate(series, np.random.default_rng(1), "circular")
    phase = _make_surrogate(series, np.random.default_rng(1), "phase")
    # Circular shift is a permutation of the same values.
    np.testing.assert_allclose(np.sort(circular), np.sort(series))
    # Phase randomisation keeps length and (approximately) variance/power.
    assert phase.shape == series.shape
    assert np.std(phase) == pytest.approx(np.std(series), rel=0.2)


def test_surrogate_pvalue_is_deterministic_for_a_seed() -> None:
    rng = np.random.default_rng(9)
    x = (lambda a: (a - a.mean()) / (a.std() + 1e-12))(np.cumsum(rng.normal(0, 1, 200)))
    y = (lambda a: (a - a.mean()) / (a.std() + 1e-12))(np.cumsum(rng.normal(0, 1, 200)))
    config = PipelineConfig(synchrony_surrogate_count=100, synchrony_surrogate_method="circular")
    first = _surrogate_xcorr_pvalue(x, y, 1.0, config, np.random.default_rng(42))
    second = _surrogate_xcorr_pvalue(x, y, 1.0, config, np.random.default_rng(42))
    assert first == second
    assert 0.0 < first["p_value"] <= 1.0


def test_surrogate_batches_honor_cancellation_callback() -> None:
    rng = np.random.default_rng(19)
    x = rng.normal(size=120)
    y = rng.normal(size=120)
    checks = 0

    def cancel() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("canceled")

    config = PipelineConfig(synchrony_surrogate_count=100)
    with pytest.raises(RuntimeError, match="canceled"):
        _surrogate_xcorr_pvalue(x, y, 1.0, config, np.random.default_rng(4), cancel)


def test_signed_clustering_separates_antiphase_pairs() -> None:
    aliases = ["A", "B", "C"]
    # A,B in phase; C anti-phase with both.
    sync_matrix = np.array(
        [
            [1.0, 0.9, -0.9],
            [0.9, 1.0, -0.9],
            [-0.9, -0.9, 1.0],
        ]
    )
    signed = _cluster_signals(
        aliases,
        sync_matrix,
        PipelineConfig(synchrony_signed=True, synchrony_cluster_distance_threshold=1.5),
    )
    magnitude = _cluster_signals(
        aliases,
        sync_matrix,
        PipelineConfig(synchrony_signed=False, synchrony_cluster_distance_threshold=0.5),
    )

    labels_signed = signed["labels"]
    assert labels_signed["A"] == labels_signed["B"]
    assert labels_signed["A"] != labels_signed["C"]  # anti-phase excluded when signed
    # Legacy magnitude mode treats anti-correlation as synchrony → all together.
    labels_magnitude = magnitude["labels"]
    assert labels_magnitude["A"] == labels_magnitude["B"] == labels_magnitude["C"]


def test_window_active_groups_respects_sign() -> None:
    values = {(0, 1): -0.9}
    signed = _window_active_groups(values, 2, threshold=0.6, min_size=2, signed=True)
    magnitude = _window_active_groups(values, 2, threshold=0.6, min_size=2, signed=False)
    assert signed == []  # anti-phase pair is not a synchronized group
    assert len(magnitude) == 1 and magnitude[0] == frozenset({0, 1})


def test_window_active_groups_returns_overlapping_maximal_cliques() -> None:
    # AB and BC clear the signed threshold, while AC is strongly anti-phase.
    values = {(0, 1): 0.9, (1, 2): 0.9, (0, 2): -0.9}

    groups = _window_active_groups(values, 3, threshold=0.6, min_size=2, signed=True)

    assert groups == [frozenset({0, 1}), frozenset({1, 2})]


def _contagion_result(tmp_path: Path):
    files = {}
    for idx, alias in enumerate(["A", "B", "C"]):
        path = tmp_path / f"cg_{alias}.csv"
        build_esense_csv(path, rows=600, seed_offset=idx * 0.5)
        files[alias] = path
    config = PipelineConfig(scaling_enabled=True, downsample_interval_seconds=1, contagion_surrogate_count=50)
    return run_pipeline(files, config), config


def test_compute_coarousal_payload_shape_and_determinism(tmp_path: Path) -> None:
    result, config = _contagion_result(tmp_path)
    payload = compute_coarousal(result, None, config)

    assert set(payload) >= {
        "aliases", "state_percentages", "episodes", "primary_timeline",
        "state_timeline", "coarousal_index", "coarousal_p_value", "stats",
    }
    assert [row["alias"] for row in payload["state_percentages"]] == ["A", "B", "C"]
    for row in payload["state_percentages"]:
        total = row["high_pct"] + row["neutral_pct"] + row["low_pct"]
        assert total == pytest.approx(100.0, abs=1e-6)
    assert payload["coarousal_p_value"] is None or 0.0 <= payload["coarousal_p_value"] <= 1.0
    for episode in payload["episodes"]:
        assert set(episode) >= {"direction", "start_s", "end_s", "duration_s", "contributors", "non_contributors"}
        assert episode["direction"] in {"High", "Low", "Co-movement"}

    # Same seed → identical surrogate result (reproducible).
    again = compute_coarousal(result, None, config)
    assert again["coarousal_p_value"] == payload["coarousal_p_value"]


def test_coarousal_analyzes_exactly_one_window_and_keeps_partial_heatmap_bin(tmp_path: Path) -> None:
    result, _ = _contagion_result(tmp_path)
    # 61 one-second samples: one complete 61-second analysis window and a
    # trailing one-sample heatmap bin after the first 60 seconds.
    frame = result.merged_decimated.iloc[:61].copy().reset_index(drop=True)
    frame["time"] = np.arange(61, dtype=float) / 60.0
    result.merged_decimated = frame
    config = PipelineConfig(
        scaling_enabled=True,
        contagion_window_seconds=61,
        contagion_step_seconds=61,
        contagion_comovement_rho=-1.0,
        contagion_surrogate_count=0,
    )

    payload = compute_coarousal(result, None, config)

    assert payload["stats"]["comovement_episode_count"] == 1
    assert len(payload["state_timeline"]["times_min"]) == 2
    assert all(len(row["states"]) == 2 for row in payload["state_timeline"]["per_alias"])


def test_preview_signal_statistics_cover_raw_and_clean_full_data(tmp_path: Path) -> None:
    file_a = tmp_path / "a.csv"
    file_b = tmp_path / "b.csv"
    build_esense_csv(file_a, rows=240, seed_offset=0.0)
    build_esense_csv(file_b, rows=240, seed_offset=0.7)

    result = run_pipeline({"A": file_a, "B": file_b}, PipelineConfig(median_kernel=9, downsample_factor=8))
    preview = serialize_preview(result, max_points=20)

    statistics = preview["cleaning_summary"]["signal_statistics"]
    assert [entry["alias"] for entry in statistics] == ["A", "B"]
    for entry in statistics:
        state = result.dataset_states[entry["alias"]]
        for variant, frame in (("raw", state.raw), ("clean", state.clean)):
            values = frame["microsiemens"].dropna()
            stats = entry[variant]
            # Computed on full data, not on the (here 20-point) preview sample.
            assert stats["count"] == len(values) > 20
            assert stats["mean"] == pytest.approx(float(values.mean()))
            assert stats["std"] == pytest.approx(float(values.std(ddof=1)))
            assert stats["min"] == pytest.approx(float(values.min()))
            assert stats["median"] == pytest.approx(float(values.median()))
            assert stats["max"] == pytest.approx(float(values.max()))
