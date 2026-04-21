# GamePulse — Project Status

Last updated: 2026-04-21 by claude-code (infrastructure session — Python hooks, cross-platform guard)
Active streams: main (cross-platform cloud) + offline (air-gapped, not yet forked)

## For AI agents reading this file

- This file is the single source of truth for project state.
- claude.ai (planning) reads at session start, writes at session end.
- Claude Code (implementation) reads at session start, writes after each WP completion.
- Other agents (Codex, etc.) follow the same contract.
- Update the "Last updated" line on every edit.
- Move completed work packages to "Completed work" — never delete.
- Historical narrative lives in `docs/HANDOFF.md`; do not duplicate it here.
- See `CLAUDE.md` for workflow rules, protected files, and validation commands.

## At a glance — main branch

| Milestone | Status | Progress |
|---|---|---|
| A  Docs reorganisation | 🟢 Done | ▓▓▓▓▓▓▓▓▓▓ |
| B  Cross-platform refactor (Windows stubs day 1) | 🟡 Partial | ▓░░░░░░░░░ |
| C  Windows collectors | ⚪ Not started | ░░░░░░░░░░ |
| D  Linux portable packaging | 🟡 Partial | ▓▓▓▓▓░░░░░ |
| E  Windows packaging | ⚪ Not started | ░░░░░░░░░░ |
| F  Cross-platform parity verification (M2) | 🔒 Blocked on C+E | — |
| G  elastic/integrations PR (M4) | 🔒 Blocked on F | — |

## At a glance — offline branch (not yet forked)

| Milestone | Status |
|---|---|
| H1  Branch + docs-sync automation | ⚪ Not started |
| H2  Bundled stack | ⚪ Not started |
| H3  Offline install flow | ⚪ Not started |
| H4  Export tooling | ⚪ Not started |

## Feature capability matrix

| Feature | Linux | Windows | Offline |
|---|---|---|---|
| Core metrics (8 streams) | ✅ | 🔲 | ✅ (inherited) |
| eBPF deep probes | ✅ | n/a | ✅ (inherited) |
| Settings Tier 1 — manual CLI/config | ✅ | 🔲 | ✅ (inherited) |
| Settings Tier 2 — auto-detect (DLL/ETW) | 🔲 | 🔲 | 🔲 |
| Settings Tier 3 — per-game config profiles | 🔲 | 🔲 | 🔲 |
| Session label (per-game-per-day counter) | ✅ | 🔲 | ✅ (inherited) |

## Platform parity matrix (populated during M2)

| Stream | Ubuntu 24.04 | Fedora 40 | Arch/CachyOS | SteamOS 3.6 | Windows 11 |
|---|---|---|---|---|---|
| cpu | ✅ | 🔲 | ✅ | 🔲 | 🔲 |
| gpu | ✅ (AMD) | 🔲 | ✅ (AMD) | 🔲 | 🔲 |
| memory | ✅ | 🔲 | ✅ | 🔲 | 🔲 |
| storage | ✅ | 🔲 | ✅ | 🔲 | 🔲 |
| network | ✅ | 🔲 | ✅ | 🔲 | 🔲 |
| audio | ✅ | 🔲 | ✅ | 🔲 | 🔲 |
| power | ✅ | 🔲 | ✅ | 🔲 | 🔲 |
| frame | ✅ (MangoHud) | 🔲 | ✅ (MangoHud) | 🔲 | 🔲 (PresentMon) |
| ebpf | ✅ | 🔲 | ✅ | 🔲 | n/a |
| session | ✅ | 🔲 | ✅ | 🔲 | 🔲 |

## Active work package

**None.** Phase B.4 (Windows signal-handler gate) complete. Next: Phase B.1–B.3 (Collector trait + Linux move + Windows stubs).
See `docs/ROADMAP.md` for milestone structure and work package definitions.

## Completed work

### Milestone B — Cross-platform refactor (partial, 2026-04-21)

