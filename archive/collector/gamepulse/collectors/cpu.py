"""CPU collector — reads /proc/stat, sysfs cpufreq, and hwmon temperature/power."""

from __future__ import annotations

import glob
import time
from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector


def _read_int(path: str | Path) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _read_float(path: str | Path) -> float | None:
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: str | Path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


class _ProcStatSnapshot:
    """One reading of /proc/stat — total jiffies per CPU."""

    def __init__(self) -> None:
        self.timestamp = time.monotonic()
        self.per_core: list[tuple[int, int]] = []  # (idle, total) per logical CPU

        lines = Path("/proc/stat").read_text().splitlines()
        for line in lines:
            if not line.startswith("cpu"):
                continue
            name, *fields = line.split()
            if name == "cpu":
                continue  # aggregate — we compute from per-core
            parts = [int(f) for f in fields]
            # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
            idle = parts[3] + parts[4]  # idle + iowait
            total = sum(parts[:8])
            self.per_core.append((idle, total))

    def utilisation(self, prev: "_ProcStatSnapshot") -> list[float]:
        result = []
        for (idle, total), (p_idle, p_total) in zip(self.per_core, prev.per_core):
            d_total = total - p_total
            d_idle = idle - p_idle
            if d_total == 0:
                result.append(0.0)
            else:
                result.append(round(100.0 * (1 - d_idle / d_total), 1))
        return result


def _governor() -> str | None:
    path = "/sys/bus/cpu/devices/cpu0/cpufreq/scaling_governor"
    return _read_str(path)


def _boost_enabled() -> bool:
    # AMD/Intel boost toggle (0 = boost disabled, 1 = enabled)
    v = _read_int("/sys/devices/system/cpu/cpufreq/boost")
    if v is not None:
        return v == 1
    # Intel no_turbo (inverted)
    v = _read_int("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if v is not None:
        return v == 0
    return True


def _clock_mhz_avg() -> int | None:
    freqs = []
    for p in sorted(glob.glob("/sys/bus/cpu/devices/cpu*/cpufreq/scaling_cur_freq")):
        v = _read_int(p)
        if v is not None:
            freqs.append(v / 1000)  # kHz → MHz
    if not freqs:
        return None
    return int(sum(freqs) / len(freqs))


def _temperature_c() -> float | None:
    """Find CPU package/die temperature from hwmon (k10temp or coretemp)."""
    for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = _read_str(f"{hwmon}/name")
        if name not in ("k10temp", "coretemp"):
            continue
        # k10temp: Tdie or Tctl; coretemp: Package id 0
        for label_path in sorted(glob.glob(f"{hwmon}/temp*_label")):
            label = _read_str(label_path)
            if label and ("Tdie" in label or "Package" in label or "Tctl" in label):
                idx = label_path.replace("_label", "_input")
                v = _read_int(idx)
                if v is not None:
                    return round(v / 1000.0, 1)
        # Fallback: temp1_input
        v = _read_int(f"{hwmon}/temp1_input")
        if v is not None:
            return round(v / 1000.0, 1)
    return None


def _power_w() -> float | None:
    """Read CPU package power via RAPL (intel-rapl or amd_energy)."""
    # Intel RAPL
    for p in glob.glob("/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"):
        try:
            e1 = int(Path(p).read_text())
            time.sleep(0.05)
            e2 = int(Path(p).read_text())
            return round((e2 - e1) / 0.05 / 1_000_000, 1)  # μJ → W
        except (OSError, ValueError):
            pass
    # AMD energy driver (single read, units vary — return None if uncertain)
    return None


class CpuCollector(Collector):
    data_stream = "metrics-gamepulse.cpu-default"

    def __init__(self) -> None:
        self._prev: _ProcStatSnapshot | None = None

    def collect(self) -> dict[str, Any] | None:
        snap = _ProcStatSnapshot()

        if self._prev is None:
            self._prev = snap
            return None  # need two snapshots for delta

        per_core = snap.utilisation(self._prev)
        self._prev = snap

        total = round(sum(per_core) / len(per_core), 1) if per_core else 0.0

        result: dict[str, Any] = {
            "cpu": {
                "total_utilisation_pct": total,
                "per_core": per_core,
            }
        }

        if (clk := _clock_mhz_avg()) is not None:
            result["cpu"]["clock_mhz_avg"] = clk
        if (temp := _temperature_c()) is not None:
            result["cpu"]["temperature_c"] = temp
        if (pwr := _power_w()) is not None:
            result["cpu"]["power_w"] = pwr
        if (gov := _governor()) is not None:
            result["cpu"]["governor"] = gov
        result["cpu"]["boost_state"] = _boost_enabled()

        return {"gamepulse": result}
