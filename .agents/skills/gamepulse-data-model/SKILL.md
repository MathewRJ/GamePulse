---
name: gamepulse-data-model
description: >
  GamePulse data model reference card: all 10 data streams with index patterns, data
  view IDs, canonical field paths, TSDS dimension fields, gamepulse-game-timeline fields,
  and session.label/id semantics. Use before writing ES|QL queries, building dashboards,
  or adding new fields. Lists known past bugs to avoid repeating them.
metadata:
  author: gamepulse-project
  version: 1.0.0
---

# GamePulse Data Model Reference

## Data Streams

All 10 data streams share the `gamepulse` integration package. Index pattern:
`metrics-gamepulse.<stream>-default`

| Stream | Index Pattern | Mode | Notes |
|--------|--------------|------|-------|
| `session` | `metrics-gamepulse.session-default` | TSDS | One doc per session start/game change/end |
| `cpu` | `metrics-gamepulse.cpu-default` | TSDS | 1-second ticks |
| `gpu` | `metrics-gamepulse.gpu-default` | TSDS | 1-second ticks (AMD sysfs) |
| `memory` | `metrics-gamepulse.memory-default` | TSDS | 1-second ticks |
| `storage` | `metrics-gamepulse.storage-default` | TSDS | 1-second ticks |
| `network` | `metrics-gamepulse.network-default` | TSDS | 1-second ticks |
| `audio` | `metrics-gamepulse.audio-default` | TSDS | 1-second ticks |
| `power` | `metrics-gamepulse.power-default` | TSDS | 1-second ticks |
| `frame` | `metrics-gamepulse.frame-default` | TSDS | 1-second ticks (MangoHud CSV) |
| `ebpf` | `metrics-gamepulse.ebpf-default` | regular metrics | NOT TSDS (has nested types) |

**Wildcard pattern for cross-stream queries:** `metrics-gamepulse.*`

## Data Views (Kibana)

| Data View ID | Title | Index Pattern | Time Field |
|---|---|---|---|
| *(not created)* | `metrics-gamepulse.*` | `metrics-gamepulse.*` | `@timestamp` |
| `gp-dv-timeline` | `gamepulse-game-timeline` | `gamepulse-game-timeline` | `session_start` |

> **Note:** Individual per-stream data views have not been formally named/created.
> Dashboards use either the wildcard view or `gp-dv-timeline` for transform data.

## TSDS Dimension Fields

These fields have `dimension: true` in fields.yml and are the TSDS routing key.
Do NOT add `dimension: true` to any other field. Do NOT use these as ES|QL metrics.

| Field | Streams |
|---|---|
| `host.name` | all 9 metric+session streams |
| `gamepulse.session.id` | all 9 metric+session streams |

## Canonical Field Paths (verified from live data)

### Session context — present in ALL streams

```
gamepulse.session.id        keyword, dimension — UUID for the agent run
gamepulse.session.label     keyword — auto: "<slug>-YYYYMMDD-HHMMSS" or manual override
gamepulse.session.agent_version  keyword
gamepulse.game.name         keyword — Steam game title (absent when no game running)
gamepulse.game.steam_app_id long
gamepulse.game.graphics_api keyword — dx_via_proton | dx12_via_vkd3d | dx11_via_dxvk | vulkan
host.name                   keyword, dimension — hostname
@timestamp                  date
```

### Frame stream (`data_stream.dataset: gamepulse.frame`)

```
gamepulse.fps.avg_1s        float  — average FPS over the last 1 second
gamepulse.fps.low_1pct      float  — 1% low FPS
gamepulse.fps.low_01pct     float  — 0.1% low FPS
gamepulse.fps.frametime_ms  float  — median frame time in ms
gamepulse.fps.stutter_count long   — frames >2× median frame time (always 0 when no data)
```

### GPU stream (`data_stream.dataset: gamepulse.gpu`)

```
gamepulse.gpu.utilisation_pct    float
gamepulse.gpu.temperature_c      float
gamepulse.gpu.hotspot_c          float
gamepulse.gpu.memory_temperature_c  float
gamepulse.gpu.power_w            float  — GPU card power (NOT TDP)
gamepulse.gpu.memory_used_mb     float
gamepulse.gpu.clock_mhz          float
gamepulse.gpu.vram_used_pct      float
```

