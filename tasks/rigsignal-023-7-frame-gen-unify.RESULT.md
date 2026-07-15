# rigsignal-023-7-frame-gen-unify — RESULT

Status: complete

Changed `src/profiles.rs` and `src/dllscan.rs` so both emit
`rigsignal.settings.frame_gen` as `{ "tech": "<value>" }`. Added direct JSON-shape
tests for the profile and DLL-scan overlay emitters; the scanner uses a small
private path-level helper so the production overlay construction is testable.

Updated `docs/metrics-reference.md` with the canonical `.frame_gen.tech` shape and
a migration note: scalar documents predate 0.2.3 and need reindexing or rollover
before mixed mappings share a data stream.

Verification (all exit 0):
- `cargo test`: 53 passed, 0 failed. Includes
  `profiles::tests::test_profile_overlay_has_source_and_confidence` and
  `dllscan::tests::test_overlay_emits_frame_gen_tech_object`.
- `cargo check`: `Finished dev profile [unoptimized + debuginfo]`.
- `cargo fmt --check`: clean.
- `rg -n 'frame_gen' docs`: canonical metrics field and migration note present.
- `git diff --check`: clean.

Deviations: none. No commit created.
