# GamePulse — Claude.ai Planning Session Context

This file is maintained by the claude.ai web interface (planning sessions) to
preserve context across conversations. It complements CLAUDE.md, which is for
Claude Code (implementation sessions).

**Update protocol:**
- claude.ai: update this file at the end of every planning session, then commit
- Claude Code: update CLAUDE.md current state section at the end of every session
- Neither should edit the other's file

Last updated: 2026-04-11 (Phase 4 distribution verified — zip upload to Fleet API works on local + Serverless)

---

## Project status snapshot

### What is built and verified ✅

- **Python collector**: live on CachyOS gaming PC, all 8 metric streams shipping.
  SIGTERM fix live (2026-04-10): raises `_ShutdownSignal` to interrupt sleep
  immediately and guarantee `finally` cleanup runs.
- **Elastic Agent integration scaffold**: `elastic-package check` + `test static`
  11/11 passing (confirmed 2026-04-10). Package builds to ~345KB via
  `bash scripts/build-package.sh`.
- **11 ingest pipelines**: deployed to Elastic Cloud Serverless, index templates
  wired, `default_pipeline` set.
- **Live session verified**: Cyberpunk 2077 end-to-end (Proton, MangoHud, all 8
  streams flowing, game detection working).
- **Kibana dashboards (Phase 3 complete)**: all 6 dashboards built and committed:
  - `dashboards/gamepulse-dashboard.ndjson` — baseline (UI-exported)
  - `dashboards/config-comparison-dashboard.json` — 16 panels (ID: 21b663d6)
  - `dashboards/session-deep-dive-dashboard.json` — 17 panels (ID: b68f1178)
  - `dashboards/storage-io-dashboard.json` — 16 panels (ID: f8a9d960)
  - `dashboards/system-health-dashboard.json` — 15 panels (ID: 1b2a1b70)
  - `dashboards/game-library-dashboard.json` — 8 panels (ID: e7d878d0)
- **Session summary document**: live-validated. Ships at session end with all
  summary fields. Live-validated 2026-04-08 (RX 9070 XT, 506s, avg 145.9 FPS).
- **Zero TODO/FIXME debt**: confirmed 2026-04-10 audit.
- **Phase 2 eBPF architecture design doc**: `docs/ebpf-architecture-design.md`
  (not `ebpf-architecture.md` — note corrected filename).

### eBPF daemon — per-probe status

| Probe | Tracepoints | Status |
|---|---|---|
| schedlatency | `sched_wakeup`, `sched_switch`, `sched_migrate_task` | ✅ CONFIRMED IN ES (231 docs, Starfield 2026-04-09) |
| bio | `block_rq_issue`, `block_rq_complete` | ✅ CONFIRMED IN ES (6,112 docs total with schedlatency+gpu_sched, date 2026-04-10) |
| gpu_sched | `drm_sched_job_queue`, `drm_sched_job_run` | ✅ CONFIRMED IN ES (6,112 docs total with schedlatency+bio, date 2026-04-10) |
| mem | `page_fault_user`, `mm_vmscan_direct_reclaim_begin` | ✅ CONFIRMED — silence correct by design (flush() returns None when working set resident; will fire under real memory pressure) |
| stutter_correlation | (userspace only) | ✅ CONFIRMED — silence correct by design (16ms threshold not crossed in healthy session; will fire under actual stutter events) |
| gpu_fence | `dma_fence_default_wait` kprobe | ✅ CONFIRMED IN ES (367 docs, blocked_count=0 healthy, 2026-04-10) |
| gpu_submit | `amdgpu_cs_ioctl` kprobe | ✅ CONFIRMED IN ES (367 docs, event_count=181/doc, 2026-04-10) |
| futex | `do_futex` kprobe/kretprobe | ✅ CONFIRMED IN ES (6 docs — GAME_PIDS filtered, sparse=correct, 2026-04-10) |
| irq | irq_handler_{entry,exit} + softirq_{entry,exit} tracepoints | ✅ CONFIRMED IN ES (367 docs, hard_irq+softirq both present, 2026-04-10) |
| vfs | `vfs_read`/`vfs_write` kprobe/kretprobe | ✅ CONFIRMED IN ES (362 docs, read+write both confirmed, 2026-04-10) |
| syscall | syscall tracepoints | 🔲 NOT STARTED |
| shader | Mesa uprobe | 🔲 NOT STARTED |
| proton | Wine/ntdll kprobes | 🔲 NOT STARTED |

### Rust agent — Phase 6 status (2026-04-11)

**COMPLETE — all 8 collectors + main loop + gameplay verified**

