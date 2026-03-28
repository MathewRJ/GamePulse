GamePulse — Open Gaming Telemetry Platform
Project Scope & Implementation Plan
Version: 2.0 — Reconciled & Expanded Date: March 2026

1. Vision & Problem Statement
The Problem
There is no unified, open platform for collecting, comparing, and analysing real-world gaming performance across hardware configurations, operating systems, driver versions, Proton/Wine layers, and kernel versions. Existing tools (MSI Afterburner, MangoHud, CapFrameX) are local-only, siloed, and lack the ability to:

Compare performance across configurations systematically
Share structured telemetry with developers, journalists, and maintainers
Correlate low-level system behaviour (I/O stalls, shader compilation, memory pressure) with user-visible performance drops
Provide actionable data to the people who can actually fix problems (engine devs, driver teams, Proton maintainers, distro packagers)
The Vision
A lightweight, privacy-respecting agent that runs alongside games, collects comprehensive telemetry, and ships it to Elasticsearch — enabling:

Audience	What they get
Gamers	"Is Starfield actually smoother on Proton 9.0-4 than 9.0-3 on my hardware?" — answered with real data, not anecdotes
Game developers	Frame-time distributions, stutter patterns, CPU/GPU bottleneck identification, syscall profiles (via eBPF) across player hardware
Engine developers	Correlate engine-level events with kernel scheduling, I/O patterns, memory pressure — identify optimisation opportunities invisible from userspace
Driver/Mesa developers	Per-driver-version regression detection across games and hardware classes
Proton/Wine maintainers	Quantified performance deltas between Proton versions, per game, per hardware config
Journalists/reviewers	Reproducible, data-backed performance comparisons rather than "it felt smoother"
Distro/package maintainers	Impact of kernel versions, Mesa builds, gamescope updates on real-world gaming workloads
2. Architecture Overview
┌─────────────────────────────────────────────────────────┐
│                    USER'S GAMING PC                      │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  Game Process │  │  System APIs │  │  eBPF Probes  │  │
│  │  (detected)   │  │  (hw sensors)│  │  (kernel-lvl) │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │
│         │                 │                   │          │
│  ┌──────▼─────────────────▼───────────────────▼───────┐  │
│  │              GamePulse Collector                    │  │
│  │  ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌────────┐  │  │
│  │  │Collector│ │ Enricher │ │ Buffer  │ │Shipper │  │  │
│  │  │ Modules │ │(metadata)│ │(queue)  │ │(ES API)│  │  │
│  │  └─────────┘ └──────────┘ └─────────┘ └────────┘  │  │
│  └────────────────────────────────────────────────────┘  │
│                                                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │          Elastic Agent Integration Wrapper          │  │
│  │          (Phase 4 — managed distribution)           │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────┬───────────────────────────────┘
                          │ HTTPS (Elasticsearch Bulk API)
                          ▼
┌─────────────────────────────────────────────────────────┐
│              ELASTIC CLOUD SERVERLESS                    │
│                                                         │
│  ┌──────────┐  ┌────────────┐  ┌─────────────────────┐  │
│  │  Ingest  │  │   Data     │  │  Kibana Dashboards  │  │
│  │ Pipelines│  │  Streams   │  │  (pre-built)        │  │
│  │(enrich,  │  │            │  │                     │  │
│  │ validate,│  │ - frame    │  │  - Session overview │  │
│  │ derive)  │  │ - gpu      │  │  - Config compare   │  │
│  │          │  │ - system   │  │  - Regression detect│  │
│  │          │  │ - session  │  │  - eBPF flamegraphs │  │
│  │          │  │ - ebpf     │  │  - Community stats  │  │
│  │          │  │ - events   │  │  - Game library     │  │
│  └──────────┘  └────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────┘
Why Elastic Agent (eventual distribution wrapper)?
Fleet management — push configuration changes to all enrolled agents centrally from Kibana, rather than asking users to update a config file
eBPF is already integrated — Elastic's Universal Profiling uses eBPF; we can leverage this infrastructure
Buffering and reliability — Agent handles backpressure, retries, and local queuing if the network drops
Enterprise features — tamper protection, diagnostics, and centralised monitoring of agent health (available on Enterprise licence)
Custom integrations — Elastic supports building custom integration packages that plug into Agent; GamePulse becomes an integration users enable, not a separate binary
3. Development Strategy: Two-Track Approach
Decision: Python Prototype + Rust Production Agent
This is the most significant architectural decision in the project. The two candidates:

Consideration	Rust from day one	Python prototype → Rust production
Time to first data in Kibana	~3 weeks (fighting cross-platform abstractions)	~3-5 days (sysfs/procfs trivial in Python)
Dashboard iteration speed	Slow (data model changes require recompile)	Fast (change a field, re-run, see result)
Runtime overhead	<0.5% CPU, <10MB RAM	~1-2% CPU, ~50MB RAM
eBPF	Aya (Rust-native, excellent)	libbpf bindings or shelling out
Distribution	Single static binary	Requires Python runtime
Community contributions	Higher barrier	Lower barrier
Production quality	Production-ready from start	Prototype needs rewrite
The choice: Python first, Rust for production. Here's why:

Your #1 priority is polished dashboards. Dashboards need data. Python gets data flowing in days, not weeks. Every day without data is a day we can't iterate on the visualisations that make this project valuable.
The data model needs validation. We don't know if our field names, granularity, and index structure are right until we run real gaming sessions. Iterating on this in Python (change a dict, re-run) is 10× faster than in Rust (change a struct, recompile, re-test serialisation).
Collection frequency is 1/second. At this rate, Python's overhead is negligible. We're not instrumenting every frame — we're reading sysfs files and parsing MangoHud output once per second. Python does this comfortably.
The Elasticsearch side is language-agnostic. Index templates, ingest pipelines, dashboards, ILM policies — none of this cares what language ships the data. All the Elastic infrastructure we build in Phase 0-2 carries forward unchanged to the Rust rewrite.
The Rust rewrite is Phase 4, when the data model is stable. By then we know exactly what fields we need, what the edge cases are, and what the performance budget looks like. The rewrite is mechanical, not exploratory.
What stays in Rust from the start: eBPF programs (Phase 5). These must be performant, and Aya (Rust-native eBPF) is the right tool. The eBPF subsystem is a separate binary anyway — it feeds data to the collector, which ships it.

