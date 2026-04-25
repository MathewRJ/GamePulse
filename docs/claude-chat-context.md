# GamePulse — claude.ai Continuity Context

This file is updated by Claude Code at milestone boundaries. Claude.ai reads it at the start
of planning sessions to establish project state without reading the full HANDOFF.md history.

Last updated: 2026-04-25 (B2 complete)

---

## Current milestone state

| Milestone | Status |
|---|---|
| A  Docs reorganisation | 🟢 Done |
| B  Cross-platform refactor | 🟢 Done |
| B2 Launcher-agnostic game detection | 🟢 Done (all 8 WPs) |
| B3 Automatic game detection | ⚪ Not started (placeheld) |
| C  Windows collectors | 🔓 Unblocked by B2 |
| D  Linux portable packaging | 🟡 Partial |
| E  Windows packaging | ⚪ Not started |
| F  Cross-platform parity verification | 🔒 Blocked on B2+C+E |
| G  elastic/integrations PR | 🔒 Blocked on F |

---

## B2 — What shipped (2026-04-25)

All 8 work packages complete:

- **B2.1** — `Target`/`TargetSource` types replacing `DetectedGame`. `scan_for_game()` dispatcher.
- **B2.2** — Schema: `gamepulse.game.source` + `gamepulse.game.launcher` fields on session+events streams. `steam_app_id` conditional (only when `source == steam`).
- **B2.3** — Lutris detector: parses `~/.local/share/lutris/games/*.yml`, matches via `/proc/<pid>/exe` or `WINEPREFIX` env var.
- **B2.4** — Heroic detector: parses `legendaryConfig/legendary/installed.json` + `gog_store/installed.json`; matches via `SteamGameId=heroic-<app_name>` env var.
- **B2.5** — Bottles detector: parses `bottle.yml` per-bottle configs; matches via `WINEPREFIX` == bottle directory path.
- **B2.6** — Enrichment: `detect_graphics_api`, `proton_version_from_env`, `dxvk_version_from_env` wired into all three non-Steam detectors.
- **B2.7** — `--target-pid` / `--target-name` CLI flags + `[session].target_pid` / `target_name` config. `resolve_user_target()` + `poll_pinned_target()` in main.rs. Pinned targets bypass `session.poll()` entirely.
- **B2.8** — Dashboard: `ctrl-source` + `ctrl-launcher` filter controls and `launcher-breakdown` Lens panel added to Game Library dashboard. Component template + backing index mapping updated in live ES.

---

## Active detectors (scan_for_game() chain)

```
scan_for_steam_game()       — SteamAppId env var
  .or_else(scan_for_lutris_game)   — ~/.local/share/lutris/games/*.yml
  .or_else(scan_for_heroic_game)   — SteamGameId=heroic-* env var
  .or_else(scan_for_bottles_game)  — WINEPREFIX == bottle dir
// UserSpecified: resolve_user_target() called at startup from main.rs,
//   not polled — pinned targets are liveness-checked each tick only.
```

---

## Fields live in ES schema

On `metrics-gamepulse.session-default` (and events stream):
- `gamepulse.game.source` — keyword: steam | lutris | heroic | bottles | user_specified | auto_detected
- `gamepulse.game.launcher` — keyword: "Steam" | "Lutris — Wine" | "Lutris — Native" | "Heroic — Epic" | "Heroic — GOG" | "Bottles" | "User-specified"
- `gamepulse.game.steam_app_id` — conditional (only present when source == steam)

Note: fields are mapped in the live index but have 0 rows with these values — all 31 existing
session docs predate B2.2. Fields will populate on the next real session.

---

## Known open follow-ups (not blocking next milestone)

- **Lutris umu runner label**: umu-backed GOG games show `launcher = "Lutris — Native"` because the top-level `wine:` key is absent from their YAML. Fixing requires process-environ inspection (UMU_ID / PROTON_*). Deferred post-B2.
- **Live verification of B2 detectors**: Complete (2026-04-25). Steam (Starfield, app_id=1716740) and Lutris (Thronebreaker, umu/GOG) both verified end-to-end. Lutris umu label limitation confirmed as expected (`launcher="Lutris — Native"`); api="unknown" also expected.
- **Dev install: systemd unit path mismatch**: Unit ships with `ExecStart=/usr/bin/gamepulse-agent` and `--config /etc/gamepulse/gamepulse.toml`; dev builds land at `/usr/local/bin/` and credentials at `~/.config/gamepulse/gamepulse.toml`. Workaround: drop-in override at `~/.config/systemd/user/gamepulse-agent.service.d/override.conf`. PKGBUILD installs to `/usr/bin/` — not affected. Document or fix before next release.
- **Component template deployment automation**: `gamepulse-session-context.json` must be manually PUT to ES when the backing index already exists. Consider scripting this as part of a `make deploy-mappings` target.

---

## Next decisions needed (for claude.ai planning)

1. **B3 vs C priority**: B3 (auto-detection heuristics) adds coverage for unlaunched games; C (Windows collectors) completes the cross-platform parity goal. Which ships first depends on whether the primary testing platform is Linux or Windows.
2. **B3 scope**: What heuristics? Window title scraping (xtitle), known exe name database, `/proc/<pid>/maps` scanning for game engine patterns? Needs scoping before implementation.
3. **C sequencing**: 8 Windows stub collectors need real implementations (ETW for CPU/GPU, etc.). Suggests breaking into C.1-C.8 sub-WPs like B2.

---

## Environment reminders

- Primary dev: CachyOS Linux (AMD Ryzen 7 9800X3D / Radeon RX 9070 XT)
- ES: `https://gamepulse-af41f9.es.us-central1.gcp.elastic.cloud`
- Kibana: `https://gamepulse-af41f9.kb.us-central1.gcp.elastic.cloud`
- Data view ID (wildcard): `18dd83e8-6f88-474f-b434-a4b6c14a04a2`
- Game Library dashboard ID: `e7d878d0-e2d6-454b-9a95-d93a4aeb70a8`
- Heroic installed: one Epic game (`911 Operator`, app_name UUID), GOG installed.json empty
- Lutris installed: one GOG/umu game (Thronebreaker), will show "Lutris — Native" until follow-up
