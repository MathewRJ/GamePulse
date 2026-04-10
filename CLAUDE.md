# GamePulse — Claude Code Project Instructions

## What this project is

GamePulse is an open-source gaming performance telemetry platform.
It collects, ships, and visualises real-world gaming metrics to Elasticsearch.

The target audience is game developers, journalists, Proton/Wine/Mesa maintainers,
and package maintainers who need real-world performance data.

## Current state — last reconciled 2026-04-10 (Sprint 3)

### What is built and verified ✅

- **Python collector** (Phase 1): All 8 metric collectors running on CachyOS gaming PC — CPU, GPU (AMD), memory, storage, network, audio, frame (MangoHud), power. Outputs `gamepulse.*` namespaced docs. SIGTERM now interrupts `time.sleep` immediately via `_ShutdownSignal` and always runs `finally` cleanup (fixed 2026-04-10, commit `8983d27`).
- **Elastic Agent integration scaffold** (Phase 0.5): `elastic-package check` PASS, `elastic-package test static` 11/11 PASS (confirmed 2026-04-10). Package builds to `gamepulse-0.1.0.zip` via `bash scripts/build-package.sh`.
- **Ingest pipelines deployed**: 11 pipelines live on Elastic Cloud Serverless. All index templates wired with `default_pipeline`. Pipeline simulation verified.
- **Live gameplay verified**: Full session end-to-end (Cyberpunk 2077, Proton, MangoHud, all 8 streams, game detection working).
- **Session summary doc**: `cli.py` `finally` block ships session-end doc. Fields: `ended`, `duration_s`, `avg_fps`, `low_1pct_fps`, `p99_frametime_ms`, `peak_gpu_temp_c`, `peak_cpu_temp_c`, `peak_gpu_power_w`, `total_frames`, `stutter_count`, `bottleneck_dominant`.
- **GPU driver version**: `gamepulse.hardware.gpu.driver_version` via `enricher/host.py` (AMD: vulkaninfo, NVIDIA: nvidia-smi).
- **Kibana dashboards** (Phase 3, 6 live dashboards):
  - `dashboards/gamepulse-dashboard.ndjson` — baseline (UI-exported)
  - `dashboards/config-comparison-dashboard.json` — 16 panels (ID: 21b663d6-de42-46c6-aeaf-e6c48e46ecec)
  - `dashboards/session-deep-dive-dashboard.json` — 17 panels (ID: b68f1178-6923-4e92-819b-33eb595197a9)
  - `dashboards/storage-io-dashboard.json` — 16 panels (ID: f8a9d960-130e-43db-8554-6033f45e8a9c)
  - `dashboards/system-health-dashboard.json` — 15 panels (ID: 1b2a1b70-a315-4ed4-91c4-11aa0abe5e1d)
  - `dashboards/game-library-dashboard.json` — 8 panels (ID: e7d878d0-e2d6-454b-9a95-d93a4aeb70a8)

### eBPF daemon (Phase 2)

**Sprint 1 — schedlatency probe** ✅ CONFIRMED IN ES
- Tracepoints: `sched_wakeup`, `sched_switch`, `sched_migrate_task`
- End-to-end test PASSED: Starfield, Proton, 231 docs in `metrics-gamepulse.ebpf-default` (2026-04-09)
- Fields: runqueue latency histogram (16-bucket log2), min/max/avg_us, event_count, migration total_count, ccx_cross_count (always 0 on 9800X3D — expected), per-thread breakdown (top 8 by switch count)

**Sprint 2 — bio + gpu_sched + mem probes + stutter correlation** ✅ CONFIRMED IN ES
- **bio** ✅ CONFIRMED IN ES (6,112 docs total with schedlatency+gpu_sched, date 2026-04-10) — `block_rq_issue` / `block_rq_complete`. System-wide (kworker submits page-cache I/O). Verified: 1–1,351 events/s; spikes on asset loads.
- **gpu_sched** ✅ CONFIRMED IN ES (6,112 docs total with schedlatency+bio, date 2026-04-10) — `drm_sched_job_queue` / `drm_sched_job_run`. System-wide (RADV uses dedicated submission threads). Verified: 1,500–10,925 jobs/s.
- **mem** ✅ CONFIRMED — silence correct by design (`flush()` returns `None` when working set resident; will fire under real memory pressure) — `page_fault_user` (GAME_PIDS filtered) + `mm_vmscan_direct_reclaim_begin` (system-wide).
- **stutter_correlation** ✅ CONFIRMED — silence correct by design (16ms threshold not crossed in healthy session; will fire under actual stutter events) — `correlate()` in `aggregator.rs`, emits `probe: "stutter_correlation"` when ≥2 probes spike in same 1s window.
- All Sprint 2 fields mapped in `data_stream/ebpf/fields/fields.yml` (bio, gpu_sched, mem, stutter groups). `elastic-package check` PASS.

