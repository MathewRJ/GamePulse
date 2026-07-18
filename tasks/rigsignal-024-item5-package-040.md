# Task: rigsignal-024-item5-package-040 — RigSignal-Integration 0.4.0 (item 5, package-first)

Session: 2026-07-18-024-item5. Workspace: the git worktree you are launched in
(branch `codex-040-item5` of `/home/dev/coding/RigSignal-Integration`, cut from main
`c28428e` / package 0.3.1). Do NOT commit — leave all changes in the working tree; the
orchestrator commits after review.

## Contract (read first, follow exactly — no semantic choices remain)

`/home/dev/coding/Workflow/projects/RigSignal/RIGSIGNAL-024-ITEM5-SPEC.md`, sections:
"Integration package mapping contract", "Event document and parser contract" (for fixture
shapes), "Privacy boundary", and the fields.yml paragraph inside "P4 — PipeWire audio
re-source". Where this task file and the spec conflict, the spec wins; report the
conflict.

## Scope — five workstreams

### 1. NEW `data_stream/stream_client/`
- `manifest.yml` per spec **plus a `streams:` filestream section** following the existing
  12 streams' convention exactly (mirror `data_stream/gpu/manifest.yml`; spool path
  `rigsignal-stream_client-*.ndjson`). Missing `streams:` sections broke Fleet at 0.3.0 —
  do not omit.
- `fields/base-fields.yml` + `fields/fields.yml` EXACTLY per spec. TSDS dimensions:
  `host.name` + `rigsignal.session.id` ONLY. Gauges: `video_busy_pct`, `gfx_busy_pct`;
  `video_engine` keyword non-dimension.
- `elasticsearch/ingest_pipeline/default.yml` following the metric-stream convention.
- `sample_event.json`: the common client-only case — NO session/game groups,
  `video_engine: "enc"`, gauges in range.
- `_dev/test/pipeline/` fixtures (input + expected).
- In the dataset docs, state the one-metric-doc-per-tick TSDS invariant (design addendum
  2026-07-17c; the enforcing unit test lands agent-side, not here).

### 2. `data_stream/events/` augment
- `fields/fields.yml`: add the `rigsignal.stream.client.{event,transport,peer.id,peer.name}`
  group per spec — peer field descriptions MUST begin with the literal words
  "Privacy-sensitive". Add `rigsignal.session.label` (keyword, non-dimension) beside the
  existing session id. Do NOT re-declare ECS `event.*` fields (already external).
- **CRITICAL pipeline fix**: `elasticsearch/ingest_pipeline/default.yml` currently throws
  when `rigsignal.event.kind` is missing and unconditionally rewrites `event.type`. Add a
  conditional path: when `ctx.rigsignal?.stream?.client?.event != null`, the document
  passes through with its incoming `event.kind` / `event.category` / `event.type`
  UNTOUCHED; legacy documents (with `rigsignal.event.kind`) keep the existing behavior
  bit-for-bit; documents with neither field keep the existing throw.
- Pipeline fixtures for BOTH shapes (a legacy event + a Remote Play connected AND
  disconnected doc per the spec's JSON examples). `event.category` / `event.type` are
  JSON ARRAYS in every fixture input, expected JSON, and sample_event.json — never
  scalars (standing ECS-array rule).

### 3. `data_stream/audio/` catch-up
- `fields/fields.yml`: REMOVE `xruns` and `buffer_size`. ADD `quantum` (integer, gauge,
  description "effective PipeWire scheduling quantum in frames, from pw-metadata"). Ensure
  `sample_rate_hz` description = effective PipeWire clock rate (or PulseAudio server rate
  on that backend). Change `latency_ms` description to configured scheduling latency (not
  observed/driver/round-trip). ADD pactl sink fields: `sink_name` (keyword),
  `card_profile` (keyword), `channels` (integer, gauge), `sample_format` (keyword),
  `driver_latency_ms` (float, gauge, unit ms).
- Update `sample_event.json` AND pipeline fixtures: no `xruns`/`buffer_size` anywhere;
  `quantum` present; keep values consistent (512/48000 → latency_ms 10.67).

### 4. Docs
- `README.md` data-stream table: add `stream_client` row (client GPU stream utilization);
  events row mentions Remote Play connection events. Add the peer-fields local-only
  privacy warning and the `cfg.privacy`-unconsumed debt note per the spec's Privacy
  boundary section. Fix stale stream-count claims (now 13 data streams).

### 5. Version
- `manifest.yml` version → `0.4.0`; `changelog.yml` entry: stream_client dataset, events
  Remote Play augment + conditional pipeline pass-through, audio field catch-up
  (xruns/buffer_size removed, quantum + pactl sink fields added).

## Gates you run (report results honestly)

From the worktree root: `elastic-package check` and `elastic-package test static` if the
tool runs without network/stack access; if a step needs a live stack or network, SKIP it
and say so — the orchestrator runs those after review. Run nothing that modifies state
outside the worktree.

## STM contract

Before starting: `CHRONO_SESSION=2026-07-18-024-item5 bash /home/dev/coding/Workflow/scripts/stm.sh recall`.
On completion and on any non-obvious discovery:
`stm.sh save "<title>" "<content>" --kind learning|failure|decision|status` with
`STM_AGENT=codex@nuc`. Return only a condensed summary — detail goes in STM.
