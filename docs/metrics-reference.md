# Metrics Reference

RigSignal ships data to 11 metrics data streams plus one logs data stream. This
repository does not currently ship a `data_stream/*/fields/fields.yml` — that
schema is produced separately for the Elastic integration package submission
(see `docs/README.md`). The authoritative field definitions are the collector
source: `src/collectors/linux/*.rs`, `src/collectors/windows/*.rs`,
`src/main.rs` (session/summary), and `ebpf/rigsignal-ebpf/src/es_model.rs`
(eBPF streams). This document summarises the key fields per stream, kept in
sync with that source.

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
| `rigsignal.game.name` | keyword | Game title. Present only once a game target has been detected (absent on the initial session-start document). |
| `rigsignal.game.source` | keyword | `steam` \| `epic_games` \| `gog_galaxy` \| `lutris` \| `heroic` \| `bottles` \| `user_specified` \| `auto_detected` |
| `rigsignal.game.launcher` | keyword | Human-readable launcher (e.g. `Steam`, `Heroic — Epic`). Present only when detected. |
| `rigsignal.game.steam_app_id` | long | Steam App ID (present only when `source == steam`) |
| `rigsignal.game.graphics_api` | keyword | `vulkan` \| `dx12_via_vkd3d` \| `dx11_via_dxvk` \| `opengl` \| etc. Present only when detected. |

---

## `metrics-rigsignal.session-default`

One document on game start, updated on game end. The primary correlation anchor.

### Session identity

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.session.label` | keyword | Auto-generated label: `<slug>-YYYYMMDD-N`. Present only when a label has been assigned. |
| `rigsignal.session.label_source` | keyword | `auto` or `manual`. Always present. |
| `rigsignal.session.sequence_number` | long | Per-game-per-day counter (auto labels only). Present only when assigned. |
| `rigsignal.session.opt_in_public` | boolean | Whether anonymous data is shared publicly. Always present; currently hardcoded to `false` — opt-in public sharing is not yet implemented. |

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
| `rigsignal.hardware.ram.total_mb` | long | Total system RAM. This is the one-time hardware-snapshot copy; the memory data stream also emits a per-tick `rigsignal.memory.total_mb` (see below) — the two describe the same physical RAM figure but are separate fields on separate streams. |

### Game context

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.compatibility.proton_version` | keyword | Proton version (e.g. `GE-Proton9-20`). Present only when detected (Linux/Proton titles). |
| `rigsignal.compatibility.dxvk_version` | keyword | DXVK version. Present only when detected. |

Note: a `rigsignal.compatibility.vkd3d_proton_version` field does not currently
exist in the agent — VKD3D-Proton version detection is not implemented. Do not
rely on this field; it was previously documented in error.

