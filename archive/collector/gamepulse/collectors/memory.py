"""Memory collector — reads /proc/meminfo and optionally a game process's RSS."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector


def _meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            try:
                result[key] = int(parts[1])  # values are in kB
            except ValueError:
                pass
    return result


def _game_rss_mb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024  # kB → MB
    except OSError:
        pass
    return None


def _game_virtual_mb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmSize:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _page_faults(pid: int) -> tuple[int, int] | None:
    """Returns (major, minor) page fault counts from /proc/pid/stat."""
    try:
        parts = Path(f"/proc/{pid}/stat").read_text().split()
        # Fields (0-indexed): 9=minflt, 11=majflt
        return int(parts[9]), int(parts[11])
    except (OSError, IndexError, ValueError):
        return None


class MemoryCollector(Collector):
    data_stream = "metrics-gamepulse.memory-default"

    def __init__(self, game_pid: int | None = None) -> None:
        self._game_pid = game_pid

    def set_game_pid(self, pid: int | None) -> None:
        self._game_pid = pid

    def collect(self) -> dict[str, Any] | None:
        info = _meminfo()

        mem_total_kb = info.get("MemTotal", 0)
        mem_available_kb = info.get("MemAvailable", 0)
        mem_used_kb = mem_total_kb - mem_available_kb
        page_cache_kb = info.get("Cached", 0)
        shared_kb = info.get("Shmem", 0)
        swap_total_kb = info.get("SwapTotal", 0)
        swap_free_kb = info.get("SwapFree", 0)

        doc: dict[str, Any] = {
            "memory": {
                "system_used_mb": mem_used_kb // 1024,
                "page_cache_mb": page_cache_kb // 1024,
                "shared_mb": shared_kb // 1024,
                "swap_used_mb": (swap_total_kb - swap_free_kb) // 1024,
            }
        }

        if self._game_pid:
            if (rss := _game_rss_mb(self._game_pid)) is not None:
                doc["memory"]["game_rss_mb"] = rss
            if (virt := _game_virtual_mb(self._game_pid)) is not None:
                doc["memory"]["virtual_mb"] = virt
            if (faults := _page_faults(self._game_pid)) is not None:
                major, minor = faults
                doc["memory"]["page_faults_major"] = major
                doc["memory"]["page_faults_minor"] = minor

        return {"gamepulse": doc}
