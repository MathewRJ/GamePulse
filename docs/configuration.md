# Configuration Reference

GamePulse is configured via a TOML file. The agent checks these locations in order:

1. Path specified with `--config` CLI flag
2. `./gamepulse.toml` (current directory)
3. `~/.config/gamepulse/gamepulse.toml` (user install)
4. `/etc/gamepulse/gamepulse.toml` (system install)

All settings can also be overridden via CLI flags. Run `gamepulse-agent --help` for the full list.

## `[elasticsearch]`

Connection settings for your Elasticsearch instance.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `endpoint` | string | `http://localhost:9200` | Elasticsearch URL including port |
| `api_key` | string | — | API key for authentication (recommended) |
| `cloud_id` | string | — | Elastic Cloud ID (alternative to endpoint) |
| `username` | string | — | Basic auth username (if not using API key) |
| `password` | string | — | Basic auth password |
| `index_prefix` | string | `gamepulse` | Prefix for all indices (e.g. `gamepulse-metrics-2026.03.30`) |
| `flush_interval_secs` | integer | `5` | How often to flush buffered metrics to ES |
| `batch_size` | integer | `100` | Number of documents per bulk API request |

**Authentication priority:** API key > Basic auth > No auth

## `[collection]`

Controls which metrics are collected.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `interval_ms` | integer | `1000` | Collection interval in milliseconds. 1000 = 1 sample/sec. Lower values increase CPU usage. |
| `cpu` | bool | `true` | CPU utilisation, clocks, temperature, power |
| `memory` | bool | `true` | RAM, swap, page faults, PSI pressure |
| `gpu` | bool | `true` | GPU utilisation, clocks, VRAM, temperature, power |
| `storage` | bool | `true` | Disk I/O throughput, latency, queue depth |
| `network` | bool | `false` | Network throughput, TCP retransmits. Enable for multiplayer games. |
| `ebpf` | bool | `false` | eBPF deep telemetry. Requires kernel 5.8+ and CAP_BPF. See [eBPF Guide](ebpf.md). |
| `frame_timing` | bool | `true` | FPS and frame time via MangoHud/gamescope |
| `game_detection` | bool | `true` | Auto-detect running Steam games |

## `[privacy]`

Controls what data is shared.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `opt_in_public` | bool | `false` | Share anonymous performance data in community dashboards |
| `share_ebpf` | bool | `false` | Include eBPF data in public sharing |
| `share_network` | bool | `false` | Include network metrics in public sharing |

When `opt_in_public` is false (default), all data stays in your personal Elasticsearch index. No data leaves your machine without explicit opt-in.

## Example configuration

```toml
[elasticsearch]
endpoint = "https://my-deployment.es.us-central1.gcp.cloud.es.io:9243"
api_key = "base64-encoded-api-key"
index_prefix = "gamepulse"
flush_interval_secs = 5
batch_size = 100

[collection]
interval_ms = 1000
cpu = true
memory = true
gpu = true
storage = true
network = false
ebpf = false
frame_timing = true
game_detection = true

[privacy]
opt_in_public = false
share_ebpf = false
share_network = false
```

## CLI flags

| Flag | Description |
|------|-------------|
| `--config PATH` | Config file path (default: `gamepulse.toml`) |
| `--es-endpoint URL` | Override ES endpoint |
| `--es-api-key KEY` | Override ES API key |
| `--interval-ms MS` | Override collection interval |
| `--debug` | Print metrics to stdout instead of shipping to ES |
| `--once` | Collect a single sample and exit |
| `--no-game-detection` | Disable game auto-detection |

## Environment variables

| Variable | Description |
|----------|-------------|
| `GAMEPULSE_NO_EBPF=1` | Disable eBPF even if configured (used by user-mode service) |
| `RUST_LOG=gamepulse=debug` | Enable debug logging |
| `RUST_LOG=gamepulse=trace` | Enable trace logging (very verbose) |

## Performance impact

The agent is designed to have minimal impact on gaming performance:

- CPU usage: typically less than 0.5% of a single core
- Memory: ~15–30 MB RSS
- Disk I/O: reads `/proc` and sysfs (no writes except to ES)
- Network: small bulk API requests every 5 seconds

The collection interval can be increased (e.g. `interval_ms = 2000`) to further reduce overhead.
