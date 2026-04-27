# GamePulse — claude.ai Continuity Context

This file is updated by Claude Code at milestone boundaries. Claude.ai reads it at the start
of planning sessions to establish project state without reading the full HANDOFF.md history.

Last updated: 2026-04-26 (Phase C complete — all 8 Windows collectors)

---

## Current milestone state

| Milestone | Status |
|---|---|
| A  Docs reorganisation | 🟢 Done |
| B  Cross-platform refactor | 🟢 Done |
| B2 Launcher-agnostic game detection | 🟢 Done (all 8 WPs) |
| B3 Automatic game detection | ⚪ Not started (placeheld) |
| C  Windows collectors | 🟢 Done (all 8 WPs, C.0 PDH infra + C.1–C.8) |
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

## C — What shipped (2026-04-25 → 2026-04-26)

All Windows collectors implemented, dry-run verified on Windows 11. Per-collector parity gaps documented in `docs/STATUS.md` ("Phase C parity gap summary").

- **C.0** — `src/collectors/windows/pdh.rs` PDH infra. `PdhQuery`/`PdhCounter` newtypes wrap raw `isize` handles (windows 0.58 quirk). `counter_value_f64` + `counter_values_array` (two-pass).
- **C.1** — CPU: PDH `\Processor(*)\% Processor Time` + `\Processor Information(_Total)\Processor Frequency` + WMI temperature subprocess.
- **C.2** — Memory: `GlobalMemoryStatusEx` + `GetProcessMemoryInfo`. `game_rss_mb` from `WorkingSetSize`.
- **C.3** — Storage: PDH `\PhysicalDisk(_Total)\Disk Read|Write Bytes/sec`. Aggregate only (no per-disk, no game IO — ETW required).
- **C.4** — Network: PDH `\Network Interface(*)\Bytes Sent|Received/sec` summed across non-tunnel adapters.
- **C.5** — GPU: COM init + DXGI `IDXGIAdapter3::QueryVideoMemoryInfo` for VRAM + PDH `\GPU Engine(*engtype_3D*)\Utilization Percentage` + WMI thermal-zone temperature with `temp_source: "wmi_acpi"`.
- **C.6** — Power: `GetSystemPowerStatus` → `ac_connected` + `battery_pct` (Option types; absent on desktops).
- **C.7** — Audio: scaffold emitting `backend: "wasapi"`. `GlitchListener` stub for future ETW xrun upgrade.
- **C.8** — Frame: PresentMon subprocess via `std::process::Command`, background `std::thread` reader, bounded `mpsc::sync_channel`, 120-sample `VecDeque` ring. CSV header parsed by name (resilient to PresentMon 1.x→2.x renames). Discovery: `GAMEPULSE_PRESENTMON` → binary directory → `where`.

### IMPORTANT: frame collector field-path quirk

The frame collector's **dataset name** is `gamepulse.frame` (used for routing to the `metrics-gamepulse.frame-*` data stream), but the **emitted JSON field group** is `gamepulse.fps.*` — not `gamepulse.frame.*`. This mirrors the Linux MangoHud collector (`src/collectors/linux/mangohud.rs:270`) and matches `SessionAccumulators` in `src/main.rs:359-368`, which reads `gp.get("fps")` and aggregates `avg_1s`, `frametime_ms`, `stutter_count`.

Concrete fields emitted under `gamepulse.fps.*`:
- `avg_1s` (f64) — smoothed FPS over the source's rolling ring
- `current` (i64) — most recent frame's instantaneous FPS
- `low_1pct` (i64), `low_01pct` (i64) — percentile lows from sorted ring
- `frametime_ms` (f64) — per-tick mean frametime
- `frametime_variance` (f64) — per-tick variance
- `stutter_count` (i64) — frames in the tick > 2× tick mean

When building dashboard panels for FPS / frametime / stutters, query the `metrics-gamepulse.frame-*` index pattern but reference the `gamepulse.fps.*` field paths. The same convention has been in place since the Linux collector landed; do not assume `gamepulse.frame.*` is the field path — it is the dataset only.

### Windows-specific notes for dashboard / parity work

