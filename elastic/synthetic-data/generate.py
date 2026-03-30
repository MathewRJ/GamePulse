#!/usr/bin/env python3
"""GamePulse synthetic data generator.

Produces realistic gaming session documents and time-series metrics for all
GamePulse data streams. Output is NDJSON suitable for the Elasticsearch _bulk
API or direct ingestion.

Usage:
    python generate.py --sessions 5 --duration 3600 --output bulk_data.ndjson
    python generate.py --sessions 1 --duration 600 --stdout
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

GAMES = [
    {"name": "Baldur's Gate 3", "steam_app_id": 1086940, "graphics_api": "vulkan"},
    {"name": "Cyberpunk 2077", "steam_app_id": 1091500, "graphics_api": "vulkan"},
    {"name": "Elden Ring", "steam_app_id": 1245620, "graphics_api": "dx12"},
    {"name": "Counter-Strike 2", "steam_app_id": 730, "graphics_api": "vulkan"},
    {"name": "Hogwarts Legacy", "steam_app_id": 990080, "graphics_api": "dx12"},
    {"name": "Red Dead Redemption 2", "steam_app_id": 1174180, "graphics_api": "vulkan"},
    {"name": "The Witcher 3: Wild Hunt", "steam_app_id": 292030, "graphics_api": "dx11"},
    {"name": "DOOM Eternal", "steam_app_id": 782330, "graphics_api": "vulkan"},
]

GPUS = [
    {"model": "AMD Radeon RX 7900 XTX", "vendor": "amd", "vram_mb": 24576, "driver_version": "24.1.1", "mesa_version": "24.0.2", "vulkan_driver": "radv"},
    {"model": "AMD Radeon RX 6800 XT", "vendor": "amd", "vram_mb": 16384, "driver_version": "23.3.1", "mesa_version": "23.3.6", "vulkan_driver": "radv"},
    {"model": "NVIDIA GeForce GTX 1080 Ti", "vendor": "nvidia", "vram_mb": 11264, "driver_version": "550.54.14", "mesa_version": None, "vulkan_driver": "nvidia"},
    {"model": "NVIDIA GeForce RTX 2080", "vendor": "nvidia", "vram_mb": 8192, "driver_version": "550.54.14", "mesa_version": None, "vulkan_driver": "nvidia"},
    {"model": "AMD Radeon 780M (Steam Deck OLED)", "vendor": "amd", "vram_mb": 8192, "driver_version": "23.3.4", "mesa_version": "23.3.3", "vulkan_driver": "radv"},
]

CPUS = [
    {"model": "AMD Ryzen 9 7950X", "cores": 16, "threads": 32, "base_clock_mhz": 4500, "boost_clock_mhz": 5700},
    {"model": "AMD Ryzen 7 5800X3D", "cores": 8, "threads": 16, "base_clock_mhz": 3400, "boost_clock_mhz": 4500},
    {"model": "AMD Ryzen 5 7600X", "cores": 6, "threads": 12, "base_clock_mhz": 4700, "boost_clock_mhz": 5300},
    {"model": "AMD Custom APU (Steam Deck)", "cores": 4, "threads": 8, "base_clock_mhz": 2400, "boost_clock_mhz": 3500},
    {"model": "Intel Core i7-13700K", "cores": 16, "threads": 24, "base_clock_mhz": 3400, "boost_clock_mhz": 5400},
]

OS_PROFILES = [
    {"type": "linux", "distro": "Arch Linux", "version": "rolling", "kernel": "6.7.4-arch1-1", "desktop": "KDE Plasma 6"},
    {"type": "linux", "distro": "SteamOS", "version": "3.5", "kernel": "6.1.52-valve16-1-neptune-61", "desktop": "gamescope"},
    {"type": "linux", "distro": "Fedora", "version": "39", "kernel": "6.6.9-200.fc39.x86_64", "desktop": "GNOME 45"},
    {"type": "windows", "distro": "Windows 11", "version": "23H2", "kernel": "10.0.22631", "desktop": None},
]

UPSCALERS = [
    {"type": "fsr2", "quality": "quality"},
    {"type": "fsr2", "quality": "balanced"},
    {"type": "dlss", "quality": "quality"},
    {"type": "none", "quality": None},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def jitter(base: float, pct: float = 0.1) -> float:
    """Add random jitter within +-pct of base."""
    return base * (1 + random.uniform(-pct, pct))


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def make_session_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Document generators
# ---------------------------------------------------------------------------


def gen_session_doc(
    session_id: str,
    ts: datetime,
    game: dict,
    gpu: dict,
    cpu: dict,
    os_profile: dict,
) -> dict:
    """Generate a session environment snapshot document."""
    upscaler = random.choice(UPSCALERS)
    is_linux = os_profile["type"] == "linux"

    doc = {
        "@timestamp": ts.isoformat(),
        "session_id": session_id,
        "agent_version": "0.1.0-dev",
        "user_id": f"user-{random.randint(1000, 9999)}",
        "opt_in_public": random.choice([True, False]),
        "game": {
            "name": game["name"],
            "steam_app_id": game["steam_app_id"],
            "version": f"1.{random.randint(0, 9)}.{random.randint(0, 20)}",
            "launch_args": ["-fullscreen"] if random.random() > 0.5 else [],
            "graphics_api": game["graphics_api"],
            "upscaler": upscaler if upscaler["type"] != "none" else {"type": "none"},
        },
        "os": os_profile,
        "compatibility": {},
        "hardware": {
            "cpu": cpu,
            "gpu": {
                "model": gpu["model"],
                "vendor": gpu["vendor"],
                "vram_mb": gpu["vram_mb"],
                "driver_version": gpu["driver_version"],
                "mesa_version": gpu["mesa_version"],
                "vulkan_driver": gpu["vulkan_driver"],
                "pcie_gen": random.choice([3, 4, 5]),
                "pcie_width": 16,
            },
            "ram": {
                "total_mb": random.choice([16384, 32768, 65536]),
                "speed_mhz": random.choice([3200, 3600, 5600, 6000]),
                "type": random.choice(["DDR4", "DDR5"]),
            },
            "storage": {
                "game_drive": {
                    "type": "NVMe",
                    "model": "Samsung 980 Pro 1TB",
                    "firmware": "5B2QGXA7",
                    "interface": "PCIe 4.0 x4",
                    "nvme_spec": "1.4",
                    "capacity_gb": 1000,
                    "free_gb": random.randint(100, 600),
                    "free_pct": round(random.uniform(10.0, 60.0), 1),
                    "temperature_c": round(random.uniform(30.0, 55.0), 1),
                    "smart_health": "healthy",
                    "encrypted": False,
                },
                "game_filesystem": {
                    "type": "btrfs" if is_linux else "NTFS",
                    "mount_options": ["compress=zstd:1", "noatime"] if is_linux else [],
                    "compression": "zstd" if is_linux else "none",
                    "trim_enabled": True,
                },
                "io_scheduler": "none" if is_linux else "storport",
                "read_ahead_kb": 128 if is_linux else 0,
            },
            "device": {
                "type": "desktop" if os_profile["distro"] != "SteamOS" else "handheld",
                "model": "Custom Build" if os_profile["distro"] != "SteamOS" else "Steam Deck OLED",
                "power_source": "ac" if os_profile["distro"] != "SteamOS" else random.choice(["ac", "battery"]),
                "tdp_watts": 15 if os_profile["distro"] == "SteamOS" else None,
            },
        },
    }

    if is_linux and game["graphics_api"] in ("dx11", "dx12"):
        doc["compatibility"] = {
            "proton_version": "Proton 9-3",
            "wine_version": "wine-9.0",
            "dxvk_version": "2.3.1" if game["graphics_api"] == "dx11" else None,
            "vkd3d_proton_version": "2.12" if game["graphics_api"] == "dx12" else None,
        }
        if os_profile["distro"] == "SteamOS":
            doc["compatibility"]["gamescope_version"] = "3.14.2"

    return doc


def gen_frame_metric(session_id: str, ts: datetime, base_fps: float) -> dict:
    """Generate a single frame performance metric."""
    fps = max(1, int(jitter(base_fps, 0.15)))
    frametime = round(1000.0 / fps, 2)
    variance = round(abs(random.gauss(0, 2.5)), 2)
    # Occasional stutter spike
    if random.random() < 0.02:
        variance = round(random.uniform(10.0, 30.0), 2)
        frametime = round(frametime * random.uniform(1.5, 3.0), 2)

    return {
        "@timestamp": ts.isoformat(),
        "session_id": session_id,
        "fps": {
            "current": fps,
            "avg_1s": round(jitter(fps, 0.05), 1),
            "low_1pct": max(1, int(fps * random.uniform(0.5, 0.8))),
            "low_01pct": max(1, int(fps * random.uniform(0.3, 0.6))),
            "frametime_ms": frametime,
            "frametime_variance": variance,
            "present_mode": "flip",
        },
    }


def gen_gpu_metric(session_id: str, ts: datetime, gpu: dict) -> dict:
    """Generate GPU telemetry for one second."""
    util = clamp(random.gauss(85, 12), 0, 100)
    temp = clamp(random.gauss(72, 8), 25, 105)
    return {
        "@timestamp": ts.isoformat(),
        "session_id": session_id,
        "gpu": {
            "utilisation_pct": round(util, 1),
            "clock_mhz": int(jitter(2200 if "7900" in gpu["model"] else 1800, 0.05)),
            "memory_used_mb": int(jitter(gpu["vram_mb"] * 0.6, 0.2)),
            "memory_total_mb": gpu["vram_mb"],
            "temperature_c": round(temp, 1),
            "hotspot_c": round(temp + random.uniform(5, 15), 1),
            "memory_temperature_c": round(temp - random.uniform(5, 10), 1),
            "power_w": round(jitter(250 if "7900" in gpu["model"] else 200, 0.1), 1),
            "fan_pct": round(clamp(temp * 0.8 + random.uniform(-5, 10), 0, 100), 1),
            "fan_speed_rpm": int(jitter(1200, 0.15)),
            "voltage": round(random.uniform(0.9, 1.15), 3),
        },
    }


def gen_cpu_metric(session_id: str, ts: datetime, cpu: dict) -> dict:
    """Generate CPU telemetry for one second."""
    total_util = clamp(random.gauss(45, 15), 0, 100)
    return {
        "@timestamp": ts.isoformat(),
        "session_id": session_id,
        "cpu": {
            "total_utilisation_pct": round(total_util, 1),
            "game_utilisation_pct": round(clamp(total_util * random.uniform(0.5, 0.9), 0, 100), 1),
            "per_core": [round(clamp(random.gauss(total_util, 20), 0, 100), 1) for _ in range(cpu["cores"])],
            "clock_mhz_avg": int(jitter(cpu["boost_clock_mhz"] * 0.85, 0.08)),
            "temperature_c": round(clamp(random.gauss(65, 10), 25, 100), 1),
            "power_w": round(jitter(95, 0.2), 1),
            "governor": "performance",
            "boost_state": True,
        },
    }


def gen_memory_metric(session_id: str, ts: datetime) -> dict:
    """Generate memory telemetry for one second."""
    return {
        "@timestamp": ts.isoformat(),
        "session_id": session_id,
        "memory": {
            "system_used_mb": int(jitter(12000, 0.1)),
            "game_rss_mb": int(jitter(6000, 0.15)),
            "virtual_mb": int(jitter(14000, 0.05)),
            "page_cache_mb": int(jitter(3000, 0.2)),
            "shared_mb": int(jitter(500, 0.1)),
            "swap_used_mb": random.choice([0, 0, 0, int(jitter(200, 0.5))]),
            "page_faults_major": random.randint(0, 3),
            "page_faults_minor": random.randint(50, 500),
            "oom_events": 0,
        },
    }


def gen_storage_metric(session_id: str, ts: datetime) -> dict:
    """Generate storage I/O telemetry for one second."""
    read_mbps = round(jitter(150, 0.4), 1)
    write_mbps = round(jitter(30, 0.5), 1)
    return {
        "@timestamp": ts.isoformat(),
        "session_id": session_id,
        "storage": {
            "read_mbps": read_mbps,
            "write_mbps": write_mbps,
            "read_iops": int(read_mbps * random.uniform(50, 200)),
            "write_iops": int(write_mbps * random.uniform(20, 100)),
            "io_latency_read_us": {
                "avg": random.randint(30, 120),
                "p50": random.randint(20, 80),
                "p95": random.randint(100, 500),
                "p99": random.randint(300, 2000),
            },
            "io_latency_write_us": {
                "avg": random.randint(40, 150),
                "p50": random.randint(30, 100),
                "p95": random.randint(150, 600),
                "p99": random.randint(400, 3000),
            },
            "queue_depth_current": random.randint(1, 8),
            "queue_depth_max": random.randint(8, 32),
            "io_wait_pct": round(random.uniform(0, 5), 2),
            "merged_reads": random.randint(0, 50),
            "merged_writes": random.randint(0, 20),
            "game_process_read_mb": round(read_mbps * random.uniform(0.3, 0.8), 2),
            "game_process_write_mb": round(write_mbps * random.uniform(0.1, 0.5), 2),
            "game_process_read_ops": random.randint(100, 5000),
            "drive_temperature_c": round(random.uniform(32, 50), 1),
        },
    }


def gen_network_metric(session_id: str, ts: datetime) -> dict:
    return {
        "@timestamp": ts.isoformat(),
        "session_id": session_id,
        "network": {
            "rtt_ms": round(random.uniform(5, 80), 1),
            "packets_sent": random.randint(50, 500),
            "packets_received": random.randint(50, 500),
            "packet_loss_pct": round(random.uniform(0, 2), 2),
            "bandwidth_utilisation_mbps": round(random.uniform(0.5, 10), 2),
            "connection_type": random.choice(["Ethernet", "WiFi"]),
        },
    }


# ---------------------------------------------------------------------------
# Bulk output helpers
# ---------------------------------------------------------------------------


def bulk_action(data_stream: str) -> dict:
    return {"create": {"_index": data_stream}}


def generate_session_data(
    num_sessions: int,
    duration_seconds: int,
    sample_interval: int = 1,
) -> list[str]:
    """Generate NDJSON lines for the _bulk API."""
    lines: list[str] = []
    now = datetime.now(timezone.utc)

    for i in range(num_sessions):
        session_id = make_session_id()
        game = random.choice(GAMES)
        gpu = random.choice(GPUS)
        cpu = random.choice(CPUS)
        os_profile = random.choice(OS_PROFILES)
        session_start = now - timedelta(hours=num_sessions - i)
        base_fps = random.uniform(30, 144)

        # Session document
        session_doc = gen_session_doc(session_id, session_start, game, gpu, cpu, os_profile)
        lines.append(json.dumps(bulk_action("metrics-gamepulse.session-default")))
        lines.append(json.dumps(session_doc))

        # Time-series metrics at sample_interval
        for sec in range(0, duration_seconds, sample_interval):
            ts = session_start + timedelta(seconds=sec)

            # Frame metrics (every second)
            lines.append(json.dumps(bulk_action("metrics-gamepulse.frame-default")))
            lines.append(json.dumps(gen_frame_metric(session_id, ts, base_fps)))

            # GPU metrics (every second)
            lines.append(json.dumps(bulk_action("metrics-gamepulse.gpu-default")))
            lines.append(json.dumps(gen_gpu_metric(session_id, ts, gpu)))

            # CPU metrics (every second)
            lines.append(json.dumps(bulk_action("metrics-gamepulse.cpu-default")))
            lines.append(json.dumps(gen_cpu_metric(session_id, ts, cpu)))

            # Memory metrics (every 5 seconds)
            if sec % 5 == 0:
                lines.append(json.dumps(bulk_action("metrics-gamepulse.memory-default")))
                lines.append(json.dumps(gen_memory_metric(session_id, ts)))

            # Storage metrics (every 5 seconds)
            if sec % 5 == 0:
                lines.append(json.dumps(bulk_action("metrics-gamepulse.storage-default")))
                lines.append(json.dumps(gen_storage_metric(session_id, ts)))

            # Network metrics (every 10 seconds)
            if sec % 10 == 0:
                lines.append(json.dumps(bulk_action("metrics-gamepulse.network-default")))
                lines.append(json.dumps(gen_network_metric(session_id, ts)))

    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="GamePulse synthetic data generator")
    parser.add_argument("--sessions", type=int, default=3, help="Number of gaming sessions to generate (default: 3)")
    parser.add_argument("--duration", type=int, default=600, help="Duration of each session in seconds (default: 600)")
    parser.add_argument("--interval", type=int, default=1, help="Sample interval in seconds (default: 1)")
    parser.add_argument("--output", type=str, default=None, help="Output file path (NDJSON). If omitted, writes to stdout")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible output")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    lines = generate_session_data(args.sessions, args.duration, args.interval)

    # _bulk API requires trailing newline
    output = "\n".join(lines) + "\n"

    if args.output:
        Path(args.output).write_text(output)
        doc_count = len(lines) // 2
        print(f"Wrote {doc_count:,} documents ({len(lines):,} lines) to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)
        doc_count = len(lines) // 2
        print(f"Generated {doc_count:,} documents", file=sys.stderr)


if __name__ == "__main__":
    main()
