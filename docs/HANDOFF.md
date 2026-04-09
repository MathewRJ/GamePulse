# GamePulse Session Handoff

Each session prepends a new entry. Read the **most recent entry first**, then follow
the "Previous sessions" chain for context on why decisions were made.

---

## Session: 2026-04-09b (Sprint 1 end-to-end PASSED)

### Context coming in
Previous session had committed all_pids expansion (`1271b1e`) and was ready to re-test.

### What was done this session

#### Root cause diagnosis — why eBPF produced zero docs in first re-test

Three compounding bugs found in the async ring buffer drain:

**Bug 1 — AsyncFd + EPOLLET race (primary cause, zero events)**
`drain_ring_buf` used `AsyncFd<RingBuf<MapData>>` with Tokio's edge-triggered epoll (EPOLLET).
The drain loop called `rb.next()` until None, then `guard.clear_ready()`. If new events arrived
between the last `next()==None` and `clear_ready()`, there was no edge transition (fd already
readable), so EPOLLET never fired again. Drain task hung indefinitely → no events → no docs.
Fix: removed async drain task entirely; drain ring buffer synchronously in `collect()` on each
1-second tick. `rb.next()` is non-blocking (returns None immediately when buffer empty).

**Bug 2 — GAME_PIDS at 100% capacity**
`GAME_PIDS` had `max_entries=64` and we were inserting exactly 64 TIDs. BPF hash maps at
100% load can fail inserts due to hash collisions (hash table has no overflow headroom).
Fix: increased `max_entries` to 256 in probe; TID cap to 256 in daemon `collect_game_tids`.

**Bug 3 — 30-second GAME_PIDS thrash**
Session watcher's `recv_timeout(30s)` path unconditionally re-read the session file and
re-sent state every 30 seconds even when a session was already active, causing unnecessary
GAME_PIDS clear+repopulate cycles.
Fix: timeout path now only acts when `active == false` (truly missed event).

#### Test results (Starfield, Proton, 305s session)

- Ring buffer: ~5,462 sched events/second drained
- ES docs: 231 docs shipped to `metrics-gamepulse.ebpf-default`
- Latency histogram: avg 1.47μs, max 107μs (healthy gaming system)
- Thread breakdown: wineserver (459 sw/s), xalia.exe (324), Thread Pool Workers, MangoHud
- Migration: total_count=0, ccx_cross=0 (expected: single-CCX 9800X3D)
- Session lifecycle: detect → active → game exited → clear — all clean
- No 30s re-detection noise

**Phase 2 Sprint 1 end-to-end test: COMPLETE AND PASSING.**

### Commits
- `fix(ebpf): fix ring buffer drain race + GAME_PIDS capacity + 30s thrash`

### What is NOT done (next priorities)
1. **Sprint 2**: bio (block I/O latency), gpu_sched, mem probes + stutter correlation
2. **Phase 4 Rust agent**: not started
3. **SIGTERM handler**: kill bypasses finally → session.json not cleaned up (low priority)
4. **Scheduler Analysis dashboard**: blocked until Sprint 2 ebpf data

---

## Session: 2026-04-09 (all_pids expansion + session resume)

### Context coming in
Previous session ended with a code red mid-test. Working tree had uncommitted
changes to `cli.py`, `detector/game.py`, and `session.rs`. These were the
`all_pids` expansion changes — made last session but never committed due to code red.

### What was done this session

#### Resumed from code red — committed all_pids expansion (`1271b1e`)
Three files had uncommitted modifications (were in working tree, not staged):
- `collector/gamepulse/detector/game.py`: Refactored `detect()` to collect ALL
  PIDs with `SteamAppId` into `all_pids_by_appid`. Previously the helper-process
  skip loop returned early, discarding non-representative PIDs. Now ALL matching
  PIDs are collected; representative is chosen separately for metadata. `DetectedGame`
  gets a new `all_pids: list[int]` field (default = `[pid]`).
- `collector/gamepulse/cli.py`: `_write_session_json()` now accepts `all_pids`
  and writes `game_pids: [...]` to session.json. Log message updated to show pids.
- `ebpf/gamepulse-ebpf-daemon/src/session.rs`:
  - `SessionInfo` gets `game_pids: Vec<u32>` field (back-compat: falls back to
    `[game_pid]` if absent)
  - `collect_game_tids()` now takes `&[u32]` and walks each root PID's tree
  - `/tmp/gamepulse/` set to mode 1777 at startup so unprivileged collector can
    write into root-created directory
  - Log emits `pid_count` alongside `tid_count`

