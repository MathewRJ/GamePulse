"""
Audio collector — PipeWire/PulseAudio backend detection and xrun counting.

xruns (buffer underruns/overruns) are the audio equivalent of frame drops:
each one produces a click or glitch. Games with heavy CPU load often trigger
audio xruns during stutter events, making this a useful correlation signal.

Fields match gamepulse-audio-mappings component template exactly.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector


def _detect_backend() -> str:
    """Return 'pipewire', 'pulseaudio', 'alsa', or 'unknown'."""
    # PipeWire exposes itself as PulseAudio too; check PipeWire first
    if shutil.which("pw-cli"):
        try:
            out = subprocess.check_output(
                ["pw-cli", "info", "0"], stderr=subprocess.DEVNULL, timeout=2
            ).decode(errors="replace")
            if "PipeWire" in out or "pipewire" in out.lower():
                return "pipewire"
        except (OSError, subprocess.SubprocessError):
            pass

    if shutil.which("pactl"):
        try:
            out = subprocess.check_output(
                ["pactl", "info"], stderr=subprocess.DEVNULL, timeout=2
            ).decode(errors="replace")
            if "PipeWire" in out:
                return "pipewire"
            if "PulseAudio" in out:
                return "pulseaudio"
        except (OSError, subprocess.SubprocessError):
            pass

    if shutil.which("aplay"):
        return "alsa"

    return "unknown"


def _pipewire_stats() -> dict[str, Any] | None:
    """
    Read xruns and buffer info from `pw-top --count 1` or pw-dump.
    pw-top outputs one line per node; we sum xruns across all active nodes.
    """
    if not shutil.which("pw-top"):
        return None
    try:
        out = subprocess.check_output(
            ["pw-top", "-b"],  # batch/non-interactive mode
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None

    total_xruns = 0
    latency_ms: float | None = None

    for line in out.splitlines():
        # pw-top columns: ID  QUANT  RATE  WAIT  BUSY  W/Q  B/Q  ERRORS  NAME
        # or similar depending on version; look for numeric xrun column
        m = re.search(r"\s+(\d+)\s+ERR", line)
        if m:
            total_xruns += int(m.group(1))
        # Latency from quantum/rate
        m = re.search(r"(\d+)/(\d+)", line)
        if m and latency_ms is None:
            quant, rate = int(m.group(1)), int(m.group(2))
            if rate > 0:
                latency_ms = round(quant / rate * 1000, 2)

    return {"xruns": total_xruns, "latency_ms": latency_ms}


def _pulseaudio_stats() -> dict[str, Any] | None:
    """Read xrun count and sample rate from pactl stat."""
    if not shutil.which("pactl"):
        return None
    try:
        out = subprocess.check_output(
            ["pactl", "stat"], stderr=subprocess.DEVNULL, timeout=2
        ).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None

    info: dict[str, Any] = {}
    for line in out.splitlines():
        if "Sample Specification" in line:
            m = re.search(r"(\d+)\s*Hz", line)
            if m:
                info["sample_rate_hz"] = int(m.group(1))
    return info if info else None


class AudioCollector(Collector):
    data_stream = "metrics-gamepulse.audio-default"

    def __init__(self) -> None:
        self._backend = _detect_backend()
        self._prev_xruns: int | None = None

    def collect(self) -> dict[str, Any] | None:
        audio: dict[str, Any] = {"backend": self._backend}

        if self._backend in ("pipewire",):
            stats = _pipewire_stats()
            if stats:
                xruns_total = stats.get("xruns", 0)
                # Report delta xruns since last tick (cumulative → per-second)
                if self._prev_xruns is not None:
                    audio["xruns"] = max(0, xruns_total - self._prev_xruns)
                self._prev_xruns = xruns_total
                if stats.get("latency_ms") is not None:
                    audio["latency_ms"] = stats["latency_ms"]

        elif self._backend == "pulseaudio":
            stats = _pulseaudio_stats()
            if stats:
                if "sample_rate_hz" in stats:
                    audio["sample_rate_hz"] = stats["sample_rate_hz"]

        # Always return the backend even if we couldn't read stats
        return {"audio": audio}
