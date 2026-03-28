GamePulse — Project Context for Claude Code
What is this project?
GamePulse is an open gaming telemetry platform. A lightweight agent collects gaming performance metrics (FPS, GPU/CPU temps, frame times, storage I/O, etc.) on Linux and Windows gaming PCs and ships them to Elasticsearch for visualisation in Kibana dashboards.

Authoritative scope document
The full project scope, data model, metric inventory, and implementation plan lives in: docs/GamePulse — Project Scope & Implementation Plan v2.0

Always read this before making architectural decisions. It contains the agreed data model, field names, data stream naming conventions, and phasing.

Current phase: Phase 0 — Elasticsearch Foundation
We are building the Elasticsearch infrastructure first:

Component templates (reusable field mapping building blocks)
Index templates (composing component templates per data stream)
Ingest pipelines (enrichment, validation, derived fields)
Synthetic data generator (Python script producing realistic gaming session data)
Data stream lifecycle configuration
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
