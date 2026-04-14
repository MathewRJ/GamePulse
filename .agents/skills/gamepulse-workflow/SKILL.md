---
name: gamepulse-workflow
description: >
  GamePulse development workflow reference: pre/post-session checklists, field
  validation pattern, Rust change checklist, dashboard change checklist, systemd
  service patterns, journald inspection, and common mistakes with their fixes.
  Use at session start/end and before commits or deployments.
metadata:
  author: gamepulse-project
  version: 1.0.0
---

# GamePulse Development Workflow

## Pre-Session Checklist

Every session, before any implementation work:

```bash
git pull                  # sync with remote
git status                # confirm clean tree (no unstaged changes)
git log --oneline -5      # orient to recent commits
```

If `git status` shows unpushed commits or the branch is behind `origin/main`,
**stop and flag to the user before doing anything else**.

## Post-Session Checklist

At the end of every session, in order:

1. Update `CLAUDE.md` — "Current state" section with date + summary label
2. Prepend a new entry to `docs/HANDOFF.md`
3. `git add CLAUDE.md docs/HANDOFF.md && git commit -m "docs: ..."` + `git push`
4. Confirm `git status` is clean

**Never end a session with uncommitted doc changes or unpushed commits.**

## Field Validation Pattern (Before Dashboard Building)

Always validate fields against live data BEFORE building any Lens panel.
Silent dashboard failures (null panels, empty controls) are caused by wrong field paths.

```bash
# Step 1 — confirm field exists and check for type conflicts
# If verification_exception appears, see elasticsearch-tsds skill
FROM metrics-gamepulse.session-default
| KEEP gamepulse.game.name, gamepulse.session.id, gamepulse.session.label
| LIMIT 3

# Step 2 — confirm aggregation works on the field
FROM metrics-gamepulse.cpu-default
| STATS avg_cpu = AVG(gamepulse.cpu.total_utilisation_pct)
| LIMIT 1

# Step 3 — confirm filter works (for controls)
FROM metrics-gamepulse.session-default
| WHERE gamepulse.game.name == "Starfield"
| STATS count = COUNT()
```

Use the `elasticsearch-esql` skill to run these queries interactively.
If queries fail, diagnose before building the dashboard.

## Rust Change Checklist

Any change to `src/` must pass these checks before commit:

```bash
cargo check 2>&1 | grep "^error"     # must be empty
```

For changes that add/modify collector fields or data model:
```bash
elastic-package check                  # lint + build; must say "Done"
```

For commits:
```bash
git add src/<changed files>
git commit -m "feat/fix/refactor: description"
git push
```

**Never skip `cargo check`.** The CI equivalent is not configured yet — you are the check.

## Dashboard Change Checklist

Before editing a dashboard that exists in Kibana:

```bash
# 1. Fetch the live dashboard (NOT from local file — it may be stale)
# Use _export API (GET /api/saved_objects/dashboard/{id} returns 400 on Serverless)
POST /api/saved_objects/_export
Body: {"objects":[{"type":"dashboard","id":"<id>"}],"includeReferencesDeep":false}
```

After making changes to the local JSON:

```bash
# 2. Import with overwrite (file MUST have .ndjson extension)
cp dashboards/home-dashboard.json /tmp/home-dashboard.ndjson
# Then POST multipart/form-data to /api/saved_objects/_import?overwrite=true
```

Commit the updated dashboard file:
```bash
git add dashboards/<name>.json
git commit -m "fix(dashboard): description"
git push
```

## elastic-package Commands

| Command | When to run | Notes |
|---------|------------|-------|
| `elastic-package check` | After any fields.yml, manifest.yml, or pipeline change | Runs lint + build; catches type errors and schema violations |
| `elastic-package test static` | After fields.yml changes | Validates field mappings statically |
| `elastic-package test pipeline` | After ingest pipeline changes | Requires `ELASTIC_PACKAGE_*` env vars |
| `bash scripts/build-package.sh` | To build clean .zip for distribution | Stashes `target/` to avoid 277MB bloat in zip |
| `bash scripts/test-asset.sh` | Before releasing; requires Docker | Stashes large dirs before running `elastic-package test asset` |

**Never run `elastic-package build` directly** — it picks up `target/` and produces a 1.1GB zip.
**Never run `elastic-package install` without `--zip`** — rebuilds from source and overwrites the clean zip.

## Systemd Service Patterns

### gamepulse-agent.service (user service)

Runs as the logged-in user via `systemctl --user`.

```ini
[Unit]
Description=GamePulse metrics agent
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/gamepulse-agent --config /etc/gamepulse/gamepulse.toml
Restart=on-failure
RestartSec=5
Environment=HOME=/home/%u        # required — PAM may not inject HOME
Environment=GAMEPULSE_LOG=info   # agent reads GAMEPULSE_LOG, NOT RUST_LOG

[Install]
WantedBy=default.target
```

