# GamePulse — Open Gaming Telemetry Platform

## Project Scope & Implementation Plan

*Working title: GamePulse (placeholder — rename anytime)*
*Version: 0.1 — Initial Scoping*
*Date: March 2026*

---

## 1. Vision & Problem Statement

### The Problem

There is no unified, open platform for collecting, comparing, and analysing real-world gaming performance across hardware configurations, operating systems, driver versions, Proton/Wine layers, and kernel versions. Existing tools (MSI Afterburner, MangoHud, CapFrameX) are local-only, siloed, and lack the ability to:

- Compare performance across configurations systematically
- Share structured telemetry with developers, journalists, and maintainers
- Correlate low-level system behaviour (I/O stalls, shader compilation, memory pressure) with user-visible performance drops
- Provide actionable data to the people who can actually fix problems (engine devs, driver teams, Proton maintainers, distro packagers)

### The Vision

A lightweight, privacy-respecting agent that runs alongside games, collects comprehensive telemetry, and ships it to Elasticsearch — enabling:

- **Gamers**: See exactly how their system performs, compare configs, identify bottlenecks
- **Developers/Engine teams**: Understand real-world performance patterns across thousands of configurations via eBPF-level insight
- **Journalists/Reviewers**: Structured, comparable benchmark data instead of anecdotal testing
- **Proton/Wine/Mesa maintainers**: See regression/improvement impact of their changes across the player base
- **Distro/Package maintainers**: Understand how kernel versions, Mesa builds, and system configs affect gaming

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    USER'S GAMING PC                      │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  Game Process │  │  System APIs │  │  eBPF Probes  │  │
│  │  (detected)   │  │  (hw sensors)│  │  (kernel-lvl) │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │
│         │                 │                   │          │
│  ┌──────▼─────────────────▼───────────────────▼───────┐  │
│  │              GamePulse Agent (Rust)                 │  │
│  │  ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌────────┐  │  │
│  │  │Collector│ │ Enricher │ │ Buffer  │ │Shipper │  │  │
│  │  │ Plugins │ │(metadata)│ │(ring buf)│ │(ES API)│  │  │
│  │  └─────────┘ └──────────┘ └─────────┘ └────────┘  │  │
│  └────────────────────────────────────────────────────┘  │
│                                                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │          Elastic Agent Integration Wrapper          │  │
│  │          (for managed distribution)                 │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────┬───────────────────────────────┘
                          │ HTTPS (Elasticsearch Ingest API)
                          ▼
┌─────────────────────────────────────────────────────────┐
│                  ELASTIC CLOUD / SELF-HOSTED            │
│                                                         │
│  ┌──────────┐  ┌────────────┐  ┌─────────────────────┐  │
│  │  Ingest  │  │ Elastic-   │  │  Kibana Dashboards  │  │
│  │ Pipeline │  │ search     │  │  (pre-built + custom)│  │
│  │(enrich,  │  │ Indices    │  │                     │  │
│  │ normalise│  │            │  │  - Per-game FPS     │  │
│  │ validate)│  │ - metrics  │  │  - Config compare   │  │
│  │          │  │ - sessions │  │  - Regression detect│  │
│  └──────────┘  │ - hardware │  │  - eBPF flamegraphs │  │
│                │ - ebpf     │  │  - Community stats  │  │
│                └────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Metric Categories — Full Inventory

### 3.1 Frame Performance (Priority: Critical)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| FPS (current, avg, 1% low, 0.1% low) | PresentMon / ETW | MangoHud / Vulkan layer |
| Frame time (ms per frame) | PresentMon / ETW | MangoHud / Vulkan layer |
| Frame time variance / jitter | Calculated | Calculated |
| Present mode (flip/copy/composition) | DXGI / ETW | Gamescope / Wayland info |
| V-Sync state | DXGI / driver API | DRM / driver API |
| API in use (DX11/DX12/Vulkan/OpenGL) | ETW / process inspection | Vulkan layer / procfs |
| Resolution & refresh rate | DXGI / Display API | DRM / xrandr / gamescope |
| HDR state | DXGI | Gamescope / KMS |
| FSR/DLSS/XeSS state & quality | Game-specific / driver hints | Game-specific / driver hints |