- **B.4 — Windows signal-handler gate**: Unix-only `tokio::signal::unix` import and signal setup moved behind `#[cfg(unix)]`. On non-Unix hosts the agent spawns a task waiting on `tokio::signal::ctrl_c()` instead; both paths send on a `tokio::sync::oneshot` channel so the `select!` loop arm is platform-neutral. `cargo check` and `cargo clippy -- -D warnings` both pass on Windows.

### Milestone B — Cross-platform refactor (partial, 2026-04-18)

- **B.7 — Settings schema + Tier 1 manual support**: Added `gamepulse.settings` group to `data_stream/session/fields/fields.yml` (17 new fields covering preset, upscaler, frame-gen, features, render mode, source/confidence, notes). Config support via `[session.settings]` TOML section; CLI flags `--preset`, `--upscaler`, `--frame-gen`, `--features`, `--resolution`, `--vsync`, `--notes`. CLI overrides config. Settings block emitted on session-start and summary docs only (omitted entirely when nothing configured). Added `fs2` dep for file locking in B.8.
- **B.8 — Session label counter format**: Label format changed from `<slug>-YYYYMMDD-HHMMSS` to `<slug>-YYYYMMDD-N`. Counter persisted to `$XDG_STATE_HOME/gamepulse/session-counters.json` with atomic write (rename) and `fs2::FileExt::lock_exclusive`. Prunes entries >30 days on first call each day. Windows path stubbed (`%LOCALAPPDATA%\GamePulse\session-counters.json`). New `session.label_source` ("auto"|"manual") and `session.sequence_number` (integer, auto-only) fields in all session docs. 3 unit tests added (increment, prune, slug). Migration note: existing ES docs with `HHMMSS` labels unchanged; new sessions use `N` counter format.
- Added `[lints]` to `src/Cargo.toml` to suppress pre-existing dead_code/clippy warnings in collector files so `cargo clippy -- -D warnings` passes cleanly.

Commit: 561dc78

### Pre-reorg (Phases 0, 0.5, 1, 2, 3, 6)

- **Phase 0–0.5** — Elasticsearch foundation + integration package scaffold. 12 component templates, 11 index templates, 11 ingest pipelines deployed to Elastic Cloud Serverless. All `elastic-package check` + `test static` 11/11 passing.
- **Phase 1** — Python collector validated end-to-end with live Cyberpunk 2077 session.
- **Phase 2** — eBPF daemon complete. Sprints 1–3 ES-confirmed (schedlatency, bio, gpu_sched, mem, stutter, gpu_fence, gpu_submit, futex, irq, vfs). Sprint 4 (sample_event.json updates) outstanding but low-priority for beta.
- **Phase 3** — Seven Kibana dashboards built: baseline, config comparison, session deep-dive, storage I/O, system health, game library, scheduler analysis, home. File-by-value, `data_stream.dataset` filters applied.
- **Phase 6 — Rust production agent: COMPLETE AND ES-VERIFIED.** Live Starfield session 2026-04-11 (40 min, Proton, avg 286.9 FPS) confirmed all 8 metric streams shipping. Rust is production-primary; Python collector is reference/fallback.
- **Phase 4 distribution infrastructure** — `elastic-package stack up` local registry working; zip upload to Kibana Fleet API verified on local 8.13.0 (44 assets) and Serverless (47 assets). `docs/BETA-INSTALL.md` written.
- **Packaging** — AUR PKGBUILD complete; systemd user + system units (`gamepulse-agent.service`, `gamepulse-ebpf.service`) smoke-tested active. `gamepulse-launcher.sh` POSIX shell CLI with `setup / start / stop / status / run %command%` subcommands. Steam integration verified: `gamepulse run %command%` as launch option.
- **Full `elastic-package test` suite** — static 11/11, pipeline 11/11, asset 12/12 pass. Policy + system return "No test results" (acceptable per elastic/integrations guidelines for hardware-dependent integrations).

