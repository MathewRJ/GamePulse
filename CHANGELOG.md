# Changelog

All notable changes to GamePulse will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-03-30

### Added

**Core Agent**
- Rust-based telemetry agent with 1-second collection interval
- TOML configuration with CLI overrides
- Graceful shutdown via Ctrl+C / SIGTERM
- Debug mode (`--debug`) for stdout output without Elasticsearch
- Single-pass mode (`--once`) for testing

**GPU Metrics**
- AMD GPU support via sysfs/hwmon (utilisation, clocks, VRAM, temps, power, fan, voltage, PCIe)
- NVIDIA GPU support via dynamic NVML loading (cross-platform, no compile-time SDK dependency)
- Auto-detection of GPU vendor on startup
- Thermal throttle detection

**CPU Metrics**
- Per-core utilisation, clock speeds, temperature (k10temp/coretemp)
- Package power via RAPL
- CPU governor and boost state detection

**Memory Metrics**
- System RAM and swap usage
- Memory pressure via Linux PSI
- Dirty pages and buffer/cache breakdown

**Storage Metrics**
- Drive type classification: NVMe, SATA SSD, HDD, SD card, USB, eMMC
- Filesystem detection with mount options (btrfs compression, TRIM, scheduler)
- Per-second I/O: throughput, IOPS, latency, queue depth, merged operations
- Drive temperature monitoring
- SD card speed class detection (UHS, A1/A2, V30)
- Encryption detection (LUKS)

**Network Metrics**
- Interface throughput (bytes/packets per second)
- TCP retransmit tracking
- Active connection count
- Auto-detection of primary interface and connection type (ethernet/wifi)

**Game Detection**
- Automatic Steam game detection via `/proc` environment scanning
- Steam App ID resolution from appmanifest files
- Multi-library support (standard, Flatpak, Snap Steam installations)
- Graphics API detection (Vulkan, DX11 via DXVK, DX12 via VKD3D-Proton)
- Wine/Proton helper process filtering

**Compatibility Detection**
- Proton version (from env vars, tool paths, Steam config, version files)
- Wine version (from Proton bundle)
- DXVK version (from Proton directory, DXVK logs)
- VKD3D-Proton version
- Mesa version (via vulkaninfo or glxinfo)
- Gamescope version

**Frame Timing**
- MangoHud CSV log parsing (incremental, low overhead)
- Gamescope stats integration
- FPS percentiles: average, 1% low, 0.1% low
- Frame time statistics: avg, min, max, standard deviation
- Stutter detection (frames exceeding 33ms)

**Per-Process Game Metrics**
- Game process RSS/VMS/shared memory
- Major/minor page fault rates
- Context switch rates (voluntary + involuntary)
- Per-process I/O read/write bytes
- CPU user/kernel time
- Open file descriptor count

**eBPF Deep Telemetry (Linux, requires CAP_BPF)**
- Block I/O latency histograms (biolatency)
- Scheduler run-queue latency (schedlatency)
- VFS read/write latency per game process
- Syscall count and latency profiling
- Page fault latency
- Futex contention (thread sync bottlenecks)
- TCP retransmit tracing
- GPU command submission latency (amdgpu_cs_ioctl)
- GPU fence wait time (dma_fence_wait — CPU stalled on GPU)
- Automatic stutter cause correlation

**Device Detection**
- Steam Deck LCD/OLED identification
- ROG Ally, Legion Go detection
- Laptop vs desktop classification (DMI chassis type)
- Power source detection (AC/battery)

**Analytics**
- Hardware tier classification (Enthusiast/High/Mid/Low/Integrated)
- Per-session summary with avg/median FPS, stutter rate, thermal stats
- Comparison query builders: driver impact, Proton impact, OS comparison, kernel impact, storage impact

**Elasticsearch Integration**
- Bulk API shipping with configurable batching
- Index lifecycle management (hot → warm → cold → delete)
- Index templates for sessions, metrics, eBPF, and events
- Ingest pipeline: hardware tier classification, FPS bracket tagging, throttle detection, stutter tagging
- Continuous transform for community aggregation
- FPS regression watcher (6-hour schedule)
- API key and basic auth support

**Distribution**
- Systemd services (system-level and user-level)
- Debian package (.deb) with postinst
- RPM spec file
- AUR PKGBUILD (Arch Linux / Steam Deck)
- Elastic Agent Fleet integration manifest
- Interactive install script with hardware auto-detection
- Makefile with build, test, install, and package targets
- GitHub Actions CI/CD (build, lint, test, release)

**Kibana**
- Pre-built dashboard with 7 panels (FPS, GPU, CPU, memory, storage, frame time, sessions)
- NDJSON export for easy import
