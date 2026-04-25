# GamePulse — Project Status

Last updated: 2026-04-25 by claude-code (B2.5 — Bottles detection via bottle.yml WINEPREFIX scan)
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
| B  Cross-platform refactor | 🟢 Done | ▓▓▓▓▓▓▓▓▓▓ |
| B2 Launcher-agnostic game detection | 🟡 Partial | ▓▓░░░░░░░░ |
| B3 Automatic game detection (TBD) | ⚪ Not started | ░░░░░░░░░░ |
| C  Windows collectors | 🔒 Blocked on B2 | ░░░░░░░░░░ |
| D  Linux portable packaging | 🟡 Partial | ▓▓▓▓▓░░░░░ |
| E  Windows packaging | ⚪ Not started | ░░░░░░░░░░ |
| F  Cross-platform parity verification (M2) | 🔒 Blocked on B2+C+E | — |
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
| Core metrics (8 streams) | ✅ | 🔲 scaffolded | ✅ (inherited) |
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

**B2.6 — Proton/Wine env var generalisation.** Enrich non-Steam `Target`s with `graphics_api`, `proton_version`, and `dxvk_version` by reading process environ vars (PROTON_VERSION, DXVK_VERSION, etc.) at detection time. Currently these fields are always `None` for Lutris/Heroic/Bottles targets.
See `docs/ROADMAP.md` for milestone structure and work package definitions.

## Completed work

### Milestone B2 — Launcher-agnostic game detection (partial, 2026-04-25)

- **B2.5 — Bottles detection**: Added `scan_for_bottles_game() -> Option<Target>` in `src/session.rs`. Enumerates `bottle.yml` files across two roots (native + Flatpak), parses each with serde_yaml into `BottleConfig` / `BottleProgram` structs, then scans `/proc/*/environ` for `WINEPREFIX` matching the bottle directory (which IS the WINEPREFIX). `BottleProgram::is_active()` filters removed entries (handles null/bool/string variants). Display name resolved by matching `/proc/<pid>/exe` basename against `Programs` entries (case-insensitive); falls back to the bottle `Name` field if no program matches. Both roots absent → immediate `None` (Bottles not installed). Wired into `scan_for_game()` dispatcher. Smoke test: Bottles absent on both paths (not installed); detector returns None immediately. Verification: `cargo check`, `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test` (4/4) green. Commit: see feat commit below.

- **B2.4 — Heroic detection**: Added `scan_for_heroic_game() -> Option<Target>` in `src/session.rs`. Probes four `installed.json` paths (native + Flatpak × Epic/GOG) via `heroic_installed_games()` which returns `Vec<(app_name, title, HeroicStore)>`. Detects running games by scanning `/proc/*/environ` for `SteamGameId=heroic-<app_name>` — the env var Heroic sets on all child processes. `HeroicStore` enum (Epic/Gog) drives `launcher` label: "Heroic — Epic" or "Heroic — GOG". Handles object format (Legendary/newer GOG) and array format (older GOG) defensively; skips DLC entries; deduplicates by app_name across all four paths; empty or malformed JSON files are silently skipped. Wired into `scan_for_game()` dispatcher. Smoke test: Heroic installed (non-Flatpak); one Epic game found (`911 Operator`, app_name UUID `a7594e61a4f24e6d9495ea959749598e`); GOG `installed.json` exists but empty (no GOG games installed). `gogdlConfig/heroic_gogdl/installed.json` also empty — not in spec paths, documented in HANDOFF. Verification: `cargo check`, `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test` (4/4) green. Commit: see feat commit below.

- **B2.3 — Lutris detection**: Added `scan_for_lutris_game() -> Option<Target>` in `src/session.rs`. Scans `~/.local/share/lutris/games/*.yml`, deserialises each file into a minimal `LutrisGameConfig` struct (serde_yaml 0.9), derives display name from filename slug via `lutris_slug_to_title()` (strips 10+-digit Unix timestamp suffix, title-cases words), detects Wine vs native runner from presence of top-level `wine:` YAML key, cross-references `/proc/<pid>/exe` symlinks (native games) and `/proc/<pid>/environ` WINEPREFIX entries (Wine games). Wired into `scan_for_game()` dispatcher replacing the placeholder comment. Added `serde_yaml = "0.9"` to `src/Cargo.toml`. Unit test `test_lutris_slug_to_title` confirms 4 slug examples including numeric version segments and multi-word titles. Smoke test: one real Lutris game found (`thronebreaker-the-witcher-tal-gog-1777116393.yml`, GOG/umu Wine game) — YAML parses cleanly, Wine prefix `/home/cachyos/Games/gog/thronebreaker-the-witcher-tales` would be matched via WINEPREFIX scan when game is running. Note: real Lutris YAML uses no top-level `wine:` key for umu-backed games, so all GOG games will show "Lutris — Native" label until B2.6 improves runner detection. Verification: `cargo check`, `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test` (4/4) green. Commit: see feat commit below.

