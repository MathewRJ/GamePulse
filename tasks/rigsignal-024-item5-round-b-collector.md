# Task: rigsignal-024-item5-round-b-collector — streaming_client utilization collector

Session: 2026-07-18-024-item5. Workspace: the RigSignal git worktree you are launched in.
Do NOT commit — the orchestrator commits after review.

## Contract (read first, follow exactly)

`/home/dev/coding/Workflow/projects/RigSignal/RIGSIGNAL-024-ITEM5-SPEC.md` — sections
"`streaming_client` utilization collector contract", "Dataset decision" (metric payload
shape), and "No-local-game correlation rule". Where this task file and the spec conflict,
the spec wins; report the conflict.

## Scope

Add Linux-only `src/collectors/linux/stream_client.rs`, export from
`collectors/linux/mod.rs`, register the metric collector only on Linux. Dataset
`rigsignal.stream_client`, `data_stream.type: metrics` on every document.

Key contract points (full detail in the spec — implement ALL of it):
- Candidate discovery: `/proc/<pid>/comm == "streaming_client"` OR basename of first
  NUL-separated cmdline arg equals `streaming_client`. Scan numeric /proc at most once
  per 5 s (cache present AND absent results for the same TTL); 100 ms monotonic scan
  budget; ≤4096 PID entries; ≤4096 bytes of cmdline per candidate.
- Selected-PID cache validated per tick against `/proc/<pid>/stat` starttime; missing or
  changed starttime → invalidate cache AND all counter baselines (PID-reuse protection).
- Multiple candidates → newest (largest starttime; largest PID tie-break); never sum
  distinct processes; keep the choice until refresh/invalidation.
- fdinfo: ≤64 numeric fdinfo files sorted numerically, ≤16 KiB each. Dedup FDs by
  `(drm-pdev, drm-client-id)` keeping lowest FD; fallback grouping
  `(drm-pdev, drm-engine-dec, drm-engine-enc, drm-engine-gfx)` when drm-client-id absent
  (prevents the observed FD 28/30 double count). Sum retained dec+enc → video counter;
  retained gfx → gfx counter.
- Deltas: interval from `std::time::Instant` (post-sample to post-sample), never
  wall-clock/tick nominal. Separate prior totals per (PID/starttime, retained-FD identity
  set). First sample / identity change / missing prior / counter decrease / non-positive
  elapsed → new baseline, NO value emitted for that counter. Valid delta →
  `100*delta_ns/elapsed_ns`, 2 dp, clamp [0,100] (debug-count above-100 clamps). Never
  emit negative or null.
- `video_busy_pct` only when ≥1 video engine parseable with valid delta; `video_engine`
  exactly `dec`/`enc`/`dec+enc` reflecting engines in that valid total (RDNA4 client is
  enc-only: emit `enc`, never fabricate `dec`). `gfx_busy_pct` independent.
- No selected process → NO document. Read/parse errors omit that source for the tick,
  never fail the agent tick.
- Base doc: `@timestamp` + `host.name`. Merge `rigsignal.session.{id,label,agent_version}`
  + `rigsignal.game.*` ONLY when `SessionManager.current_game` present at that tick;
  otherwise omit both groups entirely (no `idle-*` association ever).
- **One-metric-doc-per-tick invariant** (design addendum 2026-07-17c): at most one
  stream_client document per tick; add a unit test asserting two same-tick emissions
  cannot occur (TSDS same-ms identity protection).

## Tests (unit; use fixture fdinfo/proc content, no live /proc dependency)

Multi-FD dedup (incl. the FD 28/30 identical-counter case + fallback identity), PID reuse
(starttime change → baseline reset, no emission), tie-break selection, backwards-counter
baseline reset, monotonic delta + clamp, no-video omission (gfx-only doc), enc-only →
`video_engine: "enc"`, no-local-game base construction (session/game groups absent), and
the one-doc-per-tick invariant. NO test may assert an `idle-*` stream session
association. Existing collector tests stay green.

## Gates you run (report honestly)

`cargo check` and `cargo test` from the worktree root; exact pass/fail counts.

## STM contract

Before starting: `CHRONO_SESSION=2026-07-18-024-item5 bash /home/dev/coding/Workflow/scripts/stm.sh recall`.
On completion/discovery: `stm.sh save ... --kind learning|failure|decision|status`
(STM_AGENT=codex@nuc). Return only a condensed summary.
