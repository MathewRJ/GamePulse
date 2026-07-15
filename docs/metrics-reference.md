# Metrics Reference

RigSignal ships data to 11 Elasticsearch data streams. The authoritative field definitions
are in `data_stream/*/fields/fields.yml`. This document summarises the key fields per stream.

All streams use the ECS data stream naming convention:
- **Data stream name:** `metrics-rigsignal.<dataset>-default`
- **Index pattern (Kibana data view):** `metrics-rigsignal.*`

---

## Common fields — all streams

Every document includes these fields.

| Field | Type | Description |
|-------|------|-------------|
| `@timestamp` | date | Collection timestamp |
| `data_stream.type` | keyword | `metrics` or `logs` |
| `data_stream.dataset` | keyword | e.g. `rigsignal.cpu` |
| `data_stream.namespace` | keyword | `default` |
| `host.name` | keyword | Hostname |
| `rigsignal.session.id` | keyword | Session UUID (correlates all streams for one game session) |
| `rigsignal.session.agent_version` | keyword | Agent version string |
| `rigsignal.game.name` | keyword | Game title |
| `rigsignal.game.source` | keyword | `steam` \| `lutris` \| `heroic` \| `bottles` \| `user_specified` \| `auto_detected` |
| `rigsignal.game.launcher` | keyword | Human-readable launcher (e.g. `Steam`, `Heroic — Epic`) |
| `rigsignal.game.steam_app_id` | long | Steam App ID (present only when `source == steam`) |

---

## `metrics-rigsignal.session-default`

One document on game start, updated on game end. The primary correlation anchor.

### Session identity

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.session.label` | keyword | Auto-generated label: `<slug>-YYYYMMDD-N` |
| `rigsignal.session.label_source` | keyword | `auto` or `manual` |
| `rigsignal.session.sequence_number` | long | Per-game-per-day counter (auto labels only) |
| `rigsignal.session.opt_in_public` | boolean | Whether anonymous data is shared publicly |

### Hardware snapshot

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.hardware.cpu.model` | keyword | CPU model string |
| `rigsignal.hardware.cpu.cores` | integer | Physical core count |
| `rigsignal.hardware.gpu.model` | keyword | GPU model string |
| `rigsignal.hardware.gpu.vendor` | keyword | `amd` \| `nvidia` \| `intel` |
| `rigsignal.hardware.gpu.vram_mb` | long | Total VRAM in MB |
| `rigsignal.hardware.gpu.driver_version` | keyword | Driver version |
| `rigsignal.hardware.gpu.mesa_version` | keyword | Mesa version (Linux) |
| `rigsignal.hardware.ram.total_mb` | long | Total system RAM |

### Game context

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.game.graphics_api` | keyword | `vulkan` \| `dx12_via_vkd3d` \| `dx11_via_dxvk` \| `opengl` \| etc. |
| `rigsignal.compatibility.proton_version` | keyword | Proton version (e.g. `GE-Proton9-20`) |
| `rigsignal.compatibility.dxvk_version` | keyword | DXVK version |
| `rigsignal.compatibility.vkd3d_proton_version` | keyword | VKD3D-Proton version |

### Settings capture (Tier 1–3)

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.settings.preset` | keyword | `low` \| `medium` \| `high` \| `ultra` \| `custom` |
| `rigsignal.settings.upscaler.tech` | keyword | `dlss` \| `fsr` \| `xess` \| `tsr` \| `none` |
| `rigsignal.settings.upscaler.preset` | keyword | `quality` \| `balanced` \| `performance` \| `ultra_performance` |
| `rigsignal.settings.frame_gen.tech` | keyword | `dlss3` \| `fsr3` \| `afmf` \| `none` |
| `rigsignal.settings.features_active` | keyword[] | Active features: `ray_tracing`, `path_tracing`, etc. |
| `rigsignal.settings.render.vsync` | keyword | `off` \| `on` \| `adaptive` \| `fast` |
| `rigsignal.settings.source` | keyword | `manual` \| `dll_scan` \| `profile` |
| `rigsignal.settings.confidence` | keyword | `high` \| `medium` \| `low` |
| `rigsignal.settings.notes` | keyword | Free-text user notes |

Migration note: documents with a scalar `rigsignal.settings.frame_gen` predate
RigSignal 0.2.3. From 0.2.3 onward, the field is an object with the technology at
`rigsignal.settings.frame_gen.tech`; reindex affected historical documents into a
new index or roll over to a new backing index before mixing both shapes in one data
stream.