### Settings capture (Tier 1 — manual only)

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.settings.preset` | keyword | `low` \| `medium` \| `high` \| `ultra` \| `custom` \| `unknown` |
| `rigsignal.settings.upscaler.tech` | keyword | `dlss` \| `fsr` \| `xess` \| etc. (free text tech name from `--upscaler tech[:preset]`) |
| `rigsignal.settings.upscaler.preset` | keyword | `quality` \| `balanced` \| `performance` \| `ultra_performance` \| etc. (free text) |
| `rigsignal.settings.frame_gen.tech` | keyword | `dlss3` \| `fsr3` \| `afmf` \| `lossless-scaling` \| `none` |
| `rigsignal.settings.features_active` | keyword[] | Active features, comma-separated on the CLI: `ray_tracing`, `path_tracing`, `direct_storage`, etc. |
| `rigsignal.settings.render.resolution_output` | keyword | Output render resolution, e.g. `3440x1440` |
| `rigsignal.settings.render.vsync` | keyword | `off` \| `on` \| `adaptive` \| `fast` |
| `rigsignal.settings.source` | keyword | Always `manual` in the current build — Tier 2 (`dll_scan`) and Tier 3 (`profile`) automatic capture are not yet implemented, though those enum values are reserved. |
| `rigsignal.settings.confidence` | keyword | Always `high` in the current build, for the same reason as `source` above. |
| `rigsignal.settings.notes` | keyword | Free-text user notes |

The entire `rigsignal.settings.*` overlay is present only when at least one
Tier 1 CLI flag (`--preset`, `--upscaler`, `--frame-gen`, `--features`,
`--resolution`, `--vsync`, or `--notes`) was passed at agent start; otherwise
the key is absent from session documents entirely.

Migration note: documents with a scalar `rigsignal.settings.frame_gen` predate
RigSignal 0.2.3. From 0.2.3 onward, the field is an object with the technology at
`rigsignal.settings.frame_gen.tech`; reindex affected historical documents into a
new index or roll over to a new backing index before mixing both shapes in one data
stream.

### Session summary (updated on game exit)

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.summary.ended` | boolean | `true` on session-end document. Always present on the summary doc. |
| `rigsignal.summary.duration_s` | long | Total session duration in seconds. Always present. (Not `duration_secs` — corrected from a prior version of this doc.) |
| `rigsignal.summary.fps_coverage_s` | long | Number of instrumented seconds (FPS samples) over the session; the coverage denominator for `total_frames`. Always present, including `0` when no FPS samples were captured. |
| `rigsignal.summary.stutter_count` | long | Total stutter-frame count summed across the session's frame-stream ticks. Always present. |
| `rigsignal.summary.avg_fps` | float | Average FPS over session. Present only when `fps_coverage_s > 0`. (Not `fps_avg` — corrected from a prior version of this doc.) |
| `rigsignal.summary.low_1pct_fps` | long | 1% low FPS over session. Present only when `fps_coverage_s > 0`. (Not `fps_1pct_low` — corrected from a prior version of this doc.) |
| `rigsignal.summary.total_frames` | long | Frames counted by summing `avg_1s` FPS samples over instrumented one-second ticks only; not session duration × average FPS. Present only when `fps_coverage_s > 0`. |
| `rigsignal.summary.p99_frametime_ms` | float | 99th-percentile frametime in ms over the session. Present only when frametime samples were captured. |
| `rigsignal.summary.peak_gpu_temp_c` | float | Peak GPU temperature observed during the session. Present only when GPU temperature data was available. |
| `rigsignal.summary.peak_cpu_temp_c` | float | Peak CPU temperature observed during the session. Present only when CPU temperature data was available. |
| `rigsignal.summary.peak_gpu_power_w` | float | Peak GPU power draw observed during the session. Present only when GPU power data was available. |
| `rigsignal.summary.bottleneck_dominant` | keyword | `gpu` \| `cpu` \| `balanced`, the most common per-tick bottleneck classification (GPU util > 90% vs CPU util > 90%). Present only when at least one tick had both GPU and CPU utilisation data. |

Note: a session-level `rigsignal.summary.fps_01pct_low` (0.1% low) field does
not currently exist — only the 1% low is aggregated into the summary. The
per-tick 0.1% low is available on the frame stream as `rigsignal.fps.low_01pct`
(see below). This row was previously documented in error and has been removed.

---

