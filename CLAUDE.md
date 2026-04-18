# GamePulse — Claude Code Project Instructions

## Where to find project state

`docs/STATUS.md` is the single source of truth for project state. Read it at the start of every session. Update it after every completed work package. This file (`CLAUDE.md`) contains rules and conventions only — no state.

---

## What this project is

GamePulse is an open-source gaming performance telemetry platform.
It collects, ships, and visualises real-world gaming metrics to Elasticsearch.

The target audience is game developers, journalists, Proton/Wine/Mesa maintainers,
and package maintainers who need real-world performance data.

## Stack

- **Collector (production)**: Rust agent in `src/` — production-primary, ES-verified
- **Collector (legacy reference)**: Python prototype in `collector/`
- **eBPF daemon**: Rust + Aya in `ebpf/` — Sprints 1–3 ES-confirmed
- **Storage / visualisation**: Elasticsearch Serverless (Elastic Enterprise), Kibana
- **Hardware target**: AMD GPU primary (Linux); NVIDIA via community; Steam Deck
- **Packaging target**: AUR (complete); Debian/RPM (Milestone D); Windows MSI (Milestone E)
- **CI/CD target**: GitHub Actions (Milestone D)
- **Key Linux interfaces**: sysfs/hwmon, /proc filesystem, MangoHud log

## Cross-session continuity

Two files maintain context across sessions:
- `docs/STATUS.md` — single source of truth for project state. Both claude.ai and Claude Code read at session start and write after each WP completion.
- `docs/HANDOFF.md` — narrative history of implementation sessions. Prepend a new entry at the start of each Claude Code session.

Claude Code must never edit planning context docs that belong to claude.ai — use `docs/STATUS.md` for all shared state.
claude.ai must never directly edit `CLAUDE.md`.

## Session hygiene

- Always run `git pull` before starting any work in a session.
- Always run `git push` immediately after every commit.
- Never start implementation work if `git status` shows unpushed commits or if the branch is behind `origin/main`.
- If the branch has diverged, stop and flag it to the user before doing anything else.

## Workflow rules

1. One task at a time. No opportunistic refactors.
2. No dependency version changes unless the task explicitly requires it.
3. No changes to protected files without a planner-assigned task targeting them.
4. After any pipeline/manifest change: run `elastic-package check` before declaring done.
5. After any Rust code change: run `cargo check` before declaring done.
6. Reviewer must approve before tester runs.
7. Progress auditor runs at every milestone boundary, not every task.

## Protected files — never edit without explicit task assignment

These files are integration-critical. Errors in them are silent until package validation:

- `manifest.yml`
- `tools/deploy_pipelines.py`
- `tools/wire_pipelines.py`
- `docs/SCOPE.md`
- Any file under `_dev/`
- Any file under `packaging/`
- Ingest pipeline YAML/JSON files (any path matching `*pipeline*`)
- Index template JSON files
- ILM policy JSON files

## Validation commands (the only approved test commands)

```
elastic-package check
elastic-package test static
elastic-package test system   # requires local ES or Docker
cargo check
cargo clippy -- -D warnings
cargo test
cargo build --release
```

Do not run any other commands that modify the repo, network, or filesystem
without explicit user approval.

## Package build

Use `bash scripts/build-package.sh` instead of `elastic-package build` directly.
Use `bash scripts/test-asset.sh` instead of `elastic-package test asset` directly.
Both scripts stash large dev-only directories to `/tmp` before building (restores
on exit). Produces a lean package vs the raw build which includes `target/` (277MB).