### 3.2 GPU Metrics (Priority: Critical)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| GPU utilisation % | NVML / ADL / ADLX | sysfs (hwmon) / NVML |
| GPU clock speed (current/max) | NVML / ADL | sysfs / NVML |
| GPU memory used / total | NVML / ADL | sysfs / NVML |
| GPU temperature | NVML / ADL | sysfs (hwmon) |
| GPU hotspot temperature | NVML / ADL | sysfs (hwmon) where avail |
| GPU memory temperature | NVML / ADL | sysfs (hwmon) where avail |
| GPU power draw (W) | NVML / ADL | sysfs (hwmon) |
| GPU fan speed (RPM / %) | NVML / ADL | sysfs (hwmon) |
| GPU voltage | NVML / ADL | sysfs where available |
| PCIe link speed/width | NVML / ADL | sysfs (pcie) |
| Shader compilation events | ETW / driver API | Vulkan pipeline cache / eBPF |
| GPU pipeline state (vertex/fragment/compute occupancy) | GPUPerfAPI (AMD) / NVML | AMDGPU perf counters / sysfs |

### 3.3 CPU Metrics (Priority: Critical)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| Per-core utilisation % | PDH / WMI | /proc/stat |
| Per-core clock speed | WMI / CPUID | /sys/devices/cpu/cpufreq |
| CPU temperature (per-die/package) | WMI / LibreHardwareMonitor | sysfs (hwmon) / k10temp / coretemp |
| CPU power draw (package) | RAPL via WMI | sysfs (RAPL) |
| Thread count (game process) | Process API | /proc/[pid]/status |
| Context switches/sec | ETW | /proc/[pid]/status / eBPF |
| IPC (instructions per cycle) | PMU via ETW | perf_event / eBPF |
| CPU governor / boost state | Power plan API | cpufreq sysfs |
| C-state residency | ETW | sysfs / turbostat |

### 3.4 Memory (Priority: High)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| RAM used / total / available | GlobalMemoryStatusEx | /proc/meminfo |
| Game process RSS / VMS | Process API | /proc/[pid]/status |
| Swap usage | Performance counters | /proc/meminfo |
| Page faults (major/minor) | ETW | /proc/[pid]/stat / eBPF |

### 3.5 Storage & Filesystem (Priority: High — expanded)

#### 3.5.1 Storage Device Identification (per session, per drive)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| Drive type classification (NVMe / SATA SSD / HDD / SD card / USB) | WMI / DeviceIoControl | sysfs (rotational, removable, transport) |
| Drive model & firmware version | WMI / SMART | smartctl / sysfs (model, rev) |
| Drive interface (PCIe Gen3/4/5, SATA III, UHS-I/II/III, USB 3.x) | WMI / DeviceIoControl | sysfs (pcie link, mmc) |
| Drive capacity & free space | GetDiskFreeSpaceEx | statvfs / df |
| PCIe lanes (x1/x2/x4) for NVMe | WMI | sysfs (current_link_width) |
| NVMe spec version | WMI / nvme-cli | nvme-cli / sysfs |
| SD card class/speed rating (UHS, A1/A2, V30 etc.) | WMI | sysfs (mmc — speed_class, ssr) |
| Drive SMART health status | WMI / CrystalDiskInfo API | smartctl |
| Drive temperature (if reported) | WMI / SMART | sysfs (hwmon) / smartctl |
| Partition scheme (GPT/MBR) | WMI | lsblk / sysfs |
| Drive encryption (BitLocker/LUKS) | WMI / manage-bde | lsblk / cryptsetup |

#### 3.5.2 Filesystem Metrics (per session)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| Filesystem type (NTFS/ext4/btrfs/f2fs/FAT32/exFAT) | GetVolumeInformation | /proc/mounts / statfs |
| Filesystem mount options (noatime, compress, discard) | N/A (less relevant) | /proc/mounts |
| Btrfs compression algo (zstd/lzo/none) | N/A | btrfs property / mount opts |
| Btrfs subvolume info | N/A | btrfs subvolume show |
| NTFS compression state | GetFileAttributes | N/A |
| Block size | GetDiskFreeSpace | statfs |
| Filesystem fragmentation estimate | Defrag analysis API | filefrag / btrfs fi defrag |
| Journal mode (if ext4: ordered/writeback/journal) | N/A | /proc/mounts / tune2fs |
| TRIM/discard support & status | WMI / fsutil | sysfs (discard_granularity) |

