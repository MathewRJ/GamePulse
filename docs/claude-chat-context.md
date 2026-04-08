# GamePulse — Claude.ai Planning Session Context

This file is maintained by the claude.ai web interface (planning sessions) to
preserve context across conversations. It complements CLAUDE.md, which is for
Claude Code (implementation sessions).

**Update protocol:**
- claude.ai: update this file at the end of every planning session, then commit
- Claude Code: update CLAUDE.md current state section at the end of every session
- Neither should edit the other's file

Last updated: April 2026

---

## Project status snapshot

### What is built and verified

- **Python collector**: live on CachyOS gaming PC, all 8 metric streams shipping
- **Elastic Agent integration scaffold**: `elastic-package check` + `test static`
  11/11 passing. Package builds to 346KB via `bash scripts/build-package.sh`.
- **11 ingest pipelines**: deployed to Elastic Cloud Serverless, index templates
  wired, `default_pipeline` set. 6 stale legacy pipelines deleted.
- **Live session verified**: Cyberpunk 2077 end-to-end (Proton, MangoHud, all 8
  streams flowing, game detection working)
- **Kibana dashboards (Phase 3 complete)**: all 6 dashboards built and committed:
  - `dashboards/gamepulse-dashboard.ndjson` — baseline (UI-exported)
  - `dashboards/config-comparison-dashboard.json` — 16 panels (ID: 21b663d6)
  - `dashboards/session-deep-dive-dashboard.json` — 17 panels (ID: b68f1178)
  - `dashboards/storage-io-dashboard.json` — 16 panels (ID: f8a9d960)
  - `dashboards/system-health-dashboard.json` — 15 panels (ID: 1b2a1b70)
  - `dashboards/game-library-dashboard.json` — 8 panels (ID: e7d878d0)
- **`driver_version` field**: confirmed present at
  `gamepulse.hardware.gpu.driver_version` (populated via `vulkaninfo` for AMD,
  `nvidia-smi` for NVIDIA). Was already implemented — the stale note about it
  being missing referred to the wrong field path.
- **Session summary document**: fully implemented and live-validated. Ships at
  session end with: `summary.ended`, `summary.duration_s`, `summary.avg_fps`,
  `summary.low_1pct_fps`, `summary.p99_frametime_ms`, `summary.total_frames`,
  `summary.peak_gpu_temp_c`, `summary.peak_cpu_temp_c`, `summary.peak_gpu_power_w`,
  `summary.stutter_count`, `summary.bottleneck_dominant`.
  Live-validated 2026-04-08: session doc confirmed in ES with all fields populated
  (RX 9070 XT, Ryzen 7 9800X3D, 506s session, avg 145.9 FPS).
- **Zero TODO/FIXME debt**: confirmed by progress-auditor 2026-04-08.
- **Phase 2 eBPF architecture design doc**: complete at
  `docs/ebpf-architecture.md`. Covers full daemon design — probe architecture,
  BPF map strategy, ring buffer sizing, PID filtering (including Proton process
  trees), GPU tracing across three layers (DRM scheduler uAPI → DMA fence →
  vendor-specific kprobes), userspace aggregation, ES data model with histogram
  fields, stutter correlation events, build system, systemd deployment, and
  phased implementation plan (5 sprints). Ready for Claude Code implementation.

### Git state (end of last Claude Code session)

Branch: `main`, clean, up to date with `origin/main`

Key recent commits:
- Session summary: added `p99_frametime_ms`, `peak_gpu_power_w`, `total_frames`
  fields + `fields.yml` definitions, fixed invalid `w` unit enum
- (driver_version and dashboards were already committed in prior sessions)

**Pending commit (from this planning session):**
- `docs/ebpf-architecture.md` — Phase 2 eBPF design doc (new file)
- `docs/claude-chat-context.md` — this file (updated)

### Hardware confirmed in live session (2026-04-08)

- GPU: AMD Radeon RX 9070 XT (RADV GFX1201), driver 26.0.4, Mesa 26.0.4-arch2.2
- CPU: AMD Ryzen 7 9800X3D, 8c/16t
- RAM: 61,910 MB
- OS: CachyOS Linux, kernel 6.19.11-1-cachyos-deckify
- Vulkan driver: radv

### CPU topology verified (2026-04-08)

- Single CCD, single CCX — all 16 logical CPUs share L3 index 0 (`shared_cpu_list: 0-15`)
- `ccx_cross_count` eBPF metric will always be zero on this chip (expected; metric
  is architecturally correct for multi-CCD chips like 7950X/9950X)
- Core-to-core migrations within the single CCX are still tracked by
  `migration.total_count`

---

## Priorities (in order)

1. **Phase 2: eBPF daemon** — design doc complete (`docs/ebpf-architecture.md`).
   Claude Code implementation follows from that doc. Implementation order:
   - Sprint 1: Scaffold Aya project + `schedlatency` probe end-to-end
   - Sprint 2: `bio`, `gpu_sched`, `mem` probes + stutter correlation
   - Sprint 3: `gpu_fence`, `gpu_submit`, `futex`, `irq`, `vfs` probes
   - Sprint 4: Integration package updates (`fields.yml`, pipeline), Scheduler
     Analysis dashboard, packaging (AUR, systemd)
   - Sprint 5 (stretch): `syscall`, `shader`, `proton` probes
2. **Scheduler Analysis dashboard** — blocked on Sprint 2+ eBPF data.
3. **Pipeline/system tests** — require local ES or Docker setup.
4. **Phase 6: move integration to `package/` subdirectory** — long-term fix for
   `elastic-package-ignore` not applying during build copy step.

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
| Scheduler Analysis | 🔲 Phase 2 data required | needs eBPF stream |

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