**Sprint 3 — extended probes** ✅ IMPLEMENTED (2026-04-10) — ES confirmation pending (needs root to run daemon)
- **futex** ✅ — kprobe/kretprobe on `do_futex` (kernel symbol confirmed in /proc/kallsyms as `T do_futex`). GAME_PIDS filtered. FutexSnapshot: latency_histogram, min/max/avg_us, event_count, contended_count (>1ms).
- **irq** ✅ — tracepoints `irq/irq_handler_{entry,exit}` + `irq/softirq_{entry,exit}`. System-wide. IrqSnapshot: hard_irq + softirq sub-groups with latency_histogram, avg_us, event_count.
- **vfs** ✅ — kprobe/kretprobe on `vfs_read` + `vfs_write` (`T vfs_read`, `T vfs_write` confirmed in kallsyms). GAME_PIDS filtered. VfsSnapshot: read + write sub-groups with latency_histogram, avg_us, event_count.
- **gpu_fence** ✅ — kprobe/kretprobe on `dma_fence_default_wait` (`T dma_fence_default_wait` confirmed in kallsyms). System-wide. GpuFenceSnapshot: latency_histogram, min/max/avg_us, event_count, blocked_count (>1ms).
- **gpu_submit** ✅ — kprobe on `amdgpu_cs_ioctl` (confirmed in kallsyms as `t amdgpu_cs_ioctl [amdgpu]`; module-symbol kprobes work). System-wide count-only. GpuSubmitSnapshot: event_count.
- All 5 probes wired end-to-end: BPF kernel side → userspace aggregator → EbpfPayload → ES fields. `cargo check` PASS. `elastic-package check` PASS. `elastic-package test static` 11/11 PASS.

**Sprint 4–5** 🔲 NOT STARTED
- Scheduler Analysis dashboard, packaging (systemd, AUR), advanced probes (syscall, shader, proton).

### Key learnings

- **BPF verifier requires opt-level=2**: Debug Rust builds emit BPF-to-BPF calls to panic infrastructure → verifier rejects ("processed 0 insns"). `-C opt-level=2` set in `ebpf/.cargo/config.toml`. Never remove.
- **Async ring buffer drain race**: `AsyncFd<RingBuf>` + Tokio EPOLLET silently drops events. Drain synchronously in `collect()` on each tick instead.
- **GAME_PIDS capacity**: `max_entries=256` — BPF hash maps at 100% load fail inserts. Always leave headroom.
- **session.json path**: Always `/tmp/gamepulse/session.json`. `$XDG_RUNTIME_DIR` is stripped by sudo — daemon and collector would watch different paths.
- **RADV GPU scheduling**: `drm_sched_job_queue` must be system-wide. RADV uses dedicated submission threads not in the game PID tree.
- **ES histogram field type on Serverless TSDS (resolved 2026-04-10)**: The `type: histogram` field mapping is accepted by Elasticsearch Serverless in TSDS mode. LatencyHistogram docs (`{"values":[…],"counts":[…]}`) land without bulk errors. No fallback to scalar percentile fields required. This was the last open architectural risk for the eBPF data model.
- **Sprint 3 kernel symbol availability (confirmed 2026-04-10)**: `T do_futex`, `T vfs_read`, `T vfs_write`, `T dma_fence_default_wait` all present in /proc/kallsyms on kernel 6.19.11 CachyOS. `t amdgpu_cs_ioctl [amdgpu]` also present (lowercase = module-local, kprobes still work on module symbols). sys_enter_futex/sys_exit_futex tracepoints exist but format files are root-only at build time; using do_futex kprobe as implementation instead. irq tracepoint format files also root-only — layout inferred from kernel source (offset 8: s32 irq for irq_handler_{entry,exit}; offset 8: u32 vec for softirq_{entry,exit}) consistent with kernel 5.x–6.x standard.

