# TK-1 — ES/Kibana compatibility spike (turnkey-readiness slice)

Context: STRATEGY-2026H2.md Amendment 1 (2026-07-22). This is the cheap early leg of the
turnkey critical path: prove (or disprove) that the canonical dashboard NDJSON imports
cleanly at both candidate stack endpoints BEFORE the asset bundle (TK-2) or clean-stack
matrix (TK-3) are built. Authoring only — the harness will be EXECUTED by the orchestrator
on the NUC (docker is available there; it is NOT available in your sandbox — do not try
to run containers, write the scripts so they are runnable as-is).

## Deliverables (new files only; do not modify existing files)

1. `scripts/clean-stack/spike.sh` — the spike runner.
2. `scripts/clean-stack/lib.sh` — shared helpers (container lifecycle, waits, teardown)
   written for reuse by the later TK-3 matrix script.
3. `scripts/clean-stack/README.md` — usage + what the spike proves/does not prove.

## Requirements

- `spike.sh ES_VERSION [KB_VERSION]` — exact patch tags (e.g. `9.4.3`); KB defaults to
  ES_VERSION. No `latest` accepted: reject non-`X.Y.Z` args.
- Plain `docker run` (no compose — compose v2 plugin absent on the target box).
- Ephemeral + isolated per run: unique suffix in container/network names, randomized
  published ports (or fixed high ports parameterized via env), NO named volumes, teardown
  removes containers + network; a `--keep` flag skips teardown for debugging. Trap-based
  cleanup on failure. Never touch a container/network the script did not create.
- Single-node ES (`discovery.type=single-node`), security ENABLED (matches real installs):
  set the elastic password via env, wait for green/yellow health with a bounded timeout;
  then Kibana wired to it via a service token or `kibana_system` password (choose the
  simplest correct mechanism for vanilla images), wait for Kibana `status.overall.level ==
  available` with bounded timeout. On timeout: dump the last 50 container log lines
  (sanitized: never echo passwords/tokens) and fail.
- Record and print: exact image tags AND repo digests (`docker inspect` RepoDigests) for
  both images.
- Import EVERY `dashboards/v0.3.1/*.ndjson` via the Kibana saved-objects `_import` API
  (`overwrite=true`). Produce a per-file result table: file, successCount, errors (type +
  id + error.type for each failure). The spike SUCCEEDS if the stack boots and the report
  is produced — import failures are FINDINGS, not script failures; exit 0 with findings,
  exit 1 only on infrastructure failure (boot/timeout/API unreachable).
- After import, run one smoke ES|QL query via `/_query` (`FROM .kibana* | LIMIT 1` is NOT
  acceptable — use a harmless query against a created probe index you ingest one doc into)
  to prove the ES|QL API path works at that version; include result in the report.
- Machine-readable report: also write `spike-report-<ES_VERSION>.json` (image digests,
  per-file import results, esql probe result, timestamps) to the working directory.
- Bash strict mode, shellcheck-clean, no dependencies beyond bash/curl/jq/docker.
  NOTE: repo hook context blocks nothing for you here, but the ORCHESTRATOR's shell blocks
  `curl` — that block does NOT apply inside scripts run via bash file execution; still,
  keep all curl calls inside the scripts (never require the operator to hand-run curl).

## Acceptance criteria (binary)

- AC1: `bash -n` passes on both scripts; `shellcheck` reports no errors (warnings OK, note them).
- AC2: `spike.sh` rejects `latest`, missing args, and malformed versions with usage text.
- AC3: dry-run mode (`--dry-run`) prints the exact docker commands without executing —
  the orchestrator will review these before first live run.
- AC4: teardown path is provably symmetric with creation (same names derived from one
  suffix variable) and runs on EXIT trap.
- AC5: README documents: what the spike proves, the two candidate endpoints (min 9.4.3 =
  production-proven; max = newest GA 9.x, resolved by the operator at run time), and that
  publishing a supported range is TK-4's job, not TK-1's.

## Constraints

- New files only. No changes to docs/, packaging/, src/, dashboards/.
- Commit on the current branch (codex/tk1-spike) with a conventional message.
- Your final message: condensed summary only — files created, AC status, any deviations.