Track 1: Elasticsearch Infrastructure (starts immediately)
Index templates, component templates, ingest pipelines, data stream configuration, Kibana dashboards, synthetic data generators. This is the foundation everything else builds on.

Track 2: Python Collector (starts in parallel)
Cross-platform metric collection, game detection, environment fingerprinting, ES shipping. Validates the data model with real gaming sessions.

Track 3: Rust Agent (Phase 4, after data model stabilises)
Production-quality rewrite. Single binary, minimal overhead, Elastic Agent integration, distribution packaging.

4. Metric Categories — Full Inventory
4.1 Frame Performance (Priority: Critical)
Metric	Source (Windows)	Source (Linux)
FPS (current, avg, 1% low, 0.1% low)	PresentMon / ETW	MangoHud / Vulkan layer
Frame time (ms per frame)	PresentMon / ETW	MangoHud / Vulkan layer
Frame time variance / jitter	Calculated	Calculated
Present mode (flip/copy/composition)	DXGI / ETW	Gamescope / Wayland info
V-Sync state	DXGI / driver API	DRM / driver API
API in use (DX11/DX12/Vulkan/OpenGL)	ETW / process inspection	Vulkan layer / procfs
Resolution & refresh rate	DXGI / Display API	DRM / xrandr / gamescope
HDR state	DXGI	Gamescope / KMS
FSR/DLSS/XeSS state & quality	Game-specific / driver hints	Game-specific / driver hints
4.2 GPU Metrics (Priority: Critical)
Metric	Source (Windows)	Source (Linux)
GPU utilisation %	NVML / ADL / ADLX	sysfs (hwmon) / NVML
GPU clock speed (current/max)	NVML / ADL	sysfs / NVML
GPU memory used / total	NVML / ADL	sysfs / NVML
GPU temperature (core/hotspot/memory)	NVML / ADL	sysfs (hwmon)
GPU power draw (W)	NVML / ADL	sysfs (hwmon)
GPU fan speed (RPM / %)	NVML / ADL	sysfs (hwmon)
GPU voltage	NVML / ADL	sysfs where available
PCIe link speed/width	NVML / ADL	sysfs (pcie)
Shader compilation events	ETW / driver API	Vulkan pipeline cache / eBPF
GPU pipeline state (occupancy)	GPUPerfAPI (AMD) / NVML	AMDGPU perf counters / sysfs
4.3 CPU Metrics (Priority: Critical)
Metric	Source (Windows)	Source (Linux)
Per-core utilisation %	PDH / WMI	/proc/stat
Per-core clock speed	WMI / CPUID	/sys/devices/cpu/cpufreq
CPU temperature (per-die/package)	WMI / LibreHardwareMonitor	sysfs (hwmon) / k10temp / coretemp
CPU power draw (package)	RAPL via WMI	sysfs (RAPL)
Thread count (game process)	Process API	/proc/[pid]/status
Context switches/sec	ETW	/proc/[pid]/status / eBPF
IPC (instructions per cycle)	PMU via ETW	perf_event / eBPF
CPU governor / boost state	Power plan API	cpufreq sysfs
C-state residency	ETW	sysfs / turbostat
4.4 Memory (Priority: High)
Metric	Source (Windows)	Source (Linux)
RAM used / total / available	GlobalMemoryStatusEx	/proc/meminfo
Game process RSS / VMS	Process API	/proc/[pid]/status
Swap usage	Performance counters	/proc/meminfo
Page faults (major/minor)	ETW	/proc/[pid]/stat / eBPF
4.5 Storage & Filesystem (Priority: High)
This is an expanded section — storage behaviour is one of the most underexplored factors in gaming performance, particularly on Steam Deck where SD card vs NVMe makes an enormous real-world difference.