`elastic-package check`, `elastic-package test static`, and `elastic-package test pipeline`
can still be run directly (they don't rebuild the package from the repo root).

## elastic-package test suite status

| Test type | Result | Notes |
|-----------|--------|-------|
| `elastic-package test static` | 11/11 PASS | Run directly |
| `elastic-package test pipeline` | 11/11 PASS | Run directly; uses remote ES (ELASTIC_PACKAGE_* env vars) |
| `elastic-package test asset` | 12/12 PASS | Run via `bash scripts/test-asset.sh`; requires local Docker stack |
| `elastic-package test policy` | "No test results" | No policy test fixtures configured; acceptable |
| `elastic-package test system` | "No test results" — acceptable skip | Hardware-dependent integration; elastic/integrations guidelines allow skip |

## Hardware notes (gaming PC)

Hardware-validated details for CachyOS (AMD Ryzen + RX 9070 XT):

- AMD GPU: **RX 9070 XT** (RADV GFX1201), Mesa 26.0.4, driver 26.0.4. Discrete card is **card1** (not card0); max-VRAM scoring heuristic selects it correctly
- CPU temps: k10temp at hwmon5; temp1=Tctl (primary), temp3=Tccd1
- RAPL power: permission-denied without root — collector returns None gracefully
- CPU driver: amd-pstate-epp; cpufreq paths at `/sys/bus/cpu/devices/cpu*/cpufreq/`
- Storage: 3× NVMe (nvme0n1/nvme1n1/nvme2n1); `/games` ext4 (Steam library); collector detects nvme1n1p6

## Remote access

- **Elasticsearch**: `$ES_URL` / `$ES_API_KEY` — Elastic Cloud Serverless
- **Gaming PC**: `ssh gamingpc` (CachyOS, AMD GPU, MangoHud installed)

## Key file locations

### Rust agent (production)

- `src/main.rs` — main loop, SIGTERM handling, session lifecycle, bulk shipper call
- `src/session.rs` — Steam /proc scan, ACF name lookup, session.json, label generation
- `src/host.rs` — once-at-startup hardware snapshot, dGPU selection heuristic
- `src/config.rs` — config loading, mirrors Python config.py
- `src/shipper.rs` — ES Bulk API shipper
- `src/collectors/` — 8 per-subsystem collectors (cpu, gpu_amd, memory, storage, network, power, audio, mangohud)

### Python collector (legacy reference)

- `collector/gamepulse/cli.py` — main loop, `_merge_docs()` deep-merge, bulk shipper
- `collector/gamepulse/session.py` — session lifecycle
- `collector/gamepulse/enricher/host.py` — host OS enrichment
- `collector/gamepulse/collectors/` — per-subsystem collectors

### Integration package

- `data_stream/` — 11 data streams (manifest, fields, pipeline, sample_event)
- `manifest.yml` — package root
- `docs/SCOPE.md` — canonical scope document

### Kibana dashboards

- `dashboards/` — all dashboard files. See `docs/dashboards.md` for the full list with IDs.

### Packaging

- `packaging/gamepulse-launcher.sh` — unified launcher CLI (setup/start/stop/status/run)
- `packaging/PKGBUILD` — AUR package build script
- `packaging/systemd/gamepulse-agent.service` — user systemd unit
- `packaging/systemd/gamepulse-ebpf.service` — system systemd unit (CAP_BPF)
- `packaging/config/gamepulse.toml.example` — example config installed to `/etc/gamepulse/`

### Documentation

- `docs/STATUS.md` — project state (single source of truth)
- `docs/ROADMAP.md` — milestone and work package definitions (structure-only)
- `docs/SCOPE.md` — canonical scope document
- `docs/HANDOFF.md` — session narrative history
- `docs/install.md` — unified installation guide
- `docs/dashboards.md` — dashboard build guide + NDJSON reference
- `architecture/` — architecture stubs at repo root (ebpf.md, data-model.md, agent.md)

## Skills inventory

Project-specific reference skills in `.agents/skills/` (force-added to git despite
parent directory gitignore; Elastic-provided skills are not committed).

| Skill | SKILL.md | Coverage |
|-------|----------|----------|
| `elasticsearch-tsds` | `.agents/skills/elasticsearch-tsds/SKILL.md` | keyword vs text rules, .keyword suffix, TSDS dimension restrictions, backing index conflict detection/resolution, rollover procedure, ES\|QL validation pattern |
| `gamepulse-data-model` | `.agents/skills/gamepulse-data-model/SKILL.md` | All 10 data stream index patterns + modes, canonical field paths, TSDS dimension fields, session.id vs session.label, gamepulse-game-timeline fields, data view IDs, known bugs |
| `gamepulse-workflow` | `.agents/skills/gamepulse-workflow/SKILL.md` | Pre/post-session checklists, field validation pattern, Rust/dashboard change checklists, elastic-package commands, systemd service patterns, journald commands, common mistakes |
| `kibana-dashboards` | `.agents/skills/kibana-dashboards/SKILL.md` | Kibana 9.4 Dashboards API, Lens panel types, GamePulse-specific Serverless lessons (.keyword rules, _import/.ndjson, _export, game-timeline field inventory) |
| `elasticsearch-esql` | `.agents/skills/elasticsearch-esql/SKILL.md` | ES\|QL query execution, time bucketing, aggregations (Elastic-provided) |
| `kibana-vega` | `.agents/skills/kibana-vega/SKILL.md` | Vega/Vega-Lite with ES\|QL data sources (Elastic-provided) |
| `elastic-mcp-setup` | `.agents/skills/elastic-mcp-setup/SKILL.md` | Elastic Agent Builder MCP server setup — API key creation, Claude Code wiring, when to use for ES\|QL field validation |

Elastic-provided skills (not committed; recreate with `npx skills add elastic/agent-skills -a claude-code`):
`cloud-network-security`, `elasticsearch-file-ingest`, `elasticsearch-onboarding`,
`kibana-connectors`, `kibana-streams`, `observability-logs-search`,
`observability-manage-slos`, `observability-service-health`

## Kibana dashboard conventions

Dashboard files live in `dashboards/` (not `kibana/`). The `kibana/` directory
is reserved for the Phase G integration package format (`kibana/dashboard/`
with proper NDJSON saved objects).

Two approved methods — use the kibana-dashboards skill where possible:

**Method A — Kibana Dashboards API (preferred)**
Validate fields with ES|QL first, then generate dashboard JSON and POST via the skill.
Retrieve result and save to `dashboards/<name>.json`. Commit and push.

**Method B — Manual Kibana UI export (fallback)**
Use when the API doesn't support a needed panel type. Build in Kibana UI →
Stack Management → Saved Objects → Export → commit as `.ndjson`.

**Never do**: hand-author NDJSON files — version-sensitive, will fail on Serverless.

### Elastic compliance rules (required for elastic/integrations submission)
- All visualizations must be defined by value (part of the dashboard), not saved to the Visualize library
- Every panel must include a `data_stream.dataset` filter
- Visualization titles must not include the package name
- Use Kibana Lens only — no TSVB, no Vega, no legacy aggregation-based panels
- Build against stable Kibana (Serverless current), never SNAPSHOT