### Milestone A — Docs reorganisation (2026-04-18)

- Created `docs/STATUS.md` as single source of truth for project state
- Created `architecture/` subdirectory at repo root (ebpf.md moved, data-model.md + agent.md stubs added) — note: lives at repo root, **not** `docs/architecture/`, because `elastic-package check` rejects subdirectories inside `docs/`. Do not re-nest.
- Created `docs/install.md` (unified installation guide, absorbs elasticsearch-setup.md content)
- Created `docs/dashboards.md` (merged dashboard-guide.md + kibana-lens-ndjson-reference.md)
- Renamed `docs/GamePulse-Scope-v3_2.md` → `docs/SCOPE.md`
- Stripped `docs/ROADMAP.md` to structure-only; all status content moved to STATUS.md
- Stripped `CLAUDE.md` to rules-only; all state content moved to STATUS.md
- Rewrote `README.md` to lead with Rust agent as primary
- Deleted: `docs/project-scope.md`, `docs/scope-v2.md`, `docs/claude-chat-context.md`,
  `docs/elasticsearch-setup.md`, `docs/dashboard-guide.md`, `docs/kibana-lens-ndjson-reference.md`

### Infrastructure session — 2026-04-21 (Python hooks — cross-platform guard)

- **Claude Code hooks ported to Python**: Three hooks (pre-edit-check, pre-command-check, post-edit-check) rewritten as Python 3 replacements; bash scripts retained as fallback. Windows machine now has functional protected-file guard, blocked-command guard, and auto cargo check on .rs edits — all three were silently dead on Windows before this change (bash does not execute). Linux behaviour preserved; when Linux switches to Python hooks, fixes propagate automatically.
- **Absolute-path bug fixed in pre-edit-check**: exact-match (manifest.yml) and prefix checks (_dev/, packaging/) never fired against absolute paths on either OS. Fixed in the Python port by stripping the cwd prefix (available in hook JSON) before applying checks.
- **Self-test harness**: `python3 .claude/hooks/pre-edit-check.py --test` — 10 cases covering Windows absolute paths, Linux relative paths, backslash normalization, outside-cwd safety, and sibling-directory guard. All pass.
- Commits: 6c05cdf (Python files + absolute-path fix), e49d7db (settings.json switched to python3)

### Infrastructure session — 2026-04-20 (token optimisation + security + Windows prep)

- **CLAUDE.md progressive disclosure refactor** — slimmed from 208 → 84 lines. Reference content (file locations, hardware, skills, Kibana conventions, test suite status, package build) moved to `docs/claude-reference.md`. Agent routing table and grep-first rule added as enforced rules. Estimated 60–70% reduction in per-turn system prompt overhead.
- **ES_API_KEY security consolidation** — single canonical key name across all scripts. `scripts/kibana-lib.sh` ELASTIC_API_KEY fallback removed. `~/.elastic/claude-memory-credentials.json` scrubbed of plaintext key fields. `~/.config/gamepulse/gamepulse.toml` hardcoded expired key cleared. `/etc/gamepulse/gamepulse.toml` still needs `sudo` clear (user action).
- **Rust agent (`src/config.rs`) env var override** — `ES_API_KEY` and `ES_URL` env vars override TOML values at load time. Enables keyless TOML on Windows.
- **eBPF daemon (`ebpf/.../config.rs`) env var override** — same pattern applied; `api_key` made optional with env var fallback and clear error if neither TOML nor env var provides the key.
- **ES memory migration** — all 6 prior file-based memories migrated to `agent-memory` ES index. `recall_memory` / `recall_recent` verified working. MEMORY.md reduced to 3-line pointer. Cross-platform: Windows clone needs only `ES_API_KEY` env var set + `wire-mcp.sh` run.
- **`settings.local.json` cleanup** — removed ~40 stale entries (old `/home/cachyos/claude/GamePulse/` paths, specific PIDs, one-off diagnostic commands); consolidated overlapping wildcards; fixed broken hook path to current repo location.

