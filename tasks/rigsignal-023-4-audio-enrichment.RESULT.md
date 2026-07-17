# rigsignal-023-4 — RESULT

Status: complete

Changed `src/collectors/linux/audio.rs` only (plus this handoff). PipeWire
collection now caches `pactl get-default-sink` and `pactl list sinks` for five
seconds, parses the selected/default-or-first sink, and emits available sink,
profile, format, channel, rate, latency, and quantum values. Added canned
`pactl` parser tests for full fields, profile flip, fallback, and malformed
input; existing field behavior is retained.

Verification (all exit 0):
- `cargo fmt --check` — clean.
- `cargo test` — `test result: ok. 59 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out`.
  New tests: `parses_default_sink_fields_from_full_block`,
  `profile_flip_uses_matching_default_sink_block`,
  `falls_back_to_first_sink_without_default_sink`, `omits_malformed_sink_fields`.
- `cargo check` — `Finished dev profile [unoptimized + debuginfo] target(s) in 0.36s`.
- `git diff --check` — clean.

Deviations: no code-scope deviations and no commit attempted. Required STM
recall and completion save were attempted but the sandbox denied the curl socket:
`curl: (7) failed to open socket: Operation not permitted`.
