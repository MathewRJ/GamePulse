# GamePulse — Project Status

Last updated: 2026-04-18 by claude-code (MCP tooling)
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
| B  Cross-platform refactor (Windows stubs day 1) | ⚪ Not started | ░░░░░░░░░░ |
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
| Settings Tier 1 — manual CLI/config | 🔲 | 🔲 | 🔲 |
| Settings Tier 2 — auto-detect (DLL/ETW) | 🔲 | 🔲 | 🔲 |
| Settings Tier 3 — per-game config profiles | 🔲 | 🔲 | 🔲 |
| Session label (per-game-per-day counter) | 🔲 | 🔲 | 🔲 |

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

**None.** Next up: B.7 + B.8 (settings schema + session label counter).
See `docs/ROADMAP.md` for milestone structure and work package definitions.

## Completed work

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

### Developer tooling

- **T.1 Elastic Agent Builder MCP server** — `.mcp.json.example` + `.agents/skills/elastic-mcp-setup/SKILL.md` committed. Wires Claude Code and claude.ai to ES for live ES|QL field validation during dashboard builds. API key creation and wiring steps documented in the skill. Not a runtime dependency.

## Blockers & decisions pending

- None currently.

## Environment

- Primary dev host: CachyOS Linux (AMD Ryzen 7 9800X3D / Radeon RX 9070 XT)
- Secondary host: Windows 11 desktop (needs Steam + Rust + WiX setup before Phase C)
- ES endpoint: Elastic Cloud Serverless — `https://gamepulse-af41f9.es.us-central1.gcp.elastic.cloud`
- Repo: github.com/MathewRJ/GamePulse (private)

## Follow-ups to investigate

- `bottleneck_dominant` null in session summary docs vs populated in `gamepulse-game-timeline` — ingest pipeline enrichment issue on 2026-04-12 backing index
- HOME env fallback via `getpwuid` in `game_name_from_appid()` (src/session.rs)
- No-game system metrics dashboard panel (system health without game filter)
- Startup ES credential validation (ping at startup)
- `docs/BETA-INSTALL.md` to be merged into `docs/install.md` once .deb/.rpm ship