- **B2.2 — Schema generalisation**: Added `gamepulse.game.source` (keyword enum: steam|lutris|heroic|bottles|user_specified|auto_detected) and `gamepulse.game.launcher` (free-form keyword) to session and events streams (Path 2: per-tick metric streams receive description-only update, no new fields). Made `gamepulse.game.steam_app_id` conditionally emitted — only present when `source == steam`. New helpers `target_source_str()` + `target_to_game_doc()` in `src/session.rs` centralise emission logic; both `base_doc()` and `build_summary_doc()` in `src/main.rs` use the helper. Session.json on-disk format gains `target_source` field; `steam_app_id` now optional. `Target::from_steam()` sets `launcher: Some("Steam")`. Component template `gamepulse-session-context.json` updated with source + launcher properties. Lutris pipeline test fixture added (`test-session-pipeline-lutris.json` + expected) to prove schema accepts non-Steam targets without `steam_app_id`. Daemon compatibility confirmed: `SessionInfo` uses `serde(default)` + no `deny_unknown_fields` — new field silently ignored. Verification: `cargo check`, `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test` (3/3) green. Live gaming-PC verification deferred (see follow-ups). Commit: aec9c24.

- **B2.1 — Target type + detection abstraction**: Renamed `DetectedGame` struct to `Target` and introduced a `TargetSource` enum (`Steam` | `Lutris` | `Heroic` | `Bottles` | `UserSpecified` | `AutoDetected`) in `src/session.rs`. Only `Steam` is constructed today via `Target::from_steam()`; the other variants are reserved for B2.3-B2.7 + B3 and rely on the per-crate `dead_code = "allow"` lint to stay quiet until their detectors land. `SessionEvent` variants kept their `Game{Started,Ended}` names but now carry `Target` instead of `DetectedGame`. `SessionManager::current_game` retyped to `Option<Target>`. `scan_for_game` is now a public dispatcher that calls `scan_for_steam_game` (the renamed Steam-specific helper); its body has commented-out `.or_else(scan_for_…)` lines documenting the slot for each future detector. Field accesses migrated: `game.name` → `target.display_name`, `game.steam_app_id: u32` → `target.steam_app_id: Option<u32>` with `.expect("Steam target without steam_app_id — invariant violation")` at the two emission sites (`write_session_json` and `base_doc`/summary `game_doc`). Behaviour-neutral: session.json on-disk format byte-for-byte unchanged (eBPF daemon reader untouched), and the ES schema is unchanged — `gamepulse.game.{source,launcher}` are deferred to B2.2 alongside making `steam_app_id` optional. Verification on Windows: `cargo check`, `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test` (3/3) all green. Live game-session verification on the Linux gaming PC deferred (see follow-ups). Single migration commit + docs commit.

### Milestone B — Cross-platform refactor (complete, 2026-04-24 session 2)

- **B.5 — GitHub Actions CI matrix**: Added `.github/workflows/ci.yml`. `check` job matrix runs `cargo check --locked` and `cargo clippy --locked -- -D warnings` on `ubuntu-latest` and `windows-latest` for every push to `main` and every PR. A Linux-only conditional step also exercises `cargo check --features ebpf` and `cargo clippy --features ebpf -- -D warnings` to verify B.6's feature plumbing. Separate `fmt` job runs `cargo fmt --check` on Linux only. Caching via `Swatinem/rust-cache@v2` with per-OS keys; toolchain via `dtolnay/rust-toolchain@stable`; fail-fast disabled; concurrency group cancels in-progress runs on same ref. eBPF workspace (`ebpf/`) intentionally excluded from CI — separate workspace requiring bpf-linker. A `style: cargo fmt` commit (c556589) preceded B.5 to ensure the fmt gate is green on first CI run. Commits: c556589 (fmt prep), 93ad4e6 (ci.yml).
- **B.6 — eBPF feature flag (Linux-only)**: Added `[features]` block to `src/Cargo.toml` with `default = []` and `ebpf = []`. Added `compile_error!` gated on `cfg(all(feature = "ebpf", not(target_os = "linux")))` at the top of `src/main.rs`. Today the agent crate has zero eBPF deps and no in-agent eBPF integration (the daemon is an out-of-process sibling workspace), so the feature is a scaffold — it reserves the name, enforces the Linux constraint the moment someone enables it on Windows, and makes the pattern obvious for future in-agent eBPF work. CI exercises both feature modes on Linux. Verified on Windows: default build OK, `--all-features` fails with the compile_error as designed. Commit: ad1aa93.