### What is not yet started

- **Rust production agent** (Phase 6): `src/`, `Cargo.toml` do not exist. Python collector is the only working implementation. This gates closed beta (Phase 4) and the elastic/integrations PR.
- **Scheduler Analysis dashboard**: blocked — needs Sprint 2+ eBPF data confirmed live in Kibana.
- **Packaging**: no `.deb`, `.rpm`, AUR PKGBUILD, or systemd service file.
- **Full elastic-package test suite**: only `test static` passes. `test asset`, `test system`, `test policy` not yet configured.

### Package build

Use `bash scripts/build-package.sh` instead of `elastic-package build` directly.
This stashes `.agents/` and `collector/.venv/` to `/tmp` before building (restores
on exit). Produces a lean 345KB zip vs 18MB when built raw.

`elastic-package check` and `elastic-package test static` can still be run directly.

Background: `elastic-package-ignore` v0.122.0 only applies during lint, not the build
copy step. Long-term fix is moving the integration to a `package/` subdirectory (Phase 6).

### Pending work (in priority order)

1. **Sprint 3 ES confirmation**: Run daemon as root with Sprint 3 probes live — confirm futex/irq/vfs/gpu_fence/gpu_submit docs appear in Elasticsearch.
2. **Phase 6 Rust agent scaffold**: `src/Cargo.toml`, CLI, config, ES shipper — `cargo check` only, no collectors yet.
3. **Phase 6 Rust collectors** (one per session): CPU, memory, storage, network, power, audio, AMD GPU (needs gaming PC online), MangoHud frame.
4. **Scheduler Analysis dashboard**: build after Sprint 3 data confirmed in ES.
5. **Packaging**: systemd unit, AUR PKGBUILD, .deb/.rpm.

## Stack

- **Collector (current)**: Python 3.11+ prototype
- **Collector (target)**: Rust + Aya framework for eBPF (Phase 4, not started)
- **Storage / visualisation**: Elasticsearch Serverless (Elastic Enterprise), Kibana
- **Hardware target**: AMD GPU primary (Linux); NVIDIA via community; Steam Deck
- **Packaging target**: Debian, RPM, AUR (not yet built)
- **CI/CD target**: GitHub Actions (not yet configured)
- **Key Linux interfaces**: sysfs/hwmon, /proc filesystem, MangoHud log

## Kibana dashboards

### Current state
Dashboard files live in `dashboards/` (not `kibana/`). The `kibana/` directory
is reserved for the Phase 6 integration package format (`kibana/dashboard/`
with proper NDJSON saved objects). Until then, all dashboard JSON files live in
`dashboards/`.

### Planned dashboards (Phase 3)

| Dashboard | Status | Location |
|-----------|--------|----------|
| Session Deep-Dive | ✅ built | `dashboards/session-deep-dive-dashboard.json` (ID: b68f1178-6923-4e92-819b-33eb595197a9) |
| Configuration Comparison | ✅ built | `dashboards/config-comparison-dashboard.json` (ID: 21b663d6-de42-46c6-aeaf-e6c48e46ecec) |
| Baseline (UI export) | ✅ reference | `dashboards/gamepulse-dashboard.ndjson` |
| Storage & I/O Analysis | ✅ built | `dashboards/storage-io-dashboard.json` (ID: f8a9d960-130e-43db-8554-6033f45e8a9c) |
| System Health | ✅ built | `dashboards/system-health-dashboard.json` (ID: 1b2a1b70-a315-4ed4-91c4-11aa0abe5e1d) |
| Game Library | ✅ built | `dashboards/game-library-dashboard.json` (ID: e7d878d0-e2d6-454b-9a95-d93a4aeb70a8) |
| Scheduler Analysis | Phase 2 data required | needs eBPF stream |

