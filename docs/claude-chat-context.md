# GamePulse — Claude.ai Planning Session Context

This file is maintained by the claude.ai web interface (planning sessions) to
preserve context across conversations. It complements CLAUDE.md, which is for
Claude Code (implementation sessions).

**Update protocol:**
- claude.ai: update this file at the end of every planning session, then commit
- Claude Code: update CLAUDE.md current state section at the end of every session
- Neither should edit the other's file

Last updated: April 2026

---

## Project status snapshot

### What is built and verified
- **Python collector**: live on CachyOS gaming PC, all 8 metric streams shipping
- **Elastic Agent integration scaffold**: `elastic-package check` + `test static` 11/11 passing. Package builds to `gamepulse-0.1.0.zip`
- **11 ingest pipelines**: deployed to Elastic Cloud Serverless, index templates wired, `default_pipeline` set. 6 stale legacy pipelines deleted.
- **Live session verified**: Cyberpunk 2077 end-to-end (Proton, MangoHud, all 8 streams flowing, game detection working)
- **Kibana Dashboards API**: confirmed working on Serverless 9.4+ via `kibana-dashboards` agent skill
- **Config Comparison dashboard**: built via API, 16 panels, ID `21b663d6-de42-46c6-aeaf-e6c48e46ecec`, at `kibana/config-comparison-dashboard.json`
- **Baseline dashboard**: `kibana/gamepulse-dashboard.ndjson` ("GamePulse - Manual Creation v2") — UI-built and exported
- **CLAUDE.md**: updated with two-method dashboard workflow (API preferred, manual export fallback) and API schema notes
- **docs/dashboard-guide.md**: updated with corrected field paths, notes `driver_version` missing from session stream
- **Elastic Agent skills**: installed at `.claude/skills/` via `npx skills add elastic/agent-skills -a claude-code`
- **docs/claude-chat-context.md**: this file — cross-session continuity for claude.ai planning sessions

### Git state (end of last Claude Code session)
Branch: `main`, clean, up to date with `origin/main`

Key recent commits:
- `91536e3` — CLAUDE.md: two-method dashboard workflow, API schema notes
- `6a3692d` — kibana/config-comparison-dashboard.json: 16-panel dashboard
- `eaa029b` — docs/dashboard-guide.md: fixed proton_version path, noted missing driver_version
- `60657d9` — CLAUDE.md housekeeping

### Architecture notes
- Repo is cloned on the gaming PC (CachyOS) — Claude Code runs there
- MacBook also has a local clone for convenience
- Claude Code reads from and writes to git at the start and end of every session to prevent drift
- ES API key lives at `~/.config/gamepulse/gamepulse.toml` on the gaming PC

---

## Priorities (in order)

1. **Session Deep-Dive dashboard** — next dashboard to build via Kibana API
   - Expand baseline (`kibana/gamepulse-dashboard.ndjson`) with:
     - p95/p99 frame time percentile overlays
     - Stutter event annotations
     - Environment badge bar (game, OS, kernel, GPU driver, Proton)
   - Use same workflow: ES|QL validation → Kibana API → save JSON → commit
   - Spec in `docs/dashboard-guide.md`

2. **Add `driver_version` field** to session data stream
   - `gamepulse.gpu.driver_version` missing from `data_stream/session/fields/fields.yml`
   - After adding: run `elastic-package check`

3. **Fix CLAUDE.md planned dashboards list** — Configuration Comparison is marked
   as unbuilt (item 4) but is actually complete. Claude Code should update this.

4. **Fix package bloat** — `collector/.venv` inflating zip to ~12MB
   - Exclude via `.elastic-package-ignore` or equivalent

5. **Pipeline/system tests** — `elastic-package test system` needs local ES or Docker

6. **Phase 2: eBPF daemon design** — Rust + Aya, no code yet, needs design session

---

## Key decisions and why

### Dashboard workflow (two approved methods)
The original "never generate NDJSON" rule was written after painful failures with
hand-authored Kibana saved object NDJSON on Serverless 9.x. The `kibana-dashboards`
agent skill uses a new Kibana REST API (9.4+) designed for LLM generation — clean
JSON, no version tokens, server handles translation. This sidesteps the old problems.