#### 3.5.3 Storage I/O Metrics (per second, during gameplay)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| Sequential read throughput (MB/s) | PDH / ETW | /proc/diskstats / eBPF |
| Sequential write throughput (MB/s) | PDH / ETW | /proc/diskstats / eBPF |
| Random read IOPS | ETW | /proc/diskstats / eBPF |
| Random write IOPS | ETW | /proc/diskstats / eBPF |
| Read latency (avg, p50, p95, p99) | ETW | eBPF (biolatency) |
| Write latency (avg, p50, p95, p99) | ETW | eBPF (biolatency) |
| I/O queue depth (current, max) | PDH | /sys/block/*/stat |
| I/O scheduler in use | N/A | /sys/block/*/queue/scheduler |
| I/O merge rate (adjacent request merging) | ETW | /proc/diskstats (merged field) |
| Read-ahead setting | N/A | /sys/block/*/queue/read_ahead_kb |
| I/O wait % (CPU time waiting on storage) | PDH | /proc/stat (iowait field) |
| Game-process-specific I/O (bytes, ops) | ETW / Process API | /proc/[pid]/io |
| Asset loading time (correlated with frame stalls) | eBPF | eBPF (vfs_read on game fd) |

#### 3.5.4 Storage-Specific Scenarios to Capture
| Scenario | Why it matters |
|----------|---------------|
| SD card vs NVMe on Steam Deck | Quantify real-world load time and stutter difference |
| Btrfs vs ext4 vs f2fs gaming perf | Filesystem choice affects I/O patterns significantly |
| Btrfs compression impact (zstd levels) | CPU trade-off vs storage throughput |
| NTFS vs ReFS (Windows) | Relevant for large game installs |
| Encrypted vs unencrypted drive | LUKS/BitLocker overhead during gameplay |
| Full drive (>90% capacity) vs free | SSD performance degrades when near-full |
| SATA SSD vs NVMe for open-world games | Streaming/loading difference in practice |
| USB external drive gaming | Common setup, rarely benchmarked properly |
| DirectStorage / GPU decompression | Windows 12 / future Linux equivalent |
| I/O scheduler impact (mq-deadline vs bfq vs none) | Linux tuning for gaming workloads |

### 3.6 Network (Priority: Medium — multiplayer relevance)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| Network RTT to game server | ETW / raw sockets | eBPF (tcp_rtt) |
| Packets sent/received | Performance counters | /proc/net/dev / eBPF |
| Packet loss % | Calculated | eBPF |
| Bandwidth utilisation | Performance counters | /proc/net/dev |
| Connection type (WiFi/Ethernet) | WMI | NetworkManager / sysfs |

### 3.7 System Environment (Priority: Critical — collected once per session)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| OS version & build | Registry / WMI | /etc/os-release / uname |
| Kernel version | N/A (NT version) | uname -r |
| GPU driver version | Registry / NVML / ADL | modinfo / sysfs |
| Mesa version | N/A | glxinfo / vulkaninfo |
| Vulkan driver version | vulkaninfo | vulkaninfo |
| Proton version | N/A | $PROTON_VERSION / steam compat |
| Wine version | N/A | wine --version (via Proton) |
| DXVK version | N/A | DXVK_LOG or file version |
| VKD3D-Proton version | N/A | env vars / file version |
| Gamescope version | N/A | gamescope --version |
| Steam Deck model/variant | N/A | DMI / device-specific sysfs |
| CPU model & core count | CPUID / WMI | /proc/cpuinfo |
| GPU model & VRAM | NVML / ADL / DXGI | sysfs / lspci |
| RAM capacity & speed | WMI / SMBIOS | dmidecode / sysfs |
| Display resolution & refresh | DXGI | DRM / xrandr |
| Power profile (battery/AC) | Power API | upower / sysfs |
| BIOS/UEFI version | WMI | dmidecode |
| Motherboard model | WMI | dmidecode |
| Storage model & firmware | WMI | smartctl / sysfs |

### 3.8 Game Context (Priority: Critical — collected per session)
| Metric | Source (Windows) | Source (Linux) |
|--------|-----------------|----------------|
| Game name (auto-detected) | Process name + Steam API | Process name + Steam API |
| Steam App ID | Steam client API / registry | Steam client API / env |
| Game version / build | File version info | Steam manifest |
| Game launch parameters | Command line inspection | /proc/[pid]/cmdline |
| In-game graphics settings | Config file parsing (per-game) | Config file parsing |
| Mod list (if applicable) | Game-specific | Game-specific |

### 3.9 eBPF Deep Telemetry (Priority: High — unique differentiator)
| Metric | Purpose | eBPF Program Type |
|--------|---------|-------------------|
| Syscall latency distribution | Identify kernel-level bottlenecks | tracepoint/kprobe |
| File I/O patterns (what files, latency) | Asset loading, shader cache behaviour | kprobe (vfs_read/write) |
| Memory allocation patterns (mmap, brk) | Memory pressure, fragmentation | tracepoint (mm) |
| Page fault distribution | VRAM overcommit, texture streaming issues | tracepoint (mm/page_fault) |
| Scheduler latency (runqueue wait) | Thread starvation, core migration issues | tracepoint (sched) |
| IRQ/softirq latency | Driver interrupt handling issues | tracepoint (irq) |
| TCP retransmits & RTT | Multiplayer lag root cause | tracepoint (tcp) |
| Futex contention | Game engine threading bottlenecks | tracepoint (syscalls) |
| DRM/GPU submit latency | Time from userspace submit to GPU execution | kprobe (amdgpu_cs_ioctl etc.) |
| GPU fence wait time | CPU waiting on GPU (pipeline bubbles) | kprobe (dma_fence_wait) |
| Shader compilation duration | Stutter from runtime shader compilation | kprobe/uprobe on compiler |
| Wine/Proton syscall translation overhead | Translation layer performance cost | kprobe on ntdll mappings |

---

## 4. Data Model (Elasticsearch)

### 4.1 Index Strategy

```
gamepulse-sessions-YYYY.MM       # One doc per gaming session (environment snapshot)
gamepulse-metrics-YYYY.MM.DD     # Time-series metrics (1-second granularity default)
gamepulse-ebpf-YYYY.MM.DD        # eBPF event data (histograms, traces)
gamepulse-events-YYYY.MM.DD      # Discrete events (shader compile, stutter, crash)
```

### 4.2 Session Document (example)

```json
{
  "@timestamp": "2026-03-28T14:00:00Z",
  "session_id": "uuid-here",
  "agent_version": "0.1.0",
  "user_id": "anonymous-hash",
  "opt_in_public": true,

  "game": {
    "name": "Starfield",
    "steam_app_id": 1716740,
    "version": "1.12.30",
    "launch_args": ["-fullscreen"],
    "graphics_api": "dx12",
    "upscaler": { "type": "fsr2", "quality": "balanced" }
  },

  "os": {
    "type": "linux",
    "distro": "SteamOS",
    "version": "3.6.22",
    "kernel": "6.11.2-valve1",
    "desktop": "gamescope"
  },

  "compatibility": {
    "proton_version": "Proton 9.0-4",
    "wine_version": "wine-9.0",
    "dxvk_version": "2.5.1",
    "vkd3d_proton_version": "2.13",
    "gamescope_version": "3.15.7"
  },

  "hardware": {
    "cpu": {
      "model": "AMD Ryzen 7 7840U",
      "cores": 8,
      "threads": 16,
      "base_clock_mhz": 3300,
      "boost_clock_mhz": 5100
    },
    "gpu": {
      "model": "AMD Radeon 780M",
      "vendor": "amd",
      "vram_mb": 4096,
      "driver_version": "24.3.1",
      "mesa_version": "24.3.1",
      "vulkan_driver": "radv",
      "pcie_gen": 4,
      "pcie_width": 8
    },
    "ram": {
      "total_mb": 16384,
      "speed_mhz": 6400,
      "type": "LPDDR5X"
    },
    "storage": {
      "game_drive": {
        "type": "nvme",
        "model": "WD SN740 512GB",
        "firmware": "73110012",
        "interface": "pcie_gen4_x4",
        "nvme_spec": "1.4",
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
      "read_ahead_kb": 128,
      "additional_drives": [
        {
          "type": "sd_card",
          "model": "Samsung EVO Select 512GB",
          "interface": "uhs_i",
          "speed_class": "A2_V30_U3",
          "capacity_gb": 512,
          "free_gb": 210,
          "filesystem": "ext4"
        }
      ]
    },
    "device": {
      "type": "handheld",
      "model": "Steam Deck OLED",
      "power_source": "battery",
      "tdp_watts": 15
    }
  }
}
```

### 4.3 Metrics Document (example, 1-second sample)

```json
{
  "@timestamp": "2026-03-28T14:05:32Z",
  "session_id": "uuid-here",

  "fps": {
    "current": 42,
    "avg_1s": 41.8,
    "low_1pct": 34,
    "low_01pct": 28,
    "frametime_ms": 23.8,
    "frametime_variance": 2.1
  },

  "gpu": {
    "utilisation_pct": 98.2,
    "clock_mhz": 2400,
    "memory_used_mb": 3812,
    "temperature_c": 78,
    "hotspot_c": 85,
    "power_w": 14.2,
    "fan_pct": 0
  },

  "cpu": {
    "total_utilisation_pct": 62.1,
    "game_utilisation_pct": 55.3,
    "per_core": [88, 72, 45, 61, 55, 48, 42, 38],
    "clock_mhz_avg": 4200,
    "temperature_c": 72,
    "power_w": 18.5
  },

  "memory": {
    "system_used_mb": 12400,
    "game_rss_mb": 8200,
    "swap_used_mb": 0,
    "page_faults_major": 0,
    "page_faults_minor": 1240
  },

  "storage": {
    "read_mbps": 120.5,
    "write_mbps": 2.1,
    "read_iops": 3200,
    "write_iops": 85,
    "io_latency_read_us": { "avg": 62, "p50": 45, "p95": 340, "p99": 890 },
    "io_latency_write_us": { "avg": 28, "p50": 22, "p95": 110, "p99": 450 },
    "queue_depth_current": 4,
    "queue_depth_max": 16,
    "io_wait_pct": 0.8,
    "merged_reads": 120,
    "merged_writes": 5,
    "game_process_read_mb": 118.2,
    "game_process_write_mb": 0.4,
    "game_process_read_ops": 3100,
    "drive_temperature_c": 44
  }
}
```

---

## 5. Technology Decisions

### 5.1 Agent Core: Rust

**Why Rust over Go or Python:**

- **Minimal overhead** — the agent runs alongside games where every % of CPU and MB of RAM matters. Rust's zero-cost abstractions and no GC make it the only serious choice for a gaming performance agent. A Go GC pause during a benchmark would corrupt the data it's measuring.
- **Cross-platform systems access** — Rust has excellent crates for sysfs, hwmon, Windows WMI/PDH, and raw syscall interfaces.
- **eBPF integration** — `libbpf-rs` and `aya` are mature Rust eBPF frameworks. Aya in particular lets you write eBPF programs in Rust (not just load C-compiled ones), keeping the entire stack in one language.
- **Safety** — a telemetry agent running as root (required for eBPF and some hardware sensors) should not have memory safety bugs.
- **Single binary distribution** — `cargo build --release` produces one static binary, no runtime dependencies. Critical for easy distribution.

### 5.2 eBPF Framework: Aya (Rust-native)

- Write eBPF programs in Rust (no C/clang dependency for users)
- CO-RE (Compile Once, Run Everywhere) support via BTF
- Mature enough for production (used by Cloudflare, Datadog, etc.)
- Falls back gracefully on kernels without BTF (pre-5.2)
- Windows: eBPF for Windows is experimental (Microsoft's ebpf-for-windows project) — we'll make eBPF Linux-only initially and use ETW on Windows for equivalent deep telemetry

### 5.3 Elastic Integration

**Phase 1:** Direct HTTP shipping to Elasticsearch Ingest API (simplest, works immediately with Elastic Cloud)

**Phase 2:** Custom Elastic Agent integration package — this lets users install via Fleet, get automatic updates, and centralised management. The Elastic Agent wraps our Rust binary and handles auth, buffering, and fleet management.

**Phase 3:** Custom Kibana plugin with pre-built dashboards, shipped as an Elastic integration package.

### 5.4 Frame Timing

- **Linux**: Vulkan layer (like MangoHud's approach) or integrate with MangoHud's stats pipe directly. For non-Vulkan (OpenGL), use `GL_EXT_disjoint_timer_query` or eBPF on DRM submit.
- **Windows**: PresentMon's ETW-based approach (MIT licensed, Intel's library). Alternatively, DXGI frame statistics.
- **Steam Deck / Gamescope**: Gamescope exposes frame timing via its own stats interface.

---

## 6. Implementation Plan

### Phase 0: Foundation (Weeks 1–3)
**Goal: Skeleton agent that ships basic metrics to Elasticsearch**

```
├── gamepulse-agent/
│   ├── Cargo.toml
│   ├── src/
│   │   ├── main.rs                    # CLI, config, lifecycle
│   │   ├── config.rs                  # TOML/YAML config + CLI args
│   │   ├── collector/
│   │   │   ├── mod.rs                 # Collector trait definition
│   │   │   ├── cpu.rs                 # CPU metrics (cross-platform)
│   │   │   ├── memory.rs              # Memory metrics
│   │   │   └── gpu/
│   │   │       ├── mod.rs             # GPU trait + auto-detection
│   │   │       ├── amd_linux.rs       # sysfs/hwmon for AMD
│   │   │       ├── amd_windows.rs     # ADL/ADLX for AMD
│   │   │       ├── nvidia.rs          # NVML (cross-platform)
│   │   │       └── intel.rs           # future
│   │   ├── enricher/
│   │   │   ├── mod.rs                 # Session metadata enrichment
│   │   │   ├── os.rs                  # OS/kernel/distro detection
│   │   │   ├── game.rs               # Game auto-detection
│   │   │   ├── proton.rs             # Proton/Wine/DXVK version detection
│   │   │   └── hardware.rs           # Hardware inventory
│   │   ├── shipper/
│   │   │   ├── mod.rs                 # Shipper trait
│   │   │   ├── elasticsearch.rs       # ES bulk API client
│   │   │   └── buffer.rs             # Ring buffer for backpressure
│   │   └── platform/
│   │       ├── linux.rs               # Linux-specific implementations
│   │       └── windows.rs             # Windows-specific implementations
│   └── tests/
```

**Deliverables:**
- [ ] Rust project scaffolding with cross-platform build (cargo cross)
- [ ] Config system (ES endpoint, API key, poll interval, opt-in settings)
- [ ] CPU collector (Linux: /proc/stat, Windows: PDH)
- [ ] Memory collector (Linux: /proc/meminfo, Windows: GlobalMemoryStatusEx)
- [ ] AMD GPU collector for Linux (sysfs/hwmon) — your primary GPU
- [ ] Basic session metadata (OS, kernel, CPU model, GPU model)
- [ ] Elasticsearch bulk API shipper with buffering
- [ ] ES index templates and ILM policies
- [ ] First working end-to-end: start agent → play game → see data in Kibana

### Phase 1: Game Intelligence (Weeks 4–6)
**Goal: Auto-detect games and collect frame timing**

**Deliverables:**
- [ ] Game auto-detection via process name + Steam API
- [ ] Steam App ID resolution and game metadata enrichment
- [ ] Proton/Wine/DXVK/VKD3D-Proton version auto-detection
- [ ] Gamescope version detection
- [ ] Mesa/Vulkan driver version detection
- [ ] Frame timing collection (MangoHud stats pipe on Linux)
- [ ] FPS calculations (avg, 1% low, 0.1% low, variance)
- [ ] Session lifecycle (detect game start → collect → detect game exit → finalise session)
- [ ] GPU temperature, power, clock, VRAM collectors (AMD Linux via hwmon)
- [ ] Kibana dashboard: basic per-game FPS over time

### Phase 2: Deep System Metrics + Windows (Weeks 7–10)
**Goal: Full metric coverage, Windows support, storage/network metrics**

**Deliverables:**
- [ ] Per-core CPU metrics (clock, utilisation, temperature)
- [ ] CPU power (RAPL on Linux, WMI on Windows)
- [ ] Storage I/O metrics (throughput, latency, queue depth)
- [ ] Network metrics (RTT, packet stats for multiplayer)
- [ ] Windows GPU support (NVML for NVIDIA, ADL/ADLX for AMD)
- [ ] Windows frame timing (PresentMon / ETW)
- [ ] Windows game detection (process + Steam registry)
- [ ] Windows system environment collection
- [ ] NVIDIA GPU support (Linux + Windows via NVML)
- [ ] Power/battery state for handhelds and laptops
- [ ] Steam Deck specific detection (model, TDP, device type)
- [ ] Kibana dashboards: hardware comparison, config comparison

### Phase 3: eBPF Telemetry (Weeks 11–16)
**Goal: Deep kernel-level observability for developers**

**Deliverables:**
- [ ] Aya eBPF framework integration
- [ ] eBPF: Syscall latency distribution (per-syscall histograms)
- [ ] eBPF: File I/O tracing (which files, latency, size — shader cache, asset loading)
- [ ] eBPF: Block I/O latency (biolatency equivalent)
- [ ] eBPF: Memory allocation tracing (mmap/brk patterns, page faults)
- [ ] eBPF: Scheduler latency (runqueue wait time per game thread)
- [ ] eBPF: DRM/GPU submit tracing (amdgpu_cs_ioctl latency)
- [ ] eBPF: GPU fence wait tracing (CPU stalls waiting on GPU)
- [ ] eBPF: Futex contention (thread synchronisation bottlenecks)
- [ ] eBPF: TCP RTT and retransmit tracing (multiplayer)
- [ ] eBPF: Wine/Proton syscall translation overhead measurement
- [ ] Graceful degradation on kernels without BTF
- [ ] eBPF data model in ES (histograms, flamegraph-compatible stacks)
- [ ] Kibana dashboards: I/O heatmaps, latency distributions, flamegraphs

### Phase 4: Elastic Agent Integration & Distribution (Weeks 17–20)
**Goal: Packaged for easy community distribution**

**Deliverables:**
- [ ] Elastic Agent custom integration package
- [ ] Fleet-managed deployment and configuration
- [ ] Pre-built Kibana dashboards shipped as saved objects
- [ ] Ingest pipeline for data normalisation and enrichment
- [ ] Data privacy controls (opt-in public sharing, field anonymisation)
- [ ] User identity: anonymous hardware hash (no PII)
- [ ] Installation packages: .deb, .rpm, Flatpak, AUR, Windows MSI
- [ ] Steam Deck: Flatpak or Decky Loader plugin
- [ ] CLI installer with guided setup (ES endpoint, API key)
- [ ] Documentation site (mdBook or similar)

### Phase 5: Community Platform & Advanced Analytics (Weeks 21–26)
**Goal: Public dashboards, regression detection, comparison tools**

**Deliverables:**
- [ ] Multi-tenant data model (public vs private sessions)
- [ ] Community Kibana dashboards (aggregated, anonymous)
- [ ] Game performance leaderboards (by hardware tier)
- [ ] Automatic regression detection (alerts on FPS drops after driver/Proton updates)
- [ ] Configuration comparison tool (same game, different configs side-by-side)
- [ ] Driver version impact analysis (aggregate FPS by driver version per game)
- [ ] Proton version impact analysis
- [ ] Kernel version impact analysis
- [ ] API for third-party tools to query aggregated data
- [ ] Embeddable widgets for journalists/reviewers
- [ ] Intel Arc GPU support
- [ ] Mobile GPU support (Adreno, Mali — future Android gaming)

---

## 7. Extended Scope — Beyond Initial Vision

### 7.1 Shader Compilation Stutter Detection
Correlate eBPF shader-compile events with frame time spikes. Produce per-game "stutter maps" showing exactly when and why compilation stutters happen. This is enormously valuable to Valve's shader pre-caching team and game engine developers.

### 7.2 Proton/DXVK Overhead Quantification
For games that have both native Linux and Windows builds, compare metrics to quantify the exact overhead of the translation layer. Break down overhead by category (CPU translation, draw call batching, shader compilation, API differences).

### 7.3 Power Efficiency Metrics
Especially relevant for handhelds (Steam Deck, ROG Ally, Legion Go): calculate performance-per-watt, battery life estimates, and optimal TDP settings per game. Users could query "what TDP gives me 60fps in Starfield on Steam Deck OLED?"

### 7.4 Thermal Throttling Detection
Correlate temperature sensors with clock speed drops and FPS dips. Automatically tag sessions where thermal throttling occurred and quantify the performance impact.

### 7.5 Crash & Hang Detection
Detect when a game process crashes or hangs (via process monitoring + eBPF). Capture the last N seconds of telemetry before the crash for post-mortem analysis. Correlate with system state (OOM, GPU reset, driver timeout).

### 7.6 Audio Pipeline Metrics
Audio glitches (buffer underruns) correlate with performance issues. Monitor PipeWire/PulseAudio (Linux) and WASAPI (Windows) for xruns, latency, and buffer usage.

### 7.7 Input Latency
End-to-end input latency measurement using eBPF to trace from USB HID event → kernel input → game process read → frame presented. This is cutting-edge and extremely valuable to competitive gaming communities.

### 7.8 Machine Learning Anomaly Detection
Use Elastic's ML capabilities to automatically detect anomalous performance patterns — regressions, thermal throttling, memory leaks, shader compilation storms — without manual threshold configuration.

### 7.9 Game Config File Parsing
For popular games, parse their config/settings files to capture exact in-game graphics settings (texture quality, shadow quality, RT settings, etc.). This makes performance data far more meaningful — "60fps at Ultra" vs "60fps at Low" tells a very different story.

### 7.10 Replay & Benchmark Standardisation
Provide a way to tag "benchmark runs" (specific in-game benchmark sequences) vs "general gameplay" to enable apples-to-apples comparison. Integrate with game-specific benchmark modes where available.

---

## 8. Privacy & Security Design

### 8.1 Principles
- **Opt-in everything**: No data leaves the machine without explicit consent
- **No PII by default**: User identity is a salted hardware hash, no usernames/emails/IPs
- **Granular controls**: Users choose what categories to share (basic metrics, eBPF, session data)
- **Local-first option**: Users can run their own ES instance and never share externally
- **Open source**: Full transparency on what is collected

### 8.2 Data Tiers
| Tier | Data | Default |
|------|------|---------|
| Tier 0 — Local only | All raw data, stored locally | Always on |
| Tier 1 — Anonymous metrics | FPS, temps, utilisation, hardware config | Opt-in |
| Tier 2 — Session metadata | Game name, driver versions, Proton version | Opt-in |
| Tier 3 — Deep telemetry | eBPF traces, I/O patterns, syscall data | Opt-in |

### 8.3 Security
- Agent runs with minimum required privileges (non-root where possible, CAP_BPF for eBPF)
- ES communication over TLS only
- API key authentication (Elastic Cloud or self-hosted)
- No shell-out or command execution from config
- eBPF programs are compiled into the binary (no runtime loading of external BPF objects)

---

## 9. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Agent overhead affects game performance | Undermines core mission | Budget: <1% CPU, <50MB RAM. Continuous profiling. Ring buffer with drop-oldest semantics. |
| eBPF kernel compatibility | Won't work on older kernels | CO-RE with BTF, graceful feature degradation, skip eBPF on kernels <5.8 |
| Anti-cheat interference (EAC, BattlEye) | Agent blocked or user banned | Whitelist process, avoid hooking game processes directly. Use OS-level APIs only. |
| GPU vendor API fragmentation | Maintenance burden per vendor | Abstract behind trait/interface, start with AMD only, add vendors incrementally |
| Windows ETW complexity | Frame timing hard to get right | Leverage PresentMon (MIT) as library rather than reimplementing |
| Data volume at scale | Elastic Cloud costs | Configurable sample rate, ILM with rollover, downsampling for old data |
| Privacy incidents | Trust destruction | Minimal collection defaults, no PII, open source audit trail |

---

## 10. Tools & Access Required

| Need | Purpose | Status |
|------|---------|--------|
| Elastic Cloud instance | Development and testing | ✅ Mat has access |
| AMD GPU Linux machine | Primary development platform | ✅ Mat has this |
| Windows 11 machine | Windows agent development | ✅ Mat has this |
| Steam Deck (or SteamOS device) | Handheld testing | ❓ To confirm |
| NVIDIA GPU machine | NVIDIA support testing | ❓ To confirm |
| Rust toolchain | Agent development | Can install |
| Cross-compilation (cargo-cross) | Build for Linux/Windows from one machine | Can install |
| GitHub/GitLab repo | Source control, CI/CD | To set up |
| CI/CD (GitHub Actions) | Automated builds, tests, releases | To set up |

---

## 11. Success Metrics

| Milestone | How we know it works |
|-----------|---------------------|
| Phase 0 complete | Agent running on Mat's Linux PC, metrics visible in Kibana |
| Phase 1 complete | Can see "Starfield on Linux with Proton 9.0, AMD 780M, avg 42fps" in dashboard |
| Phase 2 complete | Same game, same system, Windows vs Linux side-by-side comparison in Kibana |
| Phase 3 complete | Can identify "shader compilation stutter at timestamp X caused 200ms frame time spike" via eBPF |
| Phase 4 complete | New user can install agent, configure ES endpoint, and see data within 10 minutes |
| Phase 5 complete | Public dashboard shows community-aggregated performance data per game |

---

## 12. What We Build First (Phase 0 — Next Steps)

Starting with the highest-impact, lowest-risk work:

1. **Rust project scaffolding** with cross-platform abstractions
2. **AMD GPU metrics on Linux** (sysfs/hwmon — your primary setup)
3. **CPU + Memory basics** (cross-platform)
4. **Elasticsearch shipper** with bulk API
5. **Index templates + ILM policies** deployed to your Elastic Cloud
6. **One Kibana dashboard** showing live system metrics

This gets data flowing end-to-end immediately. Every subsequent phase adds new collectors and enrichers to the same pipeline.

---

*This is a living document. Each phase will be broken into specific implementation tasks as we begin work.*
