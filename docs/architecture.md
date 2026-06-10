# Architecture

## Overview

RigSignal is a single Rust binary that runs alongside games, collecting metrics from multiple sources and shipping them to Elasticsearch. It's designed for minimal overhead (< 0.5% CPU, < 30 MB RAM) and zero configuration beyond the ES endpoint.

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

Each collector implements the `Collector` trait: read data from a source, return a typed struct. Collectors are independent — if one fails, the rest continue. Collectors are enabled/disabled per-platform via `cfg(target_os)` dispatch; on Windows, Linux-specific collectors return `Ok(None)` stubs and vice versa.

**Linux collectors:**

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

**Windows collectors (Phase C):**

| Collector | Source | Frequency |
|-----------|--------|-----------|
| CPU | PDH (Performance Data Helper) counters | Every tick |
| GPU | DXGI adapter enumeration + WMI ACPI for temperature | Every tick |
| GPU (NVIDIA) | NVML via dynamic loading (same binary works on AMD) | Every tick |
| Memory | `GlobalMemoryStatusEx` | Every tick |
| Storage | PDH disk counters | Every tick (delta) |
| Network | PDH network counters | Every tick (delta) |
| Frame timing | PresentMon ETW provider | Every tick |
| Process | Windows `OpenProcess` / `GetProcessMemoryInfo` | Every tick |

Delta-based collectors (storage, network) keep the previous snapshot in a `Mutex` and compute per-second rates.

### Game Detection (`src/detector/`)

**Current design (Phase B2+):** Launcher-agnostic. The `Target` enum resolves the game to collect from via one of:

- **Steam** — scans `/proc/*/environ` for `SteamAppId`, resolves name from `appmanifest_*.acf`
- **Lutris / Heroic / Bottles** — detects launcher process and resolves game metadata from launcher config
- **Manual** — `--target-name <name>` or `--target-pid <pid>` flag for any launcher not auto-detected (Battle.net, EA, Ubisoft, Epic, etc.)

On Windows, `/proc` scanning is cfg-gated out; game detection uses the manual target mode until B3 auto-detection lands.

Once a target is identified, the detector:

1. Resolves game name and Steam App ID (if available) from manifests or launcher config
2. Enriches with Proton/Wine env vars (`WINE_PREFIX`, `DXVK_STATE_CACHE`, `PROTON_VERSION`, etc.)
3. Detects graphics API from process memory maps, Wine DLL overrides, and DLL presence on Windows
4. Reads Mesa, gamescope, DXVK, and VKD3D versions from the Proton install directory and log files
5. Captures per-session settings (FSR/DLSS/frame-gen/RT state) at Tier 1–3 fidelity depending on what the launcher exposes

The detector maintains a cache of Steam library paths and app manifests built once at startup.

**Prior to Phase B2**, detection was Steam-only: the agent scanned `/proc/*/environ` every 5 seconds looking for processes with `SteamAppId` set. That approach is still active for Steam on Linux/SteamOS but no longer the sole detection path.

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

The BPF programs themselves are in `rigsignal-ebpf/` and compile to BPF bytecode using Aya.

### DLL Scanner (`src/dllscan.rs`)

On Windows, the DLL scanner inspects the loaded modules of the target game process to detect the active graphics runtime. It distinguishes:

- `d3d11.dll` / `d3d12.dll` — native DirectX 11/12
- `d3d11.dll` via DXVK — DirectX-over-Vulkan (emulated)
- `d3d12.dll` via VKD3D-Proton — DirectX 12-over-Vulkan
- Vulkan ICD — native Vulkan

Results populate `rigsignal.settings.graphics_api` in the session document.

### Profile Loader (`src/profiles.rs`)

Game-specific telemetry profiles live in TOML files under `profiles/` (overridable via `RIGSIGNAL_PROFILES_DIR`). A profile can set per-game collection intervals, eBPF probe selection, and metadata overrides for games that don't expose standard launch parameters. The profile loader resolves the active profile at session start based on Steam App ID or game name match.

### Diagnose subcommand (`src/diagnose.rs`)

`rigsignal diagnose` runs a pre-flight check that verifies:

1. Elasticsearch connectivity and API key permissions
2. Required index templates and component templates are present
3. MangoHud installation and CSV log path
4. eBPF capability (`CAP_BPF`, `CAP_PERFMON`, kernel BTF)
5. GPU driver version and NVML availability

Diagnose exits with a structured pass/fail report suitable for inclusion in bug reports.

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
