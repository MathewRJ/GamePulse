# Architecture

## Overview

GamePulse is a single Rust binary that runs alongside games, collecting metrics from multiple sources and shipping them to Elasticsearch. It's designed for minimal overhead (< 0.5% CPU, < 30 MB RAM) and zero configuration beyond the ES endpoint.

## Data flow

```
Hardware Sensors ──┐
  (sysfs, hwmon,   │
   NVML, /proc)    │
                   ├──→ Collectors ──→ CollectedMetrics ──→ Buffer ──→ ES Bulk API
Game Process ──────┤     (1/sec)         (JSON struct)      (NDJSON)
  (auto-detected,  │
   MangoHud logs)  │
                   │
eBPF Probes ───────┘
  (kernel tracing)
```

## Components

### Collectors (`src/collector/`)

Each collector implements a simple pattern: read data from a source, return a typed struct. Collectors are independent — if one fails, the rest continue.

| Collector | Source | Frequency |
|-----------|--------|-----------|
| CPU | `/proc/stat`, sysfs cpufreq, hwmon | Every tick |
| GPU (AMD) | sysfs hwmon, `gpu_busy_percent`, `pp_dpm_sclk` | Every tick |
| GPU (NVIDIA) | NVML via `libloading` dynamic loading | Every tick |
| Memory | `/proc/meminfo`, sysinfo crate | Every tick |
| Storage | `/proc/diskstats`, sysfs block | Every tick (delta) |
| Network | `/proc/net/dev`, `/proc/net/snmp` | Every tick (delta) |
| Frame timing | MangoHud CSV log, gamescope stats | Every tick |
| Process | `/proc/PID/stat`, `/proc/PID/status`, `/proc/PID/io` | Every tick |

Delta-based collectors (storage, network) keep the previous snapshot in a `Mutex` and compute per-second rates.

### Game Detection (`src/detector/`)

The game detector scans `/proc/*/environ` every 5 seconds looking for processes with `SteamAppId` set. When found, it:

1. Resolves the game name from Steam's `appmanifest_*.acf` files
2. Detects the graphics API from process memory maps and Wine DLL overrides
3. Identifies Proton/Wine/DXVK/VKD3D versions from the Proton install directory and log files
4. Reads Mesa and gamescope versions from system commands

The detector maintains a cache of Steam library paths and app manifests, built once at startup.

### Lifecycle Manager (`src/lifecycle.rs`)

State machine managing game sessions:

```
      ┌─────┐  game detected   ┌──────────┐  game exits   ┌──────┐
      │ Idle │ ──────────────→ │ Tracking │ ────────────→ │ Idle │
      └─────┘                  └──────────┘                └──────┘
        ↑                          │                          │
        │                          │ every tick               │
        │                          ├─ collect frame timing    │
        │                          ├─ collect process metrics │
        │                          ├─ feed session summarizer │
        │                          └─ set eBPF PID filter     │
        │                                                     │
        └─────────────────────────────────────────────────────┘
```

When a game exits, the lifecycle manager finalizes the `SessionSummarizer` into a `SessionSummary` document containing aggregate statistics for the entire session.

### eBPF Manager (`src/ebpf/`)

Manages 9 independent kernel probes:

1. Checks kernel support (version, BTF, capabilities) at startup
2. Loads each probe independently — if one fails, others continue
3. Sets PID filter when a game is detected
4. Reads BPF maps each collection tick and converts to histograms
5. Correlates across probes to detect stutter causes

The BPF programs themselves are in `gamepulse-ebpf/` and compile to BPF bytecode using Aya.

### Shipper (`src/shipper/`)

The Elasticsearch shipper buffers metrics documents as NDJSON lines and flushes to the Bulk API when the batch size is reached or the flush interval expires. It handles:

- Authentication (API key or basic auth)
- Index template creation on first connect
- ILM policy deployment
- Session document indexing (PUT by ID)
- Metrics bulk indexing
- Session close (update with end timestamp and duration)

### Analytics (`src/analytics.rs`)

Post-processing and query generation:

- **Hardware tier classification** — categorizes systems by VRAM into Enthusiast/High/Mid/Low/Integrated
- **Session summarizer** — incrementally computes aggregate stats (avg FPS, median, stutter rate, thermal data) from per-second samples
- **Comparison queries** — generates Elasticsearch aggregation queries for driver/Proton/OS/kernel/storage impact analysis

## Design decisions

### Why Rust?

The agent runs alongside games where every CPU cycle and MB of RAM matters. Rust gives us zero-cost abstractions, no garbage collector pauses (which would corrupt the data we're measuring), a single static binary for easy distribution, and native eBPF support via Aya.

### Why dynamic NVML loading?

We use `libloading` to load `libnvidia-ml.so` at runtime rather than linking against the NVIDIA SDK. This means the same binary works on AMD systems (where NVML isn't installed), NVIDIA systems, and systems with no discrete GPU. No compile-time SDK dependency.

### Why MangoHud for frame timing?

MangoHud is already the standard overlay for Linux gaming. Rather than reimplementing Vulkan layer frame timing (which requires injecting into the graphics pipeline), we read MangoHud's CSV log file. This is non-intrusive, doesn't risk anti-cheat issues, and works with MangoHud's existing user base.

### Why per-second granularity?

1-second samples balance detail against data volume. At 60 FPS, each sample covers ~60 frames — enough for meaningful percentile calculation. At 1 hour of gaming, this produces 3,600 documents (~2 MB compressed) which is manageable for any Elasticsearch deployment.

### Why anonymous hardware hashing?

User identity is a SHA hash of `/etc/machine-id`. The same machine always produces the same ID (for session correlation), but the hash can't be reversed to identify the user. No usernames, emails, IPs, or other PII are ever collected.