### Milestone B — Cross-platform refactor (partial, 2026-04-24)

- **B.1 — Collector trait + uniform constructor**: `Collector` trait now requires `Send + 'static` and includes a default-no-op `set_game_pid(&mut self, _pid: Option<u32>)`. All 8 collectors now implement the trait (previously only some did). Every collector's `new()` signature is uniform: `pub fn new(game_pid: Option<u32>) -> Self` — collectors that don't use the pid prefix with `_`. AudioCollector's previously-expensive `detect_backend()` call in `new()` moved into lazy init at first `collect()` via an `Option<String>` field. Collectors with process-scoped state (cpu, memory, mangohud, gpu_amd) keep both an inherent `set_game_pid` method and a delegating trait impl. Commit: ce29210.
- **B.2 — Linux collectors moved to `src/collectors/linux/`**: 8 collectors (cpu, memory, storage, network, power, audio, mangohud, gpu_amd) relocated via `git mv` into a platform submodule. New `src/collectors/linux/mod.rs` declares and re-exports each collector; `GpuAmdCollector` re-exported as `GpuCollector` for platform symmetry. Imports in moved files: `use super::Collector` → `use crate::collectors::Collector`. `src/collectors/mod.rs` now cfg-gates the linux module behind `#[cfg(target_os = "linux")]` and re-exports via `pub use linux::*`.
- **B.3 — Windows collector stubs + `Vec<Box<dyn Collector>>` dispatch**: 8 Windows stub files in `src/collectors/windows/` (cpu, memory, storage, network, power, audio, frame, gpu). Each stub implements the trait with the same `dataset()` string as its Linux counterpart but returns `Ok(None)` from `collect()` — Phase C will replace with real collection. The MangoHud↔frame split follows platform-symmetric naming: `frame.rs` defines `FrameCollector`, re-exported in `windows/mod.rs` as `MangoHudCollector` so main.rs uses a single type name across both platforms. main.rs collector instantiation replaced with `build_collectors(game_pid) -> Vec<Box<dyn Collector>>`; per-tick collect and game-pid propagation are now Vec iterations using the trait's `dataset()` method. `cargo check` passes on Windows. Commit: 40d1612.
- B.1–B.3 shipped as two commits rather than three: B.2 and B.3 were combined because B.2's intermediate state leaves Windows `cargo check` broken, and this session was Windows-only (no Linux host to verify a standalone B.2). Splitting post-hoc via hunk-staging was rejected as higher-risk than combining.

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

### Infrastructure session — 2026-04-21 (Windows MCP wiring)

- **Windows machine MCP wiring (2026-04-21)**: elastic-agent-builder registered and connected via npx mcp-remote against `https://gamepulse-af41f9.kb.us-central1.gcp.elastic.cloud/api/agent_builder/mcp`. `GAMEPULSE_MCP_API_KEY` set as persistent user env var; `.mcp.json` uses variable substitution (`${GAMEPULSE_MCP_API_KEY}`) rather than baked-in key — no API key material on disk. New MCP key was created with explicit `feature_agentBuilder.read` + `feature_actions.read` application privileges via Kibana Dev Console (superuser session). Previous attempt with derived inherits-parent key failed because parent `ES_API_KEY` lacks `feature_agentBuilder.read`; documented here to save future time. Tools (`recall_memory`, `recall_recent`) activate on next session restart per the MCP protocol.

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

- **Security hardening follow-up — MCP key exposure via `claude mcp list`**: During initial MCP setup attempt, the first `gamepulse-mcp` API key was printed verbatim in `claude mcp list` output and exposed to a conversation transcript. That key has been invalidated. A separate properly-scoped key now lives in env var only. `claude mcp list` displaying raw API key values in its output is a design-level issue — future setup flows should either redact or be executed in non-logged contexts. Worth surfacing to Anthropic as a product feedback item.