## `metrics-rigsignal.cpu-default`

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.cpu.total_utilisation_pct` | float | % | Total CPU utilisation across all cores. Always present. |
| `rigsignal.cpu.per_core` | float[] | % | Per-core utilisation. Always present (may be empty on the very first tick). |
| `rigsignal.cpu.boost_state` | boolean | — | Boost/turbo active. Always present. On Windows this is currently hardcoded `true` (no cross-vendor boost-state API without a vendor SDK); on Linux it reads the AMD boost sysfs toggle or Intel `no_turbo`. |
| `rigsignal.cpu.clock_mhz_avg` | long | MHz | Average clock across cores. Present only when readable (sysfs `scaling_cur_freq` on Linux, a PDH frequency counter on Windows). |
| `rigsignal.cpu.temperature_c` | float | °C | Package/die temperature. Present only when readable (Linux: k10temp/coretemp hwmon; Windows: WMI thermal zone, which is frequently ambient/ACPI rather than true die temperature). |
| `rigsignal.cpu.power_w` | float | W | Package power (Intel RAPL only, Linux). Absent on AMD and on Windows. |
| `rigsignal.cpu.governor` | keyword | — | cpufreq scaling governor, e.g. `performance`, `schedutil` (Linux only; not emitted on Windows). |
| `rigsignal.cpu.game_utilisation_pct` | float | % | Process-scoped CPU utilisation. **Not currently emitted on either platform** — documented as a known parity gap in the collector source (`TODO(C.1-game-util)`); requires ETW kernel callbacks or a Job Object on Windows, and is not yet wired on Linux either. Do not rely on this field being present.

---

## `metrics-rigsignal.gpu-default`

Linux GPU metrics currently support AMD only (`/sys/class/drm` cards are
filtered to PCI vendor `0x1002`); there is no NVIDIA or Intel GPU collector on
Linux yet. Windows GPU metrics use PDH + DXGI + WMI and are vendor-agnostic
but expose fewer fields than the Linux/AMD path.

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.gpu.utilisation_pct` | float | % | Core utilisation (`gpu_busy_percent` on Linux/AMD; PDH 3D engine counter on Windows). Present when readable. |
| `rigsignal.gpu.memory_used_mb` | long | MB | VRAM in use. Present when readable. |
| `rigsignal.gpu.memory_total_mb` | long | MB | Total VRAM. Present when readable. |
| `rigsignal.gpu.temperature_c` | float | °C | Edge/die temperature. Present when readable. |
| `rigsignal.gpu.temp_source` | keyword | — | Temperature data source: `hwmon` (Linux) \| `wmi_acpi` (Windows). Present alongside `temperature_c`. |
| `rigsignal.gpu.clock_mhz` | long | MHz | Current core clock, from the active `pp_dpm_sclk` P-state marker. Linux/AMD only; not emitted on Windows. |
| `rigsignal.gpu.hotspot_c` | float | °C | Hotspot/junction temperature. Linux/AMD only (hwmon `temp2_input`); not emitted on Windows. |
| `rigsignal.gpu.memory_temperature_c` | float | °C | VRAM temperature. Linux/AMD only (hwmon `temp3_input`); not emitted on Windows. |
| `rigsignal.gpu.power_w` | float | W | Board power draw. Linux/AMD only (hwmon `power1_average`); not emitted on Windows. |
| `rigsignal.gpu.fan_speed_rpm` | long | RPM | Fan speed in RPM. Linux/AMD only; present whenever the fan sensor is readable. |
| `rigsignal.gpu.fan_pct` | float | % | Fan speed as a percentage of max RPM. Linux/AMD only; present only when both `fan_speed_rpm` and a nonzero `fan1_max` are readable. |

Note: a `rigsignal.gpu.voltage` field does not currently exist — no collector
on either platform reads GPU core voltage. This row was previously documented
in error and has been removed.

---

## `metrics-rigsignal.memory-default`

The Linux and Windows memory collectors emit different field sets from
different OS memory APIs; they are not a 1:1 match. Both always include
`total_mb`.

### Fields common to both platforms

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.memory.total_mb` | long | MB | Total system RAM. Always present. This is the per-tick figure; the session stream also carries a one-time `rigsignal.hardware.ram.total_mb` snapshot (see above) describing the same physical RAM. |
| `rigsignal.memory.game_rss_mb` | long | MB | Game process resident set size (Linux: `/proc/<pid>/status` VmRSS; Windows: `GetProcessMemoryInfo` working-set size). Present only while a game is being monitored. |

### Linux-only fields (`/proc/meminfo`, `/proc/<pid>/status`, `/proc/<pid>/stat`)

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.memory.system_used_mb` | long | MB | `MemTotal - MemAvailable`. Always present. |
| `rigsignal.memory.page_cache_mb` | long | MB | `Cached` from `/proc/meminfo`. Always present. |
| `rigsignal.memory.shared_mb` | long | MB | `Shmem` from `/proc/meminfo`. Always present. |
| `rigsignal.memory.swap_used_mb` | long | MB | `SwapTotal - SwapFree`. Always present. |
| `rigsignal.memory.virtual_mb` | long | MB | Game process virtual memory size (`VmSize`). Present only while a game is being monitored. |
| `rigsignal.memory.page_faults_major` | long | — | Field 9 of `/proc/<pid>/stat` for the game process. Present only while a game is being monitored. |
| `rigsignal.memory.page_faults_minor` | long | — | Field 11 of `/proc/<pid>/stat` for the game process. Present only while a game is being monitored. |

