from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from gsr.pipeline.parser import ParseError, detect_skiprows, load_esense_csv

from .esense_csv import build_esense_csv


def test_detect_skiprows_and_parse_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "sample_timestamp.csv"
    build_esense_csv(path, time_column="TIMESTAMP", rows=120)

    skiprows = detect_skiprows(path)
    assert skiprows > 0

    frame, metadata = load_esense_csv(path)
    assert len(frame) == metadata["row_count"]
    assert metadata["time_column"] == "TIMESTAMP"
    assert frame["microsiemens"].dtype.kind in {"f", "i"}


def test_parse_polish_time_column(tmp_path: Path) -> None:
    path = tmp_path / "sample_polish.csv"
    build_esense_csv(path, time_column="ZNACZNIKI CZASU", rows=100)

    frame, metadata = load_esense_csv(path)
    assert metadata["time_column"] == "ZNACZNIKI CZASU"
    assert frame["marker"].iloc[5] != "Wstrzymane"


def test_reconstructs_subsecond_time_from_second_column(tmp_path: Path) -> None:
    path = tmp_path / "seconds_reconstruction.csv"
    lines = [
        "Device: test;",
        "DESCRIPTION;VALUE",
        *[f"META_{idx};value" for idx in range(28)],
        "STATISTICS;;;;;",
        "SECOND;MICROSIEMENS;TIMESTAMP;SCR;SCR/MIN;MARKER",
        "0.0;8,00;12:00:00:000;0;0;",
        "0.2;8,10;12:00:00;0;0;",
        "0.4;8,20;12:00:00;0;0;",
        "1.0;8,30;12:00:01;0;0;",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    frame, _ = load_esense_csv(path)
    base = pd.Timestamp(frame["time"].iloc[0])
    diffs = (frame["time"] - base).dt.total_seconds().round(3).tolist()

    assert diffs[0] == 0.0
    assert diffs[1] == 0.2
    assert diffs[2] == 0.4
    assert diffs[3] == 1.0


def test_loads_windows_encoded_csv_with_micro_sign_in_metadata(tmp_path: Path) -> None:
    path = tmp_path / "windows_encoded.csv"
    lines = [
        "Device: test;",
        "MAXIMUM;25.05 µS",
        "STATISTICS;;;;;",
        "SECOND;MICROSIEMENS;TIMESTAMP;SCR;SCR/MIN;MARKER",
        "0;7,76;11:39:56:124;0;0;",
        "0,2;7,71;11:39:56:324;0;0;",
        "0,4;7,68;11:39:56:524;0;0;",
    ]
    path.write_text("\n".join(lines), encoding="cp1252")

    frame, metadata = load_esense_csv(path)

    assert len(frame) == 3
    assert metadata["row_count"] == 3
    assert metadata["time_column"] == "TIMESTAMP"
    assert str(metadata["encoding"]) in {"cp1250", "cp1252", "latin-1"}
    assert frame["second"].tolist() == [0.0, 0.2, 0.4]


def _write_timestamp_rows(path: Path, timestamps: list[str]) -> None:
    lines = [
        "Device: test;",
        "STATISTICS;;;;;",
        "SECOND;MICROSIEMENS;TIMESTAMP;SCR;SCR/MIN;MARKER",
        *[f"{index};8,0;{timestamp};0;0;" for index, timestamp in enumerate(timestamps)],
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_parser_preserves_order_across_midnight(tmp_path: Path) -> None:
    path = tmp_path / "midnight.csv"
    _write_timestamp_rows(path, ["23:59:59", "00:00:00", "00:00:01"])

    frame, _ = load_esense_csv(path)

    assert frame["marker"].tolist() == ["23:59:59", "00:00:00", "00:00:01"]
    assert frame["time"].is_monotonic_increasing
    assert (frame["time"].iloc[-1] - frame["time"].iloc[0]).total_seconds() == 2


def test_parser_rejects_non_midnight_backward_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "backward.csv"
    _write_timestamp_rows(path, ["12:00:00", "11:59:59", "12:00:01"])

    with pytest.raises(ParseError, match="Non-monotonic timestamp"):
        load_esense_csv(path)


def test_repairs_excel_mangled_decimals_and_english_paused_marker(tmp_path: Path) -> None:
    path = tmp_path / "excel_mangled.csv"
    lines = [
        "Device: test;",
        "STATISTICS;;;;;",
        "SECOND;MICROSIEMENS;TIMESTAMP;SCR;SCR/MIN;MARKER",
        "0.0;0.64;10:24:47:858;0;0;",
        "0.2;sty.85;Paused;0;0;",
        "0.4;mar.79;10:24:48:258;0;0;",
        "0.6;lis.58;10:24:48:458;0;0;",
        "0.8;1.lut;10:24:48:658;0;0;",
        "1.0;3.pa\u017a;10:24:48:858;0;0;",
        "1.lut;25.mar;10:24:49:058;0;0;",
        "1.kwi;17.60;10:24:49:258;0;0;",
        "1.cze;3.paX;10:24:49:458;0;0;",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    frame, metadata = load_esense_csv(path)

    assert frame["microsiemens"].round(2).tolist() == [0.64, 1.85, 3.79, 11.58, 1.02, 3.10, 25.03, 17.60, 3.10]
    assert frame["second"].round(1).tolist() == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6]
    assert metadata["row_count"] == 9
    assert metadata["excel_repaired_values"] == 7
    # "Paused" is treated like "Wstrzymane": the previous timestamp is carried forward.
    assert frame["marker"].iloc[1] == "10:24:47:858"


def test_clean_files_report_no_excel_repairs(tmp_path: Path) -> None:
    path = tmp_path / "clean.csv"
    build_esense_csv(path, rows=50)
    _, metadata = load_esense_csv(path)
    assert "excel_repaired_values" not in metadata
