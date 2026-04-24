# GamePulse — Roadmap

This file defines the milestones and work packages for GamePulse. It describes structure only — current status lives in `docs/STATUS.md`.

## Milestone structure

- Main branch (cross-platform cloud):
  - A  Docs reorganisation
  - B  Cross-platform refactor (Windows stubs from day 1)
  - B2 Launcher-agnostic game detection
  - B3 Automatic game detection (TBD)
  - C  Windows collectors
  - D  Linux portable packaging
  - E  Windows packaging
  - F  Cross-platform parity verification
  - G  elastic/integrations PR
- Offline branch (air-gapped bundle, forks from main after B):
  - H1  Branch + docs-sync automation
  - H2  Bundled stack
  - H3  Offline install flow
  - H4  Export tooling
  - H5  Ongoing merge-from-main cadence
- Deferred: community platform, Windows eBPF equivalents

---

## Phase A — Docs reorganisation

Established `docs/STATUS.md` as single source of truth; stripped planning docs; rewrote README to lead with Rust agent. Complete.

---

## Phase B — Cross-platform refactor

**Goal:** introduce a `Collector` trait and OS-specific module split so Windows collectors can be added without forking the codebase. Windows stubs land on day 1 returning `Ok(None)`; real collection lives in Phase C.

| WP | Deliverable |
|---|---|
| B.1 | Define `Collector` trait in `src/collectors/mod.rs`; no OS-specific types in the trait signature |
| B.2 | Move existing collectors to `src/collectors/linux/` |
| B.3 | Scaffold `src/collectors/windows/` with stub impls that return `Ok(None)` |
| B.4 | Platform dispatch in `src/main.rs` via `#[cfg(target_os)]` |
| B.5 | GitHub Actions CI matrix — `cargo check` on linux + windows targets for every PR |
| B.6 | eBPF as `features = ["ebpf"]` flag, Linux-only |
| B.7 | Settings capture schema (manual Tier 1) — new `gamepulse.settings.*` fields on session stream; CLI flags; config section |
| B.8 | Session label counter — change auto-generated label from `-HHMMSS` to `-N` per-game-per-day counter |

---

## Phase B2 — Launcher-agnostic game detection

**Goal:** Generalise game detection beyond Steam. Add first-class support for Lutris, Heroic, and Bottles on Linux. Add user-specified target mode for long-tail launchers (Battle.net, EA, Ubisoft, Rockstar, Epic on Linux via Heroic, and anything else). Generalise Proton/Wine detection to work regardless of which launcher started the process (incl. umu-launcher). Ensure Phase C Windows collectors inherit the generalised detection model rather than baking new Steam-specific assumptions.

| WP | Deliverable |
|---|---|
| B2.1 | `Target` enum in `src/session.rs` wrapping Steam/Lutris/Heroic/Bottles/UserSpecified variants; refactor current Steam-only detection as one source of many |
| B2.2 | Schema generalisation: `gamepulse.game.steam_app_id` becomes optional; add `gamepulse.game.source` (steam\|lutris\|heroic\|bottles\|user_specified\|auto_detected) and `gamepulse.game.launcher` (human-readable) fields. Backwards-compatible addition. |
| B2.3 | Lutris detection via `~/.local/share/lutris/games/*.yml` config parse |
| B2.4 | Heroic detection via `~/.config/heroic/` JSON config parse |
| B2.5 | Bottles detection via Bottles' config format |
| B2.6 | Proton/Wine detection generalised via environment variables (`WINEPREFIX`, `PROTONPATH`, `STEAM_COMPAT_*`, `UMU_ID`). Works for Steam-launched Proton, Lutris-launched Wine, Heroic-launched Wine, umu-launcher, and raw Wine invocations. |
| B2.7 | User-specified target CLI: `--target-process <name>`, `--target-pid <pid>`, `gamepulse run <command>`. Config-file equivalent in `[session]` section. |
| B2.8 | Dashboard query updates: existing dashboards that filter on `steam_app_id` switch to filtering on a launcher-agnostic identifier. Minimum-churn updates only. |

---

## Phase B3 — Automatic game detection (scope + timing TBD)

**Goal:** Ship auto-detection so non-technical users don't have to configure launcher pre-launch hooks or specify targets manually. Agent runs in background, notices when a game starts, begins collection automatically.

**Scope: NOT COMMITTED.** This phase is placeheld to reserve roadmap position. Work packages are sketched below but no implementation happens until Phase B2 ships and real usage signal exists. The decision to commit Phase B3 depends on whether B2's manual-target UX turns out to be a genuine pain point for users or merely a theoretical one.

**Architectural note:** Inspired by behavioural-classification patterns from EDR systems (e.g. Elastic Defend) — specifically the insight that "is this process X-type" is better answered by observing kernel-level runtime behaviour than by matching allowlists. Implementation is a lightweight in-agent classifier reusing GamePulse's existing eBPF infrastructure (gpu_sched, gpu_submit, page fault probes). **No third-party EDR dependency, no Elastic Defend integration** — the pattern is borrowed, the product is not. Licensing, performance overhead, installation friction, and data-model pollution all rule out literal reuse of Defend.

Sketch of likely work packages (not committed):
- Per-PID signal aggregation on top of existing eBPF probes (rolling-window GPU submission rate)
- Graphics API detection via library load events (libvulkan, libGL, d3d11)
- Fullscreen state + input device activity signals (X11/Wayland window state, /dev/input activity)
- Scoring function combining signals with tunable thresholds
- Allowlist for known non-games (video apps, 3D modelling tools)
- Validation dataset from real usage (own gaming + non-gaming sessions)
- Service/daemon mode + tray/notification UI for auto-start UX
- User feedback mechanism for misclassifications

