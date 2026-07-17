# rigsignal-0.2.4-gpu-sched-cadence RESULT

Status: complete; no commit created.

## Exact mechanism

This was not a gpu_sched BPF emission, RingBuf, or aggregation-window loss.
All probes are invoked sequentially by the same one-second Tokio interval in
`main.rs`. `schedlatency` is first and `gpu_sched` is third; each `collect()`
fully drains its RingBuf and then flushes exactly one non-empty snapshot.

The BPF probes use `bpf_ktime_get_ns()` only to pair entry/exit events and
measure latency. It does not gate snapshot cadence. The userspace aggregators
used separate `Utc::now()` calls for their documents, but collection is fast
enough that Elasticsearch's millisecond `@timestamp` precision collapsed many
of them to the same value. The ES bulk shipper then sends all documents with
`create`. In this TSDB stream the probe discriminator is not a time-series
dimension, so documents with the same host/session dimensions and millisecond
timestamp have the same TSDB identity. The first document (schedlatency) wins;
later probe documents are version conflicts and are dropped by the existing
best-effort shipper.

This explains the signature precisely: schedlatency is first and stays at
60/min; gpu_sched/gpu_fence/gpu_submit are later and only survive when normal
collection timing happens to cross a millisecond boundary, producing irregular
6--28 second spacing. A surviving gpu_sched snapshot contains its complete
single collection window (~1020 events), rather than a multi-second backlog.
The common `.323Z` phase is the shared Tokio tick phase. BPF loss counters stay
zero because the drop is after collection, during TSDB indexing.

The old 5 x 1000-event replay called `GpuAggregator::flush()` in isolation. It
therefore tested event aggregation but neither the real multi-probe tick nor
the same-millisecond TSDB identity collision. The replacement test creates
schedlatency and gpu_sched documents in each of five shared ticks, explicitly
models their pre-fix collapsed timestamp, and verifies that the full 1000-event
gpu snapshot receives a distinct timestamp after assignment.

## Change

- Capture one wall-clock timestamp per aggregation tick and assign every metric
  probe a stable 0--10 ms slot (`schedlatency=0`, `gpu_sched=2`,
  `gpu_fence=7`, `gpu_submit=8`, etc.) before the bulk flush. This preserves
  the ES field schema and one-second windows while giving each probe a unique
  TSDB timestamp/identity.
- Changed `read_loss_counters()` to return errors instead of silently turning a
  failed per-CPU map read into `None`. Failures now log
  `could not read gpu_sched BPF loss counters` at most once per 30 seconds;
  ordinary nonzero BPF loss counters retain their existing 30-second,
  cumulative-delta warning.

## Expected live signature after deploy

With continuous GPU activity, `gpu_sched` should emit 60 documents/minute,
with roughly one second's events (~1020) in each document. Its timestamps will
remain tick-phased but occupy the stable gpu_sched slot (normally two ms after
schedlatency), while gpu_fence and gpu_submit occupy their own slots. There
should be no TSDB duplicate-conflict losses. Normal healthy operation emits no
loss-counter warning; a BPF loss still logs `gpu_sched BPF loss counters
increased`, and any unreadable counter slot logs the new read-failure line
(each rate-limited to once per 30 seconds).

## Validation

- `cargo check --workspace` — passed.
- `cargo test --workspace` — passed (59 tests).
- `cargo test -p rigsignal-ebpf` from `ebpf/` — passed (18 tests).
- `cargo xtask build-all --release` from `ebpf/` — passed.
- `rustfmt --check` on changed Rust files and `git diff --check` — passed.