### Windows-only fields (`GlobalMemoryStatusEx`)

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.memory.used_mb` | long | MB | `total_mb - available_mb`. Always present. |
| `rigsignal.memory.available_mb` | long | MB | Available physical RAM (`ullAvailPhys`). Always present. |
| `rigsignal.memory.used_pct` | float | % | `used_mb / total_mb * 100`, rounded to 1 decimal. Always present. |

Note: `used_mb`, `available_mb`, and `used_pct` are Windows-only — the Linux
collector does not emit them (it emits `system_used_mb` etc. instead, above).
This doc previously implied a single unified field set; it was wrong.

---

## `metrics-rigsignal.frame-default`

Fields are under `rigsignal.fps.*` (not `rigsignal.frame.*`) to match the Linux MangoHud collector naming. On Linux, one of two collectors is active: the Gamescope stats-pipe collector (SteamOS, or any Gamescope session — always overrides MangoHud CSV logging) or the MangoHud CSV collector (used when no Gamescope stats pipe is present). Windows uses a PresentMon-backed collector. All three emit the same `rigsignal.fps.*` field names, but with the caveats noted below.

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.fps.current` | integer | fps | FPS of the most recent frame sample in the tick window. Always present. |
| `rigsignal.fps.avg_1s` | float | fps | Mean FPS over the tick interval. Always present. |
| `rigsignal.fps.low_1pct` | integer | fps | 1% low FPS (sorted-ascending percentile over the tick window). Always present. |
| `rigsignal.fps.low_01pct` | integer | fps | 0.1% low FPS. Always present. |
| `rigsignal.fps.frametime_ms` | float | ms | Mean frame time. Present only when frametime data is available for the tick. **Gamescope path only:** this value is a sample-derived approximation computed as `1000 / fps` for each sample, not a per-frame measurement — it is coarser than the MangoHud and PresentMon paths, which read frametimes directly. |
| `rigsignal.fps.frametime_variance` | float | ms² | Frame time variance. Present only on the MangoHud (Linux) and PresentMon (Windows) paths — **not emitted on the Gamescope stats-pipe path**, which does not compute variance. |
| `rigsignal.fps.stutter_count` | integer | — | Frames this tick with frametime > 2× the tick's mean frametime. Always present (`0` when no stutters or no frametime data). On the Gamescope path this is derived from the same sample-based approximate frametimes as `frametime_ms` above. |

Note: a `rigsignal.fps.stutter_detected` boolean field and a per-tick
`rigsignal.performance.bottleneck` field do not currently exist in any
collector — only `rigsignal.summary.bottleneck_dominant` (session-summary,
end of session) exists for bottleneck classification. Both rows were
previously documented in error and have been removed; use `stutter_count > 0`
and `rigsignal.summary.bottleneck_dominant` respectively.

---

## `metrics-rigsignal.storage-default`

The Linux and Windows storage collectors emit substantially different field
sets. Windows currently emits only the two aggregate throughput fields;
Linux emits a much richer set (I/O latency, queue depth, merge rates) derived
from `/proc/diskstats`.

### Linux (`/proc/diskstats` delta, per selected device)

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.storage.read_mbps` | float | MB/s | Read throughput, 2 dp. Always present once a second delta sample exists. |
| `rigsignal.storage.write_mbps` | float | MB/s | Write throughput, 2 dp. Always present once a second delta sample exists. |
| `rigsignal.storage.read_iops` | long | IOPS | Read I/Os per second (truncated). Always present. |
| `rigsignal.storage.write_iops` | long | IOPS | Write I/Os per second (truncated). Always present. |
| `rigsignal.storage.io_latency_read_us.avg` | long | µs | Average read latency this tick. Always present (`0` when no reads occurred). |
| `rigsignal.storage.io_latency_write_us.avg` | long | µs | Average write latency this tick. Always present (`0` when no writes occurred). |
| `rigsignal.storage.queue_depth_current` | long | — | I/Os currently in progress (instantaneous). Always present. |
| `rigsignal.storage.io_wait_pct` | float | % | Percentage of the tick window the drive was busy, 1 dp, capped at 100. Always present. |
| `rigsignal.storage.merged_reads` | long | ops/s | Merged read operations per second (truncated). Always present. |
| `rigsignal.storage.merged_writes` | long | ops/s | Merged write operations per second (truncated). Always present. |

### Windows (PDH `\PhysicalDisk(_Total)`, aggregate across all physical disks)

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.storage.read_bytes_per_sec` | long | B/s | Aggregate read throughput across all physical disks. Always present once PDH initialises. |
| `rigsignal.storage.write_bytes_per_sec` | long | B/s | Aggregate write throughput across all physical disks. Always present once PDH initialises. |

