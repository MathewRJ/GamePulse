# GamePulse Roadmap

Last updated: 2026-04-10 (Sprint 3 complete)
Source of truth reconciled: 2026-04-10

## Status legend

| Symbol | Meaning |
|---|---|
| ✅ | Complete and verified end-to-end (code + ES confirmation) |
| ⚠️ | Built, hardware-validated, ES receipt not re-confirmed this session |
| 🔲 | Not started — no code exists |
| 🚫 | Blocked on a dependency listed inline |

---

## Current position

Phase 2 Sprint 3 is code-complete (2026-04-10). All 5 new probes (futex, irq, vfs,
gpu_fence, gpu_submit) are implemented kernel-side (BPF) and userspace-side (Aya/Rust),
with field mappings in fields.yml. `cargo check` passes; `elastic-package check` and
`test static` both pass 11/11. ES confirmation (live run as root) is pending —
requires recompiling the BPF object and running the daemon. The daemon now supports
9 total probes (sched, bio, gpu_sched, mem, stutter_correlation, futex, irq, vfs,
gpu_fence, gpu_submit) with graceful per-probe requirements checking. The active
frontier is: (1) Sprint 3 ES confirmation, then (2) Phase 6 Rust production agent
scaffold — the critical path item blocking closed beta.

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

### Sprint 3 — extended probes ⚠️

**Status:** Code complete — ES confirmation pending (needs daemon recompile + root run)

| Probe | Kernel attachment | Symbol source | ES confirmed |
|---|---|---|---|
| futex | kprobe/kretprobe `do_futex` | `T do_futex` in kallsyms | Pending |
| irq | tracepoints `irq/irq_handler_{entry,exit}`, `irq/softirq_{entry,exit}` | `/sys/kernel/tracing/events/irq/` | Pending |
| vfs | kprobe/kretprobe `vfs_read`, `vfs_write` | `T vfs_read`, `T vfs_write` in kallsyms | Pending |
| gpu_fence | kprobe/kretprobe `dma_fence_default_wait` | `T dma_fence_default_wait` in kallsyms | Pending |
| gpu_submit | kprobe `amdgpu_cs_ioctl` | `t amdgpu_cs_ioctl [amdgpu]` in kallsyms | Pending |

Fields added to `data_stream/ebpf/fields/fields.yml`: futex, irq (hard_irq + softirq),
vfs (read + write), gpu_fence, gpu_submit field groups. `elastic-package check` PASS.

**Next step:** Run `cargo xtask build-ebpf && gamepulse-ebpf` as root with Sprint 3
probes compiled and confirm ES doc receipt.

### Sprint 4 — integration + Scheduler Analysis dashboard 🔲

**Status:** Not started. 🚫 Blocked on Sprint 3 probes.

- Update `data_stream/ebpf/sample_event.json` to add examples for bio, gpu_sched,
  mem, and stutter_correlation probe types (currently only schedlatency covered)
- Build **Scheduler Analysis dashboard** in Kibana (runqueue latency distribution,
  CPU migration frequency, CFS vs SCHED_FIFO comparison)
- Add systemd service unit for `gamepulse-ebpf` daemon
- AUR PKGBUILD for the eBPF daemon binary

**Session to allocate:** 1–2 Claude Code sessions

### Sprint 5 — stretch probes 🔲

**Status:** Not started

| Probe | Method | Blocker |
|---|---|---|
| syscall | syscall enter/exit tracepoints | High frequency — needs careful rate limiting |
| shader | uprobe on Mesa `nir_shader_compiler_init` or equivalent | Target path not stable across Mesa versions; discovery needed at runtime |
| proton | kprobes on Wine/ntdll translation entry points | Only meaningful when Proton is running |

**Session to allocate:** 1–2 Claude Code sessions

---

## Phase 6: Rust Production Agent — CRITICAL PATH 🔲

**Status:** Not started. `src/` does not exist.

This gates Phase 4 (closed beta) and the elastic/integrations PR. The data model
is completely stable — field names are proven by 6 live dashboards and real gameplay
data. The Rust port is translation work, not design work.

### Implementation order (one session per item)

| Step | Deliverable | Notes |
|---|---|---|
| 1 | `src/Cargo.toml`, CLI, config, ES shipper — `cargo check` passes | Reuse config format from `ebpf/gamepulse-ebpf-daemon/src/config.rs` |
| 2 | CPU collector (`/proc/stat`, `/proc/loadavg`, k10temp hwmon) | Simplest — good warm-up |
| 3 | Memory collector (`/proc/meminfo`, `/proc/<pid>/status`) | |
| 4 | Storage collector (`/proc/diskstats`, `/sys/block/`) | |
| 5 | Network collector (`/proc/net/dev`) | |
| 6 | Power collector (`/sys/class/power_supply/`, RAPL if available) | RAPL needs root; return None gracefully |
| 7 | Audio collector (PipeWire/PulseAudio via `pactl`/`pw-cli`) | |
| 8 | AMD GPU collector (sysfs/hwmon — card1/hwmon3 heuristic) | **Needs gaming PC online for live testing.** Preserve card-scoring heuristic from Python exactly. |
| 9 | MangoHud frame timing collector (log file tail) | |
| 10 | Merge eBPF daemon as feature-flagged module | Fold `ebpf/` into `src/ebpf/` |
| 11 | Packaging: systemd unit, `.deb`, `.rpm`, AUR PKGBUILD | |

**Biggest risk:** AMD GPU collector sysfs path heuristic is hardware-specific
(card1 not card0, hwmon3 scoring). Dedicate a full session with the gaming PC
online for step 8. Do not attempt it without the hardware available for testing.

---

## Phase 4: Closed Beta 🚫

**Status:** Blocked on Phase 6 Rust agent

**Needs:**
- Rust binary that installs without a Python venv
- `.deb`/`.rpm`/AUR packaging
- Self-hosted Elastic Package Registry for one-click Fleet install
- Full `elastic-package test` suite passing (currently only `test static` passes)

**Currently passing:** `elastic-package check` ✅, `test static` 11/11 ✅
**Not yet configured:** `test asset`, `test system`, `test policy` (require Docker or local ES)

---

## elastic/integrations PR (end goal) 🚫

**Status:** Blocked on multiple items below

**Requirements checklist:**
- [ ] `elastic-package test` all types passing
- [ ] Rust binary builds and runs
- [ ] README with screenshots
- [ ] `CHANGELOG.md` maintained
- [ ] ECS compliance verified
- [ ] Dashboard panels all by-value with `data_stream.dataset` filters
- [ ] Fork `elastic/integrations`, add to `packages/`, submit PR
- [ ] Engage Elastic integrations team for review

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
