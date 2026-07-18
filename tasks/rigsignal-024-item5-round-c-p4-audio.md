# Task: rigsignal-024-item5-round-c-p4-audio — P4 PipeWire re-source + docs sweep

Session: 2026-07-18-024-item5. Workspace: the RigSignal git worktree you are launched in.
Do NOT commit — the orchestrator commits after review.

## Contract (read first, follow exactly)

`/home/dev/coding/Workflow/projects/RigSignal/RIGSIGNAL-024-ITEM5-SPEC.md` — section
"P4 — PipeWire audio re-source". Where this task file and the spec conflict, the spec
wins; report the conflict.

## Scope

Replace ONLY the PipeWire `pw-top -b` statistics source. The cached
`pactl get-default-sink` / `pactl list sinks` path and its fields stay unchanged.

- Run exactly `pw-metadata -n settings 0` (stdout captured, stderr discarded, 2000 ms
  timeout) through the existing `run_cmd` helper. Cache success AND failure for 5 s,
  same as the existing audio caches.
- Parse the FINAL valid `update:` value for exactly these integer keys: `clock.quantum`,
  `clock.rate`, `clock.force-quantum`, `clock.force-rate`. Values must be positive
  integers, except zero is valid only for `force-*`. Effective quantum = positive
  force-quantum else positive quantum; effective rate same precedence. Zero/negative/
  malformed/missing force values do NOT override base. Missing/non-positive required base
  → omit dependent output; NO fallback to pw-top/pw-dump/pactl for these settings.
- Emit `rigsignal.audio.quantum` (frames) and `rigsignal.audio.sample_rate_hz` (Hz) from
  effective values; when both valid, `rigsignal.audio.latency_ms =
  round(quantum/rate*1000, 2)` — documented as CONFIGURED scheduling latency.
  Live fixture (re-confirmed on the client 2026-07-18): quantum 512, rate 48000, both
  force keys 0 → latency 10.67.
- REMOVE the PipeWire xrun feature entirely: `pw-top` execution/parsing,
  `PipewireStats`, `prev_xruns`, its cache, and its tests. `rigsignal.audio.xruns` is
  never emitted on any PipeWire tick. Removal is truthful (no systemd-visible xrun
  source), not a zero-valued replacement.

## Docs sweep (agent repo)

- `docs/metrics-reference.md`: match rows by CONTENT, not line numbers (spec cites
  298–301). xruns row removed; quantum/sample-rate/latency rows name the `pw-metadata`
  source and configured-latency meaning; add rows for the new
  `rigsignal.stream.client.*` metric gauges + `video_engine`, the Remote Play event
  fields on the events stream, and the peer-fields local-only privacy warning
  (per spec "Privacy boundary").
- `docs/QA-MATRIX.md` + `docs/STATUS.md`: update claims identifying `pw-top`/PipeWire as
  an xrun source.
- README (agent repo) if it claims xruns.

## Tests

Parser fixtures: the four live lines (quantum 512 / rate 48000 / force 0 / force 0),
nonzero force overrides, zero-force fallback to base, malformed/missing values, and the
512/48000 → 10.67 latency calculation. Existing pactl sink parser tests remain and pass
unchanged.

## Gates you run (report honestly)

`cargo check` and `cargo test` from the worktree root; exact pass/fail counts.

## STM contract

Before starting: `CHRONO_SESSION=2026-07-18-024-item5 bash /home/dev/coding/Workflow/scripts/stm.sh recall`.
On completion/discovery: `stm.sh save ... --kind learning|failure|decision|status`
(STM_AGENT=codex@nuc). Return only a condensed summary.