4.5.1 Storage Device Identification (per session, per drive)
Metric	Source (Windows)	Source (Linux)
Drive type (NVMe/SATA SSD/HDD/SD card/USB)	WMI / DeviceIoControl	sysfs (rotational, removable, transport)
Drive model & firmware version	WMI / SMART	smartctl / sysfs
Drive interface (PCIe Gen3/4/5, SATA III, UHS-I/II/III)	WMI / DeviceIoControl	sysfs (pcie link, mmc)
Drive capacity & free space	GetDiskFreeSpaceEx	statvfs
PCIe lanes for NVMe	WMI	sysfs (current_link_width)
NVMe spec version	WMI / nvme-cli	nvme-cli / sysfs
SD card class/speed rating (UHS, A1/A2, V30)	WMI	sysfs (mmc)
SMART health status	WMI / CrystalDiskInfo API	smartctl
Drive temperature (if reported)	WMI / SMART	sysfs (hwmon) / smartctl
Drive encryption (BitLocker/LUKS)	WMI / manage-bde	lsblk / cryptsetup
4.5.2 Filesystem Metrics (per session)
Metric	Source (Windows)	Source (Linux)
Filesystem type (NTFS/ext4/btrfs/f2fs/FAT32/exFAT)	GetVolumeInformation	/proc/mounts / statfs
Mount options (noatime, compress, discard)	N/A (less relevant)	/proc/mounts
Btrfs compression algo & level (zstd/lzo/none)	N/A	btrfs property / mount opts
Block size	GetDiskFreeSpace	statfs
TRIM/discard support & status	WMI / fsutil	sysfs (discard_granularity)
I/O scheduler in use	N/A	/sys/block/*/queue/scheduler
Read-ahead setting	N/A	/sys/block/*/queue/read_ahead_kb
Journal mode (ext4)	N/A	/proc/mounts / tune2fs
4.5.3 Storage I/O Metrics (per second, during gameplay)
Metric	Source (Windows)	Source (Linux)
Read/write throughput (MB/s)	PDH / ETW	/proc/diskstats / eBPF
Read/write IOPS	ETW	/proc/diskstats / eBPF
I/O latency (avg, p50, p95, p99)	ETW	eBPF (biolatency)
I/O queue depth (current, max)	PDH	/sys/block/*/stat
I/O merge rate	ETW	/proc/diskstats
I/O wait % (CPU time on storage)	PDH	/proc/stat (iowait)
Game-process-specific I/O	ETW / Process API	/proc/[pid]/io
Drive temperature during load	SMART	sysfs / smartctl
4.5.4 Key Storage Scenarios to Capture
Scenario	Why it matters
SD card vs NVMe on Steam Deck	Quantify real-world load time and stutter difference
Btrfs vs ext4 vs f2fs gaming perf	Filesystem choice affects I/O patterns significantly
Btrfs compression impact (zstd levels)	CPU trade-off vs storage throughput
Encrypted vs unencrypted drive	LUKS/BitLocker overhead during gameplay
Full drive (>90% capacity) vs free	SSD performance degrades when near-full
SATA SSD vs NVMe for open-world games	Streaming/loading difference in practice
USB external drive gaming	Common setup, rarely benchmarked properly
I/O scheduler impact (mq-deadline vs bfq vs none)	Linux tuning for gaming workloads
DirectStorage / GPU decompression	Future Windows/Linux path
4.6 Network (Priority: Medium — multiplayer relevance)
Metric	Source (Windows)	Source (Linux)
Network RTT to game server	ETW / raw sockets	eBPF (tcp_rtt)
Packets sent/received	Performance counters	/proc/net/dev / eBPF
Packet loss %	Calculated	eBPF
Bandwidth utilisation	Performance counters	/proc/net/dev
Connection type (WiFi/Ethernet)	WMI	NetworkManager / sysfs
4.7 System Environment (Priority: Critical — once per session)
Metric	Source (Windows)	Source (Linux)
OS version & build	Registry / WMI	/etc/os-release / uname
Kernel version	N/A (NT version)	uname -r
GPU driver version	Registry / NVML / ADL	modinfo / sysfs
Mesa version	N/A	glxinfo / vulkaninfo
Vulkan driver version	vulkaninfo	vulkaninfo
Proton version	N/A	$PROTON_VERSION / steam compat
Wine version	N/A	wine --version (via Proton)
DXVK version	N/A	DXVK_LOG or file version
VKD3D-Proton version	N/A	env vars / file version
Gamescope version	N/A	gamescope --version
Steam Deck model/variant	N/A	DMI / device-specific sysfs
CPU model & core count	CPUID / WMI	/proc/cpuinfo
GPU model & VRAM	NVML / ADL / DXGI	sysfs / lspci
RAM capacity, speed & type	WMI / SMBIOS	dmidecode / sysfs
Display resolution & refresh	DXGI	DRM / xrandr
Power profile (battery/AC)	Power API	upower / sysfs
BIOS/UEFI version	WMI	dmidecode
Motherboard model	WMI	dmidecode
4.8 Game Context (Priority: Critical — per session)
Metric	Source (Windows)	Source (Linux)
Game name (auto-detected)	Process name + Steam API	Process name + Steam API
Steam App ID	Steam client API / registry	Steam client API / env
Game version / build	File version info	Steam manifest
Game launch parameters	Command line inspection	/proc/[pid]/cmdline
In-game graphics settings	Config file parsing (per-game)	Config file parsing
Mod list (if applicable)	Game-specific	Game-specific
4.9 Audio Pipeline (Priority: Medium — often overlooked)
Metric	Source (Windows)	Source (Linux)
Audio backend & version	WASAPI enumeration	PipeWire/PulseAudio version
Buffer underruns (xruns)	WASAPI diagnostics	PipeWire/PA stats
Audio latency (roundtrip)	WASAPI	PipeWire/PA stats
Sample rate & buffer size	WASAPI	PipeWire/PA config
4.10 Power & Battery (Priority: High for handhelds)
Metric	Source (Windows)	Source (Linux)
Battery drain rate	Power API	upower / sysfs
TDP limit (configurable on Deck)	Vendor API	sysfs / ryzenadj
Power plan / governor	Power API	cpufreq sysfs
AC vs battery state	Power API	upower / sysfs
Performance-per-watt (derived)	Calculated	Calculated
4.11 Display Output (Priority: Medium)
Metric	Source (Windows)	Source (Linux)
Actual achieved refresh rate	DXGI / ETW	DRM / gamescope
VSync / FreeSync / G-Sync status	Driver API	DRM properties
HDR active / colour space	DXGI	KMS / gamescope
FSR/DLSS/XeSS mode & scaling	Game/driver specific	Game/driver specific
Compositor latency contribution	DWM stats	Gamescope / Wayland
4.12 eBPF Deep Telemetry (Priority: High — unique differentiator)
Metric	Purpose	eBPF Program Type
Syscall latency distribution	Identify kernel-level bottlenecks	tracepoint/kprobe
File I/O patterns (files, latency, size)	Asset loading, shader cache behaviour	kprobe (vfs_read/write)
Block I/O latency (biolatency)	Storage bottleneck identification	tracepoint (block)
Memory allocation patterns (mmap, brk)	Memory pressure, fragmentation	tracepoint (mm)
Page fault distribution	VRAM overcommit, texture streaming	tracepoint (mm/page_fault)
Scheduler latency (runqueue wait)	Thread starvation, core migration	tracepoint (sched)
CPU migration frequency	CCX/CCD thrashing on AMD Zen	tracepoint (sched)
IRQ/softirq latency	Driver interrupt handling issues	tracepoint (irq)
TCP retransmits & RTT	Multiplayer lag root cause	tracepoint (tcp)
Futex contention	Game engine threading bottlenecks	tracepoint (syscalls)
DRM/GPU submit latency	Userspace submit → GPU execution	kprobe (amdgpu_cs_ioctl)
GPU fence wait time	CPU stalls waiting on GPU	kprobe (dma_fence_wait)
Shader compilation duration	Runtime shader compilation stutter	kprobe/uprobe on compiler
Wine/Proton syscall translation overhead	Translation layer cost	kprobe on ntdll mappings
5. Data Model (Elasticsearch)
5.1 Data Stream Strategy
Using Elastic data streams (not traditional indices) — the modern approach for time-series data on Serverless. Data streams handle rollover, ILM, and retention automatically.

metrics-gamepulse.frame-default      # Frame timing metrics (1/s)
metrics-gamepulse.gpu-default        # GPU metrics (1/s)
metrics-gamepulse.cpu-default        # CPU metrics (1/s)
metrics-gamepulse.memory-default     # Memory metrics (1/s)
metrics-gamepulse.storage-default    # Storage I/O metrics (1/s)
metrics-gamepulse.network-default    # Network metrics (1/s)
metrics-gamepulse.power-default      # Power/battery metrics (1/s)
metrics-gamepulse.audio-default      # Audio pipeline metrics (1/s)
metrics-gamepulse.session-default    # Session documents (1 per session)
metrics-gamepulse.ebpf-default       # eBPF histograms/traces (1/s aggregates)
metrics-gamepulse.events-default     # Discrete events (shader compile, stutter, crash)
Why data streams over traditional indices:

Automatic rollover based on size/age
Built-in lifecycle management (no separate ILM policy wiring)
Append-only semantics match our use case (telemetry is write-once)
Serverless-native — traditional ILM has limitations on Serverless
Component templates are shared across data streams for common field sets (session context, host environment).

5.2 Session Document (example)
json
{
  "@timestamp": "2026-03-28T14:00:00Z",
  "data_stream": { "type": "metrics", "dataset": "gamepulse.session", "namespace": "default" },

  "gamepulse": {
    "session": {
      "id": "uuid-here",
      "duration_s": 7200,
      "agent_version": "0.1.0"
    },
    "user": {
      "id": "anonymous-hardware-hash",
      "opt_in_public": true,
      "privacy_tier": 2
    },
    "game": {
      "name": "Starfield",
      "steam_appid": 1716740,
      "version": "1.12.30",
      "store": "steam",
      "launch_args": ["-fullscreen"],
      "graphics_api": "dx12",
      "upscaler": { "type": "fsr2", "quality": "balanced" }
    },
    "compat": {
      "layer": "proton",
      "proton_version": "Proton 9.0-4",
      "wine_version": "wine-9.0",
      "dxvk_version": "2.5.1",
      "vkd3d_version": "2.13",
      "gamescope_version": "3.15.7"
    },
    "gpu": {
      "vendor": "amd",
      "model": "AMD Radeon 780M",
      "vram_mb": 4096,
      "driver": { "version": "24.3.1", "name": "radv", "mesa_version": "24.3.1" },
      "pcie": { "gen": 4, "width": 8 }
    },
    "storage": {
      "game_drive": {
        "type": "nvme",
        "model": "WD SN740 512GB",
        "firmware": "73110012",
        "interface": "pcie_gen4_x4",
        "capacity_gb": 512,
        "free_gb": 128,
        "free_pct": 25.0,
        "temperature_c": 42,
        "smart_health": "ok",
        "encrypted": false
      },
      "game_filesystem": {
        "type": "btrfs",
        "mount_options": ["noatime", "compress=zstd:1", "discard=async", "ssd"],
        "compression": "zstd",
        "compression_level": 1,
        "block_size": 4096,
        "trim_enabled": true
      },
      "io_scheduler": "none",
      "read_ahead_kb": 128
    },
    "device": {
      "type": "handheld",
      "model": "Steam Deck OLED",
      "power_source": "battery",
      "tdp_watts": 15
    }
  },

  "host": {
    "os": { "type": "linux", "name": "SteamOS", "version": "3.6.22", "kernel": "6.11.2-valve1" },
    "architecture": "x86_64",
    "cpu": { "model": "AMD Ryzen 7 7840U", "cores": 8, "threads": 16 },
    "memory": { "total_gb": 16, "speed_mhz": 6400, "type": "LPDDR5X" }
  }
}
5.3 Metrics Document (example, 1-second sample)
json
{
  "@timestamp": "2026-03-28T14:05:32Z",
  "data_stream": { "type": "metrics", "dataset": "gamepulse.frame", "namespace": "default" },

  "gamepulse.session.id": "uuid-here",

  "gamepulse.frame": {
    "fps": { "current": 42, "avg_1s": 41.8, "low_1pct": 34, "low_01pct": 28 },
    "time": { "avg_ms": 23.8, "p50_ms": 22.1, "p95_ms": 31.2, "p99_ms": 45.6, "max_ms": 52.3, "variance": 2.1 },
    "stutter": { "count": 2 },
    "dropped": 0
  }
}
json
{
  "@timestamp": "2026-03-28T14:05:32Z",
  "data_stream": { "type": "metrics", "dataset": "gamepulse.gpu", "namespace": "default" },

  "gamepulse.session.id": "uuid-here",

  "gamepulse.gpu": {
    "utilisation_pct": 98.2,
    "clock": { "core_mhz": 2400, "mem_mhz": 2000 },
    "temp": { "core_c": 78, "hotspot_c": 85, "vram_c": 72 },
    "power": { "draw_w": 14.2, "limit_w": 15.0 },
    "vram": { "used_mb": 3812, "total_mb": 4096 },
    "fan": { "speed_pct": 0 }
  }
}
5.4 Events Document (discrete events)
json
{
  "@timestamp": "2026-03-28T14:05:32.456Z",
  "data_stream": { "type": "metrics", "dataset": "gamepulse.events", "namespace": "default" },

  "gamepulse.session.id": "uuid-here",
  "gamepulse.event": {
    "type": "shader_compile",
    "duration_ms": 187,
    "frametime_impact_ms": 142,
    "shader_hash": "abc123def"
  }
}
6. Implementation Phases
Phase 0: Elasticsearch Foundation (Week 1–2)
Goal: Elastic Cloud configured, data model deployed, synthetic data flowing, dashboards roughed in.

Tasks:

 Set up Elastic Cloud Serverless project (Observability type)
 Create component templates:
gamepulse-session-context — session ID, game name, AppID (shared across all data streams)
gamepulse-host-environment — OS, kernel, CPU, GPU, driver (shared)
gamepulse-frame-mappings — frame-specific field types
gamepulse-gpu-mappings — GPU-specific field types
gamepulse-storage-mappings — storage-specific field types
(one per metric category)
 Create index templates composing the component templates for each data stream
 Configure data stream lifecycle (hot tier retention, rollover settings)
 Create ingest pipelines:
Enrichment pipeline: resolve Steam AppID → game name, normalise GPU model strings
Derived fields pipeline: calculate stutter ratios, bottleneck classification (CPU-bound vs GPU-bound)
Validation pipeline: reject malformed documents, enforce required fields
 Write synthetic data generator (Python script) that produces realistic gaming session data
 Deploy synthetic data and verify index patterns in Kibana Discover
 Initialise GitHub repository with monorepo structure
 Set up GitHub Actions CI/CD skeleton
Repository Structure:

gamepulse/
├── README.md
├── LICENSE
├── docs/
│   ├── project-scope.md             # This document
│   ├── data-model.md
│   └── architecture.md
├── elastic/
│   ├── component-templates/          # Reusable field mapping templates
│   ├── index-templates/              # Per-data-stream templates
│   ├── ingest-pipelines/             # Enrichment, validation, derived fields
│   ├── lifecycle-policies/           # Data stream lifecycle config
│   ├── kibana/
│   │   ├── dashboards/              # NDJSON exports
│   │   ├── saved-searches/
│   │   └── visualisations/
│   └── synthetic-data/              # Test data generator
├── collector/                        # Python prototype collector
│   ├── pyproject.toml
│   ├── gamepulse/
│   │   ├── __init__.py
│   │   ├── cli.py                   # Entry point
│   │   ├── config.py                # Configuration handling
│   │   ├── session.py               # Session lifecycle management
│   │   ├── collectors/
│   │   │   ├── __init__.py
│   │   │   ├── base.py              # Abstract collector interface
│   │   │   ├── cpu.py               # CPU metrics (cross-platform)
│   │   │   ├── memory.py            # Memory metrics
│   │   │   ├── gpu/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── amd_linux.py     # sysfs/hwmon
│   │   │   │   ├── nvidia.py        # NVML bindings
│   │   │   │   └── intel.py         # Future
│   │   │   ├── storage.py           # Storage I/O + device identification
│   │   │   ├── network.py           # Network metrics
│   │   │   ├── frame/
│   │   │   │   ├── mangohud.py      # MangoHud log parsing
│   │   │   │   ├── gamescope.py     # Gamescope stats
│   │   │   │   └── presentmon.py    # PresentMon (Windows)
│   │   │   ├── audio.py             # Audio pipeline
│   │   │   └── power.py             # Power/battery
│   │   ├── enrichers/
│   │   │   ├── __init__.py
│   │   │   ├── os_info.py           # OS/kernel/distro
│   │   │   ├── game_detect.py       # Game auto-detection
│   │   │   ├── proton.py            # Proton/Wine/DXVK versions
│   │   │   ├── hardware.py          # Hardware inventory
│   │   │   └── steam.py             # Steam integration
│   │   ├── shipper/
│   │   │   ├── __init__.py
│   │   │   ├── elasticsearch.py     # ES bulk API client
│   │   │   └── buffer.py            # Local queue for resilience
│   │   └── platform/
│   │       ├── linux.py             # Linux-specific implementations
│   │       └── windows.py           # Windows-specific implementations
│   └── tests/
├── agent/                            # Rust production agent (Phase 4)
│   ├── Cargo.toml
│   └── src/
├── ebpf/                             # eBPF programs (Rust/Aya, Phase 5)
│   ├── Cargo.toml
│   ├── programs/                     # eBPF probe code
│   └── userspace/                    # Ring buffer readers
└── tools/
    ├── steam-appid-resolver/
    └── game-config-parsers/          # Per-game settings file parsers
Phase 1: Linux Collector MVP (Weeks 2–4)
Goal: Working Python collector on Linux that captures core metrics during a gaming session and ships to Elasticsearch. Covers desktop Linux and Steam Deck.

Tasks:

 Core collector framework:
Abstract Collector base class with collect() → dict interface
Collector registry: auto-discover platform-appropriate collectors
Collection loop: 1/s tick, gather from all collectors, batch and ship
 Game session detector:
Monitor for game processes (Steam library scan + process matching)
Parse Steam libraryfolders.vdf for installed games
Detect Proton/Wine wrapper processes → resolve actual game binary
Create session document with UUID, timestamps, full environment snapshot
Detect session end (process exit) → finalise session
 Frame timing:
MangoHud integration: configure MANGOHUD_LOG output, parse CSV
Gamescope stats: read from --stats-path socket
Calculate: FPS, 1% low, 0.1% low, frame time percentiles, stutter count
 GPU (AMD Linux — primary target):
sysfs hwmon: utilisation, clocks, temps (core/hotspot/vram), power, fan, VRAM usage
Vendor detection: scan /sys/class/drm/card*/device/vendor
 CPU:
