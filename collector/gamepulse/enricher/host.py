"""
Host environment enricher.

Reads OS info, hardware specs, and compatibility layer versions once
at startup and builds the session document that goes to
metrics-gamepulse.session-default.

Field names match gamepulse-host-environment component template exactly.
"""

from __future__ import annotations

import glob
import platform
import re
import subprocess
from pathlib import Path
from typing import Any


def _read_str(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return result


def _cpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
        for line in cpuinfo.splitlines():
            if line.startswith("model name") and "model" not in info:
                info["model"] = line.split(":", 1)[1].strip()
            elif line.startswith("cpu cores") and "cores" not in info:
                try:
                    info["cores"] = int(line.split(":")[1].strip())
                except ValueError:
                    pass

        # Thread count = number of "processor" entries
        info["threads"] = cpuinfo.count("processor\t:")

        # Clock speeds from cpufreq
        max_freqs = []
        for p in glob.glob("/sys/bus/cpu/devices/cpu*/cpufreq/cpuinfo_max_freq"):
            v = _read_int(p)
            if v:
                max_freqs.append(v / 1000)  # kHz → MHz
        if max_freqs:
            info["boost_clock_mhz"] = int(max(max_freqs))

        min_freqs = []
        for p in glob.glob("/sys/bus/cpu/devices/cpu*/cpufreq/cpuinfo_min_freq"):
            v = _read_int(p)
            if v:
                min_freqs.append(v / 1000)
        if min_freqs:
            info["base_clock_mhz"] = int(min(min_freqs))

    except OSError:
        pass
    return info


def _gpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        device = f"{card}/device"
        vendor = _read_str(f"{device}/vendor")
        if vendor == "0x1002":
            info["vendor"] = "amd"
        elif vendor == "0x10de":
            info["vendor"] = "nvidia"
        elif vendor == "0x8086":
            info["vendor"] = "intel"
        else:
            continue

        # GPU model from device label or uevent
        uevent = _read_str(f"{device}/uevent") or ""
        for line in uevent.splitlines():
            if line.startswith("DRIVER="):
                info["driver"] = line.split("=", 1)[1]

        # VRAM total (bytes → MB)
        vram = _read_int(f"{device}/mem_info_vram_total")
        if vram:
            info["vram_mb"] = vram // 1_048_576

        # Mesa/driver version from vulkaninfo
        try:
            out = subprocess.check_output(
                ["vulkaninfo", "--summary"], stderr=subprocess.DEVNULL, timeout=3
            ).decode(errors="replace")
            m = re.search(r"driverVersion\s*=\s*(\S+)", out)
            if m:
                info["driver_version"] = m.group(1)
            m = re.search(r"driverName\s*=\s*(\S+)", out)
            if m:
                info["vulkan_driver"] = m.group(1).lower()
        except (OSError, subprocess.SubprocessError):
            pass

        # Mesa version
        try:
            out = subprocess.check_output(
                ["mesa-overlay-control.py", "--version"],
                stderr=subprocess.STDOUT,
                timeout=2,
            ).decode(errors="replace")
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            # Try glxinfo as fallback
            out = subprocess.check_output(
                ["glxinfo", "-B"], stderr=subprocess.DEVNULL, timeout=3
            ).decode(errors="replace")
            m = re.search(r"Mesa (\S+)", out)
            if m:
                info["mesa_version"] = m.group(1)
        except (OSError, subprocess.SubprocessError):
            pass

        break  # first GPU only

    return info


def _ram_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                info["total_mb"] = kb // 1024
                break
    except (OSError, ValueError):
        pass

    # RAM type/speed from DMI (may require root)
    try:
        out = subprocess.check_output(
            ["dmidecode", "-t", "17"], stderr=subprocess.DEVNULL, timeout=3
        ).decode(errors="replace")
        m = re.search(r"Speed:\s*(\d+)\s*MT/s", out)
        if m:
            info["speed_mhz"] = int(m.group(1))
        m = re.search(r"Type:\s+(\S+)", out)
        if m and m.group(1) not in ("Unknown", "Other"):
            info["type"] = m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass

    return info


def _device_info() -> dict[str, Any]:
    info: dict[str, Any] = {}

    # DMI chassis type: 8=laptop, 9=notebook, 11=handheld, 3=desktop
    chassis = _read_str("/sys/class/dmi/id/chassis_type")
    if chassis:
        try:
            ct = int(chassis)
            if ct == 11:
                info["device_type"] = "handheld"
            elif ct in (8, 9, 10, 14):
                info["device_type"] = "laptop"
            else:
                info["device_type"] = "desktop"
        except ValueError:
            pass

    # Device model
    product = _read_str("/sys/class/dmi/id/product_name")
    if product:
        info["model"] = product

    # Power source
    for supply in glob.glob("/sys/class/power_supply/AC*") + glob.glob(
        "/sys/class/power_supply/ADP*"
    ):
        online = _read_int(f"{supply}/online")
        if online is not None:
            info["power_source"] = "ac" if online else "battery"
            break

    return info


def _desktop_env() -> str | None:
    import os
    return (
        os.environ.get("XDG_CURRENT_DESKTOP")
        or os.environ.get("DESKTOP_SESSION")
        or None
    )


class HostEnricher:
    """Collects the static host environment snapshot for the session document."""

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] = self._build()

    def _build(self) -> dict[str, Any]:
        os_rel = _os_release()
        doc: dict[str, Any] = {
            "os": {
                "type": "linux",
                "distro": os_rel.get("NAME") or os_rel.get("ID"),
                "version": os_rel.get("VERSION_ID") or os_rel.get("BUILD_ID"),
                "kernel": platform.release(),
            }
        }

        desktop = _desktop_env()
        if desktop:
            doc["os"]["desktop"] = desktop

        cpu = _cpu_info()
        gpu = _gpu_info()
        ram = _ram_info()
        device = _device_info()

        hardware: dict[str, Any] = {}
        if cpu:
            hardware["cpu"] = cpu
        if gpu:
            hardware["gpu"] = gpu
        if ram:
            hardware["ram"] = ram
        if device:
            hardware["device"] = device

        if hardware:
            doc["hardware"] = hardware

        return doc

    @property
    def snapshot(self) -> dict[str, Any]:
        return self._snapshot
