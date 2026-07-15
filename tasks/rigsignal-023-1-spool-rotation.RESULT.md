# RESULT: rigsignal-023-1 spool rotation timer

Status: implementation and verification complete; commit blocked by read-only git metadata.

Changed:
- `src/shipper.rs`: added `SpoolWriter::rotate_stale_files`, which scans open per-dataset spools and rotates files with pending bytes once `max_file_age_secs` is reached.
- `src/main.rs`: calls the stale rotation pass on every 1-second main tick, independent of whether the tick produced docs.
- `src/shipper.rs`: added `rotate_stale_files_rotates_pending_file_without_new_writes`, covering a pending tmp file that rotates after idle time with no second write and does not double-rotate an empty replacement tmp.

Verification:
- `cargo test` exit 0; tail: `test result: ok. 49 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 2.21s`
- New test observed in suite: `shipper::tests::rotate_stale_files_rotates_pending_file_without_new_writes ... ok`
- `cargo check` exit 0; tail: `Finished dev profile [unoptimized + debuginfo] target(s) in 7.85s`
- `cargo fmt --check` exit 0; no output.

Deviations:
- Commit could not be created: `git add src/main.rs src/shipper.rs tasks/rigsignal-023-1-spool-rotation.RESULT.md` exited 128 with `fatal: Unable to create '/home/dev/coding/RigSignal/.git/worktrees/codex-023-1-spool-rotation/index.lock': Read-only file system`.
