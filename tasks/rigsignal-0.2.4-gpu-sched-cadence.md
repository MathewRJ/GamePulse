# Task: rigsignal-0.2.4-gpu-sched-cadence — find + fix the REAL gpu_sched emission gap

Session: `CHRONO_SESSION=2026-07-17-024-kickoff`. You are `codex-impl@dev` in worktree
`worktrees/codex-024-gpu-sched` (fast-forward to main d1143bf — includes your loss fix,
which deployed live and did NOT restore density). Git read-only; no commits. Write
`tasks/rigsignal-0.2.4-gpu-sched-cadence.RESULT.md` in the worktree.

> STM contract as before (recall --all-sessions first; save findings; STM_AGENT=codex-impl@dev; RESULT fallback if curl blocked). Condensed summary only.

## Live evidence AFTER your loss fix (new pair attested, valve box, HFW running, 20:29–20:37Z)

1. Attach: `variant="legacy" key_field="id" key_offset=32 scope_field="entity" scope_offset=8`, 9/9.
2. **ZERO loss warnings in the journal** — either nothing drops, or (see item 6) the
   counter read fails silently.
3. Density UNCHANGED: gpu_sched docs at 20:30:03, 20:30:31, 20:30:50, 20:31:06,
   20:31:12, 20:33:30, 20:33:40, 20:33:46, 20:33:54, 20:35:45 — irregular 6–28s spacing
   plus multi-minute gaps. Every doc's event_count is ~1010–1022 (≈ exactly one second
   at the kernel's ~1020/s). Every @timestamp has the SAME millisecond phase (.323Z).
4. **The killer comparison**: over 20:33–20:36 (3 min), docs per probe:
   `schedlatency: 180` (= exactly 60/min, perfectly dense, per-second pipeline healthy),
   `irq: 44`, `vfs: 38`, `gpu_fence: 13`, `gpu_sched: 6`, `futex: 4`, `gpu_submit: 2`,
   `bio: 1`. GPU activity is CONTINUOUS (ftrace ground truth ~1020 pairs/s sustained),
   yet the whole gpu_* family is sparse while schedlatency is dense.
5. Each ES doc carries ONE probe's snapshot (`rigsignal.ebpf.probe` + that object).
6. Reviewer flag from the loss-fix review, now possibly load-bearing:
   `read_loss_counters()` returns `None` on ANY single per-CPU map-read failure
   (`.ok()?` in a loop) — a failing read silently disables all loss reporting.

## Mandate

1. **Diff the schedlatency pipeline against the gpu_sched pipeline end-to-end** — how
   each probe's collect() is scheduled/invoked, how its events are windowed, what
   condition gates snapshot emission, which clock domains are involved (bpf_ktime boot
   ns vs wall vs tick counter), and how per-probe docs are flushed/shipped. schedlatency
   achieves 60/min; find precisely what gpu_sched does differently. The fixed .323Z
   phase and the ~exactly-one-second event_count are constraints your explanation MUST
   satisfy, as is the irregular 6–28s spacing.
2. Explain why your 5×1000-events/s replay test passes while live fails — the test
   models some cadence/clock assumption wrongly. Fix the test to mirror the REAL
   runtime invocation pattern (whatever you find in 1) so it fails before your fix and
   passes after.
3. Fix the defect. Constraints as before: emitted ES field schema unchanged; pair may
   change; no opportunistic refactors; do not degrade schedlatency or other probes.
   If the root cause is shared by gpu_fence/gpu_submit, fix the shared mechanism (they
   ride the same family) — but verify schedlatency's density is preserved by test.
4. Also fix evidence item 6: a loss-counter read failure must log (rate-limited) rather
   than silently disable reporting.
5. Validation: `cargo check --workspace`, `cargo test --workspace`, `cargo test -p
   rigsignal-ebpf` (from ebpf/), `cargo xtask build-all --release`, rustfmt on changed
   files.

Report in the RESULT: the exact mechanism, why it produced this signature, what the fix
changes, and what the expected live signature is after deploy (docs/min, counter lines).
