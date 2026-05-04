# GamePulse Agent System

This document describes the multi-agent pipeline used to develop GamePulse.
It extends the Codex worktree pipeline already documented in
`docs/AGENT-COLLABORATION.md` (which remains the authoritative Codex
contract — branch naming, protected files, prompt templates) into a full
software-team pipeline mapped onto Claude Code, OpenAI Codex, and Gemini CLI.

## Constraints that shape the design

- **No API keys.** Mat runs on Claude Pro, ChatGPT Plus, and a free Google
  account. All LLM work goes through the local `claude`, `codex`, and
  `gemini` CLIs, which auth via the user's subscriptions. **GitHub Actions
  cannot run any LLM step** — CI is deterministic-only.
- **LLM gates run locally.** Reviewer, security-auditor, integration-auditor
  are invoked by `bin/gpx` on the user's machine. The optional pre-push
  hook (`.githooks/pre-push`, opt-in via `git config core.hooksPath
  .githooks`) wires `gpx audit security` into pushes to `main`.
- **Codex offloading.** Mechanical edits with predictable diff shape are
  routed to Codex (ChatGPT Plus subscription) via `gpx implement <task-id>`.
  This minimises Claude token usage on routine work.
- **Gemini offloading.** Bulk file reads, multi-file scans, and web research
  are delegated to Gemini CLI (free tier). The reusable patterns live in
  `prompts/gemini/research.md`.

## Design principles

1. **Agents only exist if they earn their seat.** Every agent must produce
   an artefact something downstream consumes. Pure orchestrators are
   deleted — `bin/gpx` is the orchestrator.
2. **Roles map to the cheapest capable tool.** Reasoning on Claude.
   Mechanical edits on Codex. Bulk reads / research on Gemini.
3. **Inputs and outputs are files.** Every agent reads from and writes to
   well-known paths. No chat-only state.
4. **Gates are enforced by `gpx`, not by trust.** `gpx ci` chains
   reviewer → tester → security-auditor in order. Skipping a gate requires
   `GPX_FORCE=1` and is logged.

## Role mapping

The 12-role spec from the canonical software-team model collapses to
**11 agents** for GamePulse. Project Manager folds into `planner`. Tech
Lead and Business Analyst fold into `architect`. Observability folds into
`dashboard-designer` because in GamePulse the dashboards *are* the
observability product.

| Spec role             | GamePulse agent                  | Tool                 |
| --------------------- | -------------------------------- | -------------------- |
| Product Manager       | (Mat, via claude.ai)             | claude.ai            |
| Business Analyst      | merged into `architect`          | —                    |
| Project Manager       | merged into `planner`            | Claude Code          |
| UX/UI Designer        | `dashboard-designer`             | Claude Code          |
| Software Architect    | `architect`                      | Claude Code          |
| Tech Lead             | merged into `architect`          | —                    |
| Software Engineer     | `implementer` + Codex pipeline   | Claude Code / Codex  |
| QA / Test Engineer    | `tester`                         | Claude Code          |
| DevOps / SRE          | `devops`                         | Claude Code          |
| Security Engineer     | `security-auditor` (Opus)        | Claude Code          |
| Observability         | merged into `dashboard-designer` | —                    |
| Code Reviewer         | `reviewer` + `integration-auditor` (Opus) | Claude Code |
| Docs                  | `docs-writer` (delegates to Gemini) | Claude Code + Gemini |
| Progress audit        | `progress-auditor` (existing)    | Claude Code          |

Existing agents kept unchanged: `planner`, `implementer`, `reviewer`,
`tester`, `progress-auditor`. New additive agents: `architect`,
`dashboard-designer`, `devops`, `security-auditor`, `integration-auditor`,
`docs-writer`.

## The pipeline

```
                docs/SCOPE.md, docs/ROADMAP.md
                              │
                              ▼
                     planner  (Claude, read-only)
                              │  next task
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        architect     dashboard-designer    implementer
        (data model)    (NDJSON, panels)    (Claude or Codex)
              │               │               │
              └───────────────┼───────────────┘
                              ▼
                     reviewer  (Claude, diff-driven)
                              ▼
                     tester  (cargo + elastic-package)
                              ▼
                     devops  (CI, packaging, release)
                              ▼
              security-auditor  (Opus, ultrathink)
                              ▼
              integration-auditor  (Opus, ultrathink — pre-elastic/integrations PR only)
                              ▼
                       merge / submit
```

`docs-writer` runs out-of-band, triggered when `docs/`, `fields.yml`
description fields, or `changelog.yml` need an update. It delegates bulk
reads to Gemini.

## Tool routing

| Task                                              | Tool         | Entry point          |
| ------------------------------------------------- | ------------ | -------------------- |
| Decide what to do next                            | Claude       | `gpx plan`           |
| Design a data-model or package change             | Claude       | `gpx architect`      |
| Mechanical edit with predictable diff shape       | Codex        | `gpx implement <id>` |
| Edit that needs judgement                         | Claude       | direct invocation    |
| Bulk reads, web research                          | Gemini       | delegated by agents  |
| Diff review                                       | Claude       | `gpx review`         |
| Validation (cargo + elastic-package)              | local CLI    | `gpx test`           |
| Dashboard validate / propose                      | Claude       | `gpx dashboard …`    |
| Pre-merge audit                                   | Claude (Opus)| `gpx audit security` |
| Pre-elastic/integrations PR audit                 | Claude (Opus)| `gpx audit integration` |
| Full local pre-merge gate                         | mixed        | `gpx ci`             |

## CI pipeline (GitHub Actions)

Two workflows, both deterministic:

- `.github/workflows/ci.yml` *(existing)* — Rust check / clippy / fmt across
  Linux + Windows. Unchanged by this work.
- `.github/workflows/agent-ci.yml` *(new)* — elastic-package check, static,
  asset, pipeline tests; dashboard token-hygiene grep. Runs on every PR and
  push to main.
- `.github/workflows/integration-audit.yml` *(new, manual dispatch)* —
  deterministic pre-check before opening a PR against
  `github.com/elastic/integrations`. Runs `elastic-package check / build /
  test static / test asset`, validates manifest version against the input,
  and uploads `state.md` as an artifact for the local LLM auditor to chew on.

The local pre-merge gate (`gpx ci`) chains:

1. `gpx review` — Claude reviewer on the diff vs `origin/main`
2. `gpx test`   — cargo + elastic-package validation
3. `gpx audit security` — security-auditor on the diff

## Cost notes

Auditors (`security-auditor`, `integration-auditor`) run on Opus 4.7 with
ultrathink because the cost of missing a leak or a packaging issue dwarfs
the cost of an extra reasoning pass. They run only at two gates: pre-push
to main and pre-PR to `elastic/integrations`. Everything else runs on
Sonnet 4.6.

## Migration from the existing 4-agent setup

Nothing in the existing setup changes its name or behaviour. The five
existing agents (`planner`, `implementer`, `reviewer`, `tester`,
`progress-auditor`) keep their current contracts. The new agents are
additive. Roll-back is `git revert` of one commit.

## Cross-session continuity rules carry over unchanged

- claude.ai owns `docs/claude-chat-context.md` and this document.
- Claude Code owns `CLAUDE.md`, `docs/STATUS.md`, `docs/HANDOFF.md`,
  `docs/ROADMAP.md`.
- The new agent definitions in `.claude/agents/` and the `bin/gpx`
  orchestrator are owned by Claude Code (they live in the repo and evolve
  as part of normal commits).
- `docs/AGENT-COLLABORATION.md` remains the authoritative Codex contract.