- PresentMon binary is **not bundled**. Users must install it themselves (github.com/GameTechDev/PresentMon) and either set `GAMEPULSE_PRESENTMON=...` or place `PresentMon.exe` alongside `gamepulse-agent.exe`. If absent, `gamepulse.fps.*` stays empty for that session — the other 7 streams continue normally. Dashboards must tolerate missing frame data on Windows hosts.
- GPU `temperature_c` may be absent on Windows even when emitted on Linux — WMI ACPI thermal zones don't always include a GPU-labelled zone. `temp_source: "wmi_acpi"` in the doc signals that the reading (when present) is approximate, vs Linux `temp_source: "hwmon"` which is exact.
- CPU has no `game_utilisation_pct` on Windows (parity gap, ETW required). CPU has no `governor` field (no Windows equivalent).
- Storage on Windows is aggregate only — no per-disk breakdown, no game-scoped IO.
- Audio on Windows always reports `backend: "wasapi"` and emits no xruns (scaffold).
- Power on Windows lacks `battery_rate_w`.

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

## Dashboard build status

| Dashboard | Status | ID |
|---|---|---|
| Games | Done | `5e898d7c-8de1-45b8-ae04-4cdc745f046d` |
| Environment | Done | `3a55c257-0537-42a8-94a7-24dc773a703b` |
| Hardware | Done | `ed9d9b94-2003-429c-b294-9d3f2ef737e7` |
| Compare | Done | `828db140-b330-4d26-8045-40a7895bfc41` |
| Engine | Next | — |

## Next decisions needed (for claude.ai planning)

1. **E vs B3 priority**: With Phase C done, the next significant milestone is either Phase E (Windows MSI packaging — gives users a real install path on Windows) or Phase B3 (auto-detection heuristics — improves UX on Linux first, would need to be redone for Windows). E is the natural follow-on if Windows users are the next audience; B3 if Linux user-acquisition is the priority.
2. **Windows live verification**: Phase C is dry-run verified on Windows 11 only. Live ES shipping from Windows (real game session, real PresentMon, real ES docs in Discover) has not been done. Worth doing before E lands so any Windows-specific shipper bugs surface before the MSI ships.
3. **PresentMon distribution**: Phase E must decide whether to bundle PresentMon.exe in the MSI (license-permissive, MIT) or direct users to download it. Bundling is friendlier; downloading keeps the MSI small.
4. **B3 scope** (unchanged): What heuristics? Window title scraping (xtitle), known exe name database, `/proc/<pid>/maps` scanning for game engine patterns? Needs scoping before implementation.

---

## Environment reminders

- Primary dev: CachyOS Linux (AMD Ryzen 7 9800X3D / Radeon RX 9070 XT)
- ES: `https://gamepulse-af41f9.es.us-central1.gcp.elastic.cloud`
- Kibana: `https://gamepulse-af41f9.kb.us-central1.gcp.elastic.cloud`
- Data view ID (wildcard): `18dd83e8-6f88-474f-b434-a4b6c14a04a2`
- Game Library dashboard ID: `e7d878d0-e2d6-454b-9a95-d93a4aeb70a8`
- Games dashboard ID: `5e898d7c-8de1-45b8-ae04-4cdc745f046d` (gamepulse-game-timeline, 9 panels, PASS verify)
- Environment dashboard ID: `3a55c257-0537-42a8-94a7-24dc773a703b` (metrics-gamepulse.* wildcard, 11 panels, PASS verify)
- Hardware dashboard ID: `ed9d9b94-2003-429c-b294-9d3f2ef737e7` (metrics-gamepulse.* wildcard, 12 panels, PASS verify, AMD RX 9070 XT)
- Compare dashboard ID: `828db140-b330-4d26-8045-40a7895bfc41` (gp-dv-timeline, 9 panels, PASS API verify; built via dashboard-designer agent — first agent-driven dashboard with no claude.ai prompt round-trip)
- Field-path rule on wildcard view: BARE keyword paths only (no `.keyword`) — Environment + Games + Hardware all bare; documented as rule #6 in `.claude/agents/dashboard-designer.md`
- Kibana 9.5.0 schema breaking changes: panel type "lens"→"vis", uid→id, dataset→data_source w/ data_view_reference, Elastic-Api-Version "1"→"2023-10-31"; documented in games-dashboard.json schema_notes
- Dual-axis XY: `"axis": "y2"` for right axis (not "right"); confirmed working in Environment dashboard; documented as schema note #10 in docs/dashboards.md
- Heroic installed: one Epic game (`911 Operator`, app_name UUID), GOG installed.json empty
- Lutris installed: one GOG/umu game (Thronebreaker), will show "Lutris — Native" until follow-up
