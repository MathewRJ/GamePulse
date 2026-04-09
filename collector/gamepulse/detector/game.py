"""
Steam game detector.

Scans /proc/*/environ every poll_interval seconds looking for processes
with SteamAppId set. When found, resolves the game name from Steam's
appmanifest ACF files and extracts Proton/DXVK/VKD3D version strings.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class DetectedGame:
    name: str
    steam_app_id: int
    pid: int
    graphics_api: str | None
    uses_proton: bool
    proton_version: str | None
    dxvk_version: str | None
    vkd3d_version: str | None
    # All PIDs associated with this game (includes wine/Proton subprocesses).
    # Used by the eBPF daemon to build a complete TID filter.
    all_pids: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.all_pids is None:
            self.all_pids = [self.pid]


def _read_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        pairs = raw.split(b"\x00")
        result = {}
        for pair in pairs:
            if b"=" in pair:
                k, _, v = pair.partition(b"=")
                try:
                    result[k.decode()] = v.decode(errors="replace")
                except UnicodeDecodeError:
                    pass
        return result
    except (OSError, PermissionError):
        return {}


_ACF_PATTERN = re.compile(r'"name"\s+"([^"]+)"')


def _game_name_from_appid(app_id: int) -> str | None:
    """Search Steam library paths for the appmanifest ACF file."""
    steam_roots = [
        Path.home() / ".steam" / "steam" / "steamapps",
        Path.home() / ".local" / "share" / "Steam" / "steamapps",
        # Flatpak Steam
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam" / "steamapps",
    ]

    # Also check libraryfolders.vdf for additional library paths
    extra_roots: list[Path] = []
    for root in steam_roots:
        vdf_path = root / "libraryfolders.vdf"
        if vdf_path.exists():
            try:
                content = vdf_path.read_text()
                for match in re.finditer(r'"path"\s+"([^"]+)"', content):
                    extra_roots.append(Path(match.group(1)) / "steamapps")
            except OSError:
                pass

    for root in steam_roots + extra_roots:
        acf = root / f"appmanifest_{app_id}.acf"
        if acf.exists():
            try:
                content = acf.read_text()
                m = _ACF_PATTERN.search(content)
                if m:
                    return m.group(1)
            except OSError:
                pass
    return None


def _detect_graphics_api(env: dict[str, str]) -> tuple[str | None, bool]:
    """Return (graphics_api, uses_proton) from a process environment."""
    uses_proton = "PROTON_VERSION" in env or "STEAM_COMPAT_DATA_PATH" in env

    # Check DLL overrides for translation layer hints
    dll_overrides = env.get("WINEDLLOVERRIDES", "").lower()
    if "vkd3d" in dll_overrides or env.get("VKD3D_CONFIG"):
        return "dx12_via_vkd3d", uses_proton
    if "dxvk" in dll_overrides or env.get("DXVK_CONFIG_FILE"):
        return "dx11_via_dxvk", uses_proton
    if "VULKAN_DEVICE_INDEX" in env or env.get("VK_ICD_FILENAMES"):
        return "vulkan", uses_proton
    if uses_proton:
        return "dx_via_proton", uses_proton

    return None, False


def _proton_version(env: dict[str, str]) -> str | None:
    # Proton sets this in newer versions
    v = env.get("PROTON_VERSION")
    if v:
        return v
    # Fall back to reading the version file from the Proton install path
    compat_path = env.get("STEAM_COMPAT_TOOL_PATHS", "")
    if compat_path:
        for part in compat_path.split(":"):
            vf = Path(part) / "version"
            if vf.exists():
                try:
                    return vf.read_text().strip()
                except OSError:
                    pass
    return None


def _dxvk_version(env: dict[str, str]) -> str | None:
    # DXVK logs its version in the log file; check env for hints
    log_path = env.get("DXVK_LOG_PATH", "")
    if log_path:
        log_file = Path(log_path) / "dxvk.log"
        if log_file.exists():
            try:
                first_line = log_file.read_text().splitlines()[0]
                m = re.search(r"v(\d+\.\d+[\.\d]*)", first_line)
                if m:
                    return m.group(1)
            except (OSError, IndexError):
                pass
    return None


def _vkd3d_version(env: dict[str, str]) -> str | None:
    # VKD3D-Proton version from the DLL itself is hard to get; skip for now
    return None


class GameDetector:
    """
    Polls /proc every poll_interval seconds for a running Steam game.
    Call detect() each tick; it returns the current game or None.
    """

    def __init__(self, poll_interval: float = 5.0) -> None:
        self._poll_interval = poll_interval
        self._last_scan: float = 0.0
        self._current: DetectedGame | None = None

    def detect(self) -> DetectedGame | None:
        now = time.monotonic()
        if now - self._last_scan < self._poll_interval:
            return self._current

        self._last_scan = now
        self._current = self._scan()
        return self._current

    def _scan(self) -> DetectedGame | None:
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return None

        # First pass: group all PIDs by SteamAppId and find the representative
        # (non-helper) process for metadata.
        representative: tuple[int, dict[str, str]] | None = None
        all_pids_by_appid: dict[int, list[int]] = {}

        for pid in pids:
            env = _read_environ(pid)
            if not env:
                continue

            app_id_str = env.get("SteamAppId") or env.get("STEAM_APP_ID")
            if not app_id_str:
                continue

            try:
                app_id = int(app_id_str)
            except ValueError:
                continue

            if app_id == 0:
                continue

            all_pids_by_appid.setdefault(app_id, []).append(pid)

            # Use the first non-helper process as the representative for metadata.
            # Wine/Proton subprocesses are valid game PIDs for eBPF filtering but
            # are not good sources of metadata (graphics_api, proton_version, etc.).
            if representative is None:
                exe_path = ""
                try:
                    exe_path = os.readlink(f"/proc/{pid}/exe")
                except OSError:
                    pass
                if not any(skip in exe_path for skip in ("proton", "wine", "steam", "reaper")):
                    representative = (pid, env)

        if not all_pids_by_appid:
            return None

        # If every process was a helper (no representative found), use the first pid.
        if representative is None:
            app_id = next(iter(all_pids_by_appid))
            first_pid = all_pids_by_appid[app_id][0]
            representative = (first_pid, _read_environ(first_pid))
        else:
            app_id = int(
                representative[1].get("SteamAppId")
                or representative[1].get("STEAM_APP_ID")
                or 0
            )

        pid, env = representative
        name = _game_name_from_appid(app_id) or f"App {app_id}"
        api, uses_proton = _detect_graphics_api(env)

        return DetectedGame(
            name=name,
            steam_app_id=app_id,
            pid=pid,
            graphics_api=api,
            uses_proton=uses_proton,
            proton_version=_proton_version(env),
            dxvk_version=_dxvk_version(env),
            vkd3d_version=_vkd3d_version(env),
            all_pids=all_pids_by_appid.get(app_id, [pid]),
        )