/proc/stat parsing for per-core utilisation
/sys/devices/system/cpu/cpu*/cpufreq/ for clock speeds
hwmon for temperatures (k10temp, coretemp)
RAPL sysfs for package power
 Memory:
/proc/meminfo for system memory
/proc/{pid}/status for game process RSS/VMS
Swap usage
 Storage:
Device identification: sysfs traversal for drive type, model, interface
Filesystem detection: /proc/mounts, statfs
I/O metrics: /proc/diskstats for throughput, IOPS
Per-process I/O: /proc/{pid}/io
 Environment fingerprinting:
OS: /etc/os-release, uname -r
GPU driver: vulkaninfo, sysfs driver link
Proton: STEAM_COMPAT_DATA_PATH, version files
DXVK/VKD3D: /proc/{pid}/maps library inspection
Gamescope: version, active resolution/refresh
Mesa: library version inspection
 Elasticsearch shipper:
elasticsearch-py async client
Bulk indexing with configurable batch size (default: 5s batches)
Local file buffer for resilience
API key authentication for Elastic Cloud Serverless
 Steam Deck specifics:
Read-only filesystem awareness (config in ~/.config/gamepulse/)
Low-overhead mode (reduced collection frequency option)
Gamescope-specific: FSR scaling, refresh rate switching, TDP
SD card detection and metrics
 Configuration:
