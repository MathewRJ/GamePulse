# GamePulse Session Handoff

Each session prepends a new entry. Read the **most recent entry first**, then follow
the "Previous sessions" chain for context on why decisions were made.

---

## Session: 2026-04-08 (Sprint 1 completion + integration wiring)

### Context coming in
Continued from Session 2026-04-08 (earlier). Phase 2 Sprint 1 was scaffolded but
the BPF probes were failing the kernel verifier with "last insn is not an exit or
jmp / processed 0 insns / processed 0 insns". The previous session ended mid-debug.

### What happened this session

#### 1. BPF verifier fix (opt-level=2)
**Problem**: The daemon loaded the BPF ELF but every tracepoint attachment failed
with "processed 0 insns". Root cause was traced through aya-obj source:
- Debug Rust builds (no `-C opt-level=2`) emit BPF-to-BPF calls (`BPF_PSEUDO_CALL`,
  src_reg=1) to panic/unreachable infrastructure in the `.text` section
- These come from bounds checks inside `ctx.read_at()` calls in sched.rs
- aya's `relocate_calls` / `FunctionLinker` recognises them as valid BPF function
  calls and links the panic functions inline into each tracepoint program
- The combined program fails the BPF verifier's pre-loop last-instruction check

**Fix**: Added `-C opt-level=2` to `[target.bpfel-unknown-none]` rustflags in
`ebpf/.cargo/config.toml`. The compiler now eliminates dead unreachable branches
before bpf-linker sees them.

**Commit**: `7e785b8 fix(ebpf): add opt-level=2 to BPF target rustflags`

**DO NOT REMOVE `-C opt-level=2`**. Without it, any `ctx.read_at()` call or slice
indexing in BPF programs will regenerate the panic infrastructure BPF calls.

#### 2. Code red emergency save hook
User requested an emergency git save triggered by typing "code red" anywhere in a
message (useful when SSH connection might drop mid-session).

**Implementation**:
- `.claude/hooks/code-red-save.sh`: bash script, reads stdin JSON, greps for
  "code red" (case-insensitive), runs `git add -A && git commit --allow-empty && git push`
- Registered as a `UserPromptSubmit` hook in `.claude/settings.local.json`
- On trigger: saves code immediately, then injects `additionalContext` instructing
  Claude to update HANDOFF.md + memory before continuing
- Pipe-tested: match/non-match both work correctly

**Commit**: `9e50398 emergency save [code red] 2026-04-08T22:01:40`

#### 3. session.json handoff wired (collector → daemon bridge)
**Problem**: The eBPF daemon watches `$XDG_RUNTIME_DIR/gamepulse/session.json` to
know which PIDs to filter in the BPF maps. But the Python collector never wrote it.

**Fix**: Added to `collector/gamepulse/cli.py`:
- `_session_json_path()`: returns `$XDG_RUNTIME_DIR/gamepulse/session.json` or
  `/tmp/gamepulse/session.json` fallback
- `_write_session_json(session_id, game_pid, game_name, steam_app_id)`: called when
  game is first detected (line ~177 in cli.py)
- `_remove_session_json()`: called when game exits AND in the `finally` block on
  collector shutdown

