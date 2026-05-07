# Metrics Reference

GamePulse ships data to 11 Elasticsearch data streams. The authoritative field definitions
are in `data_stream/*/fields/fields.yml`. This document summarises the key fields per stream.

All streams use the ECS data stream naming convention:
- **Data stream name:** `metrics-gamepulse.<dataset>-default`
- **Index pattern (Kibana data view):** `metrics-gamepulse.*`

---

## Common fields — all streams

Every document includes these fields.

| Field | Type | Description |
|-------|------|-------------|
| `@timestamp` | date | Collection timestamp |
| `data_stream.type` | keyword | `metrics` or `logs` |
| `data_stream.dataset` | keyword | e.g. `gamepulse.cpu` |
| `data_stream.namespace` | keyword | `default` |
| `host.name` | keyword | Hostname |
| `gamepulse.session.id` | keyword | Session UUID (correlates all streams for one game session) |
| `gamepulse.session.agent_version` | keyword | Agent version string |
| `gamepulse.game.name` | keyword | Game title |
| `gamepulse.game.source` | keyword | `steam` \| `lutris` \| `heroic` \| `bottles` \| `user_specified` \| `auto_detected` |
| `gamepulse.game.launcher` | keyword | Human-readable launcher (e.g. `Steam`, `Heroic — Epic`) |
| `gamepulse.game.steam_app_id` | long | Steam App ID (present only when `source == steam`) |

---

## `metrics-gamepulse.session-default`

One document on game start, updated on game end. The primary correlation anchor.

### Session identity

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.session.label` | keyword | Auto-generated label: `<slug>-YYYYMMDD-N` |
| `gamepulse.session.label_source` | keyword | `auto` or `manual` |
| `gamepulse.session.sequence_number` | long | Per-game-per-day counter (auto labels only) |
| `gamepulse.session.opt_in_public` | boolean | Whether anonymous data is shared publicly |

### Hardware snapshot

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.hardware.cpu.model` | keyword | CPU model string |
| `gamepulse.hardware.cpu.cores` | integer | Physical core count |
| `gamepulse.hardware.gpu.model` | keyword | GPU model string |
| `gamepulse.hardware.gpu.vendor` | keyword | `amd` \| `nvidia` \| `intel` |
| `gamepulse.hardware.gpu.vram_mb` | long | Total VRAM in MB |
| `gamepulse.hardware.gpu.driver_version` | keyword | Driver version |
| `gamepulse.hardware.gpu.mesa_version` | keyword | Mesa version (Linux) |
| `gamepulse.hardware.ram.total_mb` | long | Total system RAM |

### Game context

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.game.graphics_api` | keyword | `vulkan` \| `dx12_via_vkd3d` \| `dx11_via_dxvk` \| `opengl` \| etc. |
| `gamepulse.compatibility.proton_version` | keyword | Proton version (e.g. `GE-Proton9-20`) |
| `gamepulse.compatibility.dxvk_version` | keyword | DXVK version |
| `gamepulse.compatibility.vkd3d_proton_version` | keyword | VKD3D-Proton version |

### Settings capture (Tier 1–3)

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.settings.preset` | keyword | `low` \| `medium` \| `high` \| `ultra` \| `custom` |
| `gamepulse.settings.upscaler.tech` | keyword | `dlss` \| `fsr` \| `xess` \| `tsr` \| `none` |
| `gamepulse.settings.upscaler.preset` | keyword | `quality` \| `balanced` \| `performance` \| `ultra_performance` |
| `gamepulse.settings.frame_gen.tech` | keyword | `dlss3` \| `fsr3` \| `afmf` \| `none` |
| `gamepulse.settings.features_active` | keyword[] | Active features: `ray_tracing`, `path_tracing`, etc. |
| `gamepulse.settings.render.vsync` | keyword | `off` \| `on` \| `adaptive` \| `fast` |
| `gamepulse.settings.source` | keyword | `manual` \| `dll_scan` \| `profile` |
| `gamepulse.settings.confidence` | keyword | `high` \| `medium` \| `low` |
| `gamepulse.settings.notes` | keyword | Free-text user notes |

### Session summary (updated on game exit)

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.summary.ended` | boolean | `true` on session-end document |
| `gamepulse.summary.duration_secs` | float | Total session duration |
| `gamepulse.summary.fps_avg` | float | Average FPS over session |
| `gamepulse.summary.fps_1pct_low` | float | 1% low FPS over session |
| `gamepulse.summary.fps_01pct_low` | float | 0.1% low FPS over session |
| `gamepulse.summary.bottleneck_dominant` | keyword | `gpu` \| `cpu` \| `memory` \| `storage` |

---

## `metrics-gamepulse.cpu-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `gamepulse.cpu.total_utilisation_pct` | float | % | Total CPU utilisation across all cores |
| `gamepulse.cpu.per_core` | float[] | % | Per-core utilisation |
| `gamepulse.cpu.clock_mhz_avg` | long | MHz | Average clock across cores |
| `gamepulse.cpu.temperature_c` | float | °C | Package/die temperature |
| `gamepulse.cpu.power_w` | float | W | Package power (RAPL, Linux) |
| `gamepulse.cpu.governor` | keyword | — | `performance`, `schedutil`, etc. (Linux) |
| `gamepulse.cpu.boost_state` | boolean | — | Boost/turbo active |
| `gamepulse.cpu.game_utilisation_pct` | float | % | Process-scoped CPU (Linux, cgroup) |

