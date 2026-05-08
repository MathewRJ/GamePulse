# Changelog

All notable changes to GamePulse will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Milestone D — Linux portable packaging (2026-04-27)

- `/proc/<pid>/maps` DLL scan for Tier 2 settings auto-detection: detects graphics API
  (VKD3D > DXVK D3D9/D3D11 > Vulkan > OpenGL), upscaler (DLSS/XeSS/FSR), and frame
  generation technology from loaded shared libraries (`src/dllscan.rs`)
- Game profile loader and three starter profiles — Starfield, Cyberpunk 2077, Baldur's
  Gate 3 — for Tier 3 settings capture (`src/profiles.rs`, `profiles/`)
- GitHub Actions release workflow: `.deb` (cargo-deb), `.rpm` (cargo-generate-rpm), and
  Arch `.pkg.tar.zst` (makepkg) attached to GitHub Releases on `v*` tag pushes
- `gamepulse-agent diagnose` subcommand: single-file bug-report dump covering kernel,
  GPU, Elasticsearch reachability, and resolved config path
- Unified logging flags: `--verbose` / `--log-level LEVEL` / `--print-config` (redacted)

### Milestone C — Windows collectors (2026-04-26)

- Full 8-stream Windows support: CPU (PDH), memory (Win32), storage (PDH), network (PDH),
  power (GetSystemPowerStatus), audio (WASAPI), GPU (DXGI VRAM + PDH util + WMI temperature),
  frame timing (PresentMon subprocess with auto-discovery)
- PDH infrastructure (`src/collectors/windows/pdh.rs`) wrapping Windows Performance Counters
- `gamepulse.gpu.temp_source` field distinguishing `hwmon` (Linux) from `wmi_acpi` (Windows)
- Platform parity gaps documented: `cpu.game_utilisation_pct`, `gpu.power_w`, `audio.xruns`,
  `storage.game_io` require ETW/vendor SDK and are deferred to later milestones

### Milestone B2 — Launcher-agnostic game detection (2026-04-25)

- Multi-launcher game detection: Lutris (YAML game configs + WINEPREFIX scan), Heroic
  (Epic and GOG via installed.json), Bottles (bottle.yml + WINEPREFIX), plus manual
  `--target-pid` / `--target-name` override flags
- `gamepulse.game.source` and `gamepulse.game.launcher` fields on all session and metric docs
- Proton/Wine env var enrichment (`PROTON_VERSION`, `DXVK_CONFIG_FILE`) wired to all launchers
- `steam_app_id` made conditional (present only when `source == steam`)

### Milestone B — Cross-platform refactor (2026-04-24)

- Unified `Collector` trait (`Send + 'static`) with platform-gated `linux::*` / `windows::*`
  implementations; `build_collectors()` dispatch replaces per-platform main.rs branches
- Linux collectors relocated to `src/collectors/linux/`; Windows stubs added in
  `src/collectors/windows/` (replaced with real implementations in Milestone C)
- GitHub Actions CI matrix: `cargo check` + `cargo clippy -- -D warnings` on
  `ubuntu-latest` and `windows-latest` for every push to `main` and every PR
- eBPF feature flag (`--features ebpf`) with compile-time Linux guard
- Session label counter: format changed from `<slug>-YYYYMMDD-HHMMSS` to `<slug>-YYYYMMDD-N`;
  counter persisted atomically to `$XDG_STATE_HOME/gamepulse/session-counters.json`
- Tier 1 settings capture: `[session.settings]` TOML section and CLI flags `--preset`,
  `--upscaler`, `--frame-gen`, `--features`, `--resolution`, `--vsync`, `--notes`
- Signal handling ported to cross-platform (`tokio::signal::ctrl_c` on Windows)

---

## [0.1.5] — 2026-05-08

### Fixed

- **Gamescope / Gaming Mode crash (launcher)**: `systemctl --user reset-failed` is now called before `start` in `cmd_run` so a FAILED unit (from a prior crash loop hitting the restart rate limit) is properly reset instead of silently failing to start. The `wait_agent_active` poll that previously blocked game launch for up to 10 seconds is removed — the agent detects already-running games by scanning `/proc`, so the game launches immediately regardless of agent initialisation state. This prevents Gamescope session-launch timeouts from killing the game before it renders.

---

## [0.1.4] — 2026-05-08

### Fixed

- **Gamescope / Gaming Mode (launcher)**: `cmd_run` now falls back to running `gamepulse-agent` directly in the background when `systemctl --user` fails (DBUS absent in Gamescope). Previously the launcher exited, preventing the game from launching. The agent binary is resolved relative to the launcher's own directory so `~/.local/bin` does not need to be on PATH in the gamescope session.

---

## [0.1.3] — 2026-05-08

### Fixed

- **Elasticsearch 403 on startup**: Ping endpoint changed from `GET /` (requires `cluster:monitor/main`) to `GET /_cluster/health` (requires `cluster:monitor/health`). HTTP 4xx responses from the ping are now non-fatal — the agent continues and ships data even if the health check returns 401/403/410.

---

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