### CPU stream (`data_stream.dataset: gamepulse.cpu`)

```
gamepulse.cpu.total_utilisation_pct  float
gamepulse.cpu.temperature_c          float  — primary die temp (Tctl on AMD)
gamepulse.cpu.clock_mhz_avg          float
gamepulse.cpu.governor               keyword
```

### Memory stream (`data_stream.dataset: gamepulse.memory`)

```
gamepulse.memory.system_used_mb  float
gamepulse.memory.swap_used_mb    float
gamepulse.memory.game_rss_mb     float  — ⚠ UNRELIABLE under Proton (tracks launcher PID)
```

### Storage stream (`data_stream.dataset: gamepulse.storage`)

```
gamepulse.storage.read_mbps          float
gamepulse.storage.write_mbps         float
gamepulse.storage.queue_depth_current  float
gamepulse.storage.read_iops          float
gamepulse.storage.write_iops         float
gamepulse.storage.read_latency_ms    float
gamepulse.storage.write_latency_ms   float
```

### Network stream (`data_stream.dataset: gamepulse.network`)

```
gamepulse.network.rx_mbps   float
gamepulse.network.tx_mbps   float
```

### Audio stream (`data_stream.dataset: gamepulse.audio`)

```
gamepulse.audio.backend     keyword  — pipewire | pulseaudio | alsa
gamepulse.audio.xruns       long
gamepulse.audio.latency_ms  float
gamepulse.audio.sample_rate long
```

### Power stream (`data_stream.dataset: gamepulse.power`)

```
gamepulse.power.tdp_current_w  float  — system TDP cap (hwmon AMD PPT)
gamepulse.power.battery_pct    float  — None on desktop
gamepulse.power.source         keyword — ac | battery
```

> **Critical distinction**: GPU power is `gamepulse.gpu.power_w` (in the gpu stream).
> `gamepulse.power.tdp_current_w` is the system-level TDP cap, not the GPU card power.

### Session stream (`data_stream.dataset: gamepulse.session`)

```
gamepulse.session.id             keyword, dimension
gamepulse.session.label          keyword  — auto-generated slug
gamepulse.game.name              keyword
gamepulse.game.steam_app_id      long
gamepulse.game.graphics_api      keyword
gamepulse.compatibility.proton_version  keyword
gamepulse.compatibility.dxvk_version   keyword
gamepulse.hardware.gpu.model     keyword
gamepulse.hardware.gpu.driver_version  keyword
gamepulse.hardware.gpu.vram_mb   long
gamepulse.hardware.cpu.model     keyword
gamepulse.hardware.monitors      nested  — array (session stream is NOT TSDS, nested OK)
gamepulse.summary.ended          boolean — true on session-end summary doc
gamepulse.summary.duration_s     long
gamepulse.summary.avg_fps        float
gamepulse.summary.low_1pct_fps   long
gamepulse.summary.p99_frametime_ms  float
gamepulse.summary.peak_gpu_temp_c   float
gamepulse.summary.peak_cpu_temp_c   float
gamepulse.summary.peak_gpu_power_w  float
gamepulse.summary.stutter_count  long
gamepulse.summary.total_frames   long
gamepulse.summary.bottleneck_dominant  keyword — gpu | cpu | balanced
host.os.name                     keyword
host.os.kernel                   keyword
```

### eBPF stream (`data_stream.dataset: gamepulse.ebpf`) — NOT TSDS

```
gamepulse.ebpf.probe           keyword — schedlatency|bio|gpu_sched|mem|futex|irq|vfs|gpu_fence|gpu_submit|stutter_correlation
gamepulse.ebpf.schedlatency.*  — runqueue latency histogram, avg_us, min_us, max_us, event_count
gamepulse.ebpf.bio.*           — block I/O read/write latency and counts
gamepulse.ebpf.gpu_sched.*     — GPU job queue/run rates
gamepulse.ebpf.irq.*           — hard IRQ and softirq latency/event_count
gamepulse.ebpf.vfs.*           — VFS read/write latency (game PIDs)
gamepulse.ebpf.gpu_fence.*     — DMA fence wait latency, blocked_count
gamepulse.ebpf.gpu_submit.*    — amdgpu_cs_ioctl event_count
gamepulse.ebpf.futex.*         — futex latency, contended_count
gamepulse.ebpf.mem.*           — page fault and reclaim events
thread_breakdown               nested — per-thread breakdown (top 8 by switch count)
```

