# Task: rigsignal-024-item5-round-a-fix1 — fix tailer inflight stall (reviewer REJECT)

Session: 2026-07-18-024-item5. Workspace: the SAME worktree as round A
(`/home/dev/coding/RigSignal/worktrees/codex-024-round-a` — your previous uncommitted
changes are present). Do NOT commit.

## Reviewer finding (verbatim, verified)

`RemoteConnectionsTailer.inflight` (`src/remote_connections.rs:50`) is set only in
`read_batch` (~line 280) and cleared only in `ack_success` (~line 192). `poll()`
short-circuits to `Ok(Vec::new())` whenever `inflight.is_some()` (~lines 86–88), and no
other code path clears it. In `main.rs` the two failure branches — bulk item failure
(~line 989) and transport error (~line 990) — log and correctly withhold the ack, but
never reset `inflight`. Net effect: one ES outage or bulk error permanently freezes the
tailer for the process's life — every later `poll()` returns empty, silently. This
defeats the store-and-forward durability the addendum mandates.

## Required fix

1. Add a reset method to the tailer — `nack(&mut self)` → clears `inflight` WITHOUT
   advancing any offset/checkpoint state, so the next `poll()` re-reads and re-emits the
   same batch (same byte ranges → identical sha256 `_id`s → idempotent replay).
2. Call it from BOTH failure branches in `main.rs` (bulk item failure and transport
   error). Shutdown path needs no call (restart replays from checkpoint).
3. New unit test — same-process retry: poll a batch, simulate bulk failure, call `nack`,
   poll again on the SAME instance → identical envelopes (same `_id`s, same token), then
   ack_success → checkpoint advances. The existing crash/restart test stays.
4. Also add (reviewer minor note): a test where a FRESH instance (restart simulation)
   discovers a rotated `remote_connections.txt.1` carrying the saved dev/ino and drains
   it before switching to the current generation — distinct from the existing
   live-rotation test.

No other changes. Re-run `cargo check` and `cargo test` and report exact pass/fail
counts.

## STM contract

Before starting: `CHRONO_SESSION=2026-07-18-024-item5 bash /home/dev/coding/Workflow/scripts/stm.sh recall`
(if sandbox-blocked, note and continue). On completion: condensed summary only.
