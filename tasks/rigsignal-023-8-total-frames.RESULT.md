# rigsignal-023-8-total-frames — RESULT

Status: complete (not committed, as required).

Changed `src/main.rs` to emit `rigsignal.summary.fps_coverage_s` as the i64 count
of collected FPS samples (including zero coverage). `total_frames` remains its
existing rounded sum of FPS samples over the one-second interval.

Added unit tests:
- `tests::summary_total_frames_reports_sparse_fps_coverage`: samples 60, 30,
  and 45 over a 10 s session produce 135 total frames and 3 s coverage.
- `tests::summary_fps_coverage_matches_duration_with_full_coverage`: three
  samples in a 3 s session report matching coverage and duration.

Updated `docs/metrics-reference.md`: `total_frames` is an honest count over
instrumented one-second ticks, not duration × average FPS; `fps_coverage_s` is
its coverage denominator.

Root-cause trace: each tick only enters `tick_docs` when a collector returns a
payload, and `SessionAccumulators::update` pushes an FPS sample only for a
`rigsignal.fps.avg_1s` field. The summary sums those samples, while `duration_s`
uses elapsed wall time; the selected Gamescope/MangoHud frame collector can emit
no FPS document for unavailable or no-sample ticks.

Verification (all exit code 0):
- `cargo test` tail: `55 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out`.
- `cargo check` tail: `Finished dev profile [unoptimized + debuginfo]`.
- `cargo fmt --check`: clean (no output).
- `git diff --check`: clean (no output).

Deviations: none. No commit created.
