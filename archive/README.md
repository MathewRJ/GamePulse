# Archive

This directory contains code that is no longer actively maintained.
It is kept for historical reference only — **do not use any of this in production**.

| Directory | What it was | Why archived | Archived |
|-----------|-------------|--------------|---------|
| `collector/` | Legacy Python reference collector | Superseded by the Rust agent in `src/`. The Python implementation was the original prototype; all data streams now ship via the Rust binary. | 2026-05-04 |
| `elastic-legacy/` | Freestyle Elasticsearch templates (pipeline, transform, watcher) | Superseded by TSDS component templates in `elastic/`. The legacy templates used free-form index patterns; current design uses Time Series Data Streams with `@timestamp` dimension enforcement. | 2026-05-04 |
| `docs/` | Historical planning and design documents | Moved from `docs/archive/` to satisfy elastic-package validation (no subdirectories allowed under `docs/`). Includes scope documents, agent collaboration notes, and architecture design records. | 2026-05-04 |

## Security note

No credentials, API keys, or private endpoints are stored in this directory.
All authentication in the archived Python code reads from environment variables (`ES_API_KEY`, `ES_URL`) — never hardcoded.