**Session Deep-Dive** (`dashboards/session-deep-dive-dashboard.json`):
17 panels — 3 filter controls (Game/Session/OS), 6 metric tiles (Median FPS,
1% Low, 0.1% Low, Median frame time, Peak stutter/tick, Avg GPU temp), FPS
timeline (avg + 1%/0.1% lows), frame time with p95/p99 overlays, stutter
events area chart, GPU util/temp + power/VRAM, CPU util/temp, memory, and
session config table (Game, OS, Kernel, GPU, driver, Proton).

**Configuration Comparison** (`dashboards/config-comparison-dashboard.json`):
16 panels — 4 filter controls, 3 metrics, 9 charts, 1 session config table.

**Storage & I/O Analysis** (`dashboards/storage-io-dashboard.json`):
16 panels — 3 filter controls (Game/Session/OS), 6 metric tiles (read/write MB/s,
read/write IOPS, I/O wait %, drive temp), throughput timeline, IOPS area chart,
I/O wait + queue depth, read/write latency (avg/p95/p99), game process I/O, drive temp.

**System Health** (`dashboards/system-health-dashboard.json`):
15 panels — 3 filter controls, 6 metric tiles (GPU temp/hotspot, CPU temp, GPU
power/clock, CPU clock), GPU thermals timeline (die/hotspot/VRAM), CPU thermals
+ util, GPU power+clock, CPU clock+util, GPU VRAM+util, system TDP.
Note: `gamepulse.power.tdp_current_w` is the only power stream field; GPU power
comes from `gamepulse.gpu.power_w` in the gpu stream.

**Game Library** (`dashboards/game-library-dashboard.json`):
8 panels — 2 filter controls (Game/OS), avg FPS by game (bar), 1% low FPS by
game (bar), FPS over time broken out by game, GPU util and GPU power timelines
by game, and a performance summary data table (avg/1%/0.1% FPS, frame time,
max stutter, GPU util/power, session count per game). Default range: now-30d.

**Scheduler Analysis** (Phase 2 data required):
   - Runqueue latency distribution per thread
   - CPU migration frequency / CCX boundary crossings
   - Comparison: CFS vs SCHED_FIFO for same game
   - Target data stream: ebpf

### Dashboard workflow

Two approved methods — use the kibana-dashboards skill where possible:

**Method A — Kibana Dashboards API (preferred)**
Use the `kibana-dashboards` agent skill to create and update dashboards
programmatically. This API (Kibana 9.4+ Serverless) is LLM-friendly and
not version-sensitive. Workflow:
1. Validate fields with ES|QL first (`elasticsearch-esql` skill)
2. Generate dashboard JSON and POST via the skill
3. Retrieve the result and save definition to `kibana/<name>.json`
4. Commit and push

API schema notes (verified 2026-04-07 against Serverless 9.4.0):
- `options_list_control`: use `field_name` (snake_case), `data_view_id`
- `options_list_control` field_name MUST use `.keyword` sub-field for text fields
  (e.g. `gamepulse.game.name.keyword`, `gamepulse.session.id.keyword`, `host.os.name.keyword`)
  Using the bare text field silently produces a non-functional filter control
- OS filter control: use `host.os.name.keyword` (not `host.os.type` or `host.os.type.keyword`)
- `data_table` `last_value` metrics for text fields also need `.keyword` sub-field
  (e.g. `host.os.kernel.keyword`, `gamepulse.hardware.gpu.model.keyword`)
- `data_table` rows and x-axis `terms` fields need `.keyword` for text fields
- `xy` terms x-axis: `{operation:"terms", fields:[...]}` — no `size`
- `breakdown_by` terms: `{operation:"terms", fields:[...]}` — no `size`
- Datatable type is `data_table` (not `datatable`), rows terms: no `size`
- ES|QL dataset (`type:"esql"`) not supported in inline panel attributes;
  use `type:"dataView"` or `type:"index"` instead

**Method B — Manual Kibana UI export (fallback)**
Use when the API doesn't support a needed panel type, or for complex
multi-layer visualizations. Workflow:
Build in Kibana UI → export via Stack Management → Saved Objects →
commit to `kibana/` as `.ndjson`

**Never do**: hand-author NDJSON files — these are version-sensitive and
will fail to import on Serverless.

Dashboard files live in `kibana/` at the repo root (not `data_stream/`).
When the integration matures to Phase 6, dashboards move into
`kibana/dashboard/` inside the integration package structure.

