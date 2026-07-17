# rigsignal-023-seed-running-games RESULT

## Root-cause verdict
- H1 confirmed: pre-change `session.rs:90-99` walked only recorded `game_pids`; a dead/reparented wrapper root returns no task entries, leaving an empty seed.
- H2 confirmed: pre-change `session.rs:59-87` read only `/proc/<pid>/task/<pid>/children`; children launched by another Wine/Proton thread were missed.
- H3 partial: pre-change `session.rs:60` capped collection at 256 while `GAME_PIDS` accepts 1024 (`probes/sched.rs:40-68`); this loses coverage but cannot alone produce zero docs.
- H4 confirmed: pre-change timeout path at `session.rs:199-211` refreshed only when inactive; `SchedProbe::update_pids` is event-driven and BPF has no fork tracking, so later TIDs stayed invisible.

## Changes
- `ebpf/rigsignal-ebpf/src/session.rs`: bounded (32,768 PID) SteamGameId/SteamAppId environ fallback; recorded/environ/union source logging; per-thread children traversal; sorted 1024-TID collection; changed-only 30-second active refresh logging.
- Added fixture tests: `dead_recorded_pid_uses_matching_environ_process`, `walks_children_of_every_thread`, `collects_up_to_game_pid_map_capacity`, and `refresh_detects_added_tid`.
- No BPF crate or probe ELF changes.

## Verification
- `cd ebpf && cargo test` — exit 0; 6 passed, 0 failed.
- `cd ebpf && cargo check` — exit 0.
- `cd ebpf && cargo fmt -p rigsignal-ebpf -- --check` — exit 0.
- `cargo check` at repository root — exit 0.
- `git diff --check` — exit 0.

## Deviation
- Bare `cd ebpf && cargo fmt --check` exits 1 because of pre-existing unrelated formatting in `ebpf/xtask/src/main.rs`; it was not changed because task scope permits only daemon files. Full output:
```
Diff in /home/dev/coding/RigSignal/worktrees/codex-023-seed-running/ebpf/xtask/src/main.rs:65:
     let mut args = vec![
         "+nightly",
         "build",
-        "-p", "rigsignal-ebpf-probes",
-        "--target", "bpfel-unknown-none",
-        "-Z", "build-std=core",
+        "-p",
+        "rigsignal-ebpf-probes",
+        "--target",
+        "bpfel-unknown-none",
+        "-Z",
+        "build-std=core",
     ];
 
     if release {
```
- STM recall/save calls were socket-sandbox-blocked (`curl: (7) failed to open socket: Operation not permitted`).
