# GamePulse — Claude Code Project Instructions

## What this project is

GamePulse is an open-source gaming performance telemetry platform.
It collects, ships, and visualises real-world gaming metrics to Elasticsearch.

The target audience is game developers, journalists, Proton/Wine/Mesa maintainers,
and package maintainers who need real-world performance data.

## Current state (as of last session)

### What is built and working

- **Python collector** (Phase 1): All metric collectors implemented and running on CachyOS gaming PC — CPU, GPU (AMD), memory, storage, network, audio, frame (MangoHud), power. Outputs `gamepulse.*` namespaced docs.
- **Elastic Agent integration scaffold** (Phase 0.5): `elastic-package check` and `elastic-package test static` both pass (11/11). Package builds to `gamepulse-0.1.0.zip`. 11 data streams defined with TSDS manifests, field mappings, ingest pipelines, and sample events.
- **Ingest pipelines deployed**: 11 pipelines live on Elastic Cloud Serverless (`metrics-gamepulse.<dataset>-default`). All index templates wired with `default_pipeline`. Pipeline simulation verified. 6 stale legacy pipelines deleted.
- **Live gameplay test passed**: Full session verified end-to-end (Cyberpunk 2077, Proton, MangoHud, all 8 streams, game detection working).
- **Scope document**: `docs/GamePulse-Scope-v3_2.md`

### What is not yet started

- **Rust production agent** (Phase 4): `src/`, `Cargo.toml`, `gamepulse-ebpf/` do not exist. The Python collector is the only working implementation.
- **eBPF daemon** (Phase 2 per v3.2): Not started.
- **Kibana dashboards**: Not built.

### Pending work (in priority order)

1. Fix package bloat — `collector/.venv` in zip ≈ 12 MB, needs exclusion.
2. Collector never writes session-end summary doc — `summary.*` fields always empty. Fix needed in collector.
3. Pipeline/system tests — need local ES or Docker environment.
4. Phase 2: eBPF daemon design (Rust/Aya).
5. Kibana dashboards from scratch using current field names.
   > **Note**: Do not hand-author NDJSON. Correct workflow: build/edit in
   > Kibana UI → export → commit. See `docs/kibana-lens-ndjson-reference.md`
   > for the full structural reference and Serverless constraints.
6. Phase 4: Rust production agent replacing Python collector.

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
The working baseline dashboard is `kibana/gamepulse-dashboard.ndjson`
("GamePulse - Manual Creation v2"). It covers the surface metrics
available from Phase 1 data: FPS timeline, frame time distribution,
GPU utilisation/temp/VRAM, CPU utilisation/temp, memory, storage I/O,
plus four metric tiles (Median FPS, Max GPU Temp, Max CPU Temp, Unique
Sessions) and three filter controls (Game, Session ID, OS).

This is a baseline only. The full dashboard scope per
`docs/GamePulse-Scope-v3_2.md` Phase 3 is significantly larger.

### Planned dashboards (Phase 3)
These must be built in Kibana UI and exported — never generated
programmatically (see workflow rules below).

1. **Session Deep-Dive** — exists as baseline, needs expansion:
   - Add frame time percentile overlays (p95, p99)
   - Add stutter event annotations
   - Add environment badge bar (game, OS, kernel, GPU driver, Proton)
   - Add GPU fence wait overlay (Phase 2 data, placeholder for now)
   - Target data streams: frame, gpu, cpu, memory, storage, session

2. **Scheduler Analysis** (Phase 2 data required):
   - Runqueue latency distribution per thread
   - CPU migration frequency / CCX boundary crossings
   - Comparison: CFS vs SCHED_FIFO for same game
   - IRQ latency overlay
   - Target data stream: ebpf

3. **Storage & I/O Analysis**:
   - Per-drive-type performance (NVMe vs SD card vs SATA)
   - File access pattern / I/O stall correlation with frame time
   - Filesystem comparison (btrfs vs ext4)
   - Target data stream: storage

4. **Configuration Comparison**:
   - Filter by: game, GPU, driver, Proton, kernel, filesystem, scheduler policy
   - Side-by-side FPS distributions as histograms
   - Scheduler behaviour diff
   - Target data streams: frame, session, ebpf

5. **System Health**:
   - Thermal headroom, power draw, clock speed correlation
   - Target data streams: gpu, cpu, power

6. **Game Library**:
   - Game × metrics heatmap with trend sparklines
   - Target data streams: frame, session

### Dashboard workflow (mandatory — do not deviate)
- NEVER generate dashboard NDJSON programmatically. It is version-sensitive
  and will fail to import on Elastic Serverless.
- Always build panels in the Kibana UI → export via Stack Management →
  Saved Objects → Export → commit to `kibana/`
- Claude Code's role is: (a) planning panel structure and field paths
  before you open Kibana, and (b) reviewing exported NDJSON field paths
  for correctness after export.
- Dashboard files live in `kibana/` at the repo root (not `data_stream/`).
- When the integration matures to Phase 6, dashboards move into
  `kibana/dashboard/` inside the integration package structure per the
  elastic-package spec.

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

- `kibana/gamepulse-dashboard.ndjson` — working dashboard (MacBook-built, import via Kibana UI)
- `dashboards/gamepulse-session-performance.ndjson` — session performance dashboard (local build)
- `docs/kibana-lens-ndjson-reference.md` — structural reference for Lens NDJSON and Serverless constraints

### Rust agent (target, not yet created)

- `src/main.rs` — entry point and main loop (future)
- `src/collectors/` — hardware and system collectors (future)
- `src/ebpf/` — eBPF probe manager (future)
- `src/shipper/` — Elasticsearch bulk API shipper (future)
- `gamepulse-ebpf/` — BPF kernel programs via Aya (future)