**Critical env vars:**
- `HOME=/home/%u` — `game_name_from_appid()` uses HOME to find Steam library paths.
  Without it (or with HOME=/root), Steam ACF lookup fails silently.
- `GAMEPULSE_LOG` — not `RUST_LOG`. The tracing_subscriber reads `GAMEPULSE_LOG`.
  Set to `debug` for verbose logs: `systemctl --user edit gamepulse-agent` then
  add `[Service] / Environment=GAMEPULSE_LOG=debug`.

### gamepulse-ebpf.service (system service)

Runs as root/system via `systemctl` (not `--user`).

```ini
[Unit]
Description=GamePulse eBPF kernel daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/gamepulse-ebpf --config /etc/gamepulse/gamepulse.toml \
  --probe-path /usr/lib/gamepulse/gamepulse-ebpf-probes
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH
CapabilityBoundingSet=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

**Required capabilities:**
- `CAP_BPF` — load BPF programs
- `CAP_PERFMON` — read performance counters
- `CAP_SYS_ADMIN` — attach tracepoints
- `CAP_DAC_READ_SEARCH` — read `/proc/*/environ` for game PID detection

### IPC: /tmp/gamepulse/session.json

The agent and eBPF daemon communicate via `/tmp/gamepulse/session.json`.
This path is **hardcoded** — never use `$XDG_RUNTIME_DIR` (stripped by sudo).

Directory is created with mode `1777` (world-writable, sticky) by the agent,
so the non-root daemon can write updates.

Format:
```json
{
  "session_id": "uuid",
  "game_pid": 340621,
  "game_name": "Starfield",
  "game_pids": [340621, 340630, ...],
  "steam_app_id": 1716740
}
```

### Journald Log Inspection

```bash
# Agent logs (user service)
journalctl --user -u gamepulse-agent -f
journalctl --user -u gamepulse-agent --since "10 min ago"
journalctl --user -u gamepulse-agent | grep -E "Game detected|No game|SIGTERM|error"

# eBPF daemon logs (system service)
journalctl -u gamepulse-ebpf -f
journalctl -u gamepulse-ebpf --since "10 min ago"

# Last session summary
journalctl --user -u gamepulse-agent | grep -E "session summary|ticks|stopped"
```

Look for:
- `"Game detected: <name>"` — game PID scan worked
- `"No game detected — scanning /proc every 5 s"` — idle polling (every 30s log)
- `"SIGTERM received"` — clean shutdown
- `"Failed to ship"` — ES connectivity or auth issue

## Common Mistakes and Fixes

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Shipping data before index template deployed | `verification_exception` on all ES|QL queries | Delete old backing indices (see elasticsearch-tsds skill) |
| Using `.keyword` on post-template native keyword field | Empty ES|QL results, no error | Remove `.keyword` suffix; use bare field path |
| Using bare text field in Kibana filter control | Control appears but produces no results | Add `.keyword` suffix in `field_name` |
| `proton_version` in game-timeline panel | Panel render error | Remove column — field not in this index |
| Importing dashboard with `.json` extension | HTTP 400 "Invalid file extension" | Rename to `.ndjson` before import |
| `GET /api/saved_objects/dashboard/{id}` on Serverless | HTTP 400 | Use `POST /api/saved_objects/_export` instead |
| `elastic-package build` directly (no script) | 1.1GB zip with `target/` included | Use `bash scripts/build-package.sh` |
| Running `cargo check` with `RUSTFLAGS=-C target-cpu=native` | BPF cross-compile breaks | Prefix eBPF build commands with `RUSTFLAGS=""` |
| Adding `dimension: true` to non-TSDS stream | `elastic-package check` error | `dimension: true` only valid in TSDS streams |
| Adding `nested` field type to TSDS stream | Bulk insert rejected by ES | Remove TSDS `index_mode` or use `object` instead |
| `HOME` not set in systemd service | Steam game name lookup silently returns "App 1234567" | Add `Environment=HOME=/home/%u` to service unit |
| Checking `RUST_LOG` in agent logs | No output, wrong env var | Agent reads `GAMEPULSE_LOG` |
| CachyOS LTO breaks ring crate in AUR build | `undefined symbol: ring_core_*` | Add `options=(!lto)` to PKGBUILD |
| `target-cpu=native` in eBPF cross-compile | BPF linker rejects znver5 | Prefix with `RUSTFLAGS=""` |

## Protected Files (Never Edit Without Explicit Task)

```
manifest.yml                    # integration package root
tools/deploy_pipelines.py       # pipeline deployment
tools/wire_pipelines.py         # pipeline wiring
docs/GamePulse-Scope-v3_2.md   # canonical scope
_dev/                           # all elastic-package test fixtures
packaging/                      # systemd units, PKGBUILD
*pipeline*                      # ingest pipeline YAML/JSON files
```

Changes to these require an explicit planner-assigned task.
Always run `elastic-package check` after any pipeline or manifest change.
