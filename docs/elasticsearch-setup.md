# Elasticsearch Setup Guide

GamePulse ships data to Elasticsearch and provides pre-built Kibana dashboards. This guide covers setup for both Elastic Cloud and self-hosted deployments.

## Elastic Cloud (recommended for getting started)

1. Sign up at [cloud.elastic.co](https://cloud.elastic.co/) — the free tier works for personal use
2. Create a deployment (any region, default settings are fine)
3. Note your **Elasticsearch endpoint** from the deployment overview
4. Create an API key in Kibana → Stack Management → API Keys

## Self-hosted

Any Elasticsearch 8.x instance works. Ensure you have:
- At least 2GB RAM allocated to Elasticsearch
- TLS configured (or disable certificate verification in the agent config for local dev)
- Kibana accessible for dashboards

## Automated setup

Run the setup script to deploy all infrastructure at once:

```bash
./scripts/setup-elasticsearch.sh YOUR_ES_ENDPOINT YOUR_API_KEY
```

This creates:
- ILM policy (`gamepulse-ilm`) — manages index lifecycle from hot → warm → cold → delete
- Ingest pipeline (`gamepulse-metrics`) — enriches incoming metrics with hardware tier, FPS bracket, throttle detection
- Index templates for sessions, metrics, eBPF, and events
- Community stats transform — aggregates per-session performance data
- FPS regression watcher — alerts on performance drops after driver/Proton updates

## Index structure

GamePulse creates four types of indices:

| Index pattern | Content | Rollover |
|---------------|---------|----------|
| `gamepulse-sessions-YYYY.MM` | One document per gaming session (environment snapshot) | Monthly |
| `gamepulse-metrics-YYYY.MM.DD` | Per-second time-series metrics | Daily |
| `gamepulse-ebpf-YYYY.MM.DD` | eBPF histogram and event data | Daily |
| `gamepulse-events-YYYY.MM.DD` | Discrete events (stutter, crash, shader compile) | Daily |

The ILM policy keeps hot data for 7 days, moves to warm at 30 days, cold at 90 days, and deletes at 365 days. Adjust in Kibana → Stack Management → Index Lifecycle Policies.

## Ingest pipeline

The `gamepulse-metrics` pipeline runs on every incoming metrics document and adds:

- `hardware_tier` — Enthusiast / High / Mid / Low / Integrated (based on VRAM)
- `fps_bracket` — 120+ / 90-120 / 60-90 / 30-60 / below_30
- `throttle_detected` — true if GPU > 90°C or CPU > 95°C
- `has_stutter` — true if any frames exceeded 33ms
- `severe_stutter` — true if any frame exceeded 100ms
- `storage_bottleneck` — true if I/O wait > 5% or queue depth > 32
- `gpu.memory_pressure_pct` — VRAM usage as a percentage

## Kibana dashboards

Import the pre-built dashboards:

```bash
curl -X POST "https://your-kibana:5601/api/saved_objects/_import" \
  -H "kbn-xsrf: true" \
  --form file=@kibana/gamepulse-dashboard.ndjson
```

Or import via UI: Kibana → Stack Management → Saved Objects → Import.

### Included panels

- **FPS Over Time** — line chart with avg FPS, 1% low, and 0.1% low
- **Frame Time Distribution** — avg, max, and standard deviation of frame times
- **GPU Metrics** — utilisation, temperature, and power draw
- **CPU Metrics** — utilisation and temperature
- **Memory Usage** — system RAM and VRAM utilisation
- **Storage I/O** — read/write throughput
- **Game Sessions** — table of detected games with Proton version and graphics API

### Building custom dashboards

Useful fields for custom visualisations:

- `fps.fps_avg` — average FPS for time-series charts
- `fps.fps_1pct_low` — 1% low FPS (better indicator of smoothness than average)
- `game.name` — filter by specific game
- `compatibility.proton_version` — compare Proton versions
- `hardware.gpu.driver_version` — compare driver versions
- `hardware_tier` — group by hardware class
- `throttle_detected` — filter to sessions with thermal throttling
- `storage.game_drive.drive_type` — compare NVMe vs SD card

## Community analytics

When users opt in to public sharing, the continuous transform aggregates their session summaries into `gamepulse-community-stats`. This enables cross-user comparison dashboards without exposing individual user data.

The FPS regression watcher runs every 6 hours, comparing per-game FPS averages between the current week and the previous week, grouped by driver and Proton version. When a regression is detected, it logs to `gamepulse-events-regression`.

## Data volume estimates

At the default 1-second collection interval:

| Usage | Documents/day | Storage/day (compressed) |
|-------|---------------|--------------------------|
| 1 hour gaming | 3,600 | ~2 MB |
| 4 hours gaming | 14,400 | ~8 MB |
| Heavy (8 hours) | 28,800 | ~16 MB |

Session documents are negligible in size. eBPF data (when enabled) adds roughly 50% more volume.

The Elastic Cloud free tier (8 GB storage) can hold several months of daily gaming data.
