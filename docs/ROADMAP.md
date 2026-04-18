# GamePulse — Roadmap

This file defines the milestones and work packages for GamePulse. It describes structure only — current status lives in `docs/STATUS.md`.

## Milestone structure

- Main branch (cross-platform cloud):
  - A  Docs reorganisation
  - B  Cross-platform refactor (Windows stubs from day 1)
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