**Motivation for all_pids fix**: Sprint 1 end-to-end test produced only 1 doc for
a 3.5-min session. Root cause: only Proton root PID was in GAME_PIDS → only 8
infrastructure TIDs tracked → those threads barely context-switch → aggregator
gets no events → `flush()` returns None → no doc shipped. With all_pids, the
daemon will capture wine64/wineserver/DX worker threads, generating sched events
every second.

**Daemon built clean** after commit. Ready to re-test.

### Current state
- Working tree clean. Branch up to date with origin/main.
- All prior Sprint 1 + end-to-end fixes are committed and pushed.
- Ready for re-test with Starfield (or any Steam/Proton game).

### Next step
```bash
# Terminal 1
gamepulse-collector

# Terminal 2
sudo ebpf/target/debug/gamepulse-ebpf
```
Expect: `pid_count=N` (>1), `tid_count=M` (>>8), sched docs every ~1s in ES.

### Open questions (carried forward)
1. Aggregator flush interval: confirmed 1s default. Returns None if no events.
   Once actual game threads are in GAME_PIDS, should see docs every second.
2. SIGTERM handler: `kill` bypasses `finally` → session.json not cleaned up.
   Low priority.
3. Sprint 2 probes: bio, gpu_sched, mem, stutter correlation. Design pending.

---

## Session: 2026-04-08 (HANDOFF.md + code red + end-to-end test + path fix)

### Context coming in
Sprint 1 complete. session.json handoff just wired (collector writes, daemon watches).
First real end-to-end test run. Also: user requested persistent session continuity docs
and an emergency git save mechanism triggered by typing "code red" in any message.

### What was built this session

#### HANDOFF.md system (this file)
- `docs/HANDOFF.md` created — detailed session log, newest entry at top
- Each code red (or session end) prepends a new entry: decisions, dead ends, commits, next steps
- Distinct from memory: HANDOFF.md is narrative/detailed; memory is compressed facts
- Committed at `2eb2175`

#### Code red emergency save hook (updated)
Updated `.claude/hooks/code-red-save.sh` to:
1. `git add -A && git commit --allow-empty && git push` immediately
2. Inject `additionalContext` instructing Claude to:
   - Update `docs/HANDOFF.md` (prepend new session entry)
   - Update memory `project_state.md`
   - `git add + commit + push` those files
- Hook registered in `.claude/settings.local.json` as UserPromptSubmit

#### Memory compacted
`project_state.md` trimmed from 169 → ~110 lines. Removed redundancy now covered
by HANDOFF.md. Rule going forward: update memory after each logical task, not just
milestones. HANDOFF.md carries the detailed narrative.

#### End-to-end test: partial success
Collector ran cleanly (Cyberpunk 2077, session `04f65f95`, 294s, 88 ticks, all
HTTP 200s to ES). eBPF daemon started, loaded probes, but immediately logged:
`session ended — clearing PID filter` and never picked up the game session.

#### Root cause: XDG_RUNTIME_DIR stripped by sudo
- Daemon runs as `sudo` → `sudo` strips `XDG_RUNTIME_DIR` from environment
- Daemon: `XDG_RUNTIME_DIR` not set → falls back to `/tmp/gamepulse/session.json`
- Collector: `XDG_RUNTIME_DIR=/run/user/1000` → writes to `/run/user/1000/gamepulse/session.json`
- Two processes watching/writing **different paths** → daemon never got inotify notification

#### Fix (commit `4c652f1`)
- `collector/gamepulse/cli.py`: `_session_json_path()` now always returns
  `/tmp/gamepulse/session.json`. Removed XDG_RUNTIME_DIR logic.
  **Rationale**: session.json is cross-privilege IPC (user↔root). /tmp is the
  canonical place for that. XDG_RUNTIME_DIR is per-user ephemeral storage, not
  suitable when the reader runs as root.
- `ebpf/gamepulse-ebpf-daemon/src/session.rs`: `spawn_watcher` no longer sends
  the initial inactive state to the channel. Previously this caused "session ended"
  to log on every startup (confusing, looked like a real session-end event).

### Current state
- Session.json path is now consistent: both use `/tmp/gamepulse/session.json`
- Daemon binary rebuilt successfully
- End-to-end test needs to be re-run to confirm sched docs land in ES

### Next step
Re-run with both processes:
```bash
# Terminal 1
gamepulse-collector

# Terminal 2 (pre-build done)
sudo ebpf/target/debug/gamepulse-ebpf
```
Expect to see "session started — updating PID filter" in daemon log when game launches.

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
