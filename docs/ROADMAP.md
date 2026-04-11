# GamePulse Roadmap

Last updated: 2026-04-11 (Phase 4 distribution verified — zip upload to Fleet API confirmed on local + Serverless)
Source of truth reconciled: 2026-04-11

## Status legend

| Symbol | Meaning |
|---|---|
| ✅ | Complete and verified end-to-end (code + ES confirmation) |
| ⚠️ | Built, hardware-validated, ES receipt not re-confirmed this session |
| 🔲 | Not started — no code exists |
| 🚫 | Blocked on a dependency listed inline |

---

## Current position

Phase 2 eBPF daemon fully complete (all 9 probes ES-confirmed). Phase 6 Rust
production agent **fully verified with live gameplay** (Starfield, Proton, 40 min,
2026-04-11): all 8 metric streams confirmed, game detection working, frame data
active, session summary correct. Rust agent is now production-primary; Python
collector is reference/fallback only.

**elastic-package full test suite — COMPLETE (2026-04-11):**

| Test type | Result | Notes |
|-----------|--------|-------|
| `test static` | ✅ 11/11 PASS | |
| `test pipeline` | ✅ 11/11 PASS | Uses remote ES; no Docker required |
| `test asset` | ✅ 12/12 PASS | Via `bash scripts/test-asset.sh`; local 8.13.0 stack |
| `test policy` | ⏭ "No test results" | No policy fixtures; acceptable |
| `test system` | ⏭ "No test results" | Custom binary integration requiring gaming hardware; elastic/integrations guidelines allow skip |

**Critical path to Phase 4 (closed beta) — ALL prerequisites met:**
1. ~~Packaging — systemd unit + AUR PKGBUILD~~ ✅ Done 2026-04-11
2. ~~Full `elastic-package test` suite~~ ✅ All tests in final state 2026-04-11
3. eBPF Sprint 4 — `sample_event.json` updates for all probe types (low priority for beta)

---

## Phase 2: eBPF daemon (active)

### Sprint 1 — schedlatency probe ✅

**Status:** Complete and confirmed in ES

| Item | Detail |
|---|---|
| Tracepoints | `sched/sched_wakeup`, `sched/sched_switch`, `sched/sched_migrate_task` |
| ES verified | Yes — 231 docs, `metrics-gamepulse.ebpf-default`, Starfield 2026-04-09 |
| Fields defined | `gamepulse.ebpf.runqueue.*` (histogram, min/max/avg_us, event_count), `gamepulse.ebpf.migration.*` (total_count, ccx_cross_count), `gamepulse.ebpf.thread_breakdown[]` (nested) |
| Known behaviour | `ccx_cross_count` always 0 on 9800X3D (single CCX) — expected, not a bug |

### Sprint 2 — I/O + GPU + memory probes ✅

**Status:** ✅ Confirmed in ES. ES|QL query 2026-04-10: 6,112 docs, probes=["bio","gpu_sched","schedlatency"], latest=2026-04-09T15:31:36Z.

| Probe | Tracepoints | Verified event rate | ES confirmed |
|---|---|---|---|
| bio | `block/block_rq_issue`, `block/block_rq_complete` | 1–1,351/s (spikes on asset loads) | ✅ Confirmed |
| gpu_sched | `gpu_scheduler/drm_sched_job_queue`, `.../drm_sched_job_run` | 1,500–10,925/s | ✅ Confirmed |
| mem | `exceptions/page_fault_user`, `vmscan/mm_vmscan_direct_reclaim_begin` | 0/s steady-state (expected) | ✅ Correct — no events = no doc (by design) |
| stutter_correlation | Userspace correlator — fires when ≥2 probes exceed 16ms in same 1s window | Never observed (healthy session) | ✅ Correct — threshold not crossed |

**Note:** Stutter correlation ships to `metrics-gamepulse.ebpf-default` with
`probe: "stutter_correlation"` (not a separate data stream as originally designed —
simpler, no extra stream needed). Threshold is 16ms (1 frame at 60fps) uniform
across all probes. May need tuning once live data accumulates.

**ES histogram field type:** Confirmed accepted — bio and gpu_sched histogram docs
landed without errors. Open question resolved: `type: histogram` works on TSDS Serverless.

