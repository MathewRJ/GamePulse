# gpu_sched legacy-tracepoint port — result

Implemented the Item 9 port without committing.

## Delivered

- BPF now provides both renamed (`drm_sched_job_queue`/`drm_sched_job_run`) and
  Valve 6.16 legacy (`drm_sched_job`/`drm_run_job`) tracepoint pairs. Both read
  their u64 map key at the runtime offset in `GPU_SCHED_KEY_OFFSET`; no hardcoded
  key offset remains.
- Userspace selects one complete pair (preferring renamed when both are present),
  parses each selected `format` file independently, validates a uniquely named
  8-byte `fence_seqno` or `id`, rejects mismatched offsets, configures the map,
  and logs `variant`, `key_field`, and `key_offset` in one `info!` record.
- Added a dependency-free parser with the two supplied Valve fixtures, documented
  synthetic renamed fixture, malformed, duplicate-key, and wrong-size fixtures.
  Tests cover each parser rejection and pair-offset mismatch.
- Added root-only bounded ftrace capture and stdlib-only reference parser scripts.
  The parser reports pair count, min/mean/max microseconds, and the daemon's exact
  16 histogram values/counts arrays.

The BPF source documents that legacy `id` and renamed seqno-only keys are not
globally unique, preserving the existing statistical collision limitation.

## Validation

- `cargo check` (repository userspace): passed.
- `cargo test` (repository userspace): passed, 59 tests.
- `cargo check -p rigsignal-ebpf`: passed.
- `cargo test -p rigsignal-ebpf`: passed, 14 tests.
- `cargo xtask build-ebpf`: passed with nightly and `bpf-linker`.
- Changed Rust files pass `rustfmt --check`; `bash -n` and `python3 -m py_compile`
  pass for the new scripts; static reference-parser sample passed.
- `cargo fmt --all -- --check` in `ebpf/` still reports only the pre-existing,
  out-of-scope formatting drift in `ebpf/xtask/src/main.rs`. It was not modified.
- No live tracefs capture was run (this task requires static/unit validation only).

STM status saved as `qSyRcZ8BTyUckH-jc52h`.