| Component | Status |
|---|---|
| CPU | ✅ Done 2026-04-10 |
| Memory | ✅ Done 2026-04-10 |
| Storage | ✅ Done 2026-04-10 |
| Network | ✅ Done 2026-04-10 |
| Power | ✅ Done 2026-04-10 |
| Audio | ✅ Done 2026-04-10 |
| MangoHud frame | ✅ Done 2026-04-10 |
| AMD GPU | ✅ Done 2026-04-10 — card1/hwmon3, validated on RX 9070 XT |
| Main loop (session.rs, host.rs, main.rs) | ✅ Done 2026-04-10 — ES-confirmed idle |
| Full gameplay session verified | ✅ Done 2026-04-11 — Starfield, 40 min, 286.9 avg fps |

**Gameplay verification results (Starfield, Proton, 2026-04-11):**
- All 8 streams: cpu 661, gpu 662, memory 662, storage 661, network 661, audio 662, power 662, frame 642 docs
- `gamepulse.game.name='Starfield'`, `graphics_api='dx_via_proton'` ✅
- Session summary: avg_fps=286.9, low_1pct=167, duration=2430s, bottleneck=gpu ✅

### elastic-package test suite (complete — 2026-04-11)

| Test type | Result |
|-----------|--------|
| `test static` | ✅ 11/11 PASS |
| `test pipeline` | ✅ 11/11 PASS (remote ES) |
| `test asset` | ✅ 12/12 PASS (local 8.13.0 stack, `bash scripts/test-asset.sh`) |
| `test policy` | ⏭ "No test results" — acceptable |
| `test system` | ⏭ "No test results" — acceptable skip for hardware integration |

### Git state (end of last Claude Code session — 2026-04-11)

Branch: `main`, clean, up to date with `origin/main` (after push)

Recent commits:
- `5682f97` test(system): elastic-package test system — No test results (acceptable skip)
- `0a3a2c2` feat(testing): full elastic-package test suite — static+pipeline+asset all PASS
- `f3ef6eb` feat(packaging): AUR PKGBUILD + systemd units — both services smoke-tested active

### Hardware confirmed in live session (2026-04-08/09)

- GPU: AMD Radeon RX 9070 XT (RADV GFX1201), driver 26.0.4, Mesa 26.0.4
- CPU: AMD Ryzen 7 9800X3D, 8c/16t, single CCX (ccx_cross_count always 0 — expected)
- RAM: 61,910 MB
- OS: CachyOS Linux, kernel 6.19.11-1-cachyos-deckify

---

## Phase 4 distribution status (2026-04-11)

- Registry: `elastic-package stack up` from repo root auto-serves `build/packages/*.zip` via HTTPS registry on port 8080 ✅
- Install method (Serverless): `POST /api/fleet/epm/packages` with zip body + ApiKey header ✅ (47 assets)
- Install method (local 8.13): same API, Basic auth ✅ (44 assets)
- Serverless Fleet does NOT support custom registry URLs — zip upload is the only method
- `docs/BETA-INSTALL.md` created ✅
- `elastic-package install --zip` hangs (TLS/HTTP2 bug in v0.122.0) — use direct API call or Kibana UI instead

## Priorities (in order)

**Phase 4 Closed Beta — active:**
1. **First colleague onboarding** — share `docs/BETA-INSTALL.md` + `gamepulse-0.1.0.zip`
2. **GitHub Release v0.1.0** — tag, attach zip + AUR binaries
3. **.deb/.rpm packaging** — AUR done; Debian/RPM deferred

**Next Claude Code session:**
1. Write `docs/README.md` (elastic/integrations format — screenshots, config ref, troubleshooting)
2. Write `CHANGELOG.md` (0.1.0 entry)
3. ECS compliance check
4. Fork `elastic/integrations`, prepare PR structure

---

## eBPF design decisions (summary for quick reference)

Key decisions made in this planning session — see `docs/ebpf-architecture.md`
for full rationale:

- **Separate binary**, not embedded in Python collector (capability isolation,
  language boundary, lifecycle independence). Merges into Phase 4 Rust agent.
- **Session correlation** via `$XDG_RUNTIME_DIR/gamepulse/session.json` (fallback
  `/tmp/gamepulse/session.json`). Python collector writes, eBPF daemon watches
  via inotify. No IPC, no shared memory.
- **GPU tracing in three layers**: DRM scheduler tracepoints (vendor-neutral
  stable uAPI, ships first) → `dma_fence_default_wait` kprobe (vendor-neutral) →
  `amdgpu_cs_ioctl` kprobe (AMD-specific). Layered approach gives useful data on
  any GPU vendor from day one.
- **Ring buffers** for event-driven probes, **HashMaps** for in-kernel aggregation
  of high-frequency probes (syscall, futex, irq, mem).
- **Log2 μs-scale histogram buckets** uniformly across all probes (16 buckets,
  1μs to 33ms+). Fine-grained at collection, coarsen at query time in Kibana.
