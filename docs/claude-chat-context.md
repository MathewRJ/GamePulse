# GamePulse — Claude.ai Planning Session Context

This file is maintained by the claude.ai web interface (planning sessions) to
preserve context across conversations. It complements CLAUDE.md, which is for
Claude Code (implementation sessions).

**Update protocol:**
- claude.ai: update this file at the end of every planning session, then commit
- Claude Code: update CLAUDE.md current state section at the end of every session
- Neither should edit the other's file

Last updated: 2026-04-10 (Sprint 3 code complete)

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
| gpu_fence | `dma_fence_default_wait` kprobe | ⚠️ BUILT — ES confirmation pending |
| gpu_submit | `amdgpu_cs_ioctl` kprobe | ⚠️ BUILT — ES confirmation pending |
| futex | `do_futex` kprobe/kretprobe | ⚠️ BUILT — ES confirmation pending |
| irq | irq_handler_{entry,exit} + softirq_{entry,exit} tracepoints | ⚠️ BUILT — ES confirmation pending |
| vfs | `vfs_read`/`vfs_write` kprobe/kretprobe | ⚠️ BUILT — ES confirmation pending |
| syscall | syscall tracepoints | 🔲 NOT STARTED |
| shader | Mesa uprobe | 🔲 NOT STARTED |
| proton | Wine/ntdll kprobes | 🔲 NOT STARTED |

### Git state (end of last Claude Code session — 2026-04-10)

Branch: `main`, clean, up to date with `origin/main` (after push)

Recent commits:
- `0fac78c` feat(ebpf): Sprint 3 — five new probes: futex, irq, vfs, gpu_fence, gpu_submit
- `614fb0d` docs: mark Sprint 2 fully ES-confirmed 2026-04-10
- `8983d27` fix(collector): SIGTERM now interrupts sleep and runs finally cleanup
- `92cd994` feat(ebpf): Sprint 2 mem probe

### Hardware confirmed in live session (2026-04-08/09)

- GPU: AMD Radeon RX 9070 XT (RADV GFX1201), driver 26.0.4, Mesa 26.0.4
- CPU: AMD Ryzen 7 9800X3D, 8c/16t, single CCX (ccx_cross_count always 0 — expected)
- RAM: 61,910 MB
- OS: CachyOS Linux, kernel 6.19.11-1-cachyos-deckify

---

## Priorities (in order)

1. **Sprint 3 ES confirmation**: Recompile BPF object (`cargo xtask build-ebpf`), run
   daemon as root, verify futex/irq/vfs/gpu_fence/gpu_submit docs appear in ES.
2. **Phase 6 Rust agent scaffold**: `src/Cargo.toml`, CLI, config, ES shipper —
   `cargo check` only. One session. This gates Phase 4 (closed beta) and the
   elastic/integrations PR.
3. **Phase 6 Rust collectors** (one per session): CPU, memory, storage, network,
   power, audio, AMD GPU (needs gaming PC for live testing), MangoHud frame.
4. **Scheduler Analysis dashboard**: build after Sprint 3 eBPF data confirmed in ES.
5. **Packaging**: systemd unit, AUR PKGBUILD, .deb/.rpm.
6. **Full elastic-package test suite**: asset + system + policy tests (need Docker/local ES).

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
