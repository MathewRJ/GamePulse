# D6 display mode-override detector result

## Implementation

- Added the pure Rust D6 detector and Linux-only live collector in
  `src/detectors/d6.rs`, plus the detector module root.
- Added `rigsignal-agent diagnose display` before `Config::load()`. Offline replay
  requires both fixture paths; incomplete conditions return stderr/exit 2, findings
  exit 1, and `ok`/typed `not-applicable` exit 0.
- Copied the specified D6 captures and synthetic discriminator fixtures to
  `fixtures/d6/`. The read-only Workflow reference directory was not modified.
- Preserved the existing `rigsignal-agent diagnose --output` dispatch. A manual
  regression invocation wrote the usual `=== RigSignal Diagnostic Report ===` and
  exited 0.

Fixture replay/manual CLI checks covered degraded (0.9, exit 1), healthy `ok`
(exit 0), invalid (exit 1), one offline flag (exit 2), explicitly missing fixture
(exit 2), and JSON `not-applicable` (exit 0). Nine dependency-free CLI integration
tests now cover Clap nesting, every required exit-code class, one-line diagnosis
and typed-not-applicable JSON, human contract fields, and legacy `diagnose --output`
behavior. The Rust suite has 119 passing tests, including hardening regressions
for malformed DRM with an empty modes file, oversized fixtures, and non-regular
fixture paths.

## Live-replay verification (orchestrator-owned)

Not run by this implementation worker. The orchestrator must perform Work step 4
on `.254`, including preflight, candidate hash, degraded (0.85/exit 1) and restored
healthy (ok/exit 0) commands, EXIT-trap restoration proof, and before/after SHA-256.

## Gates

- `cargo fmt --check`: PASS
- `cargo clippy --locked --all-targets -- -D warnings`: PASS
- `cargo check --locked`: PASS
- `cargo test --locked`: PASS (119 passed, 0 failed)
- Linux CI feature gates (`cargo check` and clippy with `--features ebpf`): PASS
- `bash scripts/smoke-test.sh ./target/debug/rigsignal-agent`: FAIL in this sandbox:
  the pre-existing smoke check could not observe the three required live network
  metrics. All other smoke checks passed. The Windows CI job cannot run locally
  because only the Linux Rust target is installed; it remains for CI.
- STM recall/save: skipped because `stm.sh` cannot open its network socket in this
  sandbox.
- Commit: `b76ad2e` (`feat(d6): display mode-override detector — Rust port +
  diagnose display CLI`) was created by the orchestrator.

## Threat model / accepted risk

The D6 CLI runs with the invoking user's privileges and intentionally trusts that
user's session environment (`PATH` and `HOME`). A hostile inherited environment is
out of scope for this user-invoked diagnostic.

## Changed files

- `src/detectors/mod.rs`
- `src/detectors/d6.rs`
- `src/main.rs`
- `fixtures/d6/**`
- `src/tests/d6_display_cli.rs`
- `tasks/d6-display-mode-detector.RESULT.md`
