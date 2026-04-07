# GamePulse — Claude Code Project Instructions

## What this project is

GamePulse is an open-source gaming performance telemetry platform.
It collects, ships, and visualises real-world gaming metrics to Elasticsearch.

The target audience is game developers, journalists, Proton/Wine/Mesa maintainers,
and package maintainers who need real-world performance data.

## Current state (as of 2026-04-07)

### What is built and working

- **Python collector** (Phase 1): All metric collectors implemented and running on CachyOS gaming PC — CPU, GPU (AMD), memory, storage, network, audio, frame (MangoHud), power. Outputs `gamepulse.*` namespaced docs.
- **Elastic Agent integration scaffold** (Phase 0.5): `elastic-package check` and `elastic-package test static` both pass (11/11). Package builds to `gamepulse-0.1.0.zip`. 11 data streams defined with TSDS manifests, field mappings, ingest pipelines, and sample events.
- **Ingest pipelines deployed**: 11 pipelines live on Elastic Cloud Serverless (`metrics-gamepulse.<dataset>-default`). All index templates wired with `default_pipeline`. Pipeline simulation verified. 6 stale legacy pipelines deleted.
- **Live gameplay test passed**: Full session verified end-to-end (Cyberpunk 2077, Proton, MangoHud, all 8 streams, game detection working).
- **Scope document**: `docs/GamePulse-Scope-v3_2.md`
- **Kibana dashboards** (Phase 3, complete):
  - `dashboards/gamepulse-dashboard.ndjson` — baseline dashboard (UI-exported)
  - `dashboards/config-comparison-dashboard.json` — Configuration Comparison, 16 panels (ID: 21b663d6-de42-46c6-aeaf-e6c48e46ecec)
  - `dashboards/session-deep-dive-dashboard.json` — Session Deep-Dive, 17 panels (ID: b68f1178-6923-4e92-819b-33eb595197a9)
  - `dashboards/storage-io-dashboard.json` — Storage & I/O Analysis, 16 panels (ID: f8a9d960-130e-43db-8554-6033f45e8a9c)
  - `dashboards/system-health-dashboard.json` — System Health, 15 panels (ID: 1b2a1b70-a315-4ed4-91c4-11aa0abe5e1d)
  - `dashboards/game-library-dashboard.json` — Game Library, 8 panels (ID: e7d878d0-e2d6-454b-9a95-d93a4aeb70a8)

### What is not yet started

- **Rust production agent** (Phase 4): `src/`, `Cargo.toml`, `gamepulse-ebpf/` do not exist. The Python collector is the only working implementation.
- **eBPF daemon** (Phase 2 per v3.2): Not started.
- **Scheduler Analysis dashboard**: Requires Phase 2 eBPF data stream.

### Package build

Use `bash scripts/build-package.sh` instead of `elastic-package build` directly.
This stashes `.agents/` and `collector/.venv/` to `/tmp` before building (restores
on exit). Produces a lean 345KB zip vs 18MB when built raw.

`elastic-package check` and `elastic-package test static` can still be run directly.

Background: `elastic-package-ignore` v0.122.0 only applies during lint, not the build
copy step. Long-term fix is moving the integration to a `package/` subdirectory (Phase 6).

### Pending work (in priority order)

1. Phase 2: eBPF daemon design (Rust/Aya).
2. Phase 4: Rust production agent replacing Python collector.

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

Hardware-validated details for CachyOS (AMD Ryzen + RX 7900 XTX):

- AMD GPU: discrete card is **card1** (not card0); hwmon at hwmon3; scoring heuristic selects it correctly
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
- `gamepulse-ebpf/` — BPF kernel programs via Aya (future)