Windows equivalent uses ETW providers (DxgKrnl, Win32k, PresentMon) for the same signals.

---

## Phase C — Windows collectors

**Goal:** feature parity with Linux for all collectors that have Windows equivalents. eBPF has no Windows equivalent in v1.

| WP | Collector | Data source |
|---|---|---|
| C.1 | CPU | PDH `\Processor Information(*)` + WMI |
| C.2 | Memory | `GlobalMemoryStatusEx`, `GetProcessMemoryInfo` |
| C.3 | Storage | PDH `\PhysicalDisk(*)` |
| C.4 | Network | PDH `\Network Interface(*)` |
| C.5 | Power | `GetSystemPowerStatus`, WMI battery |
| C.6 | GPU — NVIDIA | NVML (cross-platform crate) |
| C.7 | GPU — AMD | ADLX SDK |
| C.8 | Frame timing | PresentMon sidecar process + CSV parsing |
| C.9 | Game detection | Steam registry `HKCU\Software\Valve\Steam\Apps\<appid>\Running` + process scan |
| C.10 | Session lifecycle | Port `src/session.rs` paths — `%APPDATA%\GamePulse\session.json` |
| C.11 | ETW image-load subscription for Tier 2 settings auto-detect |

---

## Phase D — Linux portable packaging

**Goal:** install-and-run packages for non-Arch Linux distros; unified CLI UX; diagnostic tooling.

| WP | Deliverable |
|---|---|
| D.1 | `.deb` build + Ubuntu 24.04 clean-VM smoke test |
| D.2 | `.rpm` build + Fedora 40 clean-VM smoke test |
| D.3 | Unified `--verbose`, `--log-level`, `--dry-run`, `--print-config` flags; consolidate with `GAMEPULSE_LOG` env |
| D.4 | Optional keyring credential storage via D-Bus Secret Service (libsecret); plaintext TOML fallback |
| D.5 | `gamepulse diagnose` subcommand — single-file bug-report dump (kernel, driver, ES reach, last 20 log lines) |
| D.6 | GitHub Actions release workflow — on tag `v*` builds .deb, .rpm, Arch pkg.tar.zst; attaches to GitHub Release |
| D.7 | Game profile loader + three starter profiles (Starfield, Cyberpunk 2077, Baldur's Gate 3) for Tier 3 settings capture |
| D.8 | Linux DLL scan via `/proc/<pid>/maps` for Tier 2 settings auto-detect |

---

## Phase E — Windows packaging

| WP | Deliverable |
|---|---|
| E.1 | Portable zip: `gamepulse-<ver>-windows-x64.zip` with agent, config template, README |
| E.2 | WiX MSI: installs to `Program Files\GamePulse\`, registers Windows Service |
| E.3 | Windows `gamepulse.exe setup` — mirrors Linux UX; credentials in `%APPDATA%\GamePulse\gamepulse.toml` (current-user ACL) |
| E.4 | Steam launch wrapper: `gamepulse.exe run %command%` — subprocess + wait + stop |
| E.5 | Windows Service (admin install) vs Scheduled Task (user install) — both paths tested |
| E.6 | Code signing — self-signed for beta; plan EV cert later |
| E.7 | GitHub Actions Windows runner builds MSI + zip on tag |

---

## Phase F — Cross-platform parity verification (M2)

**Goal:** every cell in the parity matrix in STATUS.md is ✅ or has a documented known limitation. This is the gate before the elastic/integrations PR.

| WP | Target |
|---|---|
| F.1 | Define `docs/QA-MATRIX.md` — the parity test oracle |
| F.2 | Ubuntu 24.04 parity run |
| F.3 | Fedora 40 parity run |
| F.4 | Arch (non-CachyOS) parity run |
| F.5 | SteamOS 3.6+ parity run |
| F.6 | Windows 11 parity run |
| F.7 | Automated 60s smoke test in CI (docker ES target) |

---

## Phase G — elastic/integrations PR

**Requirements:**
- `elastic-package test` suite green (already done)
- `docs/README.md` in elastic/integrations format with screenshots
- `CHANGELOG.md` entry for the submitted version
- ECS compliance review
- Parity matrix from Phase F cited in PR description
- Fork `elastic/integrations`, add `packages/gamepulse/`, submit PR
- Engage Elastic integrations team for review

---

## Offline branch — Phase H

Forks from `main` after Phase B lands. Targets air-gapped benchmarkers, reviewers, NDA hardware testers.

| WP | Deliverable |
|---|---|
| H1 | Fork `offline` branch from `main`; add `.github/workflows/sync-docs-from-main.yml` to cherry-pick doc changes daily |
| H2 | Bundled stack — native Elasticsearch + Kibana tar/zip (not Docker by default); pinned version; persistent volume next to binaries |
| H3 | Offline install flow — `gamepulse setup --local`; saved-objects API asset import (bypasses Fleet registry) |
| H4 | Export tooling — `gamepulse export` outputs `sessions.csv`, `frames.jsonl`, `metrics/<stream>.jsonl` filterable by session/time |
| H5 | Merge-from-main cadence — docs auto-sync; code merges manual on feature stability boundaries |

---

## Deferred

- Windows eBPF equivalents (ETW deep telemetry)
- Community platform (public aggregated dashboards, leaderboards, regression detection)

---

## Tooling (non-critical-path)

These work packages enhance developer workflow but don't block milestones.

| WP | Deliverable |
|---|---|
| T.1 | Elastic Agent Builder MCP server connection for claude.ai + Claude Code |
| T.2 | Install elastic/elastic-docs-skills when Phase G starts (writing the integrations PR README) |