### Elastic compliance rules (required for elastic/integrations submission)
- All visualizations must be defined by value (part of the dashboard),
  not saved to the Visualize library.
- Every panel must include a `data_stream.dataset` filter to avoid hitting
  all `metrics-*` indices. Example for frame data:
  `data_stream.dataset: "gamepulse.frame"`
- Visualization titles must not include the package name. Use "FPS Timeline"
  not "[GamePulse] FPS Timeline".
- Use Kibana Lens only — no TSVB, no Vega, no legacy aggregation-based panels.
- TSDS note: counter-type metric fields do not support `avg()` in Kibana.
  Use `max()` or `rate()` instead.
- Build against stable Kibana (Serverless current), never SNAPSHOT.

### Field paths reference (verified from live data)
These are confirmed working from the Cyberpunk 2077 session:

Frame data (`data_stream.dataset: gamepulse.frame`):
- `gamepulse.fps.avg_1s`, `gamepulse.fps.low_1pct`, `gamepulse.fps.low_01pct`
- `gamepulse.fps.frametime_ms`, `gamepulse.fps.stutter_count`
- `gamepulse.session.id.keyword` (use for split-by and session filter control)

GPU data (`data_stream.dataset: gamepulse.gpu`):
- `gamepulse.gpu.utilisation_pct`, `gamepulse.gpu.temperature_c`
- `gamepulse.gpu.hotspot_c`, `gamepulse.gpu.memory_temperature_c`
- `gamepulse.gpu.power_w`, `gamepulse.gpu.memory_used_mb`, `gamepulse.gpu.clock_mhz`

CPU data (`data_stream.dataset: gamepulse.cpu`):
- `gamepulse.cpu.total_utilisation_pct`, `gamepulse.cpu.temperature_c`
- `gamepulse.cpu.clock_mhz_avg`

Memory data (`data_stream.dataset: gamepulse.memory`):
- `gamepulse.memory.system_used_mb`, `gamepulse.memory.swap_used_mb`
- `gamepulse.memory.game_rss_mb` (unreliable under Proton — tracks launcher, not game)

Storage data (`data_stream.dataset: gamepulse.storage`):
- `gamepulse.storage.read_mbps`, `gamepulse.storage.write_mbps`
- `gamepulse.storage.queue_depth_current`

Session data (`data_stream.dataset: gamepulse.session`):
- `gamepulse.game.name.keyword`, `gamepulse.game.steam_app_id`
- `gamepulse.game.graphics_api`, `gamepulse.session.id.keyword`
- `host.name`, `host.os.name.keyword`, `host.os.type.keyword`

Filter controls (use `metrics-gamepulse.*` wildcard data view):
- Game: `gamepulse.game.name.keyword`
- Session ID: `gamepulse.session.id.keyword`
- OS: `host.os.type.keyword`

### Elastic Agent Skills
The Elastic official Claude Code skills are installed in `.claude/skills/`.
These give Claude Code enhanced knowledge of ES|QL, Kibana, and
Elasticsearch. Install via:
```
npx skills add elastic/agent-skills -a claude-code
```
When planning dashboard panels, ask Claude Code to use ES|QL queries
for validation — ES|QL bypasses data view field list issues and
confirms fields exist before building Lens panels.

## Hardware notes (gaming PC)

Hardware-validated details for CachyOS (AMD Ryzen + RX 9070 XT):

- AMD GPU: **RX 9070 XT** (RADV GFX1201), Mesa 26.0.4, driver 26.0.4. Discrete card is **card1** (not card0); scoring heuristic selects it correctly
- CPU temps: k10temp at hwmon5; temp1=Tctl (primary), temp3=Tccd1
- RAPL power: permission-denied without root — collector returns None gracefully
- CPU driver: amd-pstate-epp; cpufreq paths at `/sys/bus/cpu/devices/cpu*/cpufreq/`
- Storage: 3× NVMe (nvme0n1/nvme1n1/nvme2n1); `/games` ext4 (Steam library); collector detects nvme1n1p6

## Remote access

- **Elasticsearch**: `$ES_URL` / `$ES_API_KEY` — Elastic Cloud Serverless
- **Gaming PC**: `ssh gamingpc` (CachyOS, AMD GPU, MangoHud installed)