### Developer tooling

- **T.1 Elastic Agent Builder MCP server** — `.mcp.json.example` + `.agents/skills/elastic-mcp-setup/SKILL.md` committed. Wires Claude Code and claude.ai to ES for live ES|QL field validation during dashboard builds. API key creation and wiring steps documented in the skill. Not a runtime dependency.
- **T.2 Dashboard verification script (2026-04-19)** — `scripts/verify-dashboard.sh` + `scripts/kibana-lib.sh`. Runs four checks against a deployed dashboard: saved-objects export round-trip, Lens datasource-layer invariants (catches "import-valid but UI-blank" foot-gun), internal dashboard loader (`/internal/dashboards/app/<id>`) renderability, and opt-in `--require-dataset-filter` for integration-submission compliance. Also supports `--expected-panel-types` for regression pinning. Pattern adapted from `/home/cachyos/coding/chatgpt-codex-test`. Smoke-tested live against both deployed dashboards; documented in the `kibana-dashboards` skill.

## Blockers & decisions pending

- **Infrastructure follow-up — pre-command-check allowlist non-functional**: the `allowed_prefixes` list exists in both the bash and Python hooks but non-blocked commands fall through to `exit 0` regardless, so it has no blocking effect. Behaviour preserved verbatim during Linux→Python port. Pending decision: make allowlist enforce (breaking change, risks blocking valid workflows) or remove dead code (documents true behaviour). Review before Phase G.
- **Infrastructure fix — pre-edit-check absolute-path bug (resolved in Python port)**: exact-match (manifest.yml) and prefix checks (_dev/, packaging/) never fired against absolute paths on either OS in the bash version. Fixed in Python port by stripping cwd prefix before applying checks. Bash scripts still carry the unfixed logic; when Linux migrates to Python hooks, the fix propagates automatically.
- **Hook observability — PostToolUse stdout not surfaced to Claude Code**: post-edit-check cargo check output flows to the Claude Code terminal, not back into conversation context. Claude Code cannot directly observe the hook result; it must re-run cargo check manually to verify. Not blocking; worth revisiting if hook output becomes debugging-relevant.

## Environment

- Primary dev host: CachyOS Linux (AMD Ryzen 7 9800X3D / Radeon RX 9070 XT)
- Secondary host: Windows 11 desktop (needs Steam + Rust + WiX setup before Phase C)
- ES endpoint: Elastic Cloud Serverless — `https://gamepulse-af41f9.es.us-central1.gcp.elastic.cloud`
- Repo: github.com/MathewRJ/GamePulse (private)

## Follow-ups and migration notes

- **B.8 label format migration**: ES docs indexed before B.8 carry `<slug>-YYYYMMDD-HHMMSS` labels. New sessions use `<slug>-YYYYMMDD-N`. Dashboard filters on `session.label` should use `*` wildcards or filter on `session.label_source` instead. No backfill needed.

## Follow-ups to investigate

- **Dashboard integration-compliance gap (Milestone G blocker)**: `dashboards/gamepulse-dashboard.ndjson` (id `c1249af5-dbb2-4d34-8d43-839cba2746db`) — all 11 Lens panels fail `scripts/verify-dashboard.sh --require-dataset-filter`. Panels need a `data_stream.dataset` filter embedded in each `embeddableConfig` for elastic/integrations submission. Fix before Milestone G.
- `bottleneck_dominant` null in session summary docs vs populated in `gamepulse-game-timeline` — ingest pipeline enrichment issue on 2026-04-12 backing index
- HOME env fallback via `getpwuid` in `game_name_from_appid()` (src/session.rs)
- No-game system metrics dashboard panel (system health without game filter)
- Startup ES credential validation (ping at startup)
- `docs/BETA-INSTALL.md` to be merged into `docs/install.md` once .deb/.rpm ship