---

## `metrics-gamepulse.gpu-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `gamepulse.gpu.utilisation_pct` | float | % | Core utilisation |
| `gamepulse.gpu.clock_mhz` | long | MHz | Current core clock |
| `gamepulse.gpu.memory_used_mb` | long | MB | VRAM in use |
| `gamepulse.gpu.memory_total_mb` | long | MB | Total VRAM |
| `gamepulse.gpu.temperature_c` | float | °C | Edge/die temperature |
| `gamepulse.gpu.hotspot_c` | float | °C | Hotspot temperature (AMD) |
| `gamepulse.gpu.memory_temperature_c` | float | °C | VRAM temperature (AMD) |
| `gamepulse.gpu.power_w` | float | W | Board power draw (Linux) |
| `gamepulse.gpu.fan_pct` | float | % | Fan speed |
| `gamepulse.gpu.voltage` | float | V | Core voltage (Linux) |
| `gamepulse.gpu.temp_source` | keyword | — | Temperature data source: `hwmon` (Linux) \| `wmi_acpi` (Windows) |

---

## `metrics-gamepulse.memory-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `gamepulse.memory.total_mb` | long | MB | Total system RAM |
| `gamepulse.memory.used_mb` | long | MB | Used RAM |
| `gamepulse.memory.available_mb` | long | MB | Available RAM |
| `gamepulse.memory.used_pct` | float | % | RAM utilisation |
| `gamepulse.memory.game_rss_mb` | long | MB | Game process resident set size |

---

## `metrics-gamepulse.frame-default`

Fields are under `gamepulse.fps.*` (not `gamepulse.frame.*`) to match the Linux MangoHud collector naming.

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `gamepulse.fps.current` | float | fps | Instantaneous FPS |
| `gamepulse.fps.avg_1s` | float | fps | 1-second rolling average |
| `gamepulse.fps.low_1pct` | float | fps | 1% low (ring buffer) |
| `gamepulse.fps.low_01pct` | float | fps | 0.1% low (ring buffer) |
| `gamepulse.fps.frametime_ms` | float | ms | Mean frame time |
| `gamepulse.fps.frametime_variance` | float | ms² | Frame time variance |
| `gamepulse.fps.stutter_count` | integer | — | Frames > 2× tick mean |
| `gamepulse.fps.stutter_detected` | boolean | — | `stutter_count > 0` |
| `gamepulse.performance.bottleneck` | keyword | — | `gpu` \| `cpu` \| `balanced` |

---

## `metrics-gamepulse.storage-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `gamepulse.storage.read_bytes_per_sec` | long | B/s | System read throughput |
| `gamepulse.storage.write_bytes_per_sec` | long | B/s | System write throughput |

---

## `metrics-gamepulse.network-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `gamepulse.network.bytes_sent_per_sec` | long | B/s | Upload throughput |
| `gamepulse.network.bytes_recv_per_sec` | long | B/s | Download throughput |

---

## `metrics-gamepulse.audio-default`

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.audio.backend` | keyword | `pipewire`, `pulseaudio`, `wasapi` |
| `gamepulse.audio.xruns` | long | Audio buffer underruns (Linux) |

---

## `metrics-gamepulse.power-default`

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.power.ac_connected` | boolean | AC power connected |
| `gamepulse.power.battery_pct` | float | Battery percentage (portable devices) |
| `gamepulse.power.battery_rate_w` | float | Discharge rate in watts (Linux) |

---

## `metrics-gamepulse.ebpf-default`

eBPF kernel-level metrics from the companion `gamepulse-ebpf` daemon.
Correlated to session via `gamepulse.session.id`.

Key field groups: `gamepulse.ebpf.sched.*` (scheduler latency), `gamepulse.ebpf.gpu.*`
(fence/submit/sched latency), `gamepulse.ebpf.futex.*`, `gamepulse.ebpf.bio.*` (block I/O),
`gamepulse.ebpf.mem.*` (memory pressure), `gamepulse.ebpf.vfs.*`, `gamepulse.ebpf.frame.*` (stutter).

See `data_stream/ebpf/fields/fields.yml` for the full field list.

---

## `logs-gamepulse.events-default`

Discrete events during a game session (shader compilation, save operations, etc.).

| Field | Type | Description |
|-------|------|-------------|
| `gamepulse.event.kind` | keyword | Event type: `shader_compile`, `save`, `crash`, etc. |
| `gamepulse.event.severity` | keyword | `info` \| `warning` \| `error` |
| `message` | text | Human-readable event description |
