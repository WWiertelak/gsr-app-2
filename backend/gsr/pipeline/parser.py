from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

SUPPORTED_TIME_COLUMNS = ("ZNACZNIKI CZASU", "TIMESTAMP")
SUPPORTED_CSV_ENCODINGS = ("utf-8", "utf-8-sig", "cp1250", "cp1252", "latin-1")
logger = logging.getLogger(__name__)


class ParseError(ValueError):
    """An unreadable eSense CSV; ``code`` names the reason and ``row`` the data row, if any.

    The message names the stored file; the UI shows a message built from ``code`` instead.
    """

    def __init__(self, message: str, *, code: str | None = None, row: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.row = row


# Excel (Polish or English locale) rewrites decimals such as "3.79" as dates
# ("mar.79" = March 1979, "1.lut" = 1 February) when a CSV is opened and saved.
# The mapping is deterministic, so it can be reversed.
_MONTH_NUMBERS = {
    "sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6,
    "lip": 7, "sie": 8, "wrz": 9, "paz": 10, "lis": 11, "gru": 12,
    "jan": 1, "feb": 2, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# "paź" survives Excel with several byte encodings (UTF-8, Mac CE 0x90, cp1250 0x9F);
# normalise every "pa?" spelling to "paz" before looking the month up.
_PAZ_PATTERN = re.compile(r"pa[^\d.\-/\s]{1,2}(?=$|[.\-/\s])")
_EXCEL_DATE_PATTERN = re.compile(
    r"^(?:(?P<day>\d{1,2})[.\-/\s](?P<day_month>[a-z]{3})|(?P<month>[a-z]{3})[.\-/\s](?P<year>\d{2}))$"
)
_PLAIN_DECIMAL_PATTERN = re.compile(r"^-?\d+\.(\d+)$")


def _infer_decimals(values: pd.Series, default: int = 2) -> int:
    lengths = values.str.extract(_PLAIN_DECIMAL_PATTERN, expand=False).dropna().str.len()
    if lengths.empty:
        return default
    return int(lengths.mode().iloc[0])


def _excel_date_to_number(token: str, decimals: int) -> float | None:
    normalized = _PAZ_PATTERN.sub("paz", token.strip().lower())
    match = _EXCEL_DATE_PATTERN.match(normalized)
    if match is None:
        return None
    if match.group("day") is not None:
        month = _MONTH_NUMBERS.get(match.group("day_month"))
        if month is None:
            return None
        # "1.lut" came from 1.02 (two decimals) or 1.2 (one decimal).
        return float(match.group("day")) + month / (10**decimals)
    month = _MONTH_NUMBERS.get(match.group("month"))
    if month is None:
        return None
    # "mar.79" came from 3.79: a two-digit "year" is always the two-decimal fraction.
    return month + int(match.group("year")) / 100


def repair_excel_dates(values: pd.Series) -> tuple[pd.Series, int]:
    """Convert Excel-mangled month tokens back to numbers; returns (values, repaired_count)."""
    numeric = pd.to_numeric(values, errors="coerce")
    candidates = numeric.isna() & values.notna() & values.str.contains(r"[a-zA-Z]", regex=True)
    if not candidates.any():
        return numeric, 0
    decimals = _infer_decimals(values[~candidates])
    repaired = values[candidates].map(lambda token: _excel_date_to_number(str(token), decimals))
    repaired = pd.to_numeric(repaired, errors="coerce")
    numeric = numeric.copy()
    numeric.loc[repaired.index] = repaired
    return numeric, int(repaired.notna().sum())


def detect_skiprows(path: Path, max_scan_lines: int = 80) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for idx, line in enumerate(handle):
            if idx >= max_scan_lines:
                break
            if "MICROSIEMENS" in line and ";" in line:
                return idx
    raise ParseError(f"Cannot detect data header in file: {path.name}", code="missing_header")


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(column).strip() for column in df.columns]
    unnamed_columns = [column for column in df.columns if str(column).startswith("Unnamed")]
    if unnamed_columns:
        df = df.drop(columns=unnamed_columns)
    return df


def _parse_time_column(marker: pd.Series, second: pd.Series | None) -> pd.Series:
    marker = marker.astype(str)
    parsed_with_ms = pd.to_datetime(marker, format="%H:%M:%S:%f", errors="coerce")
    parsed_without_ms = pd.to_datetime(marker, format="%H:%M:%S", errors="coerce")
    parsed = parsed_with_ms.fillna(parsed_without_ms)

    if second is None:
        return parsed

    second_numeric = pd.to_numeric(second, errors="coerce")
    should_reconstruct = parsed_with_ms.isna() & parsed_without_ms.notna() & second_numeric.notna()

    anchor_candidates = parsed_with_ms.notna() & second_numeric.notna()
    if not anchor_candidates.any():
        return parsed

    anchor_index = int(np.flatnonzero(anchor_candidates.to_numpy())[0])
    anchor_time = parsed_with_ms.iloc[anchor_index]
    anchor_second = float(second_numeric.iloc[anchor_index])
    reconstructed = anchor_time + pd.to_timedelta(second_numeric - anchor_second, unit="s")

    return parsed.where(~should_reconstruct, reconstructed)


def _read_csv_with_encoding_fallback(path: Path, skiprows: int) -> tuple[pd.DataFrame, str]:
    for encoding in SUPPORTED_CSV_ENCODINGS:
        try:
            return pd.read_csv(path, sep=";", skiprows=skiprows, engine="python", encoding=encoding), encoding
        except UnicodeDecodeError as exc:
            logger.warning("CSV decode failed for file=%s using encoding=%s: %s", path.name, encoding, exc)

    tried = ", ".join(SUPPORTED_CSV_ENCODINGS)
    raise ParseError(
        f"Could not decode file {path.name}. Supported encodings tried: {tried}. "
        "Save as UTF-8 CSV or contact support.",
        code="encoding",
    )


def _restore_midnight_rollovers(values: pd.Series, filename: str) -> pd.Series:
    """Turn time-of-day markers into a monotonic timeline without reordering rows."""
    base = pd.Timestamp("1900-01-01")
    day_offset = 0
    previous_clock_seconds: float | None = None
    restored: list[pd.Timestamp | pd.NaT] = []

    for row_number, value in enumerate(values, start=1):
        if pd.isna(value):
            restored.append(pd.NaT)
            continue
        timestamp = pd.Timestamp(value)
        clock_delta = timestamp - timestamp.normalize()
        clock_seconds = float(clock_delta.total_seconds())
        if previous_clock_seconds is not None:
            backward = clock_seconds - previous_clock_seconds
            if backward < -(12 * 60 * 60):
                day_offset += 1
            elif backward < 0:
                raise ParseError(
                    f"Non-monotonic timestamp in file {filename} at data row {row_number}; "
                    "only a midnight rollover may move time backwards",
                    code="non_monotonic",
                    row=row_number,
                )
        restored.append(base + pd.Timedelta(days=day_offset) + clock_delta)
        previous_clock_seconds = clock_seconds

    return pd.Series(restored, index=values.index, dtype="datetime64[ns]")


def load_esense_csv(path: Path) -> tuple[pd.DataFrame, dict[str, object]]:
    skiprows = detect_skiprows(path)
    raw_df, encoding = _read_csv_with_encoding_fallback(path, skiprows)
    raw_df = _clean_columns(raw_df)

    time_column = next((column for column in SUPPORTED_TIME_COLUMNS if column in raw_df.columns), None)
    if time_column is None:
        raise ParseError(
            f"Missing time column in file {path.name}. Expected one of: {', '.join(SUPPORTED_TIME_COLUMNS)}",
            code="missing_time_column",
        )
    if "MICROSIEMENS" not in raw_df.columns:
        raise ParseError(f"Missing MICROSIEMENS column in file {path.name}", code="missing_signal_column")

    second_column = "SEKUNDA" if "SEKUNDA" in raw_df.columns else "SECOND" if "SECOND" in raw_df.columns else None
    second = None
    if second_column:
        second_values = (
            raw_df[second_column].astype(str).str.replace(",", ".", regex=False).str.replace(" ", "", regex=False)
        )
        second, _ = repair_excel_dates(second_values)

    marker = raw_df[time_column].astype(str)
    marker = marker.replace({"Wstrzymane": np.nan, "Paused": np.nan, "nan": np.nan, "None": np.nan}).ffill()
    time = _parse_time_column(marker, second)
    time = _restore_midnight_rollovers(time, path.name)

    micros = raw_df["MICROSIEMENS"].astype(str).str.replace(",", ".", regex=False).str.replace(" ", "", regex=False)
    micros, excel_repaired = repair_excel_dates(micros)
    if excel_repaired:
        logger.warning(
            "Repaired %d Excel-mangled MICROSIEMENS values (month tokens) in file=%s", excel_repaired, path.name
        )

    if second is None:
        second = pd.Series(np.arange(len(raw_df), dtype=float))

    normalized = pd.DataFrame(
        {
            "second": second,
            "marker": marker,
            "time": time,
            "microsiemens": micros,
        }
    )
    normalized = normalized.dropna(subset=["time", "microsiemens"]).reset_index(drop=True)

    if normalized.empty:
        raise ParseError(f"No valid signal rows found in file {path.name}", code="no_rows")

    metadata: dict[str, object] = {
        "skiprows": skiprows,
        "encoding": encoding,
        "time_column": time_column,
        "row_count": int(len(normalized)),
        "time_start": str(normalized["marker"].iloc[0]),
        "time_end": str(normalized["marker"].iloc[-1]),
    }
    if excel_repaired:
        metadata["excel_repaired_values"] = excel_repaired
    return normalized, metadata
