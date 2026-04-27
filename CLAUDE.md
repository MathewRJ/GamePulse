# GamePulse — Claude Code Project Instructions

## Project state

`docs/STATUS.md` is the single source of truth. At session start: use `recall_memory("GamePulse project state")` or read the file directly. Update it after every completed work package. This file contains rules only — no state.

Full reference (file locations, hardware, skills, dashboards, test suite): `docs/claude-reference.md` — load only when needed for the specific task, not by default.

---

## What this project is

GamePulse is an open-source gaming performance telemetry platform collecting, shipping, and visualising real-world gaming metrics to Elasticsearch. Audience: game developers, journalists, Proton/Wine/Mesa maintainers, package maintainers.

**Stack:** Rust agent (`src/`), Python prototype (`collector/`), eBPF daemon (`ebpf/`), Elasticsearch Serverless + Kibana. Hardware: AMD GPU (Linux primary), Steam Deck, NVIDIA (community). Packaging: AUR (done), Debian/RPM (Milestone D), Windows MSI (Milestone E).

**Remote access:** Elasticsearch via `$ES_URL` / `$ES_API_KEY`. Gaming PC via `ssh gamingpc` (CachyOS, AMD GPU, MangoHud).

## Session hygiene

- Always run `git pull` before starting any work.
- Always run `git push` immediately after every commit.
- Never start implementation if `git status` shows unpushed commits or branch is behind `origin/main`.
- If the branch has diverged, stop and flag it before doing anything else.

## Workflow rules

1. One task at a time. No opportunistic refactors.
2. No dependency version changes unless the task explicitly requires it.
3. No changes to protected files without a planner-assigned task targeting them.
4. After any pipeline/manifest change: run `elastic-package check` before declaring done.
5. After any Rust code change: run `cargo check` before declaring done.
6. Reviewer must approve before tester runs.
7. Progress auditor runs at every milestone boundary, not every task.

## Protected files — never edit without explicit task assignment

Integration-critical — errors are silent until package validation:

- `manifest.yml`, `tools/deploy_pipelines.py`, `tools/wire_pipelines.py`, `docs/SCOPE.md`
- Any file under `_dev/` or `packaging/`
- Ingest pipeline YAML/JSON files (any path matching `*pipeline*`)
- Index template JSON files, ILM policy JSON files

## Validation commands (only approved test commands)

```
elastic-package check
elastic-package test static
elastic-package test system   # requires local ES or Docker
cargo check
cargo clippy -- -D warnings
cargo test
cargo build --release
```

Do not run any other commands that modify the repo, network, or filesystem without explicit user approval.

For package builds use `bash scripts/build-package.sh` (not `elastic-package build` directly); for asset tests use `bash scripts/test-asset.sh`. See `docs/claude-reference.md` for details.

## Agent routing

Use the cheapest capable agent for each task type:

| Task | Agent |
|---|---|
| Read file + extract value, summaries, boilerplate code | haiku-worker |
| Large file reads (SCOPE.md, HANDOFF.md), web research, multi-file scans | gemini-researcher |
| Open-ended codebase exploration spanning many files | Explore subagent |
| Mechanical file edits matching a clear pattern (Codex pipeline) | Codex (worktree) |
| Code changes, Rust edits, judgment calls requiring context | Sonnet (main) |
| Architecture strategy, high-level planning | Opus (ultrathink only) |

Before any large file read: call `recall_memory("topic")` first — if the answer is in ES memory, no file read needed.

## Codex pipeline

For mechanical implementation tasks: claude.ai plans → Codex implements in `worktrees/codex-<task-id>/` → Claude Code verifies → user merges.

See `docs/AGENT-COLLABORATION.md` for the full pipeline, prompt templates, work-package format, and guardrails. Worktrees are gitignored; branch naming is `codex/<task-id>`.

## Grep-first rule

For any file over 100 lines: Grep for the specific content first, then Read only the matching lines using offset/limit. Never read `docs/SCOPE.md` (~1700 lines) in full — delegate to gemini-researcher or use targeted Grep.

## Cross-session continuity

- `docs/STATUS.md` — single source of truth. Both claude.ai and Claude Code read at session start and write after each WP completion.
- `docs/HANDOFF.md` — narrative history. Prefer `recall_memory("GamePulse [topic]")` over reading the full file.
- Claude Code must never edit planning context docs that belong to claude.ai.
- claude.ai must never directly edit `CLAUDE.md`.