## Protected files — never edit without explicit task assignment

These files are integration-critical. Errors in them are silent until package validation:

- `manifest.yml`
- `tools/deploy_pipelines.py`
- `tools/wire_pipelines.py`
- `docs/GamePulse-Scope-v3_2.md`
- Any file under `_dev/`
- Any file under `packaging/`
- Ingest pipeline YAML/JSON files (any path matching `*pipeline*`)
- Index template JSON files
- ILM policy JSON files

## Validation commands (the only approved test commands)

```
elastic-package check
elastic-package test static
elastic-package test system   # requires local ES or Docker
cargo check                   # only once Rust src/ exists
cargo clippy -- -D warnings   # only once Rust src/ exists
cargo test                    # only once Rust src/ exists
cargo build --release         # only once Rust src/ exists
```

Do not run any other commands that modify the repo, network, or filesystem
without explicit user approval.

## Session hygiene

- Always run `git pull` before starting any work in a session.
- Always run `git push` immediately after every commit.
- Never start implementation work if `git status` shows unpushed commits or if the branch is behind `origin/main`.
- If the branch has diverged, stop and flag it to the user before doing anything else.

## Cross-session continuity

Two files maintain context across sessions:
- `docs/claude-chat-context.md` — maintained by claude.ai (web planning sessions).
  Update and commit at the end of every planning session.
- `CLAUDE.md` (this file) — maintained by Claude Code (implementation sessions).
  Update the "Current state" section at the end of every Claude Code session.

Claude Code must never edit `docs/claude-chat-context.md`.
claude.ai must never directly edit `CLAUDE.md`.

## Workflow rules

1. One task at a time. No opportunistic refactors.
2. No dependency version changes unless the task explicitly requires it.
3. No changes to protected files without a planner-assigned task targeting them.
4. After any pipeline/manifest change: run `elastic-package check` before declaring done.
5. After any Rust code change (once src/ exists): run `cargo check` before declaring done.
6. Reviewer must approve before tester runs.
7. Progress auditor runs at every milestone boundary, not every task.

## Key file locations

### Python collector (current implementation)

- `collector/gamepulse/cli.py` — main loop, `_merge_docs()` deep-merge, bulk shipper
- `collector/gamepulse/session.py` — session lifecycle, `base_doc()` output
- `collector/gamepulse/enricher/host.py` — host OS enrichment
- `collector/gamepulse/collectors/` — per-subsystem collectors
- `tools/deploy_pipelines.py` — pipeline deployment tool
- `tools/wire_pipelines.py` — pipeline wiring tool

### Integration package

- `data_stream/` — 11 data streams (manifest, fields, pipeline, sample_event)
- `manifest.yml` — package root
- `docs/GamePulse-Scope-v3_2.md` — canonical scope document

### Kibana dashboards

- `dashboards/` — all dashboard files live here (not `kibana/`):
  - `dashboards/gamepulse-dashboard.ndjson` — baseline (MacBook-built, import via Kibana UI)
  - `dashboards/config-comparison-dashboard.json` — API-built, live ID: 21b663d6-de42-46c6-aeaf-e6c48e46ecec
  - `dashboards/session-deep-dive-dashboard.json` — API-built, live ID: b68f1178-6923-4e92-819b-33eb595197a9
- `docs/kibana-lens-ndjson-reference.md` — structural reference for Lens NDJSON and Serverless constraints

### Elastic Agent skills (Claude Code)
Skills are in `.agents/skills/` and `.claude/skills/` (symlinks). These are
excluded from git — recreate on a fresh clone with:
```
npx skills add elastic/agent-skills -a claude-code
```
Note: `.claude/skills/` directory symlinks and `.agents/` are in `.gitignore`
because `elastic-package build` (v0.122.0) cannot handle directory symlinks.

### Rust agent (target, not yet created)

- `src/main.rs` — entry point and main loop (future)
- `src/collectors/` — hardware and system collectors (future)
- `src/ebpf/` — eBPF probe manager (future)
- `src/shipper/` — Elasticsearch bulk API shipper (future)
- `ebpf/` — BPF kernel programs via Aya (Phase 2 Sprint 1 complete)