TOML config file (~/.config/gamepulse/config.toml)
CLI overrides
ES endpoint, API key, collection interval, metric toggles
Deliverable: gamepulse-collector Python package, installable via pip, runnable as systemd service or manually.

Phase 2: Kibana Dashboards (Weeks 3–5, overlaps with Phase 1)
Goal: Polished, comprehensive Kibana dashboards. This is your #1 priority — the thing that makes GamePulse's value visible immediately.

Dashboard 1: Session Overview
"I just played for 2 hours — how did it go?"
FPS timeline (line chart) with p95/p99 frame time overlay
Stutter events as annotations on the timeline
GPU + CPU utilisation dual-axis timeline
Temperature timeline (GPU core, hotspot, CPU package)
Storage I/O timeline (read throughput, queue depth)
Session summary panel: average FPS, worst 1%, time in stutter, peak temps, power draw
Environment badge bar: game, OS, kernel, GPU driver, Proton version, filesystem
Dashboard 2: Hardware & Configuration Comparison
"How does Starfield perform on Proton 9.0-3 vs 9.0-4?"
Dropdown filters: Game, GPU model, Driver version, Proton version, OS, Kernel, Filesystem
Side-by-side comparison panels
FPS distributions as histograms (not just averages — averages lie)
Bottleneck classification indicator (CPU-bound vs GPU-bound)
Frame time consistency heatmap across configurations
Dashboard 3: System Health Monitor
"Is my hardware throttling?"
Thermal headroom gauges (current vs throttle threshold)
Power delivery timeline (GPU + CPU)
Clock speed vs thermal throttle event correlation
Memory pressure indicator (used + swap + page faults)
Storage I/O bottleneck (queue depth vs latency scatter plot)
Fan speed correlation with temperature
Dashboard 4: Game Library Performance Matrix
"Across all my games, which run well?"
Heatmap/table: games × metrics (avg FPS, stutter %, GPU util, worst 1%)
Sortable by any column
Click-through to per-game session list
Trend sparklines per game (performance improving/degrading over time?)
Dashboard 5: Cross-Platform Comparison
"Same game, Windows vs Linux — show me"
Paired comparison layout with platform filter
Proton overhead quantification (for games with native + Proton builds)
Driver version matrix: which driver version works best per platform
Statistical confidence indicators (enough sessions to be meaningful?)
Dashboard 6: Storage Performance
"Is my SD card holding me back?"
Per-drive-type performance comparison
Load time proxy metrics (I/O burst patterns at session start)
Filesystem comparison (btrfs vs ext4 on same drive)
I/O stall correlation with frame time spikes
Dashboard 7: Community Overview (Phase 5 — data model ready now)
Aggregate FPS by hardware class
"Works great / has issues / unplayable" classification
Proton version recommendation (which version has best perf per game?)
Regression detection: did a game/driver update make things worse?
Technical approach:

Kibana Lens for most visualisations (maintainable, Serverless-native)
TSVB for complex multi-overlay time series
Runtime fields for derived calculations (stutter ratio, bottleneck classification)
All dashboards exported as NDJSON, version-controlled in repo
Kibana Spaces to separate user/developer views
Phase 3: Windows Collector (Weeks 5–7)
Goal: Feature-parity with the Linux collector on Windows 11.

Tasks:

 Frame timing:
PresentMon integration (MIT licensed, Intel-maintained)
Parse PresentMon CSV or use as library
ETW as alternative for DWM/DXGI telemetry
 GPU metrics:
AMD: ADL/ADLX SDK
NVIDIA: NVML via pynvml
Intel Arc: oneAPI Level Zero / IGCL
Fallback: WMI Win32_VideoController
 CPU / System:
PDH Performance Counters for utilisation, clocks
GlobalMemoryStatusEx for memory
WMI for temperatures (vendor-dependent)
Performance Counters for disk I/O and network
 Environment fingerprinting:
OS: WMI Win32_OperatingSystem
GPU driver: registry + WMI
DirectX version: capability queries
Vulkan: vulkaninfo
 Game detection:
Steam registry keys
Process enumeration + name matching
GOG Galaxy, Epic Games Store detection
Window title fallback
 Windows service or tray application:
Background process with system tray icon
Start/stop session controls
Status indication
 NVIDIA GPU support (cross-platform via NVML):
Works on both Linux and Windows
Add during this phase since NVML is cross-platform
Key Windows differences:

No MangoHud → PresentMon fills the role
No eBPF (yet) → ETW provides some equivalent deep telemetry
Temperature access less standardised → vendor SDK dependency
No Proton fields, but DirectX/Vulkan API version + runtime become relevant
Phase 4: Rust Agent & Elastic Agent Integration (Weeks 8–12)
Goal: Production-quality Rust rewrite of the collector, packaged as an Elastic Agent custom integration.

