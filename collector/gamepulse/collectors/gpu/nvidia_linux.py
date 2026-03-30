"""
NVIDIA GPU collector — uses nvidia-smi for metric reads.

Avoids any compile-time NVML dependency: if nvidia-smi is not on PATH
(AMD-only system), the collector returns None on every tick.
Fields match gamepulse-gpu-mappings component template exactly.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from gamepulse.collectors.base import Collector

log = logging.getLogger(__name__)

# nvidia-smi query fields in the order we request them
_QUERY_FIELDS = [
    "utilization.gpu",        # %
    "clocks.current.graphics",# MHz
    "clocks.max.graphics",    # MHz
    "memory.used",            # MiB
    "memory.total",           # MiB
    "temperature.gpu",        # °C
    "power.draw",             # W
    "fan.speed",              # %
    "pcie.link.gen.current",
    "pcie.link.width.current",
]

_CMD = [
    "nvidia-smi",
    f"--query-gpu={','.join(_QUERY_FIELDS)}",
    "--format=csv,noheader,nounits",
]


def _nvidia_smi_available() -> bool:
    return shutil.which("nvidia-smi") is not None


def _query() -> list[str] | None:
    """Run nvidia-smi and return the first GPU's values, or None on failure."""
    try:
        out = subprocess.check_output(
            _CMD, stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
    except (subprocess.SubprocessError, OSError, subprocess.TimeoutExpired):
        return None

    # Take the first line (first GPU)
    first_line = out.splitlines()[0] if out else ""
    parts = [p.strip() for p in first_line.split(",")]
    if len(parts) != len(_QUERY_FIELDS):
        return None
    return parts


def _parse_float(val: str) -> float | None:
    try:
        return float(val)
    except ValueError:
        return None


def _parse_int(val: str) -> int | None:
    try:
        return int(val)
    except ValueError:
        return None


class NvidiaGpuCollector(Collector):
    data_stream = "metrics-gamepulse.gpu-default"

    def __init__(self) -> None:
        self._available = _nvidia_smi_available()
        if not self._available:
            log.debug("nvidia-smi not found — NVIDIA GPU collector inactive")

    @property
    def available(self) -> bool:
        return self._available

    def collect(self) -> dict[str, Any] | None:
        if not self._available:
            return None

        parts = _query()
        if parts is None:
            return None

        (
            util_pct, clk_mhz, clk_max_mhz, mem_used_mib, mem_total_mib,
            temp_c, power_w, fan_pct, pcie_gen, pcie_width,
        ) = parts

        gpu: dict[str, Any] = {}

        if (v := _parse_float(util_pct)) is not None:
            gpu["utilisation_pct"] = v
        if (v := _parse_int(clk_mhz)) is not None:
            gpu["clock_mhz"] = v
        if (v := _parse_int(mem_used_mib)) is not None:
            gpu["memory_used_mb"] = v
        if (v := _parse_int(mem_total_mib)) is not None:
            gpu["memory_total_mb"] = v
        if (v := _parse_float(temp_c)) is not None:
            gpu["temperature_c"] = v
        if (v := _parse_float(power_w)) is not None:
            gpu["power_w"] = round(v, 1)
        if (v := _parse_float(fan_pct)) is not None:
            gpu["fan_pct"] = v

        if not gpu:
            return None

        return {"gpu": gpu}