**Session.json format** (must match daemon's `SessionInfo` struct):
```json
{"session_id": "...", "game_pid": 12345, "game_name": "...", "steam_app_id": 12345}
```

#### 4. gamepulse-ebpf/ renamed to ebpf/
**Problem**: `elastic-package check` was failing with "directory name inside package
gamepulse contains -: gamepulse-ebpf". The `.elastic-package-ignore` file does NOT
suppress this lint rule — it only applies to the build copy step.

**Fix**: Renamed `gamepulse-ebpf/` → `ebpf/`. Inner crate names unchanged
(`gamepulse-ebpf-probes`, `gamepulse-ebpf-daemon`). Cargo workspace works identically.

**Updated**:
- `.elastic-package-ignore`: path updated to `ebpf/`
- `Makefile`: ebpf target updated to `cd ebpf && cargo xtask build-ebpf`
- `CLAUDE.md`: all references updated

#### 5. ebpf data stream fields.yml + sample_event.json aligned
The `data_stream/ebpf/fields.yml` was a Sprint 0 placeholder with ~15 probe fields
that don't exist yet. The `sample_event.json` referenced those fields, causing the
static test to fail once the fields.yml was corrected.

**Fields now defined** (matching daemon's `EbpfMetricDoc` Rust struct):
- `gamepulse.ebpf.probe` (keyword, dimension)
- `gamepulse.ebpf.runqueue.*` (latency_histogram, min/max/avg_us, event_count)
- `gamepulse.ebpf.migration.*` (total_count, ccx_cross_count)
- `gamepulse.ebpf.thread_breakdown[]` (nested: comm, tid, runqueue_avg_us, etc.)

**NOTE**: Sprint 2+ fields (bio, gpu_sched, futex, etc.) are NOT in fields.yml yet.
They will be added per sprint when implemented.

**Validation**: `elastic-package check` PASS, `elastic-package test static` 11/11 PASS.

**Commit**: `bf8094a feat(ebpf): wire session.json handoff and fix elastic-package lint`

### Current state at end of session
- Sprint 1 complete and integrated. `elastic-package check` + `test static` PASS.
- Python collector ↔ eBPF daemon handoff is wired via session.json.
- Code red emergency save hook is live.

### Next steps (Sprint 2 or end-to-end test first)
**Recommended: end-to-end test first**
```bash
# Terminal 1
gamepulse-collector

# Terminal 2
sudo ebpf/target/debug/gamepulse-ebpf
```
Launch a game → watch for `session detected` in daemon log → verify
`metrics-gamepulse.ebpf-default` has documents in Kibana.

**Sprint 2 probes** (once end-to-end verified):
- `block_rq_issue` / `block_rq_complete` tracepoints → `bio` probe (I/O latency)
- `amdgpu_cs_ioctl` / `dma_fence_wait_start` tracepoints → `gpu_sched` probe
- `mm_page_fault_*` tracepoints → `mem` probe
- Stutter correlation: when frame time > 33ms AND sched latency spike → ship to
  `logs-gamepulse.events-default`

### Open questions / things to watch
1. **ES histogram type on Serverless TSDS**: `LatencyHistogram` serializes to
   `{"values": [...], "counts": [...]}` which matches ES histogram format. Not yet
   tested against live ES. If rejected, fall back to storing `latency_p50_us`,
   `latency_p95_us`, `latency_p99_us` as plain doubles.
2. **`gamepulse.ebpf.probe` as TSDS dimension**: Currently marked as dimension.
   This means each probe type gets its own time-series. Correct for Sprint 2+ when
   there will be multiple probes per second.
3. **SIGTERM handler for Python collector**: `kill` bypasses the `finally` block,
   so session.json won't be cleaned up. Minor QoL, low priority.

---

## Session: 2026-04-08 (Sprint 1 scaffold + BPF verifier investigation start)

### Context coming in
Phase 2 design doc (`docs/ebpf-architecture-design.md`, 941 lines) was written and
committed. Phases 0, 0.5, 1, 3 were complete. Python collector was working end-to-end
on CachyOS gaming PC. All 6 Kibana dashboards were live.

### What happened this session
- Created full `gamepulse-ebpf/` Rust workspace (Cargo, aya-ebpf 0.1.1, aya 0.13.1)
- Implemented `sched.rs` BPF kernel programs: 3 tracepoints + 3 maps
- Implemented userspace daemon: loader, session watcher, aggregator, ES shipper
- Fixed multiple build issues: config field names, probe path defaults, tracepoint
  section naming (had to use `#[tracepoint(name = "...", category = "sched")]`)
- Daemon loads and all 3 tracepoints attach
- BPF verifier "processed 0 insns" error encountered but NOT YET FIXED at end of
  this session

### Key design decisions made
- Separate `gamepulse-ebpf` binary (not embedded in Python collector) — merges into
  Phase 4 Rust agent later
- Ring buffer (not perf buffer) for sched events — supports concurrent readers
- 1-second aggregation interval in userspace, not per-event shipping
- `probe` field as TSDS dimension — polymorphic docs, one data stream for all probes

---

## Session: 2026-04-07 (Phase 3 dashboards + Phase 2 design)

### Context coming in
Phases 0, 0.5, 1 complete. Live Cyberpunk 2077 sessions validated.

### What happened
- Built all 6 Kibana dashboards via API (kibana-dashboards skill)
- Wrote `docs/ebpf-architecture-design.md` (941 lines) — full Phase 2 blueprint
- Validated field paths against live ES data with ES|QL queries
- Discovered `gamepulse.memory.game_rss_mb` is unreliable under Proton (tracks launcher)

### Key decisions
- Dashboard files live in `dashboards/` not `kibana/` (breaks elastic-package lint)
- Options list controls MUST use `.keyword` sub-fields for text fields
- `stutter_count` is a TSDS counter — use MAX not avg/sum in visualizations