By this point, the data model is validated with real gaming sessions from Phases 1-3. The rewrite is mechanical, not exploratory.

Tasks:

 Rust project scaffolding with cross-platform abstractions (traits for each collector)
 Port all collectors from Python → Rust:
sysfs/procfs (Linux), WMI/PDH (Windows)
NVML bindings (nvml-wrapper crate)
AMD sysfs/ADL
 Cross-compilation setup (cargo-cross, GitHub Actions matrix builds)
 Elastic Agent custom integration package:
manifest.yml with integration metadata
Data stream definitions per metric category
Field mappings (re-use Phase 0 templates)
Dashboards bundled as saved objects
Agent policy templates:
"Gamer" — standard collection, 1/s, all metrics
"Developer" — enhanced, frame-time histograms, eBPF enabled
"Minimal" — low overhead, 5/s, core metrics only (Deck battery-conscious)
 Build and test with elastic-package tooling
 Distribution packages:
Linux: .deb, .rpm, Flatpak, AUR PKGBUILD
Windows: MSI installer, winget manifest
Steam Deck: Flatpak or Decky Loader plugin
 Documentation site (mdBook):
Installation guide per platform
Configuration reference
Troubleshooting guide
"What data does GamePulse collect?" transparency document
 Fleet policy distribution through Elastic Cloud
Phase 5: eBPF Deep Telemetry (Weeks 12–16)
Goal: Kernel-level observability that gives engine developers, Mesa/driver developers, and kernel hackers data they can't get anywhere else.

Why eBPF is transformative here: Traditional telemetry captures "what happened" (FPS dropped). eBPF captures "why it happened" (the kernel scheduler migrated the render thread to a cold core, the filesystem prefetch stalled on a page cache miss, a futex contention in the audio thread blocked the main loop). This turns GamePulse from a monitoring tool into an optimisation tool.

eBPF Framework: Aya (Rust-native)

Write eBPF programs in Rust (no C/clang dependency for users)
CO-RE support via BTF
Production-proven (Cloudflare, Datadog)
Graceful fallback on kernels without BTF (pre-5.2)
Programs to implement:
5a. Syscall Profiler

Attach: raw_syscalls/sys_enter, raw_syscalls/sys_exit
Output: per-process syscall frequency histogram, latency distribution
Use case: "This game makes 50,000 read() calls/s on tiny buffers — game bug"
5b. Scheduler Observer

Attach: sched/sched_switch, sched/sched_migrate_task, sched/sched_wakeup
Output: render thread CPU affinity, migration frequency, runqueue wait time
Use case: "Render thread migrating between CCXs on Zen 4 — affinity fix eliminates 3ms spikes"
5c. I/O Tracer

Attach: block/block_rq_issue, block/block_rq_complete
Output: I/O sizes, queue depths, latencies, sequential vs random patterns
Use case: "Stutters correlate with 4KB random reads — asset streaming thrashing"
5d. Memory Tracker

Attach: kmem/mm_page_alloc, kmem/mm_page_free
Output: page fault rate, allocation rate, memory pressure events
Use case: "Frame drops correlate with major page faults — working set exceeds RAM"
5e. Futex/Lock Contention

Attach: syscalls/sys_enter_futex
Output: lock wait time, contention frequency
Use case: "Audio and render threads contending on same mutex — needs lock-free queue"
5f. GPU Fence/Sync Observer (AMD initially)

Attach: amdgpu driver tracepoints
Output: GPU fence wait times, command submission latencies
Use case: "CPU waiting 4ms/frame for GPU fence — engine sync bottleneck"
5g. Shader Compilation Tracer

Attach: uprobe on Mesa's shader compiler
Output: compilation duration, pipeline hash
Use case: "Stutter map showing exactly when/where shader compiles cause frame drops"
5h. Wine/Proton Overhead Profiler

Attach: kprobe on ntdll syscall translation paths
Output: translation overhead per syscall type
Use case: "Quantified: Proton adds 0.3ms/frame of syscall translation overhead for this game"
eBPF integration with Elastic:
Programs compiled into the Rust agent binary (no runtime BPF object loading)
Userspace reads ring buffers, aggregates at 1/s intervals
Ships as structured metrics to metrics-gamepulse.ebpf-default
Kibana: flamegraph-style views, timeline correlation with frame metrics
Requires: CAP_BPF + CAP_PERFMON capabilities (not full root)
Phase 6: Community Platform (Weeks 16+)
Goal: Multi-user data collection with privacy, public dashboards, community value.

Tasks:

 Privacy implementation (four-tier model):
Tier 0 — Local only: All raw data, stored locally. Always on.
Tier 1 — Anonymous metrics: FPS, temps, utilisation, hardware config. Opt-in.
Tier 2 — Session metadata: Game name, driver versions, Proton version. Opt-in.
Tier 3 — Deep telemetry: eBPF traces, I/O patterns, syscall data. Opt-in.
 Hardware fingerprinting: salted hash of hardware IDs (no serial numbers)
 Community ES ingestion:
Separate data streams for community data
Rate limiting per user
Data validation pipeline
API key provisioning
 Community dashboards:
Aggregate views only (no individual user data)
"GamePulse Score" — composite performance rating per game per hardware class
Proton compatibility matrix (quantified, not binary)
Hardware recommendation engine ("best GPU for this game at 1440p")
 Regression detection:
Elastic ML anomaly detection on aggregate metrics
Alerts when a driver/Proton/game update causes performance regression
Automatic bisection: "FPS dropped 15% between Proton 9.0-3 and 9.0-4 for this game on AMD GPUs"
 Public website:
Embedded Kibana dashboards or custom frontend
Download page for the agent
Documentation
 API for third-party tools to query aggregated data
 Embeddable performance widgets for journalists/reviewers
