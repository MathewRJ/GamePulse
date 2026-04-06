# GamePulse — Claude Code Project Instructions

## What this project is

GamePulse is an open-source gaming performance telemetry platform.
It collects, ships, and visualises real-world gaming metrics to Elasticsearch.

The target audience is game developers, journalists, Proton/Wine/Mesa maintainers,
and package maintainers who need real-world performance data.

## Current state (as of last session)

### What is built and working

- **Python collector** (Phase 1): All metric collectors implemented and running on CachyOS gaming PC — CPU, GPU (AMD), memory, storage, network, audio, frame (MangoHud), power. Outputs `gamepulse.*` namespaced docs.
- **Elastic Agent integration scaffold** (Phase 0.5): `elastic-package check` and `elastic-package test static` both pass (11/11). Package builds to `gamepulse-0.1.0.zip`. 11 data streams defined with TSDS manifests, field mappings, ingest pipelines, and sample events.
- **Ingest pipelines deployed**: 11 pipelines live on Elastic Cloud Serverless (`metrics-gamepulse.<dataset>-default`). All index templates wired with `default_pipeline`. Pipeline simulation verified. 6 stale legacy pipelines deleted.
- **Live gameplay test passed**: Full session verified end-to-end (Cyberpunk 2077, Proton, MangoHud, all 8 streams, game detection working).
- **Scope document**: `docs/GamePulse-Scope-v3_2.md`

### What is not yet started

- **Rust production agent** (Phase 4): `src/`, `Cargo.toml`, `gamepulse-ebpf/` do not exist. The Python collector is the only working implementation.
- **eBPF daemon** (Phase 2 per v3.2): Not started.
- **Kibana dashboards**: Not built.

### Pending work (in priority order)

1. Fix package bloat — `collector/.venv` in zip ≈ 12 MB, needs exclusion.
2. Collector never writes session-end summary doc — `summary.*` fields always empty. Fix needed in collector.
3. Pipeline/system tests — need local ES or Docker environment.
4. Phase 2: eBPF daemon design (Rust/Aya).
5. Kibana dashboards from scratch using current field names.
   > **Note**: Do not hand-author NDJSON. Correct workflow: build/edit in
   > Kibana UI → export → commit. See `docs/kibana-lens-ndjson-reference.md`
   > for the full structural reference and Serverless constraints.
6. Phase 4: Rust production agent replacing Python collector.

## Stack

- **Collector (current)**: Python 3.11+ prototype
- **Collector (target)**: Rust + Aya framework for eBPF (Phase 4, not started)
- **Storage / visualisation**: Elasticsearch Serverless (Elastic Enterprise), Kibana
- **Hardware target**: AMD GPU primary (Linux); NVIDIA via community; Steam Deck
- **Packaging target**: Debian, RPM, AUR (not yet built)
- **CI/CD target**: GitHub Actions (not yet configured)
- **Key Linux interfaces**: sysfs/hwmon, /proc filesystem, MangoHud log

## Hardware notes (gaming PC)

Hardware-validated details for CachyOS (AMD Ryzen + RX 7900 XTX):

- AMD GPU: discrete card is **card1** (not card0); hwmon at hwmon3; scoring heuristic selects it correctly
- CPU temps: k10temp at hwmon5; temp1=Tctl (primary), temp3=Tccd1
- RAPL power: permission-denied without root — collector returns None gracefully
- CPU driver: amd-pstate-epp; cpufreq paths at `/sys/bus/cpu/devices/cpu*/cpufreq/`
- Storage: 3× NVMe (nvme0n1/nvme1n1/nvme2n1); `/games` ext4 (Steam library); collector detects nvme1n1p6

## Remote access

- **Elasticsearch**: `$ES_URL` / `$ES_API_KEY` — Elastic Cloud Serverless
- **Gaming PC**: `ssh gamingpc` (CachyOS, AMD GPU, MangoHud installed)

## Protected files — never edit without explicit task assignment

These files are integration-critical. Errors in them are silent until package validation:

- `manifest.yml`
- `tools/deploy_pipelines.py`
- `tools/wire_pipelines.py`
- `docs/GamePulse-Scope-v3_2.md`
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
cargo check                   # only once Rust src/ exists
cargo clippy -- -D warnings   # only once Rust src/ exists
cargo test                    # only once Rust src/ exists
cargo build --release         # only once Rust src/ exists
```

Do not run any other commands that modify the repo, network, or filesystem
without explicit user approval.

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
5. After any Rust code change (once src/ exists): run `cargo check` before declaring done.
6. Reviewer must approve before tester runs.
7. Progress auditor runs at every milestone boundary, not every task.

## Key file locations

### Python collector (current implementation)

- `collector/gamepulse/cli.py` — main loop, `_merge_docs()` deep-merge, bulk shipper
- `collector/gamepulse/session.py` — session lifecycle, `base_doc()` output
- `collector/gamepulse/enricher/host.py` — host OS enrichment
- `collector/gamepulse/collectors/` — per-subsystem collectors
- `tools/deploy_pipelines.py` — pipeline deployment tool
- `tools/wire_pipelines.py` — pipeline wiring tool

### Integration package

- `data_stream/` — 11 data streams (manifest, fields, pipeline, sample_event)
- `manifest.yml` — package root
- `docs/GamePulse-Scope-v3_2.md` — canonical scope document

### Kibana dashboards

- `kibana/gamepulse-dashboard.ndjson` — working dashboard (MacBook-built, import via Kibana UI)
- `dashboards/gamepulse-session-performance.ndjson` — session performance dashboard (local build)
- `docs/kibana-lens-ndjson-reference.md` — structural reference for Lens NDJSON and Serverless constraints

### Rust agent (target, not yet created)

- `src/main.rs` — entry point and main loop (future)
- `src/collectors/` — hardware and system collectors (future)
- `src/ebpf/` — eBPF probe manager (future)
- `src/shipper/` — Elasticsearch bulk API shipper (future)
- `gamepulse-ebpf/` — BPF kernel programs via Aya (future)