- **Graceful degradation**: every probe is independent. Missing tracepoints or
  capabilities skip that probe, log the reason, and continue.
- **Stutter correlation events** emitted to `logs-gamepulse.events-default` when
  eBPF thresholds are crossed (sched p99 > 5ms, bio p99 > 10ms, fence > 16.6ms).
- **Capabilities**: `CAP_BPF` + `CAP_PERFMON` + `CAP_SYS_ADMIN` +
  `CAP_DAC_READ_SEARCH` (systemd service with `AmbientCapabilities`).
- **Build**: Cargo workspace, xtask for BPF compilation, BPF bytecode embedded
  in binary at compile time (no runtime `.o` loading).

### Open questions (to resolve during implementation)

1. ES `histogram` field type support on Serverless in TSDS mode — test early.
2. Mesa shader compiler uprobe target path discovery at runtime (Sprint 5).

---

## Planned dashboards

| Dashboard | Status | Location |
|-----------|--------|----------|
| Session Deep-Dive | ✅ built | `dashboards/session-deep-dive-dashboard.json` |
| Configuration Comparison | ✅ built | `dashboards/config-comparison-dashboard.json` |
| Storage & I/O Analysis | ✅ built | `dashboards/storage-io-dashboard.json` |
| System Health | ✅ built | `dashboards/system-health-dashboard.json` |
| Game Library | ✅ built | `dashboards/game-library-dashboard.json` |
| Scheduler Analysis | ✅ built | `dashboards/scheduler-analysis-dashboard.json` (ID: 89ca0908) |

---

## New dashboard suite — design spec (locked 2026-04-11)

### Architecture
Entity-centric navigation replacing the current session-centric model.
Primary dashboards: Home, Games, Sessions, Hardware, Environment,
Engine, Compare. Persistent top navigation bar on every dashboard.
Breadcrumb trail: Home / Game: Cyberpunk 2077 / Session: 2026-04-10.
Compare available as a context button from any dashboard, not just its
own page.

### Prerequisites (complete)
- xrandr monitor enrichment: gamepulse.hardware.monitors[] nested field
  (resolution, refresh rate, VRR, HDR). Live: DP-2, 3440x1440, 119.98Hz.
- gamepulse-game-timeline Transform: Python post-enrichment approach
  (runtime fields cannot do window aggregation across sibling docs).
  Deployed to Serverless, 1 session (Starfield). Transform trigger wired
  into Rust agent after session summary ship.

### Design decisions (all locked — do not change without user confirmation)
- Session boundaries on continuous line: small visual gap + annotation
  marker between sessions
- Home trend default window: last 30 days
- Regression leaderboard: both avg FPS and 1% low shown side by side
- Games dashboard view: selector — single game / two games / two
  hardware configs
- Annotation layers: all types visible by default (hardware changes,
  driver/Proton/kernel, game updates, session boundaries)
- GPU upgrade display: same continuous line, hardware change as
  annotation — not a separate view