### Session summary (updated on game exit)

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.summary.ended` | boolean | `true` on session-end document |
| `rigsignal.summary.duration_secs` | float | Total session duration |
| `rigsignal.summary.fps_avg` | float | Average FPS over session |
| `rigsignal.summary.fps_1pct_low` | float | 1% low FPS over session |
| `rigsignal.summary.fps_01pct_low` | float | 0.1% low FPS over session |
| `rigsignal.summary.total_frames` | long | Frames counted by summing `avg_1s` FPS samples over instrumented one-second ticks only; not session duration × average FPS. |
| `rigsignal.summary.fps_coverage_s` | long | Number of instrumented seconds (FPS samples); the coverage denominator for `total_frames`, to compare with session duration. |
| `rigsignal.summary.bottleneck_dominant` | keyword | `gpu` \| `cpu` \| `memory` \| `storage` |

---

## `metrics-rigsignal.cpu-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.cpu.total_utilisation_pct` | float | % | Total CPU utilisation across all cores |
| `rigsignal.cpu.per_core` | float[] | % | Per-core utilisation |
| `rigsignal.cpu.clock_mhz_avg` | long | MHz | Average clock across cores |
| `rigsignal.cpu.temperature_c` | float | °C | Package/die temperature |
| `rigsignal.cpu.power_w` | float | W | Package power (RAPL, Linux) |
| `rigsignal.cpu.governor` | keyword | — | `performance`, `schedutil`, etc. (Linux) |
| `rigsignal.cpu.boost_state` | boolean | — | Boost/turbo active |
| `rigsignal.cpu.game_utilisation_pct` | float | % | Process-scoped CPU (Linux, cgroup) |

---

## `metrics-rigsignal.gpu-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.gpu.utilisation_pct` | float | % | Core utilisation |
| `rigsignal.gpu.clock_mhz` | long | MHz | Current core clock |
| `rigsignal.gpu.memory_used_mb` | long | MB | VRAM in use |
| `rigsignal.gpu.memory_total_mb` | long | MB | Total VRAM |
| `rigsignal.gpu.temperature_c` | float | °C | Edge/die temperature |
| `rigsignal.gpu.hotspot_c` | float | °C | Hotspot temperature (AMD) |
| `rigsignal.gpu.memory_temperature_c` | float | °C | VRAM temperature (AMD) |
| `rigsignal.gpu.power_w` | float | W | Board power draw (Linux) |
| `rigsignal.gpu.fan_pct` | float | % | Fan speed |
| `rigsignal.gpu.voltage` | float | V | Core voltage (Linux) |
| `rigsignal.gpu.temp_source` | keyword | — | Temperature data source: `hwmon` (Linux) \| `wmi_acpi` (Windows) |

---

## `metrics-rigsignal.memory-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.memory.total_mb` | long | MB | Total system RAM |
| `rigsignal.memory.used_mb` | long | MB | Used RAM |
| `rigsignal.memory.available_mb` | long | MB | Available RAM |
| `rigsignal.memory.used_pct` | float | % | RAM utilisation |
| `rigsignal.memory.game_rss_mb` | long | MB | Game process resident set size |

---

## `metrics-rigsignal.frame-default`

Fields are under `rigsignal.fps.*` (not `rigsignal.frame.*`) to match the Linux MangoHud collector naming.

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.fps.current` | float | fps | Instantaneous FPS |
| `rigsignal.fps.avg_1s` | float | fps | 1-second rolling average |
| `rigsignal.fps.low_1pct` | float | fps | 1% low (ring buffer) |
| `rigsignal.fps.low_01pct` | float | fps | 0.1% low (ring buffer) |
| `rigsignal.fps.frametime_ms` | float | ms | Mean frame time |
| `rigsignal.fps.frametime_variance` | float | ms² | Frame time variance |
| `rigsignal.fps.stutter_count` | integer | — | Frames > 2× tick mean |
| `rigsignal.fps.stutter_detected` | boolean | — | `stutter_count > 0` |
| `rigsignal.performance.bottleneck` | keyword | — | `gpu` \| `cpu` \| `balanced` |

---

## `metrics-rigsignal.storage-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.storage.read_bytes_per_sec` | long | B/s | System read throughput |
| `rigsignal.storage.write_bytes_per_sec` | long | B/s | System write throughput |

---

## `metrics-rigsignal.network-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.network.bytes_sent_per_sec` | long | B/s | Upload throughput |
| `rigsignal.network.bytes_recv_per_sec` | long | B/s | Download throughput |

---

## `metrics-rigsignal.audio-default`

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.audio.backend` | keyword | `pipewire`, `pulseaudio`, `wasapi` |
| `rigsignal.audio.xruns` | long | Audio buffer underruns (Linux) |

---

## `metrics-rigsignal.power-default`

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.power.ac_connected` | boolean | AC power connected |
| `rigsignal.power.battery_pct` | float | Battery percentage (portable devices) |
| `rigsignal.power.battery_rate_w` | float | Discharge rate in watts (Linux) |

---

## `metrics-rigsignal.ebpf-default`

eBPF kernel-level metrics from the companion `rigsignal-ebpf` daemon.
Correlated to session via `rigsignal.session.id`.

Key field groups: `rigsignal.ebpf.sched.*` (scheduler latency), `rigsignal.ebpf.gpu.*`
(fence/submit/sched latency), `rigsignal.ebpf.futex.*`, `rigsignal.ebpf.bio.*` (block I/O),
`rigsignal.ebpf.mem.*` (memory pressure), `rigsignal.ebpf.vfs.*`, `rigsignal.ebpf.frame.*` (stutter).

See `data_stream/ebpf/fields/fields.yml` for the full field list.

---

## `metrics-rigsignal.ebpf_thread-default`

Per-thread scheduler metrics from the companion `rigsignal-ebpf` daemon.
Each document represents one top-ranked game thread in a 1-second scheduler window.

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.ebpf_thread.comm` | keyword | Thread name (`comm`) |
| `rigsignal.ebpf_thread.tid` | long | Thread ID |
| `rigsignal.ebpf_thread.rank` | integer | Rank by switch count within the window |
| `rigsignal.ebpf_thread.runqueue_min_us` | double | Minimum runqueue latency for the thread |
| `rigsignal.ebpf_thread.runqueue_max_us` | double | Maximum runqueue latency for the thread |
| `rigsignal.ebpf_thread.runqueue_avg_us` | double | Mean runqueue latency for the thread |
| `rigsignal.ebpf_thread.switch_count` | long | Context switches for the thread |
| `rigsignal.ebpf_thread.migration_count` | long | CPU migrations for the thread |

---

## `logs-rigsignal.events-default`

Discrete events during a game session (shader compilation, save operations, etc.).

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.event.kind` | keyword | Event type: `shader_compile`, `save`, `crash`, etc. |
| `rigsignal.event.severity` | keyword | `info` \| `warning` \| `error` |
| `message` | text | Human-readable event description |
