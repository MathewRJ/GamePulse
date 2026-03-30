"""Storage collector — delta reads from /proc/diskstats."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector

# /proc/diskstats field indices (0-based after device name fields)
# See: https://www.kernel.org/doc/Documentation/ABI/testing/procfs-diskstats
_F_READ_IOS = 0
_F_READ_MERGES = 1
_F_READ_SECTORS = 2
_F_READ_TICKS = 3
_F_WRITE_IOS = 4
_F_WRITE_MERGES = 5
_F_WRITE_SECTORS = 6
_F_WRITE_TICKS = 7
_F_IO_IN_PROGRESS = 8
_F_IO_TICKS = 9


def _parse_diskstats() -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    try:
        for line in Path("/proc/diskstats").read_text().splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            dev = parts[2]
            stats = [int(p) for p in parts[3:]]
            result[dev] = stats
    except OSError:
        pass
    return result


def _find_game_device() -> str | None:
    """
    Return the block device backing the game installation.
    Heuristic: find the device for the Steam library path, falling back
    to the device with the most write activity.
    """
    # Check common Steam paths for the mount point, then resolve to device
    steam_paths = [
        Path.home() / ".steam" / "steam" / "steamapps",
        Path.home() / ".local" / "share" / "Steam" / "steamapps",
        Path("/run/media") ,  # SD card on Steam Deck
    ]
    for sp in steam_paths:
        if sp.exists():
            try:
                mounts = Path("/proc/mounts").read_text().splitlines()
                # Find mount with longest matching prefix
                best_dev, best_len = None, 0
                for line in mounts:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    mp = parts[1]
                    dev = parts[0]
                    if str(sp).startswith(mp) and len(mp) > best_len:
                        best_dev, best_len = dev, len(mp)
                if best_dev and best_dev.startswith("/dev/"):
                    return Path(best_dev).name
            except OSError:
                pass

    # Fallback: first non-virtual disk
    for line in Path("/proc/diskstats").read_text().splitlines():
        parts = line.split()
        dev = parts[2] if len(parts) > 2 else ""
        # Skip loop, ram, dm devices; prefer nvme or sd*
        if dev and not dev.startswith(("loop", "ram", "dm", "zram")):
            return dev
    return None


class StorageCollector(Collector):
    data_stream = "metrics-gamepulse.storage-default"

    _SECTOR_BYTES = 512

    def __init__(self) -> None:
        self._prev: dict[str, list[int]] | None = None
        self._prev_time: float = 0.0
        self._device: str | None = _find_game_device()

    def collect(self) -> dict[str, Any] | None:
        now = time.monotonic()
        current = _parse_diskstats()

        if self._prev is None:
            self._prev = current
            self._prev_time = now
            return None

        dt = now - self._prev_time
        if dt <= 0:
            return None

        # Prefer the detected game device; fall back to first available
        dev = self._device
        if dev not in current:
            candidates = [d for d in current if not d.startswith(("loop", "ram", "dm", "zram"))]
            dev = candidates[0] if candidates else None
        if not dev or dev not in self._prev:
            self._prev = current
            self._prev_time = now
            return None

        cur = current[dev]
        prv = self._prev[dev]

        d_read_ios = cur[_F_READ_IOS] - prv[_F_READ_IOS]
        d_read_sectors = cur[_F_READ_SECTORS] - prv[_F_READ_SECTORS]
        d_read_ticks = cur[_F_READ_TICKS] - prv[_F_READ_TICKS]
        d_write_ios = cur[_F_WRITE_IOS] - prv[_F_WRITE_IOS]
        d_write_sectors = cur[_F_WRITE_SECTORS] - prv[_F_WRITE_SECTORS]
        d_write_ticks = cur[_F_WRITE_TICKS] - prv[_F_WRITE_TICKS]
        d_io_ticks = cur[_F_IO_TICKS] - prv[_F_IO_TICKS]
        d_read_merges = cur[_F_READ_MERGES] - prv[_F_READ_MERGES]
        d_write_merges = cur[_F_WRITE_MERGES] - prv[_F_WRITE_MERGES]

        read_mbps = round((d_read_sectors * self._SECTOR_BYTES) / dt / 1_048_576, 2)
        write_mbps = round((d_write_sectors * self._SECTOR_BYTES) / dt / 1_048_576, 2)
        read_iops = int(d_read_ios / dt)
        write_iops = int(d_write_ios / dt)

        # Average latency in microseconds (ticks are in ms, ios is count)
        read_lat_us = int(d_read_ticks * 1000 / d_read_ios) if d_read_ios > 0 else 0
        write_lat_us = int(d_write_ticks * 1000 / d_write_ios) if d_write_ios > 0 else 0

        # io_wait_pct: ms spent doing I/O in the measurement window
        io_wait_pct = round(min(d_io_ticks / (dt * 10), 100.0), 1)

        queue_depth = cur[_F_IO_IN_PROGRESS]

        self._prev = current
        self._prev_time = now

        return {
            "storage": {
                "read_mbps": read_mbps,
                "write_mbps": write_mbps,
                "read_iops": read_iops,
                "write_iops": write_iops,
                "io_latency_read_us": {"avg": read_lat_us},
                "io_latency_write_us": {"avg": write_lat_us},
                "queue_depth_current": queue_depth,
                "io_wait_pct": io_wait_pct,
                "merged_reads": int(d_read_merges / dt),
                "merged_writes": int(d_write_merges / dt),
            }
        }
