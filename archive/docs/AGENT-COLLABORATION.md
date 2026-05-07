# Agent Collaboration — Codex Integration

Describes the four-step pipeline and the two Codex roles used when OpenAI Codex handles implementation work.

## Pipeline

```
claude.ai (planner) → Codex (implementer, worktree) → Claude Code (verifier) → user (merge)
```

1. **claude.ai** — takes a finding or prompt, produces a tightly-scoped work package written as a Codex-ready prompt block.
2. **Codex** — runs `codex` against a dedicated worktree branch, never main. Outputs the diff and runs its own checks before reporting back.
3. **Claude Code** — opens the branch, runs `cargo check`, `cargo clippy -- -D warnings`, `elastic-package check`. Green → commits and pushes. Red → fixes small issues or hands the task back with a description of what failed.
4. **User** — merges.

Expensive thinking happens once at planning (claude.ai) and once at review (Claude Code). Mechanical edits matching a clear pattern go to Codex.

---

## Worktree convention

All Codex work happens in a named worktree, never on main. Worktrees are gitignored.

```bash
# Create before handing off to Codex
git worktree add worktrees/codex-<task-id> -b codex/<task-id>

# Remove after the branch is merged
git worktree remove worktrees/codex-<task-id>
git branch -d codex/<task-id>
```

- Worktree path: `worktrees/codex-<task-id>/` (repo root, gitignored)
- Branch name: `codex/<task-id>`
- `<task-id>` matches the work package `task_id` field — short kebab slug, e.g. `add-cpu-fields`

---

## Codex roles

### Role A — Read-only reviewer

Codex reads a diff and flags issues. Does not edit any files.

**When to use:** After a human-written or Claude Code change, before merging, when you want a fast independent read.

**Prompt template:**

```
You are a read-only code reviewer for the GamePulse project.

Read CLAUDE.md and docs/STATUS.md first.

Review the diff below and flag:
- Any protected file touched without explicit task assignment
  (manifest.yml, tools/deploy_pipelines.py, tools/wire_pipelines.py,
   _dev/*, packaging/*, *pipeline*.json, index templates, ILM policies)
- Any Rust lifetime or ownership issue
- Any unwrap() on Result/Option in a production code path
- Any change to the Elasticsearch bulk API envelope format
- Any new BPF crate added outside Aya
- Any test file deleted

Output exactly one of: APPROVE / APPROVE WITH NOTES / REJECT
Give specific file:line references for every finding.

<diff>
[PASTE DIFF HERE]
</diff>
```

---

### Role B — Sandboxed implementer (worktree-only)

Codex edits files inside the worktree branch. Never touches main. Never touches protected files unless they are explicitly named in the work package.

**When to use:** Any mechanical implementation task with a clear expected diff shape — field additions, struct updates, pattern-matching a known cleanup list, boilerplate generation.

**Prompt template:**

```
You are the implementation agent for the GamePulse project.

## Context
Working branch: codex/<task-id>  (already checked out in your worktree — do not switch branches)
You MUST NOT commit directly to main or push to origin/main.

## Read first
1. CLAUDE.md — workflow rules, protected files, approved bash commands
2. docs/STATUS.md — current project state

## Task
<GOAL: one sentence — outcome, not steps>

## Files in scope
<list each file — relative path from repo root>

## Files protected — do not touch unless listed above
- manifest.yml
- tools/deploy_pipelines.py
- tools/wire_pipelines.py
- Any file under _dev/ or packaging/
- Ingest pipeline YAML/JSON files (any path matching *pipeline*)
- Index template JSON and ILM policy JSON files

## Expected diff shape
<describe what the change should look like — prevents over-engineering>
Example: "Add two fields to the CpuSample struct and update the one match arm that serialises it."

## Acceptance criteria
<list each as a testable statement>

## Approved bash commands (only these)
cargo check
cargo clippy -- -D warnings
cargo test
cargo build --release
elastic-package check
elastic-package test static
git diff
git status
git log --oneline -10

Do NOT run: elastic-package test system, rm, mv on source files, pip, npm, or any network command.

## Mandatory final step
Run these two commands and paste the complete output — do not truncate:
  cargo clippy -- -D warnings 2>&1
  elastic-package check 2>&1
Do not declare done until both pass.

## Output
- Files changed (list each)
- What was done (2–4 sentences)
- Anything uncertain or that the reviewer should pay attention to
- Full output of the two mandatory commands above
```

---

## Work package format (what claude.ai produces)

claude.ai produces a work package that maps directly onto the Role B prompt template. Minimum required fields:

| Field | Required | Description |
|---|---|---|
| `task_id` | yes | Short kebab slug — used for worktree path and branch name |
| `goal` | yes | One sentence — outcome, not steps |
| `files_in_scope` | yes | Relative paths from repo root |
| `files_protected` | yes | Always include the CLAUDE.md defaults; add task-specific extras |
| `expected_diff_shape` | yes | Prevents Codex from over-engineering |
| `acceptance_criteria` | yes | Testable statements |
| `validation_commands` | yes | Subset of approved commands relevant to this task |

---

## Protected files — same rules as CLAUDE.md

Codex follows the identical protected-files list as all other agents:

- `manifest.yml`
- `tools/deploy_pipelines.py`
- `tools/wire_pipelines.py`
- Any file under `_dev/` or `packaging/`
- Ingest pipeline YAML/JSON files (any path matching `*pipeline*`)
- Index template JSON files, ILM policy JSON files

A work package that explicitly names a protected file makes it in-scope for that task only.

---

## Guardrails summary

- Codex never commits to main or pushes to `origin/main`
- Codex never edits protected files unless the work package names them
- Codex always runs `cargo clippy -- -D warnings` and `elastic-package check` as the final step and pastes full output
- Claude Code verifies independently before merging — Codex output is unreviewed until Claude Code issues a green
- Three independent passes on every change by construction: Codex self-check → Claude Code verify → user merge
