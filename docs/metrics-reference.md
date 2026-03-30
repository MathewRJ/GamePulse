# Metrics Reference

Every field collected by GamePulse, with types, units, and Elasticsearch mapping types.

## Session Document (`gamepulse-sessions-*`)

One document created per agent start, and one per detected game launch.

### Core fields

| Field | ES Type | Description |
|-------|---------|-------------|
| `@timestamp` | date | Session start time |
| `session_id` | keyword | Unique session identifier (UUID) |
| `session_status` | keyword | `active` or `closed` |
| `session_end` | date | Session end time (set on close) |
| `agent_version` | keyword | GamePulse agent version |
| `user_id` | keyword | Anonymous hardware-derived hash |
| `duration_secs` | float | Total session duration (set on close) |

### `os.*` — Operating system

| Field | ES Type | Example |
|-------|---------|---------|
| `os.os_type` | keyword | `linux`, `windows` |
| `os.distro` | keyword | `SteamOS`, `Arch Linux`, `Fedora` |
| `os.version` | keyword | `3.6.22`, `41` |
| `os.kernel` | keyword | `6.11.2-valve1` |
| `os.desktop` | keyword | `gamescope`, `KDE`, `GNOME` |

### `hardware.cpu.*`

| Field | ES Type | Example |
|-------|---------|---------|
| `hardware.cpu.model` | keyword | `AMD Ryzen 7 7840U` |
| `hardware.cpu.cores` | integer | `8` |
| `hardware.cpu.threads` | integer | `16` |
| `hardware.cpu.base_clock_mhz` | integer | `3300` |
| `hardware.cpu.boost_clock_mhz` | integer | `5100` |

### `hardware.gpu.*`

| Field | ES Type | Example |
|-------|---------|---------|
| `hardware.gpu.vendor` | keyword | `amd`, `nvidia` |
| `hardware.gpu.model` | keyword | `AMD Radeon 780M` |
| `hardware.gpu.vram_mb` | integer | `4096` |
| `hardware.gpu.driver_version` | keyword | `24.3.1` |
| `hardware.gpu.mesa_version` | keyword | `24.3.1` |
| `hardware.gpu.vulkan_driver` | keyword | `radv` |
| `hardware.gpu.pcie_gen` | integer | `4` |
| `hardware.gpu.pcie_width` | integer | `8` |

### `hardware.ram.*`

| Field | ES Type | Example |
|-------|---------|---------|
| `hardware.ram.total_mb` | integer | `16384` |
| `hardware.ram.speed_mhz` | integer | `6400` |
| `hardware.ram.ram_type` | keyword | `LPDDR5X` |

### `hardware.device.*`

| Field | ES Type | Example |
|-------|---------|---------|
| `hardware.device.device_type` | keyword | `handheld`, `desktop`, `laptop` |
| `hardware.device.model` | keyword | `Steam Deck OLED` |
| `hardware.device.power_source` | keyword | `ac`, `battery` |
| `hardware.device.tdp_watts` | float | `15.0` |

### `compatibility.*`

| Field | ES Type | Example |
|-------|---------|---------|
| `compatibility.proton_version` | keyword | `Proton 9.0-4` |
| `compatibility.wine_version` | keyword | `wine-9.0` |
| `compatibility.dxvk_version` | keyword | `v2.5.1` |
| `compatibility.vkd3d_proton_version` | keyword | `v2.13` |
| `compatibility.gamescope_version` | keyword | `3.15.7` |
| `compatibility.mesa_version` | keyword | `24.3.1` |

### `game.*`

| Field | ES Type | Example |
|-------|---------|---------|
| `game.name` | keyword | `Starfield` |
| `game.steam_app_id` | long | `1716740` |
| `game.executable` | keyword | `/path/to/game.exe` |
| `game.pid` | integer | `12345` |
| `game.graphics_api` | keyword | `dx12_via_vkd3d`, `vulkan` |
| `game.uses_proton` | boolean | `true` |
| `game.install_dir` | keyword | `/home/user/.steam/...` |

---

## Metrics Document (`gamepulse-metrics-*`)

One document per collection tick (default: every 1 second).

### `cpu.*`

| Field | ES Type | Unit | Description |
|-------|---------|------|-------------|
| `cpu.total_utilisation_pct` | float | % | Average across all cores |
| `cpu.per_core` | float[] | % | Per-core utilisation array |
| `cpu.clock_mhz_avg` | float | MHz | Average clock speed |
| `cpu.temperature_c` | float | °C | Package/die temperature |
| `cpu.power_w` | float | W | Package power draw (RAPL) |
| `cpu.governor` | keyword | — | `performance`, `schedutil`, etc. |
| `cpu.boost_enabled` | boolean | — | Whether boost/turbo is active |

### `gpu.*`

| Field | ES Type | Unit | Description |
|-------|---------|------|-------------|
| `gpu.utilisation_pct` | float | % | GPU core utilisation |
| `gpu.clock_mhz` | long | MHz | Current core clock |
| `gpu.clock_max_mhz` | long | MHz | Maximum core clock |
| `gpu.memory_clock_mhz` | long | MHz | Current memory clock |
| `gpu.memory_used_mb` | long | MB | VRAM in use |
| `gpu.memory_total_mb` | long | MB | Total VRAM |
| `gpu.temperature_c` | float | °C | Edge/junction temperature |
| `gpu.hotspot_c` | float | °C | Hotspot temperature |
| `gpu.memory_temperature_c` | float | °C | VRAM/HBM temperature |
| `gpu.power_w` | float | W | Board power draw |
| `gpu.power_limit_w` | float | W | Power limit / TDP |
| `gpu.fan_pct` | float | % | Fan speed percentage |
| `gpu.fan_rpm` | long | RPM | Fan speed |
| `gpu.voltage_mv` | long | mV | Core voltage |
| `gpu.pcie_speed` | keyword | — | Current PCIe link speed |
| `gpu.pcie_width` | integer | — | Current PCIe link width |