Game-scoped (per-process) storage I/O is not implemented on either platform;
it would require ETW kernel-level IO tracing on Windows or an equivalent
eBPF/bio approach on Linux (the latter exists separately in the eBPF `bio`
probe — see the eBPF section below).

---

## `metrics-rigsignal.network-default`

As with storage, Linux and Windows emit different field sets. Both are
aggregate across the primary network interface, not per-process.

### Linux (`/proc/net/dev` + `/proc/net/snmp` delta, primary interface only)

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.network.rx_mbps` | float | MB/s | Receive throughput, 3 dp. Always present once a second delta sample exists. |
| `rigsignal.network.tx_mbps` | float | MB/s | Transmit throughput, 3 dp. Always present. |
| `rigsignal.network.rx_packets_per_sec` | float | pkt/s | Receive packet rate, 1 dp. Always present. |
| `rigsignal.network.tx_packets_per_sec` | float | pkt/s | Transmit packet rate, 1 dp. Always present. |
| `rigsignal.network.tcp_retransmits_per_sec` | float | retrans/s | TCP retransmit rate, 2 dp, system-wide (not scoped to the game connection). Always present. |
| `rigsignal.network.bandwidth_utilisation_mbps` | float | MB/s | `rx_mbps + tx_mbps`, 3 dp. Always present. |
| `rigsignal.network.connection_type` | keyword | — | `wifi` or `ethernet`, inferred from the interface name prefix. Always present. |
| `rigsignal.network.interface` | keyword | — | Selected interface name, e.g. `enp14s0`. The interface with the highest cumulative received bytes is chosen as primary. Always present. |

### Windows (PDH `\Network Interface(*)`, summed across non-tunnel/non-loopback adapters)

| Field | Type | Unit | Description |
|-------|------|------|-------------|
| `rigsignal.network.bytes_sent_per_sec` | long | B/s | Aggregate upload throughput across all real network interfaces. Always present once PDH initialises. |
| `rigsignal.network.bytes_recv_per_sec` | long | B/s | Aggregate download throughput across all real network interfaces. Always present once PDH initialises. |

---

## `metrics-rigsignal.audio-default`

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.audio.backend` | keyword | `pipewire` \| `pulseaudio` \| `alsa` \| `unknown` (Linux) \| `wasapi` (Windows). Always present. |
| `rigsignal.audio.xruns` | long | Delta audio buffer underruns since the last tick. PipeWire only, and only from the second `pw-top` sample onward (the first tick establishes the baseline). Not emitted on PulseAudio or on Windows (WASAPI xrun detection is scaffolded but not yet implemented — `TODO(C.7-xruns)`). |
| `rigsignal.audio.latency_ms` | float | Quantum/rate latency in ms, 2 dp. PipeWire only, present when parseable from `pw-top`. |
| `rigsignal.audio.quantum` | long | PipeWire scheduling quantum (frames). PipeWire only, present when parseable. |
| `rigsignal.audio.sample_rate_hz` | long | Server/default-sink sample rate. Present on both PipeWire (default-sink info) and PulseAudio (server info), when parseable; absent on Windows. |
| `rigsignal.audio.sink_name` | keyword | Default PipeWire sink name. PipeWire only, present when parseable. Added in 0.2.3 (audio enrichment). |
| `rigsignal.audio.card_profile` | keyword | Default sink's active device profile. PipeWire only, present when parseable. Added in 0.2.3. |
| `rigsignal.audio.channels` | long | Default sink channel count. PipeWire only, present when parseable. Added in 0.2.3. |
| `rigsignal.audio.sample_format` | keyword | Default sink sample format (e.g. `s16le`, `f32le`). PipeWire only, present when parseable. Added in 0.2.3. |
| `rigsignal.audio.driver_latency_ms` | float | Default sink's actual driver latency, 2 dp. PipeWire only, present when parseable. Added in 0.2.3. |

