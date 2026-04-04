"""
MangoHud frame timing collector.

MangoHud writes a per-frame CSV log when output_folder and autostart_log=1
are set in MangoHud.conf.  This collector watches that folder for the most
recently modified CSV, reads it incrementally (one call per collector tick),
and computes per-second FPS statistics over the frames accumulated since the
last tick.

CSV format (MangoHud ≥ 0.7):
  Line 1 — system info header:  os,cpu,gpu,ram,kernel,...
  Line 2 — system info values:  CachyOS,AMD Ryzen...,...
  Line 3 — data column header:  fps,frametime,cpu_load,...,elapsed
  Line 4+ — one row per frame:  119.3,8.38,...,16657798

All numeric values in the data rows are:
  fps        — frames per second (float)
  frametime  — frame duration in milliseconds (float)
  elapsed    — nanoseconds since logging started (int)
"""

from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector

# Where MangoHud writes CSV logs.  Must match output_folder in MangoHud.conf.
_LOG_DIR = Path.home() / ".local" / "share" / "MangoHud"
_LOG_DIR_FALLBACK = Path("/tmp/MangoHud")

_STALE_SECS = 10.0   # stop reading a file that hasn't grown for this long


def _find_log_dir() -> Path:
    if _LOG_DIR.is_dir():
        return _LOG_DIR
    if _LOG_DIR_FALLBACK.is_dir():
        return _LOG_DIR_FALLBACK
    return _LOG_DIR   # may not exist yet; _latest_log() will handle it gracefully


def _latest_log(log_dir: Path) -> Path | None:
    try:
        logs = list(log_dir.glob("*.csv"))
        if not logs:
            return None
        return max(logs, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def _percentile(values: list[float], pct: float) -> float:
    """Return the value at the given percentile (0–100) of a sorted list."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    # pct=1 → bottom 1% → index near 0; pct=0.1 → index 0 for typical sample sizes
    idx = max(0, int(len(sorted_v) * pct / 100.0) - 1)
    return sorted_v[idx]


class MangoHudCollector(Collector):
    data_stream = "metrics-gamepulse.frame-default"

    def __init__(self) -> None:
        self._log_dir = _find_log_dir()
        self._log_path: Path | None = None
        self._file_pos: int = 0
        self._fps_col: int = -1
        self._ft_col: int = -1
        self._header_done: bool = False
        self._last_check: float = 0.0
        self._last_mtime: float = 0.0

    def _maybe_switch_log(self) -> None:
        """Check every 5 s for a newer CSV log file."""
        now = time.monotonic()
        if now - self._last_check < 5.0:
            return
        self._last_check = now

        latest = _latest_log(self._log_dir)
        if latest != self._log_path:
            self._log_path = latest
            self._file_pos = 0
            self._fps_col = -1
            self._ft_col = -1
            self._header_done = False
            self._last_mtime = 0.0

    def _read_new_rows(self) -> list[list[str]]:
        if not self._log_path or not self._log_path.exists():
            return []
        try:
            stat = self._log_path.stat()
            if stat.st_mtime == self._last_mtime and self._file_pos >= stat.st_size:
                return []   # nothing new
            self._last_mtime = stat.st_mtime
        except OSError:
            return []

        try:
            with open(self._log_path) as f:
                f.seek(self._file_pos)
                chunk = f.read()
                self._file_pos = f.tell()
        except OSError:
            return []

        rows: list[list[str]] = []
        reader = csv.reader(io.StringIO(chunk))
        for row in reader:
            if not row:
                continue

            if not self._header_done:
                # MangoHud emits a 3-line preamble:
                #   Line 1: os,cpu,gpu,...      ← system info header
                #   Line 2: CachyOS,...         ← system info values
                #   Line 3: fps,frametime,...   ← real data column header
                # We recognise the data header by "fps" being the first column.
                if row[0].strip().lower() == "fps":
                    header = [c.strip().lower() for c in row]
                    try:
                        self._fps_col = header.index("fps")
                    except ValueError:
                        pass
                    try:
                        self._ft_col = header.index("frametime")
                    except ValueError:
                        pass
                    self._header_done = True
                # All other preamble lines are skipped
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
                fps_v = float(row[self._fps_col]) if self._fps_col < len(row) else None
                ft_v = (
                    float(row[self._ft_col])
                    if self._ft_col >= 0 and self._ft_col < len(row)
                    else None
                )
            except (ValueError, IndexError):
                continue

            # Exclude sub-1fps frames (hard freezes, loading transitions) —
            # these are not representative of gameplay performance and their
            # frametimes can be millions of ms, corrupting variance calculations.
            if fps_v is None or fps_v < 1.0:
                continue

            fps_values.append(fps_v)
            # Cap at 200ms — anything above that is a loading screen or menu
            # pause, not a gameplay frame, and corrupts the histogram.
            if ft_v is not None and 0 < ft_v <= 200.0:
                ft_values.append(ft_v)

        if not fps_values:
            return None

        avg_fps = round(sum(fps_values) / len(fps_values), 1)
        low_1pct = int(_percentile(fps_values, 1.0))
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

        stutter_count = 0
        if ft_values:
            avg_ft = round(sum(ft_values) / len(ft_values), 3)
            variance = round(
                sum((x - avg_ft) ** 2 for x in ft_values) / len(ft_values), 3
            )
            stutter_count = sum(1 for ft in ft_values if ft > 2.0 * avg_ft)
            doc["fps"]["frametime_ms"] = avg_ft
            doc["fps"]["frametime_variance"] = variance
        doc["fps"]["stutter_count"] = stutter_count

        return doc