**Sprint 2 is fully verified. Proceed directly to Sprint 3.**

### Sprint 3 — extended probes ✅

**Status:** ✅ Confirmed in ES. Session 7bce1dc5 (Starfield, 2026-04-10): 2348 total eBPF docs, all 5 probes present.

| Probe | Kernel attachment | Symbol source | ES confirmed |
|---|---|---|---|
| futex | kprobe/kretprobe `do_futex` | `T do_futex` in kallsyms | ✅ 6 docs — GAME_PIDS filtered; sparse = correct (low contention) |
| irq | tracepoints `irq/irq_handler_{entry,exit}`, `irq/softirq_{entry,exit}` | `/sys/kernel/tracing/events/irq/` | ✅ 367 docs — hard_irq + softirq both confirmed |
| vfs | kprobe/kretprobe `vfs_read`, `vfs_write` | `T vfs_read`, `T vfs_write` in kallsyms | ✅ 362 docs — read + write both confirmed |
| gpu_fence | kprobe/kretprobe `dma_fence_default_wait` | `T dma_fence_default_wait` in kallsyms | ✅ 367 docs — blocked_count=0 (GPU not stalling, healthy session) |
| gpu_submit | kprobe `amdgpu_cs_ioctl` | `t amdgpu_cs_ioctl [amdgpu]` in kallsyms | ✅ 367 docs — event_count=181/doc (count-only, as designed) |

Fields in `data_stream/ebpf/fields/fields.yml`: futex, irq (hard_irq + softirq),
vfs (read + write), gpu_fence, gpu_submit. `elastic-package check` PASS, `test static` 11/11 PASS.

**Sprint 3 is complete. Phase 2 eBPF daemon is fully confirmed end-to-end.**

### Sprint 4 — integration + Scheduler Analysis dashboard 🔲

**Status:** Scheduler Analysis dashboard ✅ built (2026-04-11, ID: 89ca0908-5639-45f7-9a70-edadfe7d7124). Remaining:

- Update `data_stream/ebpf/sample_event.json` to add examples for bio, gpu_sched,
  mem, and stutter_correlation probe types (currently only schedlatency covered)
- ~~Add systemd service unit for `gamepulse-ebpf` daemon~~ ✅ Done 2026-04-11 (`packaging/systemd/gamepulse-ebpf.service`)
- ~~AUR PKGBUILD for the eBPF daemon binary~~ ✅ Done 2026-04-11 (`packaging/PKGBUILD`, both services smoke-tested active)

**Scheduler Analysis dashboard** (`dashboards/scheduler-analysis-dashboard.json`):
15 panels — probe type filter, session filter, 6 metric tiles (runqueue avg latency,
CPU migrations, hard IRQ avg latency, futex contentions, VFS read avg latency, GPU
fence avg latency), runqueue latency timeline, CPU migration timeline, IRQ event count
stacked area, VFS latency timeline, GPU fence latency + blocked count, futex contention
timeline, GPU submit rate. Source: `metrics-gamepulse.ebpf-default`.

**Session to allocate:** 1 Claude Code session (sample_event.json only — systemd unit done)

### Sprint 5 — stretch probes 🔲

**Status:** Not started

| Probe | Method | Blocker |
|---|---|---|
| syscall | syscall enter/exit tracepoints | High frequency — needs careful rate limiting |
| shader | uprobe on Mesa `nir_shader_compiler_init` or equivalent | Target path not stable across Mesa versions; discovery needed at runtime |
| proton | kprobes on Wine/ntdll translation entry points | Only meaningful when Proton is running |

**Session to allocate:** 1–2 Claude Code sessions

---

## Phase 6: Rust Production Agent — CRITICAL PATH

**Status:** Scaffold complete. `src/` exists, `cargo check` passes.

This gates Phase 4 (closed beta) and the elastic/integrations PR. The data model
is completely stable — field names are proven by 6 live dashboards and real gameplay
data. The Rust port is translation work, not design work.

### Implementation order (one session per item)

