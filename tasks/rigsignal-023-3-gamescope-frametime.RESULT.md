# rigsignal-023-3 — RESULT

Status: complete (not committed, as required).

Changed `src/collectors/linux/gamescope.rs`: gamescope now derives frametimes
from valid FPS samples (`1000.0 / fps`), emits 3-dp `fps.frametime_ms`, and
always emits `fps.stutter_count` using the MangoHud `ft > 2 × avg_ft` rule.
The module documentation identifies these as sample-derived approximations;
the existing FPS fields retain their previous calculations.

Added tests:
- `collect_emits_sample_derived_frametime_and_stutter_count`: 60, 60, 10 FPS
  emits 44.444 ms and one stutter while checking existing FPS fields.
- `collect_ignores_non_positive_fps_when_deriving_frametime`: zero and negative
  FPS do not participate in derived frametime calculations.

Verification (all exit code 0):
- `cargo test gamescope` tail: `2 passed; 0 failed; 50 filtered out`.
- `cargo test` tail: `52 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out`.
- `cargo check` tail: `Finished dev profile [unoptimized + debuginfo]`.
- `cargo fmt --check`: clean (no output).
- `git diff --check`: clean (no output).

Deviations: none. Live SteamOS validation remains out of scope.
