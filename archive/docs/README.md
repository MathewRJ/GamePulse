# Archive

These files document how GamePulse was developed — design decisions, AI workflow approach, and architecture explorations. They are not required for using or contributing to GamePulse, but provide useful context for understanding the project's evolution.

## Contents

### AI workflow
- `AGENT-SYSTEM.md` — The multi-agent pipeline used during development (Claude, Codex, Gemini orchestrated via `gpx`). Documents agent routing, the Codex worktree pattern, and CI gate design.
- `AGENT-COLLABORATION.md` — How Claude Code, OpenAI Codex CLI, and Gemini CLI were used together. Prompt templates for handoff, work-package format, guardrails.

### Architecture explorations
- `architecture/data-model.md` — Early data model design stub (superseded by implemented schema in `data_stream/`)
- `architecture/agent.md` — Early Rust agent design stub (superseded by `src/`)
- `architecture/ebpf.md` — eBPF daemon architecture and probe design (still relevant as design reference for `ebpf/`)

## Note on AI-assisted development

GamePulse was built with an AI-assisted workflow. The `AGENT-SYSTEM.md` and `AGENT-COLLABORATION.md` docs describe that workflow in detail. If you're building your own integration and want to use a similar approach, the workflow infrastructure lives in a separate `Workflow` repository and is not a dependency of GamePulse itself.
