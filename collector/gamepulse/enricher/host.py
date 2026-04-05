"""
Host environment enricher.

Reads OS info, hardware specs, and compatibility layer versions once
at startup and builds the session document that goes to
metrics-gamepulse.session-default.

Emits ECS host.os.* fields at the document root and all GamePulse-specific
hardware fields under gamepulse.hardware.*.
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

        info["threads"] = cpuinfo.count("processor\t:")

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

    vendor_id: str | None = None
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        device = f"{card}/device"
        v = _read_str(f"{device}/vendor")
        if v in ("0x1002", "0x10de", "0x8086"):
            vendor_id = v
            if v == "0x1002":
                info["vendor"] = "amd"
                vram = _read_int(f"{device}/mem_info_vram_total")
                if vram:
                    info["vram_mb"] = vram // 1_048_576
            elif v == "0x10de":
                info["vendor"] = "nvidia"
            elif v == "0x8086":
                info["vendor"] = "intel"
            break

    if vendor_id == "0x10de" or (vendor_id is None and _nvidia_smi_present()):
        _enrich_nvidia(info)
    elif vendor_id == "0x1002":
        _enrich_amd(info)

    return info


def _nvidia_smi_present() -> bool:
    import shutil
    return shutil.which("nvidia-smi") is not None


def _enrich_nvidia(info: dict[str, Any]) -> None:
    query = "name,memory.total,driver_version,pci.bus_id"
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return

    parts = [p.strip() for p in out.split(",")]
    if len(parts) < 3:
        return

    name, vram_mib, driver_ver = parts[0], parts[1], parts[2]
    if name:
        info["model"] = name
    try:
        info["vram_mb"] = int(vram_mib)
    except ValueError:
        pass
    if driver_ver:
        info["driver_version"] = driver_ver
    info["vulkan_driver"] = "nvidia"


def _enrich_amd(info: dict[str, Any]) -> None:
    try:
        out = subprocess.check_output(
            ["vulkaninfo", "--summary"], stderr=subprocess.DEVNULL, timeout=4
        ).decode(errors="replace")
        m = re.search(r"deviceName\s*=\s*(.+)", out)
        if m:
            info["model"] = m.group(1).strip()
        m = re.search(r"driverVersion\s*=\s*(\S+)", out)
        if m:
            info["driver_version"] = m.group(1)
        m = re.search(r"driverName\s*=\s*(\S+)", out)
        if m:
            info["vulkan_driver"] = m.group(1).lower()
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        out = subprocess.check_output(
            ["glxinfo", "-B"], stderr=subprocess.DEVNULL, timeout=3
        ).decode(errors="replace")
        m = re.search(r"Mesa (\S+)", out)
        if m:
            info["mesa_version"] = m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass


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

    try:
        out = subprocess.check_output(
            ["dmidecode", "-t", "17"], stderr=subprocess.DEVNULL, timeout=3
        ).decode(errors="replace")
        m = re.search(r"Speed:\s*(\d+)\s*MT/s", out)
        if m:
            info["speed_mhz"] = int(m.group(1))
        m = re.search(r"Type:\s+(\S+)", out)
        if m and m.group(1) not in ("Unknown", "Other"):
            info["type"] = m.group(1).lower()
    except (OSError, subprocess.SubprocessError):
        pass

    return info


def _device_info() -> dict[str, Any]:
    info: dict[str, Any] = {}

    chassis = _read_str("/sys/class/dmi/id/chassis_type")
    if chassis:
        try:
            ct = int(chassis)
            if ct == 11:
                info["type"] = "handheld"
            elif ct in (8, 9, 10, 14):
                info["type"] = "laptop"
            else:
                info["type"] = "desktop"
        except ValueError:
            pass

    product = _read_str("/sys/class/dmi/id/product_name")
    if product:
        info["model"] = product

    for supply in glob.glob("/sys/class/power_supply/AC*") + glob.glob(
        "/sys/class/power_supply/ADP*"
    ):
        online = _read_int(f"{supply}/online")
        if online is not None:
            info["power_source"] = "ac" if online else "battery"
            break

    return info


class HostEnricher:
    """Collects the static host environment snapshot for the session document.

    Returns ECS fields under host.os.* and GamePulse-specific hardware
    info under gamepulse.hardware.*.
    """

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] = self._build()

    def _build(self) -> dict[str, Any]:
        os_rel = _os_release()

        # ECS host.os.* fields
        host_os: dict[str, Any] = {
            "type": "linux",
            "kernel": platform.release(),
        }
        if name := (os_rel.get("NAME") or os_rel.get("PRETTY_NAME")):
            host_os["name"] = name
        if version := (os_rel.get("VERSION_ID") or os_rel.get("BUILD_ID")):
            host_os["version"] = version
        if platform_id := os_rel.get("ID"):
            host_os["platform"] = platform_id

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

        doc: dict[str, Any] = {
            "host": {"os": host_os},
        }
        if hardware:
            doc["gamepulse"] = {"hardware": hardware}

        return doc

    @property
    def snapshot(self) -> dict[str, Any]:
        return self._snapshot
