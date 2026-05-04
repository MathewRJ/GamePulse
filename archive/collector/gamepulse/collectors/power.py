"""
Power and battery collector.

Reads battery state, AC status, and TDP from sysfs.
On Steam Deck (AMD APU), also reads the firmware TDP via the
amdgpu power profile interface.

Fields match gamepulse-power-mappings component template exactly.
"""

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


def _find_battery() -> str | None:
    for p in glob.glob("/sys/class/power_supply/BAT*"):
        return p
    return None


def _find_ac() -> str | None:
    for p in (
        glob.glob("/sys/class/power_supply/AC*")
        + glob.glob("/sys/class/power_supply/ADP*")
    ):
        return p
    return None


def _battery_pct(bat: str) -> float | None:
    v = _read_int(f"{bat}/capacity")
    return float(v) if v is not None else None


def _battery_rate_w(bat: str) -> float | None:
    """Return current power draw in watts. Handles µW and µA sources."""
    # Power now in µW
    power_uw = _read_int(f"{bat}/power_now")
    if power_uw is not None:
        return round(power_uw / 1_000_000, 2)

    # Current in µA + voltage in µV → watts
    current_ua = _read_int(f"{bat}/current_now")
    voltage_uv = _read_int(f"{bat}/voltage_now")
    if current_ua is not None and voltage_uv is not None:
        return round((current_ua * voltage_uv) / 1e12, 2)

    return None


def _ac_connected(ac: str | None) -> bool | None:
    if ac is None:
        return None
    v = _read_int(f"{ac}/online")
    return bool(v) if v is not None else None


def _amd_tdp_w() -> float | None:
    """
    Read the active TDP from the amdgpu power cap sysfs.
    /sys/class/drm/card0/device/hwmon/hwmon*/power1_cap (µW → W)
    """
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        name = _read_str(f"{hwmon}/name")
        if name in ("amdgpu",):
            cap = _read_int(f"{hwmon}/power1_cap")
            if cap is not None:
                return round(cap / 1_000_000, 1)
    return None


def _power_profile() -> str | None:
    """
    Read the active power profile from the platform driver.
    Steam Deck uses amd_pmf or steam-deck firmware controls.
    """
    # platform_profile is a standard kernel interface (5.11+)
    v = _read_str("/sys/firmware/acpi/platform_profile")
    if v:
        return v
    return None


class PowerCollector(Collector):
    data_stream = "metrics-gamepulse.power-default"

    def __init__(self) -> None:
        self._battery = _find_battery()
        self._ac = _find_ac()

    def collect(self) -> dict[str, Any] | None:
        power: dict[str, Any] = {}

        if self._battery:
            if (pct := _battery_pct(self._battery)) is not None:
                power["battery_pct"] = pct
            if (rate := _battery_rate_w(self._battery)) is not None:
                power["battery_rate_w"] = rate

        if (ac := _ac_connected(self._ac)) is not None:
            power["ac_connected"] = ac

        if (tdp := _amd_tdp_w()) is not None:
            power["tdp_current_w"] = tdp

        if (profile := _power_profile()) is not None:
            power["profile"] = profile

        if not power:
            return None

        return {"gamepulse": {"power": power}}
