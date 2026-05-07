"""GPU vendor detection and collector factory."""

from __future__ import annotations

import glob
import logging
from pathlib import Path

from gamepulse.collectors.base import Collector

log = logging.getLogger(__name__)

_PCI_AMD = "0x1002"
_PCI_NVIDIA = "0x10de"


def _drm_vendor() -> str | None:
    """Return PCI vendor ID of the first discrete DRM card."""
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        vendor = None
        try:
            vendor = Path(f"{card}/device/vendor").read_text().strip()
        except OSError:
            continue
        if vendor in (_PCI_AMD, _PCI_NVIDIA):
            return vendor
    return None


def make_gpu_collector() -> Collector | None:
    """
    Detect GPU vendor and return the appropriate collector.
    Returns None if no supported GPU is found.
    """
    vendor = _drm_vendor()

    if vendor == _PCI_AMD:
        from gamepulse.collectors.gpu.amd_linux import AmdGpuCollector
        c = AmdGpuCollector()
        if c.available:
            log.info("GPU: AMD detected — using sysfs collector")
            return c
        log.warning("AMD GPU found in DRM but sysfs paths unavailable")
        return None

    if vendor == _PCI_NVIDIA:
        from gamepulse.collectors.gpu.nvidia_linux import NvidiaGpuCollector
        c = NvidiaGpuCollector()
        if c.available:
            log.info("GPU: NVIDIA detected — using nvidia-smi collector")
            return c
        log.warning("NVIDIA GPU found but nvidia-smi unavailable; install nvidia-utils")
        return None

    # No DRM match — try nvidia-smi anyway (headless / Wayland without DRM node)
    from gamepulse.collectors.gpu.nvidia_linux import NvidiaGpuCollector
    c = NvidiaGpuCollector()
    if c.available:
        log.info("GPU: no DRM vendor match, nvidia-smi responded — using NVIDIA collector")
        return c

    log.info("No supported GPU detected")
    return None
