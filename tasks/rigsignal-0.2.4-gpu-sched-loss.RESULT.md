# gpu_sched loss diagnosis and fix

## Diagnosis

The old BPF pairing path had two silent BPF-side loss mechanisms:

1. `GPU_SCHED_TS` was a 4,096-entry ordinary `HashMap<u64, u64>`.  Its insertion result was
   discarded.  A queue event without a matching run event leaves its monotonically increasing
   legacy `id` resident forever; once full, each future new-key insert fails and its run event
   cannot emit a sample.  The renamed path had the same unbounded-stale-entry failure mode.
2. A single `id` (legacy) or `fence_seqno` (renamed) is only scoped to a scheduler/context.  The
   old map key omitted that scope, so simultaneous jobs on different rings could overwrite a
   timestamp and make a later run event miss or be paired with the wrong queue timestamp.
   `GPU_SCHED_EVENTS.reserve()` was also silently ignored, so a full ring buffer was invisible.

The userspace consumer is *not* batch-capped: `GpuSchedProbe::collect()` loops over `rb.next()`
until empty, then calls `GpuAggregator::flush()`, which drains its entire vector.  The new replay
test sends 1,000 events into each of five consecutive windows and receives five independent
1,000-event snapshots.  Thus (c) and (d) are ruled out by code and test.

The exact live shape is also inconsistent with a map-full-only explanation: with unique,
monotonic keys, an ordinary full map would become permanently empty after the first roughly four
seconds at 1,020 queue events/s, not produce recurrent one-second bursts.  Likewise a missed
drain would preserve up to about ten seconds in the 256-KiB ring (16-byte payload plus ring
header), so its next unbounded drain would make a multi-second snapshot rather than ~1,020 events.
The evidence therefore establishes BPF-side loss and rules out aggregation attribution, but does
not uniquely distinguish stale-pair/run-miss loss from ring reserve loss without the counters
that were absent in the deployed build.  The previous implementation made that distinction
impossible and silent.

The recurring one-second snapshots mean the BPF program was yielding approximately one second of
matched pairs at the collection instants; it was not a userspace window collapsing a backlog.
After paired deployment, the new warning fields make the next live run decisive: non-zero
`run_miss` identifies unmatched/evicted pair state, `queue_insert` identifies map insertion
failure, and `ringbuf_reserve` identifies producer overflow.  All are rate-limited to one warning
per 30 seconds while retaining cumulative deltas.

## Fix

- Replaced `GPU_SCHED_TS` with a 4,096-entry LRU hash map so stale monotonic keys cannot
  permanently exhaust it.
- Changed the key from one sequence value to `{ scope, sequence }`:
  `entity + id` on Valve legacy tracepoints and `fence_context + fence_seqno` on renamed kernels.
  Both offsets are parsed and cross-checked from the queue/run tracepoint format files before
  attach, retaining the existing schema and paired-daemon/probe contract.
- Added per-CPU BPF loss counters for key-read failures, queue insertion failures, unmatched run
  events, and ring-buffer reserve failures.  The daemon sums them and logs their changes at warn,
  rate-limited to 30 seconds.  No ES field was added or changed.
- Added tests for scoped legacy format parsing, rate-limited loss reporting, and a five-second
  synthetic 1,000-events/s aggregation replay.

## Validation

- `cargo check --workspace` (repository root): passed.
- `cargo test --workspace` (repository root): 59 passed.
- `cargo test -p rigsignal-ebpf` (`ebpf/` workspace): 17 passed.
- `cargo xtask build-all --release` (`ebpf/` workspace): passed; built both daemon and BPF ELF.
- `rustfmt --check` on changed Rust files: passed.
- `git diff --check`: passed.

`cargo test -p rigsignal-ebpf` is necessarily run from `ebpf/`: the root workspace intentionally
contains only `src`, so that package is not addressable from its root.

No commit was made.