- **Method A (preferred)**: `kibana-dashboards` skill → validate with ES|QL → POST JSON → retrieve + commit
- **Method B (fallback)**: build in Kibana UI → export NDJSON → commit
- **Never**: hand-author NDJSON

### Kibana API schema quirks (verified against Serverless 9.4.0)
- Options list control: use `field_name` (not `field`), `data_view_id`
- Terms x-axis: `{operation:"terms", fields:[...]}` — no `size` parameter
- Table type: `data_table` (not `table` or `datatable`)
- ES|QL panel type (`type:"esql"`) not supported inline — use `type:"dataView"` instead

### TSDS aggregation rules
All metric streams use TSDS mode:
- Counter fields: `MAX()` or `RATE()` only — never `AVG()` or `SUM()`
- Key counter: `gamepulse.fps.stutter_count`
- Gauge fields: `AVG()`, `MAX()`, `MIN()` all work normally

### API key
ES API key in `~/.config/gamepulse/gamepulse.toml`. The old note about needing
a separate Kibana UI key is outdated — the ES key works for dashboard operations.

---

## Planned dashboards (Phase 3)

| Dashboard | Status | Location |
|-----------|--------|----------|
| Session Deep-Dive | 🔲 next — needs expansion | `kibana/gamepulse-dashboard.ndjson` (baseline) |
| Configuration Comparison | ✅ built | `kibana/config-comparison-dashboard.json` |
| Storage & I/O Analysis | 🔲 planned | — |
| System Health | 🔲 planned | — |
| Game Library | 🔲 planned | — |
| Scheduler Analysis | 🔲 Phase 2 data required | needs eBPF stream |

---

## Verified field paths (Cyberpunk 2077 live session)

### Frame (`data_stream.dataset: "gamepulse.frame"`)
- `gamepulse.fps.avg_1s`, `gamepulse.fps.low_1pct`, `gamepulse.fps.low_01pct`
- `gamepulse.fps.frametime_ms`, `gamepulse.fps.stutter_count`
- `gamepulse.session.id.keyword`

### GPU (`data_stream.dataset: "gamepulse.gpu"`)
- `gamepulse.gpu.utilisation_pct`, `gamepulse.gpu.temperature_c`
- `gamepulse.gpu.hotspot_c`, `gamepulse.gpu.memory_temperature_c`
- `gamepulse.gpu.power_w`, `gamepulse.gpu.memory_used_mb`, `gamepulse.gpu.clock_mhz`
- ⚠️ `gamepulse.gpu.driver_version` — not in session stream fields.yml yet

### CPU (`data_stream.dataset: "gamepulse.cpu"`)
- `gamepulse.cpu.total_utilisation_pct`, `gamepulse.cpu.temperature_c`
- `gamepulse.cpu.clock_mhz_avg`

### Memory (`data_stream.dataset: "gamepulse.memory"`)
- `gamepulse.memory.system_used_mb`, `gamepulse.memory.swap_used_mb`
- ⚠️ `gamepulse.memory.game_rss_mb` — unreliable under Proton (tracks launcher not game)

### Storage (`data_stream.dataset: "gamepulse.storage"`)
- `gamepulse.storage.read_mbps`, `gamepulse.storage.write_mbps`
- `gamepulse.storage.queue_depth_current`

### Session (`data_stream.dataset: "gamepulse.session"`)
- `gamepulse.game.name.keyword`, `gamepulse.game.steam_app_id`
- `gamepulse.game.graphics_api`, `gamepulse.session.id.keyword`
- `host.name`, `host.os.name.keyword`, `host.os.type.keyword`

---

## How to resume a planning session

1. Paste or upload `docs/claude-chat-context.md` (this file) at the start
2. Paste the latest `CLAUDE.md` if anything major changed
3. Share any Claude Code session output relevant to current state
4. Ask: "what should we work on?" — claude.ai will reconcile and advise

## How to update this file

At the end of each claude.ai planning session:
```bash
# Download the updated file from the chat, then:
git add docs/claude-chat-context.md
git commit -m "docs: update claude-chat-context.md after planning session"
git push
```