- **Infrastructure follow-up — pre-command-check allowlist non-functional**: the `allowed_prefixes` list exists in both the bash and Python hooks but non-blocked commands fall through to `exit 0` regardless, so it has no blocking effect. Behaviour preserved verbatim during Linux→Python port. Pending decision: make allowlist enforce (breaking change, risks blocking valid workflows) or remove dead code (documents true behaviour). Review before Phase G.
- **Infrastructure fix — pre-edit-check absolute-path bug (resolved in Python port)**: exact-match (manifest.yml) and prefix checks (_dev/, packaging/) never fired against absolute paths on either OS in the bash version. Fixed in Python port by stripping cwd prefix before applying checks. Bash scripts still carry the unfixed logic; when Linux migrates to Python hooks, the fix propagates automatically.
- **Hook observability — PostToolUse stdout not surfaced to Claude Code**: post-edit-check cargo check output flows to the Claude Code terminal, not back into conversation context. Claude Code cannot directly observe the hook result; it must re-run cargo check manually to verify. Not blocking; worth revisiting if hook output becomes debugging-relevant.

## Environment

- Primary dev host: CachyOS Linux (AMD Ryzen 7 9800X3D / Radeon RX 9070 XT)
- Secondary host: Windows 11 desktop (needs Steam + Rust + WiX setup before Phase C)
- ES endpoint: Elastic Cloud Serverless — `https://gamepulse-af41f9.es.us-central1.gcp.elastic.cloud`
- Repo: github.com/MathewRJ/GamePulse (private)

## Follow-ups and migration notes

- **Linux `cargo check` verification for B.1–B.3 + B.6 (`--features ebpf`) pending**: 2026-04-24 sessions were Windows-only (gaming PC offline). Windows `cargo check`, `cargo clippy -- -D warnings`, `cargo fmt --check`, and `cargo check --all-features` (expected-fail via compile_error) all green. The B.5 CI workflow exercises both Linux and Windows on push, so this is self-healing as soon as CI runs on main — but a gaming-PC session to run `cargo check --manifest-path src/Cargo.toml --features ebpf` and confirm the cfg gate doesn't accidentally exclude required modules on Linux is still worth doing.
- **B2.1 live verification on gaming PC**: the Windows session can only smoke-test the migration via `cargo check`/`clippy`/`test`. The behaviour claim (session-start, in-tick, and session-summary docs are byte-for-byte identical to pre-B2.1, modulo timestamps) needs a live Linux session — boot a real game, capture a session-start doc and a session-summary doc, diff field-by-field against a known-good pre-B2.1 sample from ES Discover. Same trip should also confirm the eBPF daemon still parses `/tmp/gamepulse/session.json` cleanly (format wasn't supposed to change, but verify).
- **Hook scope redesign — PostToolUse cargo check noise during structural refactors**: deferred from the 2026-04-24 sessions. The `post-edit-check.py` hook runs `cargo check` after every `.rs` edit, which is helpful for single-file fixes but creates N-times-expected-failure noise during multi-file structural refactors (see 2026-04-24 B.2+B.3 session for the python-via-bash workaround). Options: scope the hook to only fire on the final edit in a batch, add a way to suppress it during planned structural refactors, or move the cargo check to a manual command. Infrastructure session, defer until Phase C scheduling.
- **B.8 label format migration**: ES docs indexed before B.8 carry `<slug>-YYYYMMDD-HHMMSS` labels. New sessions use `<slug>-YYYYMMDD-N`. Dashboard filters on `session.label` should use `*` wildcards or filter on `session.label_source` instead. No backfill needed.

## Follow-ups to investigate

- **Dashboard integration-compliance gap (Milestone G blocker)**: `dashboards/gamepulse-dashboard.ndjson` (id `c1249af5-dbb2-4d34-8d43-839cba2746db`) — all 11 Lens panels fail `scripts/verify-dashboard.sh --require-dataset-filter`. Panels need a `data_stream.dataset` filter embedded in each `embeddableConfig` for elastic/integrations submission. Fix before Milestone G.
- `bottleneck_dominant` null in session summary docs vs populated in `gamepulse-game-timeline` — ingest pipeline enrichment issue on 2026-04-12 backing index
- HOME env fallback via `getpwuid` in `game_name_from_appid()` (src/session.rs)
- No-game system metrics dashboard panel (system health without game filter)
- Startup ES credential validation (ping at startup)
- `docs/BETA-INSTALL.md` to be merged into `docs/install.md` once .deb/.rpm ship