- Home sparklines: all games played, scrollable list
- Rig health summary: short natural language sentence (e.g. "GPU-bound,
  thermals healthy, FPS trending up")
- Game tile playtime: "23 sessions · 47h · last played 3 days ago"
- Peripherals: monitor only (via xrandr), no keyboard/mouse/headset

### Dashboard build order (one Claude Code session each)
1. Home — ✅ prerequisites met, ready to build
2. Games — ✅ prerequisites met (Transform deployed)
3. Environment — ✅ prerequisites met (existing session stream data)
4. Hardware — ✅ prerequisites met (xrandr enrichment done)
5. Compare — ✅ prerequisites met (Transform deployed)
6. Engine — lowest priority, build last

### Home dashboard spec
Top strip: natural language rig health sentence derived from last
session (bottleneck_dominant, peak temps vs threshold, 30d FPS trend
direction). Below: all games scrollable, each tile showing game name,
"N sessions · Nh · last played X days ago", avg FPS sparkline (30d),
trend arrow. Recent sessions strip (last 5). Recent changes timeline
(driver/kernel/Proton changes in last 30d annotated against FPS).

### Games dashboard spec
Filter control: game selector at top. View selector: single game /
compare two games / compare two hardware configs. Main panel: continuous
line of avg_fps + low_1pct_fps (y-axis) vs cumulative_playtime_hours
(x-axis) from gamepulse-game-timeline index. Session boundaries shown
as small gaps + vertical annotation markers. Annotation layers (all on
by default, toggleable): hardware changes (🔴), driver/Proton/kernel
(🔵), game updates (🟡), stutter events (⚫). Below: session table
(date, duration, avg FPS, 1% low, p99 frametime, driver, Proton,
bottleneck). FPS distribution box plot across sessions.

### Environment dashboard spec
Change history timeline: every detected environment change with date,
what changed, avg FPS before vs after, 1% low before vs after. Snapshot
diff table: pick two dates, see all changed fields. Regression
leaderboard: ranked by impact, showing both avg FPS delta and 1% low
delta side by side. Groups changes by type: driver, kernel, Proton,
game update.

### Hardware dashboard spec
Top: hardware specs summary from most recent session. Sub-panels:
GPU (FPS per game, utilisation, thermals, power efficiency FPS/W,
before/after hardware upgrade comparison). CPU (utilisation per game,
scheduler latency trends from eBPF). Storage (I/O latency per game
from bio probe). Memory (page fault trends, working set per game).
Bottleneck evolution chart: bottleneck_dominant over time across all
sessions.

### Compare dashboard spec
Pre-populated from "Compare" context buttons on other dashboards.
Comparison types: session vs session / driver vs driver / Proton vs
Proton / hardware vs hardware / preset vs preset. Uses
gamepulse-game-timeline for hardware comparisons. Side-by-side panels:
avg FPS, 1% low, p99 frametime, peak temps, power draw.

---

## Verified field paths (live session 2026-04-08)

### Session (`data_stream.dataset: "gamepulse.session"`)
- `gamepulse.session.id`, `gamepulse.session.agent_version`
- `gamepulse.hardware.gpu.vendor`, `gamepulse.hardware.gpu.model`
- `gamepulse.hardware.gpu.driver_version`, `gamepulse.hardware.gpu.vulkan_driver`
- `gamepulse.hardware.gpu.mesa_version`, `gamepulse.hardware.gpu.vram_mb`
- `gamepulse.hardware.cpu.model`, `gamepulse.hardware.cpu.cores`
- `gamepulse.hardware.ram.total_mb`
- `gamepulse.summary.ended`, `gamepulse.summary.duration_s`
- `gamepulse.summary.avg_fps`, `gamepulse.summary.low_1pct_fps`
- `gamepulse.summary.p99_frametime_ms`, `gamepulse.summary.total_frames`
- `gamepulse.summary.peak_gpu_temp_c`, `gamepulse.summary.peak_cpu_temp_c`
- `gamepulse.summary.peak_gpu_power_w`, `gamepulse.summary.stutter_count`
- `gamepulse.summary.bottleneck_dominant`
- `host.name`, `host.os.name`, `host.os.kernel`

### Frame (`data_stream.dataset: "gamepulse.frame"`)
- `gamepulse.fps.avg_1s`, `gamepulse.fps.low_1pct`, `gamepulse.fps.low_01pct`
- `gamepulse.fps.frametime_ms`, `gamepulse.fps.stutter_count`
- `gamepulse.session.id`

### GPU (`data_stream.dataset: "gamepulse.gpu"`)
- `gamepulse.gpu.utilisation_pct`, `gamepulse.gpu.temperature_c`
- `gamepulse.gpu.hotspot_c`, `gamepulse.gpu.memory_temperature_c`
- `gamepulse.gpu.power_w`, `gamepulse.gpu.memory_used_mb`, `gamepulse.gpu.clock_mhz`

### CPU (`data_stream.dataset: "gamepulse.cpu"`)
- `gamepulse.cpu.total_utilisation_pct`, `gamepulse.cpu.temperature_c`
- `gamepulse.cpu.clock_mhz_avg`

### Memory (`data_stream.dataset: "gamepulse.memory"`)
- `gamepulse.memory.system_used_mb`, `gamepulse.memory.swap_used_mb`
- ⚠️ `gamepulse.memory.game_rss_mb` — unreliable under Proton (tracks launcher
  not game process)

### Storage (`data_stream.dataset: "gamepulse.storage"`)
- `gamepulse.storage.read_mbps`, `gamepulse.storage.write_mbps`
- `gamepulse.storage.queue_depth_current`

---

## How to resume a planning session

1. Paste or upload `docs/claude-chat-context.md` (this file) at the start
2. Paste the latest `CLAUDE.md` if anything major changed
3. Share any Claude Code session output relevant to current state
4. Ask: "what should we work on?" — claude.ai will reconcile and advise

## How to start Phase 2 implementation (Claude Code)

1. `git pull` on the gaming PC
2. Ensure `docs/ebpf-architecture.md` is committed and pushed
3. Point Claude Code at the design doc:
   "Read `docs/ebpf-architecture.md` and begin Sprint 1: scaffold the Aya project
   and implement the `schedlatency` probe end-to-end."
4. Claude Code should update `CLAUDE.md` current state when done

## How to update this file

At the end of each claude.ai planning session:
```bash
git add docs/claude-chat-context.md
git commit -m "docs: update claude-chat-context.md after planning session"
git push
```
