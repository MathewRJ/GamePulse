"""AMD GPU collector — reads metrics from sysfs (amdgpu driver)."""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _read_float(path: str) -> float | None:
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _find_amd_card() -> str | None:
    """Return the sysfs path to the best AMD DRM card device.

    Scores each AMD card to prefer the discrete GPU over an iGPU:
    - +10 if the hwmon has fan1_input (discrete GPU with a fan)
    - +5  if the hwmon has power1_average (GPU-level power sensor)
    - +2  if the hwmon has temp2_input (hotspot sensor = discrete GPU)
    - +1  if a hwmon directory exists at all
    Falls back to the first AMD card found if none score above -1.
    """
    best_device: str | None = None
    best_score = -1

    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        vendor = _read_str(f"{card}/device/vendor")
        if vendor != "0x1002":
            continue
        device = f"{card}/device"
        hwmon = _find_hwmon(device)
        score = 0
        if hwmon:
            score += 1
            if Path(f"{hwmon}/fan1_input").exists():
                score += 10
            if Path(f"{hwmon}/power1_average").exists():
                score += 5
            if Path(f"{hwmon}/temp2_input").exists():
                score += 2
        if score > best_score:
            best_score = score
            best_device = device

    return best_device


def _find_hwmon(device_path: str) -> str | None:
    """Find the hwmon directory for an amdgpu device."""
    for hwmon in sorted(glob.glob(f"{device_path}/hwmon/hwmon*")):
        return hwmon
    return None


def _parse_current_clock_mhz(pp_dpm_sclk: str) -> int | None:
    """
    /sys/class/drm/card0/device/pp_dpm_sclk format:
      0: 500Mhz
      1: 800Mhz *   ← active clock is marked with *
      2: 2100Mhz
    """
    try:
        for line in Path(pp_dpm_sclk).read_text().splitlines():
            if "*" in line:
                parts = line.split()
                for part in parts:
                    if part.lower().endswith("mhz"):
                        return int(part[:-3])
    except OSError:
        pass
    return None


class AmdGpuCollector(Collector):
    data_stream = "metrics-gamepulse.gpu-default"

    def __init__(self) -> None:
        self._device = _find_amd_card()
        self._hwmon = _find_hwmon(self._device) if self._device else None

    @property
    def available(self) -> bool:
        return self._device is not None

    def collect(self) -> dict[str, Any] | None:
        if not self._device:
            return None

        gpu: dict[str, Any] = {}

        # Utilisation
        util = _read_int(f"{self._device}/gpu_busy_percent")
        if util is not None:
            gpu["utilisation_pct"] = float(util)

        # Clock
        clk = _parse_current_clock_mhz(f"{self._device}/pp_dpm_sclk")
        if clk is not None:
            gpu["clock_mhz"] = clk

        # VRAM
        vram_used = _read_int(f"{self._device}/mem_info_vram_used")
        vram_total = _read_int(f"{self._device}/mem_info_vram_total")
        if vram_used is not None:
            gpu["memory_used_mb"] = vram_used // 1_048_576
        if vram_total is not None:
            gpu["memory_total_mb"] = vram_total // 1_048_576

        if self._hwmon:
            hw = self._hwmon

            # Temperatures: AMD hwmon maps temp1=edge, temp2=junction/hotspot, temp3=mem
            t_edge = _read_int(f"{hw}/temp1_input")
            if t_edge is not None:
                gpu["temperature_c"] = round(t_edge / 1000.0, 1)

            t_hot = _read_int(f"{hw}/temp2_input")
            if t_hot is not None:
                gpu["hotspot_c"] = round(t_hot / 1000.0, 1)

            t_mem = _read_int(f"{hw}/temp3_input")
            if t_mem is not None:
                gpu["memory_temperature_c"] = round(t_mem / 1000.0, 1)

            # Power (µW → W)
            pwr = _read_int(f"{hw}/power1_average")
            if pwr is not None:
                gpu["power_w"] = round(pwr / 1_000_000.0, 1)

            # Fan
            fan_rpm = _read_int(f"{hw}/fan1_input")
            if fan_rpm is not None:
                gpu["fan_speed_rpm"] = fan_rpm
            fan_max = _read_int(f"{hw}/fan1_max")
            if fan_rpm is not None and fan_max and fan_max > 0:
                gpu["fan_pct"] = round(fan_rpm / fan_max * 100.0, 1)

            # Voltage: in0_input is unreliable on many AMD GPU generations
            # (reports non-GPU voltages or near-zero when clock-gated).
            # Omitted until a reliable source is confirmed per GPU model.

        if not gpu:
            return None

        return {"gamepulse": {"gpu": gpu}}
