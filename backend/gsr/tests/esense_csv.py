from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def build_esense_csv(
    path: Path,
    *,
    time_column: str = "TIMESTAMP",
    rows: int = 180,
    seed_offset: float = 0.0,
    start_time: str = "12:00:00:000",
) -> None:
    base = datetime.strptime(start_time, "%H:%M:%S:%f")

    lines: list[str] = ["Device: test;", "DESCRIPTION;VALUE"]
    for idx in range(28):
        lines.append(f"META_{idx};value")
    lines.append("STATISTICS;;;;;")

    if time_column == "TIMESTAMP":
        header = "SECOND;MICROSIEMENS;TIMESTAMP;SCR;SCR/MIN;MARKER"
    else:
        header = "SEKUNDA;MICROSIEMENS;ZNACZNIKI CZASU;SCR;SCR/MIN;ZNACZNIK"
    lines.append(header)

    for idx in range(rows):
        timestamp = (base + timedelta(milliseconds=200 * idx)).strftime("%H:%M:%S:%f")[:-3]
        micro = 8.0 + seed_offset + (idx % 25) * 0.12
        if idx == 5:
            marker = "Wstrzymane"
        else:
            marker = timestamp
        micro_text = f"{micro:.2f}".replace(".", ",")
        lines.append(f"{idx * 0.2:.1f};{micro_text};{marker};0;0;")

    path.write_text("\n".join(lines), encoding="utf-8")
