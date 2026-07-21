# host.name lowercase normalization — result

## Contract

`host.name` is normalized at emission boundaries as trimmed Unicode lowercase via
Rust `str::to_lowercase()`. Empty inputs become `unknown`.

Userspace normalizes the OS hostname in `src/host.rs::hostname()` for both Unix
and Windows, and its session, stream-client, and direct-event builders use that
same helper. The eBPF daemon normalizes `/etc/hostname` at startup; eBPF
`HostFields` serialization also applies the shared eBPF normalizer so aggregate,
thread, and correlation documents cannot emit a case split.

No historic data migration or dashboard/query change was made.

## Changed files

- `src/host.rs` — canonical userspace hostname helper and injected-input unit test.
- `src/session.rs` — canonical host name in metrics/session and stream-client bases;
  raw document tests.
- `src/remote_connections.rs` — canonical host name in direct stream events and
  raw document test.
- `src/main.rs` — raw metrics, stream-client, session-start, and session-end
  document assertions.
- `ebpf/rigsignal-ebpf/src/main.rs` — normalized `/etc/hostname` plus raw eBPF,
  eBPF-thread, and correlation document assertions.
- `ebpf/rigsignal-ebpf/src/es_model.rs` — canonical eBPF host serialization and
  host-side serialization unit test.

## Verification

- `cargo test --manifest-path src/Cargo.toml` — 99 passed.
- `cargo test --manifest-path ebpf/rigsignal-ebpf/Cargo.toml` — 22 passed.
- `cargo check --manifest-path src/Cargo.toml` — passed.
- `cargo check --manifest-path ebpf/rigsignal-ebpf/Cargo.toml` — passed.
- `cargo build --release --manifest-path src/Cargo.toml` — passed.
- `RUSTUP_TOOLCHAIN=nightly CARGO_TARGET_DIR=<worktree>/ebpf/target cargo xtask build-all --release`
  — BPF probes passed. The pinned `nightly-2026-07-18` could not initialize because
  the sandbox prevents writes to `/home/dev/.rustup`; the already-installed nightly
  completed the equivalent BPF build. The daemon was then built successfully with
  the same worktree-local target directory.

## Candidate build artifacts (not attested)

| Artifact | SHA-256 |
| --- | --- |
| `target/release/rigsignal-agent` | `3a32630dc331768056b1ad4456e0baebfef49bdf92fda7c517096f3cd40a96b7` |
| `ebpf/target/release/rigsignal-ebpf` | `0a45623de5a4e526c14f9539d3faf741135e044bbdbc21d83017ef2bea8bc99d` |
| `ebpf/target/bpfel-unknown-none/release/rigsignal-ebpf-probes` | `ddf8199e9fe6935b43ca546ac4e1db35c7cee9334c068894817b12ae83ac0bdb` |
