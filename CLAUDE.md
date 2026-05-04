# GamePulse — Contributor Guide

## What this project is

GamePulse is an open-source gaming performance telemetry platform. It collects, ships, and
visualises real-world gaming metrics to Elasticsearch via an Elastic integration package.

**Stack:** Rust agent (`src/`), eBPF daemon (`ebpf/`), Elastic integration package
(`data_stream/`, `elastic/`, `_dev/`). Platform support: Linux (primary), Windows, Steam Deck.

## Protected files — never edit without explicit task assignment

Errors in these files are silent until package validation fails:

- `manifest.yml`, any file under `_dev/` or `packaging/`
- Ingest pipeline YAML/JSON files (any path matching `*pipeline*`)
- Index template JSON files, ILM policy JSON files

## Validation commands

```bash
elastic-package check
elastic-package test static
cargo check
cargo clippy -- -D warnings
cargo test
cargo build --release
```

After any Rust change: run `cargo check`.
After any pipeline/manifest change: run `elastic-package check`.

## Development workflow

This integration is developed using an AI-assisted workflow documented in `docs/archive/AGENT-SYSTEM.md`.
The workflow infrastructure lives in a separate private repository and is not a dependency of GamePulse.
