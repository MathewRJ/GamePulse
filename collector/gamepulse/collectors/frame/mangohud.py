"""
MangoHud frame timing collector.

MangoHud writes a CSV log when MANGOHUD_LOG=1 is set. This collector
watches /tmp/MangoHud/ for the most recently modified CSV, reads it
incrementally, and computes per-second FPS statistics.

CSV columns (MangoHud ≥ 0.6):
  fps, frametime, cpu_load, gpu_load, cpu_temp, gpu_temp, ...
"""

from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector

_LOG_DIR = Path("/tmp/MangoHud")
_STUTTER_THRESHOLD_MS = 33.0  # frame > 33ms = stutter (below 30fps)


def _latest_log() -> Path | None:
    try:
        logs = list(_LOG_DIR.glob("*.csv"))
        if not logs:
            return None
        return max(logs, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, int(len(sorted_v) * pct / 100) - 1)
    return sorted_v[idx]


class MangoHudCollector(Collector):
    data_stream = "metrics-gamepulse.frame-default"

    def __init__(self) -> None:
        self._log_path: Path | None = None
        self._file_pos: int = 0
        self._header: list[str] = []
        self._fps_col: int = -1
        self._ft_col: int = -1
        self._last_check: float = 0.0

    def _maybe_switch_log(self) -> None:
        """Check for a newer log file every 5 seconds."""
        now = time.monotonic()
        if now - self._last_check < 5.0:
            return
        self._last_check = now

        latest = _latest_log()
        if latest != self._log_path:
            self._log_path = latest
            self._file_pos = 0
            self._header = []
            self._fps_col = -1
            self._ft_col = -1

    def _read_new_rows(self) -> list[list[str]]:
        if not self._log_path or not self._log_path.exists():
            return []
        try:
            with open(self._log_path) as f:
                f.seek(self._file_pos)
                chunk = f.read()
                self._file_pos = f.tell()
        except OSError:
            return []

        rows = []
        reader = csv.reader(io.StringIO(chunk))
        for row in reader:
            if not self._header:
                # First row is the header
                self._header = [c.strip().lower() for c in row]
                try:
                    self._fps_col = self._header.index("fps")
                except ValueError:
                    pass
                try:
                    self._ft_col = self._header.index("frametime")
                except ValueError:
                    pass
                continue
            rows.append(row)
        return rows

    def collect(self) -> dict[str, Any] | None:
        self._maybe_switch_log()
        rows = self._read_new_rows()

        if not rows or self._fps_col < 0:
            return None

        fps_values: list[float] = []
        ft_values: list[float] = []

        for row in rows:
            try:
                if self._fps_col < len(row):
                    fps_values.append(float(row[self._fps_col]))
                if self._ft_col >= 0 and self._ft_col < len(row):
                    ft_values.append(float(row[self._ft_col]))
            except (ValueError, IndexError):
                continue

        if not fps_values:
            return None

        avg_fps = round(sum(fps_values) / len(fps_values), 1)
        low_1pct = int(_percentile(fps_values, 1))
        low_01pct = int(_percentile(fps_values, 0.1))
        current_fps = int(fps_values[-1])

        doc: dict[str, Any] = {
            "fps": {
                "current": current_fps,
                "avg_1s": avg_fps,
                "low_1pct": low_1pct,
                "low_01pct": low_01pct,
            }
        }

        if ft_values:
            avg_ft = round(sum(ft_values) / len(ft_values), 2)
            variance = round(
                sum((x - avg_ft) ** 2 for x in ft_values) / len(ft_values), 2
            )
            doc["fps"]["frametime_ms"] = avg_ft
            doc["fps"]["frametime_variance"] = variance

        return doc