7. Extended Scope — Future Vision
7.1 Shader Compilation Stutter Maps
Correlate eBPF shader-compile events with frame time spikes. Per-game "stutter maps" showing exactly when and why compilation stutters happen. Enormously valuable to Valve's shader pre-caching team.

7.2 Proton/DXVK Overhead Quantification
For games with native Linux + Windows builds: compare metrics to quantify exact translation layer overhead, broken down by category (CPU translation, draw call batching, shader compilation).

7.3 Power Efficiency Scoring
Performance-per-watt metrics, battery life estimates, optimal TDP settings per game on handhelds. "What TDP gives me 60fps in Starfield on Steam Deck OLED?"

7.4 Thermal Throttling Detection
Automatic detection and quantification of thermal throttling events. Tag sessions where throttling occurred, calculate performance impact.

7.5 Crash & Hang Detection
Detect game crashes/hangs via process monitoring + eBPF. Capture last N seconds of telemetry for post-mortem. Correlate with system state (OOM, GPU reset, driver timeout).

7.6 Input Latency (cutting-edge)
End-to-end measurement via eBPF: USB HID event → kernel input → game process read → frame presented. Extremely valuable to competitive gaming communities.

7.7 Machine Learning Anomaly Detection
Elastic ML for automatic detection of regressions, thermal throttling, memory leaks, shader compilation storms — without manual threshold configuration.

7.8 Game Config File Parsing
Per-game parsers for settings files (INI, XML, JSON configs). Captures exact graphics settings — "60fps at Ultra" vs "60fps at Low" tells a very different story.

7.9 Benchmark Standardisation
Tag "benchmark runs" vs "general gameplay" for apples-to-apples comparison. Integrate with game-specific benchmark modes.

7.10 Platform Expansion
Intel Arc GPU support
macOS (limited — Apple restricts GPU metric access)
ARM platforms (future handhelds, Apple Silicon via Crossover/GPTK)
Android (Adreno, Mali — mobile/cloud gaming)
8. Privacy & Security Design
8.1 Principles
Opt-in everything: No data leaves the machine without explicit consent
No PII by default: User identity is a salted hardware hash — no usernames, emails, or IPs
Granular controls: Users choose what categories to share (privacy tiers 0–3)
Local-first option: Users can run their own ES instance and never share externally
Open source: Full transparency on what is collected and shipped
8.2 Security
Agent runs with minimum required privileges (non-root where possible, CAP_BPF for eBPF)
ES communication over TLS only
API key authentication (Elastic Cloud)
No shell-out or arbitrary command execution from config
eBPF programs compiled into the binary (no runtime loading of external BPF objects)
No game process hooking or memory reading (avoids anti-cheat conflicts)
9. Risks & Mitigations
Risk	Impact	Mitigation
Agent overhead affects game performance	Undermines core mission	Budget: <1% CPU, <50MB RAM. 1/s collection, not per-frame. Ring buffer with drop-oldest. Continuous profiling of the agent itself.
eBPF kernel compatibility	Won't work on older kernels	CO-RE with BTF, graceful feature degradation, skip eBPF on kernels <5.8
Anti-cheat interference (EAC, BattlEye)	Agent blocked or user banned	No game process hooking. Use OS-level APIs only. Whitelist documentation.
GPU vendor API fragmentation	Maintenance burden per vendor	Abstract behind interface/trait, start with AMD, add vendors incrementally
Windows ETW complexity	Frame timing hard to get right	Leverage PresentMon (MIT licensed) rather than reimplementing
Data volume at scale	Elastic Cloud costs	Configurable sample rate, data stream lifecycle, downsampling for aged data
Privacy incidents	Trust destruction	Minimal defaults, no PII, open source audit trail, privacy tiers
Python prototype performance	Overhead during intense gaming	1/s collection is low-frequency. Async I/O. Profile and optimise. Rust rewrite in Phase 4.
10. Tools & Access Required
Need	Purpose	Status
Elastic Cloud Serverless (Enterprise)	Development, storage, dashboards	✅ Available
AMD GPU Linux desktop	Primary development & testing	✅ Available
Windows 11 desktop (AMD)	Windows collector development	✅ Available
Steam Deck	Handheld testing, SD card scenarios	✅ Available
NVIDIA GPU machine	NVIDIA support testing	❓ To confirm / borrow
Python 3.11+	Prototype collector	Can install
Rust toolchain	Production agent + eBPF (Phase 4-5)	Can install
GitHub repository	Source control, CI/CD, releases	To set up
GitHub Actions	Automated builds, tests, cross-platform CI	To set up
11. Success Metrics
Milestone	How we know it works
Phase 0 complete	Synthetic data flowing to ES, index patterns visible in Kibana, dashboard skeletons showing data
Phase 1 complete	Real gaming session on Linux: "Starfield on Proton 9.0, AMD 780M, avg 42fps" visible in dashboard
Phase 2 complete	All 6 dashboards polished and showing real session data. Can answer "how did that session go?" at a glance.
Phase 3 complete	Same game, same system: Windows vs Linux side-by-side comparison in Kibana
Phase 4 complete	Rust agent installable via package manager. New user: install → configure → data in 10 minutes.
Phase 5 complete	Can identify "shader compilation stutter at timestamp X caused 200ms spike" via eBPF data in dashboard
Phase 6 complete	Public dashboard showing community-aggregated performance data per game per hardware class
12. Immediate Next Steps
Validate this scope — flag anything that doesn't match the vision
Begin Phase 0 — Elasticsearch index templates, component templates, ingest pipelines, data stream configuration
Scaffold the repository — monorepo structure as defined above
Write the synthetic data generator — enables dashboard development in parallel with collector work
Begin Phase 1 collector — Linux AMD GPU metrics first (your primary hardware)
This is a living document. Each phase will be broken into specific implementation tasks as we begin work. Next review: after Phase 0 completion.