Note: parsing of the PipeWire "Sample Specification" line is atomic — if the
channel count segment fails to parse, `sample_format` and `sample_rate_hz`
are both omitted for that tick even if individually parseable, since they
come from the same line.

---

## `metrics-rigsignal.power-default`

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.power.ac_connected` | boolean | AC power connected. Present when the platform reports a definite state (absent on desktops with no battery on Windows if the state is unknown; Linux checks `/sys/class/power_supply/AC*` or `ADP*`). |
| `rigsignal.power.battery_pct` | float | Battery charge percentage. Absent on desktops with no battery on either platform. |
| `rigsignal.power.battery_rate_w` | float | Battery discharge rate in W, 2 dp. Linux only (reads `power_supply` current/voltage); not emitted on Windows (would require WMI `Win32_Battery` polling or `DeviceIoControl`, not yet implemented). |
| `rigsignal.power.tdp_current_w` | float | AMD GPU power cap in W, 1 dp (e.g. `330.0` on an RX 9070 XT). Linux/AMD only; not emitted on Windows. |
| `rigsignal.power.profile` | keyword | ACPI `platform_profile` string, e.g. `balanced`. Linux only; not emitted on Windows. |

The collector returns no document at all when no power sources are available
on the host (e.g. a VM with no battery and no AMD GPU hwmon) — this should not
occur on target gaming hardware.

---

## `metrics-rigsignal.ebpf-default`

eBPF kernel-level metrics from the companion `rigsignal-ebpf` daemon.
Correlated to session via `rigsignal.session.id`. Only one probe-specific
payload group is populated per document (selected by the `rigsignal.ebpf.probe`
discriminant); the rest are absent.

Key field groups: `rigsignal.ebpf.runqueue.*` (scheduler runqueue latency),
`rigsignal.ebpf.migration.*` (CPU migrations), `rigsignal.ebpf.gpu_sched.*`,
`rigsignal.ebpf.gpu_fence.*`, `rigsignal.ebpf.gpu_submit.*` (GPU
scheduling/fence/submit latency — three separate groups, not one combined
`gpu.*` group), `rigsignal.ebpf.futex.*`, `rigsignal.ebpf.irq.*`,
`rigsignal.ebpf.bio.*` (block I/O), `rigsignal.ebpf.mem.*` (memory pressure),
`rigsignal.ebpf.vfs.*`, and `rigsignal.ebpf.stutter.*` (cross-probe stutter
correlation — emitted when ≥2 subsystems spike in the same 1-second window).

Caveat: `rigsignal.ebpf.gpu_sched.*` is not currently emitted on kernels
without the `drm_sched_job_queue`/`drm_sched_job_run` tracepoints, including
SteamOS 6.16 — the probe attaches to tracepoint names that changed upstream.
A legacy-tracepoint compatibility port is planned for 0.2.4; until then, do
not expect `gpu_sched` documents on affected kernels.

The field definitions live in `ebpf/rigsignal-ebpf/src/es_model.rs`
(`EbpfPayload` and its snapshot structs); there is no separate `fields.yml`
for this stream in the current repository.

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

Note: the per-TID `GAME_PIDS` scheduler filter map holds up to 1024 entries
(raised from 256 in 0.2.3); games that spawn more threads than that will
silently drop the excess TIDs from this stream (with a warning logged),
rather than crashing.

---

## `logs-rigsignal.events-default` (planned — not yet implemented)

Intended to carry discrete events during a game session (shader compilation,
save operations, crashes, etc.). **No collector in the current agent emits
this stream** — there is no `rigsignal.event.*` field construction anywhere
in `src/`. The schema below is the design target, not a live field list; do
not build dashboards or alerts against it yet.

| Field | Type | Description |
|-------|------|-------------|
| `rigsignal.event.kind` | keyword | Event type: `shader_compile`, `save`, `crash`, etc. |
| `rigsignal.event.severity` | keyword | `info` \| `warning` \| `error` |
| `message` | text | Human-readable event description |
