GamePulse — Project Context for Claude Code
What is this project?
GamePulse is an open gaming telemetry platform. A lightweight agent collects gaming performance metrics (FPS, GPU/CPU temps, frame times, storage I/O, etc.) on Linux and Windows gaming PCs and ships them to Elasticsearch for visualisation in Kibana dashboards.

Authoritative scope document
The full project scope, data model, metric inventory, and implementation plan lives in: docs/GamePulse — Project Scope & Implementation Plan v2.0

Always read this before making architectural decisions. It contains the agreed data model, field names, data stream naming conventions, and phasing.

Current phase: Phase 1 core collectors validated on real CachyOS hardware (AMD Ryzen + RX 7900 XTX). Next milestones: game session detection, MangoHud frame timing, environment fingerprinting, then full gaming session test.

Completed phases:
- Phase 0: Elasticsearch infrastructure — component templates, index templates, ingest pipelines, synthetic data generator, lifecycle config
- Phase 1 (core): Python collector — GPU (AMD + NVIDIA), CPU (k10temp/coretemp), memory (/proc/meminfo), storage (/proc/diskstats). All four validated end-to-end on real CachyOS hardware with data flowing to Elasticsearch. Remaining Phase 1 collectors (network, power, audio, MangoHud frame timing, Steam/Proton detection, session enrichment) are implemented but not yet hardware-validated.
- Phase 2 (initial): Kibana dashboard — 12 Lens panels (FPS timeline, frame time, GPU util/temp, GPU VRAM/clock, CPU util/temp, memory, storage, sessions table, 4 metric tiles), 3 filter controls (Game, Session ID, OS). Dashboard is in kibana/gamepulse-dashboard.ndjson — see docs/kibana-lens-ndjson-reference.md before making any dashboard changes programmatically.

Hardware-validated details (CachyOS, AMD Ryzen, RX 7900 XTX):
- AMD GPU: discrete card is card1 (not card0); hwmon3; scoring heuristic selects it correctly
- CPU temps: k10temp hwmon5; temp1=Tctl (primary), temp3=Tccd1
- RAPL power: permission-denied without root — collector returns None gracefully
- amd-pstate-epp driver; cpufreq paths at /sys/bus/cpu/devices/cpu*/cpufreq/
- Storage: 3x NVMe (nvme0n1/nvme1n1/nvme2n1); /games ext4 (Steam library); collector detects nvme1n1p6
- venv at /tmp/gp-venv with httpx installed; bash scripts via "bash /tmp/script.sh" (fish shell default)

IMPORTANT: Do NOT attempt to hand-author Kibana NDJSON from scratch. The correct workflow is: build/edit in Kibana UI → export → commit. See docs/kibana-lens-ndjson-reference.md for the full structural reference and Serverless constraints.
Technology stack
Elasticsearch target: Elastic Cloud Serverless (Enterprise licence)
Data model: Data streams, not traditional indices. Naming: metrics-gamepulse.<dataset>-default
Prototype collector: Python 3.11+ (Phase 1)
Production agent: Rust (Phase 4)
eBPF: Rust/Aya (Phase 5)
Dashboards: Kibana Lens primarily, TSVB for complex time-series
Data streams
metrics-gamepulse.frame-default
metrics-gamepulse.gpu-default
metrics-gamepulse.cpu-default
metrics-gamepulse.memory-default
metrics-gamepulse.storage-default
metrics-gamepulse.network-default
metrics-gamepulse.power-default
metrics-gamepulse.audio-default
metrics-gamepulse.session-default
metrics-gamepulse.ebpf-default
metrics-gamepulse.events-default
Repository structure (target)
gamepulse/
├── CLAUDE.md                         # This file
├── README.md
├── docs/                             # Scope, architecture, data model docs
├── elastic/
│   ├── component-templates/          # Reusable field mapping templates (JSON)
│   ├── index-templates/              # Per-data-stream templates (JSON)
│   ├── ingest-pipelines/             # Enrichment, validation pipelines (JSON)
│   ├── lifecycle-policies/           # Data stream lifecycle config (JSON)
│   ├── kibana/
│   │   ├── dashboards/              # NDJSON exports
│   │   ├── saved-searches/
│   │   └── visualisations/
│   └── synthetic-data/              # Python test data generator
├── collector/                        # Python prototype (Phase 1)
├── agent/                            # Rust production agent (Phase 4)
├── ebpf/                             # eBPF programs (Phase 5)
└── tools/                            # Utilities (Steam AppID resolver, etc.)
Conventions
All Elasticsearch templates use JSON files deployable via Kibana Dev Tools or the ES API
Field names use the gamepulse.* namespace with ECS alignment where possible
Component templates are named gamepulse-<purpose> (e.g., gamepulse-session-context)
Index templates are named metrics-gamepulse.<dataset> matching data stream names
Python code uses pyproject.toml, type hints, and async where appropriate
Commit messages: imperative mood, concise
Key context
Owner has AMD GPU Linux desktop, Windows 11 AMD desktop, Steam Deck, GTX 1080 Ti, RTX 2080
NVIDIA support for other cards will rely on community contributions
MVP focuses on Steam games only (no GOG/Epic detection initially)
Priority order: 1) Polished dashboards, 2) Cross-platform parity, 3) Deep metrics, 4) eBPF
Collection frequency is 1/second — Python prototype overhead is acceptable

Remote Access
Elastic Cloud Serverless
Elasticsearch endpoint: environment variable ES_URL (use for template management, queries, and data ingestion)
API key: environment variable ES_API_KEY — use in header: Authorization: ApiKey $ES_API_KEY
High-volume bulk ingestion endpoint: ES_INGEST_URL (ES_URL works for development)
Version: Elasticsearch 9.4.0 serverless, build_flavor: serverless

CachyOS Gaming PC (SSH)
SSH alias: ssh gamingpc
OS: CachyOS (Arch-based Linux)
GPU: AMD — sysfs at /sys/class/drm/card0/device/hwmon/
Steam installed with games; MangoHud installed
Python 3.x available
ES credentials are passed via ES_URL and ES_API_KEY environment variables when running the collector on the gaming PC (see scripts/run-gaming-test.sh)
No repo clone on gaming PC — all code stays on the MacBook
Use scp or ssh with piped commands to push scripts for execution

Use the gaming PC for: reading real sysfs/procfs data, testing collectors against real hardware, running gaming sessions with MangoHud, validating GPU/CPU/storage metrics

Example commands:
  # Deploy a component template
  curl -X PUT "$ES_URL/_component_template/gamepulse-session-context" \
    -H "Authorization: ApiKey $ES_API_KEY" \
    -H "Content-Type: application/json" \
    -d @elastic/component-templates/gamepulse-session-context.json

  # Read GPU temp from gaming PC
  ssh gamingpc "cat /sys/class/drm/card0/device/hwmon/hwmon*/temp1_input"

  # Pipe a local script to the gaming PC
  cat collector/gamepulse/collectors/gpu/amd_linux.py | ssh gamingpc "python3 -"

  # Copy a script and run it
  scp collector/gamepulse/cli.py gamingpc:/tmp/ && ssh gamingpc "python3 /tmp/cli.py"
