# Task: rigsignal-0.2.4-gpu-sched-legacy — gpu_sched legacy-tracepoint port (item 9)

Session: `CHRONO_SESSION=2026-07-17-024-kickoff`. You are `codex-impl@dev`, working in the
pre-created worktree `worktrees/codex-024-gpu-sched` (branch `codex-024-gpu-sched` off main
f03f8b6). Git metadata is read-only in your sandbox — do NOT commit; the orchestrator
commits after review. Write your summary to
`tasks/rigsignal-0.2.4-gpu-sched-legacy.RESULT.md` in the worktree.

> Before starting: `CHRONO_SESSION=2026-07-17-024-kickoff bash /home/dev/coding/Workflow/scripts/stm.sh recall --all-sessions --last 30`. On completion and on any non-obvious discovery: `stm.sh save … --kind learning|failure|decision|status` (STM_AGENT=codex-impl@dev). If sandbox blocks curl, put learnings in the RESULT instead. Return only a condensed summary — detail goes in STM/RESULT.

## Design authority (read first)

`/home/dev/coding/Workflow/projects/RigSignal/RIGSIGNAL-023-ITEMS-5-9-DESIGN.md` —
§Item 9 "Port design" + **Addendum 2026-07-17c §A9.2-R**. That document governs; this pack
adds the verified facts and file-level scope. A9.x only — item 5 is a separate task.

## Verified facts (live, valve 6.16.12-drmexec7, captured 2026-07-17 via root read)

Both legacy events (`drm_sched_job` ID 1892, `drm_run_job` ID 1891) have IDENTICAL layout:
`entity` ptr @8, `fence` ptr @16, `__data_loc char[] name` @24 (size 4), **`uint64_t id`
@32 size 8**, `u32 job_count` @40, `int hw_job_count` @44. Full format files are fixtures:
- `/home/dev/coding/Workflow/tasks/fixtures/valve-6.16-drm_sched_job.format`
- `/home/dev/coding/Workflow/tasks/fixtures/valve-6.16-drm_run_job.format`

COPY both into this repo's test tree as fixtures (place beside the parser's tests).
Note: legacy `id` @32 coincides with the new-name variant's hardcoded `fence_seqno` @32 —
this is a coincidence and must NOT be relied on; A9.1 forbids surviving hardcoded offsets.

## Scope

1. **BPF probes crate** (`ebpf/rigsignal-ebpf-probes/src/gpu_sched.rs`): add legacy
   tracepoint program pair `drm_sched_job` (≙ queue) / `drm_run_job` (≙ run), map key =
   u64 `id` read at an offset supplied via a config map (not hardcoded). Convert the
   existing new-name pair to read `fence_seqno` via the same config-map mechanism (its
   current hardcoded offset 32 is unverified and carries identical drift risk). Both
   variants share `GPU_SCHED_TS`/`GPU_SCHED_EVENTS` and the same `GpuSchedEvent` output —
   aggregator/es_model untouched, emitted ES fields identical.
2. **Userspace attach** (`ebpf/rigsignal-ebpf/src/probes/gpu_sched.rs`): at attach time,
   probe `/sys/kernel/tracing/events/gpu_scheduler/` for which name pair exists; parse the
   chosen pair's `format` files; match the key field **by name** (`id` legacy /
   `fence_seqno` new); verify size==8; populate the config map; attach that variant only;
   `info!` one line containing the chosen variant, key field name, and parsed offset
   (grep-able — this line is a live acceptance criterion). Warn-skip (existing path) when:
   neither pair exists, key field absent, ambiguous (matched twice), size mismatch, or
   format unparseable. The two events of a pair are parsed independently; mismatched
   offsets between them → warn-skip (do not attach half a pair).
3. **Format parser**: small, dependency-free, unit-tested against the two real fixtures
   plus: a synthetic new-name fixture (construct from the documented new layout; mark
   synthetic in a comment), a malformed file, an ambiguous-duplicate-field file, and a
   wrong-size file. Rejection paths all tested (A9.2-R item 4).
4. **Reference validation tooling** (A9.2-R items 1-2, used later at live acceptance):
   `scripts/gpu-sched-ftrace-reference.sh` (root; enables both legacy events via tracefs
   for N seconds, captures trace_pipe to a file) + `scripts/gpu-sched-reference-parse.py`
   (python3 stdlib only; pairs sched→run by `id`, outputs count + min/mean/max + the same
   16 log2 latency buckets the daemon uses). Static/unit-test only here — no live run.

## Constraints

- No schema, dashboard, manifest, or docs changes in this task. No opportunistic refactors.
- `cargo fmt` + `cargo check` + full `cargo test` green in the worktree (userspace).
  Build the probes ELF with the established nightly + bpf-linker xtask path if the sandbox
  permits; if the sandbox blocks it, say so in the RESULT — the orchestrator rebuilds.
- Do not touch files outside: the two gpu_sched source files, the parser module + its
  tests/fixtures, the two new scripts, and your RESULT file.
