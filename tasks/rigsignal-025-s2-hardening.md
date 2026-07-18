# Task: rigsignal-025-s2-hardening — deep-review findings 1/2/4 (recovery streaming + bounded retention scan)

Session: 2026-07-18-s2-spool. Worktree branch `codex/rigsignal-025-s2-hardening` of
`/home/dev/coding/RigSignal`. Do NOT commit; NO version bumps. Scope: `src/shipper.rs`
only (+ tests). The S2 contract (tasks/rigsignal-025-s2-spool.md) still governs — none
of its behaviors may regress; all 93 existing tests must keep passing unchanged except
where these changes require touching them.

## Finding 1 (HIGH) — recovery must stream, not slurp

`recover_stranded_file` currently `std::fs::read`s the whole `.tmp`, accumulates valid
lines in a second in-memory buffer, and writes a FULL copy of the original into the
quarantine file. Under pathological `.tmp` growth (rotation failing while appends
succeed — the disk-pressure case recovery exists for) this needs ~2× file size RAM and
~2× file size fresh disk, turning recovery into a crash-loop.

Fix:
1. Stream the source with `BufReader` line-by-line (bounded memory; a max-line-length
   guard so a single giant line cannot blow memory — oversize line = malformed).
2. Write valid lines to the staging file AS THEY ARE READ (staging file is already
   flushed+closed before publish — keep that).
3. Kill the full-copy quarantine: after the staging file is successfully published to a
   final, dispose of the source by RENAME — to the `.quarantine` name when any
   malformed/partial content was seen (preserves full forensics at zero copy cost), or
   plain remove when the file was fully valid. All-or-nothing per source is preserved:
   any failure before the final's publish leaves the source `.tmp` untouched.
4. Update the doc comments (accepted-residual note stays valid — the crash window
   between final-publish and source-disposal is unchanged in nature).
5. Partial trailing bytes: with streaming you cannot know "partial" until EOF without a
   trailing newline — treat a final unterminated chunk as malformed/partial exactly as
   today (it must end up in quarantine via the rename path, never in a final).

## Findings 2+4 (MED+LOW) — retention scan must be bounded and timely

`prune_retained_files` currently: (a) when the candidate cache is empty and 1h has
passed, does a FULL `read_dir` + per-entry `metadata` + full `O(n log n)` sort on the
tick path (45k-file burst observed live); (b) the empty-cache + 1h guard means files
becoming eligible just after a scan can overshoot the retention window by up to 1h.

Fix — one mechanism for both:
- Replace the full-scan-then-sort with an incremental design that does BOUNDED work per
  prune call. Suggested shape (yours to refine): keep a persistent scan state; each call
  processes at most `RETENTION_SCAN_BATCH` directory entries (metadata included) and
  deletes eligible ones as found (retention does not strictly need global oldest-first —
  the spec's "oldest-first" intent is drain-order fairness, which a full directory cycle
  per hour still satisfies; document this relaxation in the doc comment referencing
  deep-review finding 2). A full directory cycle must complete within a bounded number
  of calls (45k files / batch), after which the cycle restarts; newly-eligible files are
  then found within one cycle, not one hour (fixes finding 4).
- Keep `RETENTION_PRUNE_BATCH` (max deletions per call) semantics.
- No full-directory sort on any tick. No unbounded Vec of all entries. (A bounded
  per-cycle read_dir iterator held across calls is fine.)

## Tests

- Streaming recovery: a source with valid + malformed + oversize-line + unterminated
  tail publishes exactly the valid lines once, source renamed to quarantine, staging
  gone. A fully-valid source → final published, source removed, NO quarantine.
- Existing recovery tests (incl. disk-full retain, orphaned-staging sweep) must pass —
  adapt internals only where the new disposal-by-rename changes observable file layout
  (quarantine now contains the ORIGINAL bytes — which it already did — so assertions
  should mostly hold).
- Retention: with batch sizes shrunk via test constants/params, prove (a) per-call work
  is bounded, (b) a file that becomes eligible after the first pass is deleted within
  one full cycle, (c) deletions still capped per call.

## Acceptance

- `cargo check` + `cargo test` green (`--manifest-path src/Cargo.toml`).
- `git diff` confined to `src/shipper.rs`.
- Summary: each finding → fix location; note any observable behavior change vs the S2
  contract wording.
