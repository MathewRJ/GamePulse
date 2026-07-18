# Task: rigsignal-025-s1-daemon — slot-table retirement + unknown-probe fail-closed (0.2.5 S1, daemon side)

Session: 2026-07-18-s2-spool. Workspace: the git worktree you are launched in (branch
`codex/rigsignal-025-s1-daemon` of `/home/dev/coding/RigSignal`). Do NOT commit; do NOT
bump any version field (Cargo stays 0.2.4 everywhere — release bump comes later).

## Contract (RIGSIGNAL-025-SPEC.md §S1, daemon slice — verbatim decisions)

- **D3 (revised per verdict 10) — unknown probes fail closed.** `probe` is a compile-time
  `&'static str` (`es_model.rs:125`); an unrecognized value means version skew, not
  runtime input. An unsupported probe document is DROPPED with a rate-limited structured
  diagnostic (never emitted verbatim, never merged into a shared `unknown` series — two
  unknown probes in one tick would collide on `(host, session, unknown, ts)`). Budget:
  exactly the 10 named probes = **max 10 series per (host, session)**; adding a probe
  requires a spec review and budget increment.
- Choreography step 4 (deploy is the ORCHESTRATOR's job, not yours): daemon ships with
  `metric_timestamp_offset_ms` retired; all probe docs share the tick timestamp. The
  new-daemon/old-mapping combination is unsafe (409 create-drops at
  `ebpf/rigsignal-ebpf/src/shipper.rs` bulk create) — which is why deploy waits for the
  package rollover. Your code change is still unconditional (no runtime mapping check).

## The change (all in `ebpf/rigsignal-ebpf/`)

1. `src/main.rs` (~line 46): DELETE `metric_timestamp_offset_ms` and its use in
   `assign_metric_timestamps` — every metric doc gets exactly `tick_timestamp`.
2. Fail-closed validation at document assembly/shipping: a probe value outside the 10
   named probes (schedlatency, bio, gpu_sched, mem, futex, irq, vfs, gpu_fence,
   gpu_submit, stutter_correlation) causes the DOC to be dropped (never shipped) with a
   rate-limited structured warning (e.g. once per probe value per N minutes — pick a
   simple mechanism; document the rate). The unknown value must not appear verbatim in
   the log line at unbounded rate NOR create any ES series.
3. Keep the doc-comment context accurate: the old comment explains the slot workaround —
   replace it with the dimension-based rationale (probe is a TSDS dimension as of
   package 0.5.0).

## Tests (acceptance B2, unit level)

- All probe docs in one tick share one timestamp (no offsets).
- An unrecognized probe id is dropped with the diagnostic — including the
  two-unknown-probes-in-one-tick case (both dropped, no shared series, no panic).
- The 10 named probes all pass validation (budget test: ≤10 series inputs).
- Existing daemon tests keep passing.

## Acceptance criteria

- `cargo check` + `cargo test` green for the ebpf workspace
  (`--manifest-path ebpf/Cargo.toml`, default-member rigsignal-ebpf).
- `git diff` confined to `ebpf/rigsignal-ebpf/src/`.
- Summary maps each contract point to its implementation + full test output tail.

## STM contract

Before starting: `CHRONO_SESSION=2026-07-18-s2-spool bash /home/dev/coding/Workflow/scripts/stm.sh recall`.
Save non-obvious discoveries via `stm.sh save … --kind learning` (STM_AGENT=codex@nuc).
If STM is unreachable from your sandbox (network blocked), note it once and proceed.