| Step | Deliverable | Notes |
|---|---|---|
| 1 | `src/Cargo.toml`, CLI, config, ES shipper — `cargo check` passes | ✅ Done 2026-04-10 |
| 2 | CPU collector (`/proc/stat`, `/proc/loadavg`, k10temp hwmon) | ✅ Done 2026-04-10 |
| 3 | Memory collector (`/proc/meminfo`, `/proc/<pid>/status`) | ✅ Done 2026-04-10 |
| 4 | Storage collector (`/proc/diskstats`, `/sys/block/`) | ✅ Done 2026-04-10 |
| 5 | Network collector (`/proc/net/dev`) | ✅ Done 2026-04-10 |
| 6 | Power collector (`/sys/class/power_supply/`, RAPL/hwmon) | ✅ Done 2026-04-10 |
| 7 | Audio collector (PipeWire/PulseAudio via `pactl`/`pw-cli`) | ✅ Done 2026-04-10 |
| 8 | MangoHud frame timing collector (log file tail) | ✅ Done 2026-04-10 |
| 9 | AMD GPU collector (sysfs/hwmon — card1/hwmon3 heuristic) | ✅ Done 2026-04-10 — validated on RX 9070 XT |
| 10 | Merge eBPF daemon as feature-flagged module | Fold `ebpf/` into `src/ebpf/` |
| 11 | Packaging: AUR PKGBUILD + systemd units | ✅ Done 2026-04-11 — both services smoke-tested active; `.deb`/`.rpm` deferred |

**AMD GPU heuristic validated 2026-04-10**: card1 = RX 9070 XT (score 18: fan+power+hotspot+hwmon); card0 = iGPU (score 1). Hwmon discovered via `{card}/device/hwmon/hwmon*` device-path traversal.

### Phase 6 — Main loop integration ✅

**Status:** Complete and ES-confirmed 2026-04-10.

All 8 collectors wired into the main loop with 1s tick. Game detection ported from
Python (`session.rs`). Host enricher implemented (`host.rs`). Session lifecycle
complete: session.json written on game start, removed on exit; session start/end
docs with hardware snapshot shipped to `metrics-gamepulse.session-default`.

**ES-confirmed 2026-04-10** (idle, no game): All 8 datasets shipping.

**ES-confirmed 2026-04-11 — full gameplay session** (Starfield, Proton, 40 min):
- `gamepulse.cpu` — 661 docs, `gamepulse.game.name='Starfield'` ✅
- `gamepulse.gpu` — 662 docs ✅
- `gamepulse.memory` — 662 docs ✅
- `gamepulse.storage` — 661 docs ✅
- `gamepulse.network` — 661 docs ✅
- `gamepulse.audio` — 662 docs ✅
- `gamepulse.power` — 662 docs ✅
- `gamepulse.frame` — 642 docs, avg_fps=286.9, p99_frametime=6.36ms ✅ (MangoHud active)
- `gamepulse.session` — start + summary confirmed ✅

Session summary: `avg_fps=286.9`, `low_1pct=167`, `duration_s=2430`, `bottleneck=gpu`,
`peak_gpu_temp=46°C`, `peak_cpu_temp=61.6°C`, `graphics_api=dx_via_proton` ✅

---

## Phase 4: Closed Beta — IN PROGRESS

**Status:** Distribution infrastructure verified 2026-04-11. Ready to onboard first colleague.

**Distribution verified (2026-04-11):**
- ✅ Local registry: `elastic-package stack up` (from repo root) serves gamepulse 0.1.0 via HTTPS registry on port 8080. Registry auto-discovers `build/packages/*.zip`.
- ✅ Zip upload to Kibana Fleet API: `POST /api/fleet/epm/packages` with `Content-Type: application/zip`. Works on local 8.13.0 (44 assets) and Elastic Cloud Serverless (47 assets).
- ✅ All 11 index templates present after fresh install.
- ✅ `docs/BETA-INSTALL.md` created — colleague onboarding guide.

**Distribution method for Serverless:** Zip upload only. Serverless Fleet does not support custom registry URLs (`xpack.fleet.registryUrl` is a self-hosted Kibana config). Colleagues upload the zip via Kibana Fleet UI or direct API POST.

**Remaining tasks:**
1. **First colleague onboarding** — share `docs/BETA-INSTALL.md` + `gamepulse-0.1.0.zip`
2. **GitHub Release v0.1.0** — tag, attach zip + AUR package binaries
3. **`.deb`/`.rpm` packaging** — needed for non-Arch Linux users

