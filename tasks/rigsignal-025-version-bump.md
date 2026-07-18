# Task contract: rigsignal-025-version-bump

Pre-stage the 0.2.5 release bump. This branch will NOT be merged or tagged until the
A6-24h gate passes (wall-clock, 2026-07-19T17:52:04Z+) — so the CHANGELOG entry must not
claim the A6-24h attestation row; the S2 gate-close wording lands at merge time if needed.

## Scope

1. **Version-field inventory first** (record it in your final summary): version bumps go
   to exactly these manifests, 0.2.4 → 0.2.5:
   - `src/Cargo.toml`
   - `ebpf/rigsignal-ebpf/Cargo.toml`
   - `ebpf/rigsignal-ebpf-probes/Cargo.toml`
   `ebpf/xtask/Cargo.toml` stays at 0.2.1 (build tooling, deliberately unsynced).
   Lockfiles: refresh via cargo (do not hand-edit). Historic `0.2.4` strings in
   CHANGELOG/docs/evidence stay untouched.
2. **CHANGELOG.md**: convert `[Unreleased]` into a `## [0.2.5] — 2026-07-19` release
   entry, following the existing 0.2.4 entry's style. Derive content from
   `git log 84cef47..HEAD --oneline` (everything since the 0.2.4 release commit):
   - S2 spool durability (shutdown finalization, eager startup recovery, retention
     pruning, single-writer lock) — the existing Unreleased bullet, expanded.
   - S2 hardening (deep-review findings 1/2/4): streaming recovery with 1 MiB line
     bound, quarantine-by-rename (no full-copy), bounded incremental retention scan
     via persistent directory cursor.
   - S1: probe as TSDS dimension — slot-table offsets retired, unknown probes fail
     closed (`0e34817`).
   - Fix: accept Steam's colon-reason suffix on disconnect lines (`69b838d`).
   - Dashboards: streaming-lab rows for 0.2.4 stream_client telemetry (`4bf8145`).
   Keep a fresh empty `[Unreleased]` section at top.
   Do NOT write anything implying the 24h retention check already passed.
3. **Build validation**: `cargo check --manifest-path src/Cargo.toml` and
   `cargo check --manifest-path ebpf/Cargo.toml` (use the ebpf workspace's usual
   toolchain — see Makefile/CI if unsure); then a release-build rehearsal:
   `cargo build --release --manifest-path src/Cargo.toml`. Record the built agent's
   sha256 in your summary (rehearsal only — the release artifact is rebuilt at tag time).

## Constraints
- No commits. No tags. Diff confined to: the three Cargo.toml files, lockfile(s),
  CHANGELOG.md.
- No opportunistic edits, no dependency updates beyond what the version bump itself
  forces into the lockfile.

## Acceptance criteria
- Inventory listed; three manifests at 0.2.5; xtask untouched at 0.2.1.
- cargo check green (src + ebpf); release build green; agent sha256 reported.
- CHANGELOG has [0.2.5] — 2026-07-19 entry covering S2, S2-hardening, S1, 69b838d fix,
  dashboards; fresh empty [Unreleased]; no A6/attestation claims.
- `git diff --stat` confined to the allowed files.