### `memory.*`

| Field | ES Type | Unit | Description |
|-------|---------|------|-------------|
| `memory.system_total_mb` | long | MB | Total system RAM |
| `memory.system_used_mb` | long | MB | Used system RAM |
| `memory.system_available_mb` | long | MB | Available system RAM |
| `memory.system_utilisation_pct` | float | % | RAM utilisation |
| `memory.swap_total_mb` | long | MB | Total swap |
| `memory.swap_used_mb` | long | MB | Used swap |
| `memory.pressure_some_pct` | float | % | Memory PSI (avg10) |
| `memory.dirty_mb` | float | MB | Dirty pages |
| `memory.buffers_cache_mb` | long | MB | Kernel buffers + cache |

### `storage.*`

| Field | ES Type | Unit | Description |
|-------|---------|------|-------------|
| `storage.read_mbps` | float | MB/s | Read throughput |
| `storage.write_mbps` | float | MB/s | Write throughput |
| `storage.read_iops` | long | ops/s | Read IOPS |
| `storage.write_iops` | long | ops/s | Write IOPS |
| `storage.io_wait_pct` | float | % | CPU time waiting on I/O |
| `storage.queue_depth` | long | — | Current I/O queue depth |
| `storage.merged_reads` | long | ops/s | Adjacent reads merged by scheduler |
| `storage.merged_writes` | long | ops/s | Adjacent writes merged |
| `storage.avg_read_latency_us` | float | μs | Average read latency |
| `storage.avg_write_latency_us` | float | μs | Average write latency |
| `storage.drive_temperature_c` | float | °C | Drive temperature |
| `storage.pressure_some_pct` | float | % | I/O PSI (avg10) |

### `fps.*`

| Field | ES Type | Unit | Description |
|-------|---------|------|-------------|
| `fps.fps` | float | fps | Current FPS |
| `fps.fps_avg` | float | fps | Rolling average FPS |
| `fps.fps_1pct_low` | float | fps | 1st percentile low |
| `fps.fps_01pct_low` | float | fps | 0.1st percentile low |
| `fps.frametime_avg_ms` | float | ms | Average frame time |
| `fps.frametime_max_ms` | float | ms | Worst frame time |
| `fps.frametime_min_ms` | float | ms | Best frame time |
| `fps.frametime_stdev_ms` | float | ms | Frame time jitter |
| `fps.frame_count` | integer | — | Frames in this sample |
| `fps.stutter_count` | integer | — | Frames exceeding 33ms |
| `fps.source` | keyword | — | `mangohud`, `gamescope` |

### `network.*`

| Field | ES Type | Unit | Description |
|-------|---------|------|-------------|
| `network.rx_bytes_per_sec` | float | B/s | Download throughput |
| `network.tx_bytes_per_sec` | float | B/s | Upload throughput |
| `network.rx_packets_per_sec` | float | pkt/s | Receive packet rate |
| `network.tx_packets_per_sec` | float | pkt/s | Transmit packet rate |
| `network.tcp_retransmits_per_sec` | float | evt/s | TCP retransmit rate |
| `network.tcp_connections` | integer | — | Active TCP connections |
| `network.connection_type` | keyword | — | `ethernet`, `wifi` |

### `game_process.*`

| Field | ES Type | Unit | Description |
|-------|---------|------|-------------|
| `game_process.pid` | integer | — | Game process ID |
| `game_process.rss_mb` | float | MB | Resident set size |
| `game_process.vms_mb` | float | MB | Virtual memory size |
| `game_process.thread_count` | integer | — | Number of threads |
| `game_process.page_faults_major` | long | — | Cumulative major faults |
| `game_process.page_faults_major_per_sec` | float | /s | Major fault rate |
| `game_process.ctx_switches_voluntary_per_sec` | float | /s | Voluntary context switch rate |
| `game_process.ctx_switches_involuntary_per_sec` | float | /s | Involuntary context switch rate |
| `game_process.io_read_bytes_per_sec` | float | B/s | Process read throughput |
| `game_process.io_write_bytes_per_sec` | float | B/s | Process write throughput |
| `game_process.cpu_user_secs` | float | s | Cumulative user CPU time |
| `game_process.cpu_system_secs` | float | s | Cumulative kernel CPU time |
| `game_process.fd_count` | integer | — | Open file descriptors |

### Ingest pipeline enrichments

These fields are added by the `gamepulse-metrics` ingest pipeline:

| Field | ES Type | Description |
|-------|---------|-------------|
| `hardware_tier` | keyword | `enthusiast`, `high`, `mid`, `low`, `integrated` |
| `fps_bracket` | keyword | `120+`, `90-120`, `60-90`, `30-60`, `below_30` |
| `throttle_detected` | boolean | GPU > 90°C or CPU > 95°C |
| `throttle_source` | keyword | `gpu_thermal` or `cpu_thermal` |
| `has_stutter` | boolean | Any frame > 33ms |
| `severe_stutter` | boolean | Any frame > 100ms |
| `storage_bottleneck` | boolean | I/O wait > 5% or queue depth > 32 |