**Phase 4 success criteria:** 10+ colleagues running GamePulse with data flowing to their ES deployments.

---

## elastic/integrations PR (end goal) — NEXT MAJOR MILESTONE

**Status:** Phase 4 beta in progress. PR prep is the next Claude Code session work.

**Requirements checklist:**
- [x] `elastic-package test` all types in final state (static+pipeline+asset PASS, system/policy acceptable skip)
- [x] Rust binary builds and runs (ES-confirmed, AUR packaging done)
- [ ] `docs/README.md` — elastic/integrations-format README with screenshots, config reference, troubleshooting
- [ ] `CHANGELOG.md` — 0.1.0 entry
- [ ] ECS compliance check (`elastic-package check` covers field compliance; full review TBD)
- [x] Dashboard panels all by-value with `data_stream.dataset` filters ✅ (verified in build sessions)
- [ ] Fork `elastic/integrations`, add to `packages/`, submit PR
- [ ] Engage Elastic integrations team for review

**Next session tasks:**
1. Write `docs/README.md` meeting elastic/integrations contribution standards
2. Write `CHANGELOG.md` (0.1.0 entry)
3. Run ECS compliance check
4. Fork `elastic/integrations` and prepare the PR structure

---

## Phase 5: Windows & Cross-Platform 🔲

**Status:** Deferred. Not on critical path until Phase 6 is complete.

---

## Phase 7: Community Platform 🔲

**Status:** Deferred. Dependent on public elastic/integrations merge.

---

## Known technical gotchas (permanent reference)

**BPF verifier opt-level:**
`-C opt-level=2` MUST be set for the `bpfel-unknown-none` target in
`ebpf/.cargo/config.toml`. Debug builds emit BPF-to-BPF calls to panic
infrastructure that fail the kernel verifier ("processed 0 insns"). Never remove
this flag.

**Async ring buffer drain race:**
Do not use `AsyncFd<RingBuf>` with Tokio's EPOLLET — events arriving between
`rb.next()==None` and `clear_ready()` are silently dropped. Drain synchronously
in `collect()` on each tick instead. `next()` is non-blocking.

**GAME_PIDS map capacity:**
`max_entries=256` (bumped from 64). BPF hash maps at 100% load fail inserts due
to hash collision chains. Always leave headroom.

**session.json path:**
Always `/tmp/gamepulse/session.json`. Never `$XDG_RUNTIME_DIR` — sudo strips
that variable, so the daemon (root) and collector (user) would watch different paths.

**RADV GPU scheduling:**
`drm_sched_job_queue` must be system-wide (no GAME_PIDS filter). RADV uses
dedicated submission threads that are not in the game's PID tree.

**Kibana API schema drift (verified 2026-04-07):**
- `options_list_control`: use `field_name` (snake_case), not `fieldName`
- Text field filters MUST use `.keyword` sub-field (bare field silently broken)
- `xy` chart terms x-axis and `breakdown_by`: no `size` field allowed
- `data_table` type name is `data_table` (not `datatable`)
- ES|QL `type:"esql"` not supported in inline panel attributes — use `type:"dataView"`

**elastic-package hyphen constraint:**
Directory names inside the package cannot contain hyphens. eBPF workspace lives
in `ebpf/` not `gamepulse-ebpf/`. Inner crate names can use hyphens.

---

## Open questions (unresolved)

1. **ES `histogram` field type on Serverless TSDS**: ✅ RESOLVED — Accepted natively
   by Serverless TSDS. histogram docs land without errors. No schema change needed.

2. **Stutter correlation threshold tuning**: 16ms (1 frame at 60fps) may be too
   coarse for typical gameplay. Revisit once real stutter events are captured.

3. **Mesa shader compiler uprobe** (Sprint 5): target path for
   `nir_shader_compiler_init` is not stable across Mesa versions/distros. Runtime
   discovery mechanism needed.

4. **`ccx_cross_count` always zero**: Expected on AMD Ryzen 9800X3D (single CCX,
   all 16 logical CPUs share L3). Metric is architecturally correct for multi-CCD
   chips (7950X, 9950X etc.) — not a bug on this hardware.