> **eBPF docs do NOT have `gamepulse.game.name`** for system-wide probes (bio, gpu_sched,
> irq, gpu_fence, gpu_submit, mem, stutter_correlation). Only game-PID-filtered probes
> (futex, vfs, schedlatency) carry game context. This caused a "game name missing" confusion
> when eBPF docs dominated the wildcard Discover view.

## gamepulse-game-timeline Index (ES Transform Output)

**Index:** `gamepulse-game-timeline`
**Data view:** `gp-dv-timeline` (timeField: `session_start`)
**Source:** `metrics-gamepulse.session-default` (filter: ended=true, game.name exists, gte 2026-04-12)

This is a regular index (not a data stream). All fields are flat (no nesting).

| Field | Type | Notes |
|-------|------|-------|
| `game_name` | keyword | |
| `session_id` | keyword | |
| `session_start` | date | time field |
| `avg_fps` | float | |
| `low_1pct_fps` | float | |
| `p99_frametime_ms` | float | |
| `duration_s` | float | |
| `driver_version` | keyword | |
| `kernel_version` | keyword | |
| `gpu_model` | keyword | |
| `bottleneck_dominant` | keyword | |
| `peak_gpu_temp_c` | float | |
| `peak_cpu_temp_c` | float | |
| `peak_gpu_power_w` | float | |
| `stutter_count` | long | |
| `total_frames` | long | |
| `cumulative_playtime_hours` | float | Python post-enrichment |

**NOT present:** `proton_version` — never written to this index.
Using `proton_version` in a dashboard panel causes a render error.

## session.id vs session.label

| Field | Type | Semantics |
|-------|------|-----------|
| `gamepulse.session.id` | keyword, TSDS dimension | UUID generated at agent start. Stable for the entire agent run. Use for session filtering and grouping in all dashboards. |
| `gamepulse.session.label` | keyword, NOT a dimension | Human-readable slug. Auto-generated: `idle-YYYYMMDD-HHMMSS` at startup, updated to `<game-slug>-YYYYMMDD-HHMMSS` on game detection. Can be overridden with `--label` CLI or `[session] label` config. Use for display and filtering — not as a TSDS dimension or ES pivot group-by. |

## Kibana Filter Controls

For filter controls that cover all streams, use the wildcard data view and these fields:

```
Game:       gamepulse.game.name      (keyword, native — no .keyword suffix on post-2026-04-12 data)
Session ID: gamepulse.session.id     (keyword, dimension — no .keyword suffix)
Label:      gamepulse.session.label  (keyword — no .keyword suffix)
OS:         host.os.name             (keyword — use host.os.name, not host.os.type)
```

## Known Past Bugs (Do Not Repeat)

| Bug | Root cause | Fix |
|-----|-----------|-----|
| `gamepulse.memory.game_rss_mb` unreliable under Proton | Tracks launcher PID, not game process | Don't use as a game RAM metric; display with caveat |
| `gamepulse.power.tdp_current_w` showed GPU power spikes >TDP cap | AMD firmware PPT measured instantaneously (330W cap, 529W peak) | Expected; not a bug |
| `proton_version` missing from game-timeline dashboard panel | Field never written to gamepulse-game-timeline index | Remove panel column |
| `bottleneck_dominant` null in session summary docs | Ingest pipeline not populating on 2026-04-12 backing index | Under investigation |
| Network collector silently not running | `CollectionConfig.network` defaulted to `False` | Fixed; default is now `True` |
| Wrong GPU selected (iGPU instead of dGPU) | Sorted DRM cards, broke at first AMD match | Fixed: score by VRAM, pick max |
| eBPF docs dominating wildcard Discover view | 1091/1523 eBPF docs have no game.name by design | Filter by `data_stream.dataset` |
