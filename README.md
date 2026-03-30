# GamePulse

An open gaming telemetry platform. A lightweight agent collects gaming performance metrics — FPS, frame times, GPU/CPU temperatures, storage I/O, memory pressure, and more — from Linux and Windows gaming PCs and ships them to Elasticsearch for analysis in Kibana.

**Current status: Phases 0 and 1 complete** — Elasticsearch infrastructure and Python collector are both ready. Install the collector, point it at an Elasticsearch instance, and data starts flowing immediately. The Rust production agent (Phase 4) is planned.

---

## Why GamePulse?

Existing tools (MangoHud, MSI Afterburner, CapFrameX) are local-only. They can't answer questions like:

- Is Proton 9.0-4 actually faster than 9.0-3 for this game on my hardware class?
- Did the new Mesa/driver release cause a regression across the community?
- Is my SD card causing storage stutter vs the internal NVMe?
- What's the performance-per-watt sweet spot on my Steam Deck for this game?

GamePulse ships structured telemetry to Elasticsearch, enabling cross-session, cross-hardware, and cross-configuration comparisons backed by real data.

---

## What's working today

| Component | Status |
|-----------|--------|
| Elasticsearch component templates (12) | Done |
| Index templates for all 11 data streams | Done |
| Ingest pipelines (enrichment, validation, derived fields) | Done |
| Kibana dashboard (7 panels, field names validated) | Done — import `kibana/gamepulse-dashboard.ndjson` |
| Community analytics transform | Done |
| FPS regression watcher | Done |
| Synthetic data generator | Done — `elastic/synthetic-data/generate.py` |
| **Python collector — CPU, memory, storage** | **Done** |
| **Python collector — AMD GPU (sysfs)** | **Done** |
| **Python collector — NVIDIA GPU (nvidia-smi)** | **Done** |
| **Python collector — frame timing (MangoHud)** | **Done** |
| **Python collector — network, power/battery, audio** | **Done** |
| **Steam game detection (Proton/DXVK/VKD3D)** | **Done** |
| **Session environment snapshot (OS, hardware, drivers)** | **Done** |
| Kibana dashboard polish (Phase 2) | In development |
| Windows support (Phase 3) | Planned |
| Rust production agent (Phase 4) | Planned |
| eBPF deep telemetry (Phase 5) | Planned |

---

## Architecture

```
Hardware Sensors ──┐
  (sysfs, hwmon,   │
   NVML, /proc)    ├──→ GamePulse Collector ──→ Elasticsearch Bulk API
Game Process ──────┤       (1 sample/sec)
  (auto-detected,  │              │
   MangoHud logs)  │              ▼
                   │     Ingest Pipeline
eBPF Probes ───────┘     (enrichment, classification,
  (kernel tracing)        stutter & throttle detection)
                                  │
                                  ▼
                          Kibana Dashboards
                    (FPS, GPU, CPU, storage, sessions)
```

Data is stored in [Elasticsearch data streams](https://www.elastic.co/guide/en/elasticsearch/reference/current/data-streams.html) named `metrics-gamepulse.<dataset>-default`. Each document is append-only; the ingest pipeline adds derived fields on write.

---

## Data streams and metrics

| Data stream | What it captures |
|-------------|-----------------|
| `metrics-gamepulse.frame-default` | FPS, 1% low, 0.1% low, frame time (avg/min/max/stdev), stutter count |
| `metrics-gamepulse.gpu-default` | Utilisation, clocks, VRAM, temperature (edge/hotspot/memory), power, fan, voltage, PCIe |
| `metrics-gamepulse.cpu-default` | Per-core utilisation, clocks, temperature, RAPL power, governor, boost state |
| `metrics-gamepulse.memory-default` | System RAM, swap, PSI pressure, dirty pages, buffer/cache |
| `metrics-gamepulse.storage-default` | Throughput, IOPS, latency (avg/p95/p99), queue depth, I/O wait |
| `metrics-gamepulse.network-default` | Throughput, TCP retransmits, connection type |
| `metrics-gamepulse.audio-default` | Backend, latency, xruns |
| `metrics-gamepulse.power-default` | Battery %, drain rate, TDP, AC state |
| `metrics-gamepulse.session-default` | Full environment snapshot: OS, kernel, hardware, Proton/DXVK/VKD3D versions, game info |
| `metrics-gamepulse.ebpf-default` | Block I/O latency, scheduler latency, VFS latency, futex contention, GPU fence waits |
| `metrics-gamepulse.events-default` | Shader compilation, stutter events, crashes |

Session documents capture the full compatibility stack automatically: Proton version, Wine version, DXVK version, VKD3D-Proton version, Mesa version, gamescope version, graphics API, and drive type (NVMe vs SD card).

The ingest pipeline adds derived fields to every metrics document:

| Derived field | Logic |
|--------------|-------|
| `hardware_tier` | `enthusiast` / `high` / `mid` / `low` / `integrated` (based on VRAM) |
| `fps_bracket` | `120+` / `90-120` / `60-90` / `30-60` / `below_30` |
| `throttle_detected` | GPU hotspot > 90 °C or CPU temp > 95 °C |
| `has_stutter` | Any frame > 33 ms |
| `severe_stutter` | Any frame > 100 ms |
| `storage_bottleneck` | I/O wait > 5% or queue depth > 32 |

See [docs/metrics-reference.md](docs/metrics-reference.md) for the full field inventory with ES mapping types and units.

---

## Elasticsearch setup

### Prerequisites

- Elasticsearch 8.x — [Elastic Cloud](https://cloud.elastic.co/) (free tier works for personal use) or self-hosted
- Kibana for dashboards

### 1. Get your credentials

In Kibana → Stack Management → API Keys, create a key with `all` cluster and index privileges. Note your Elasticsearch endpoint URL.

### 2. Deploy index templates and ingest pipelines

The `elastic/` directory contains all JSON files deployable via Kibana Dev Tools or the ES REST API.

**Via Kibana Dev Tools** — paste and run each file as a `PUT` request:

```
PUT _component_template/gamepulse-session-context
{ ...contents of elastic/component-templates/gamepulse-session-context.json... }

PUT _index_template/metrics-gamepulse.frame
{ ...contents of elastic/index-templates/metrics-gamepulse.frame.json... }

PUT _ingest/pipeline/gamepulse-shared-enrichment
{ ...contents of elastic/ingest-pipelines/gamepulse-shared-enrichment.json... }
```

**Suggested deploy order:**
1. `elastic/component-templates/` — all 12 files (these are referenced by index templates)
2. `elastic/index-templates/` — all 11 files
3. `elastic/ingest-pipelines/` — `gamepulse-shared-enrichment.json` first, then the rest
4. `elasticsearch/pipelines/gamepulse-metrics-pipeline.json`
5. `elasticsearch/transforms/community-stats-transform.json`
6. `elasticsearch/watchers/fps-regression-watcher.json`

### 3. Load synthetic test data

Before the collector is ready, generate realistic test data to validate templates and dashboards:

```bash
# Install dependencies
pip install requests

# Generate 5 sessions of 1 hour each, output to a bulk file
python elastic/synthetic-data/generate.py --sessions 5 --duration 3600 --output bulk_data.ndjson

# Load into Elasticsearch
curl -X POST "https://YOUR_ES_ENDPOINT/_bulk" \
  -H "Authorization: ApiKey YOUR_API_KEY" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @bulk_data.ndjson

# Or print to stdout for inspection
python elastic/synthetic-data/generate.py --sessions 1 --duration 300 --stdout
```

The generator covers 8 games, 5 GPU profiles (AMD RX 7900 XTX, RX 6800 XT, NVIDIA GTX 1080 Ti, RTX 2080, Steam Deck 780M), 5 CPU configs, 4 OS profiles (Arch Linux, SteamOS, Fedora 39, Windows 11), and 4 upscaler configurations.

### 4. Import the Kibana dashboard

```bash
# Via curl
curl -X POST "https://YOUR_KIBANA_URL/api/saved_objects/_import" \
  -H "kbn-xsrf: true" \
  -H "Authorization: ApiKey YOUR_API_KEY" \
  --form file=@kibana/gamepulse-dashboard.ndjson

# Or via Makefile (prompts for Kibana URL)
make import-dashboards

# Or via UI: Kibana → Stack Management → Saved Objects → Import
```

The dashboard includes 7 panels: FPS over time, GPU metrics, CPU metrics, memory usage, storage I/O, frame time distribution, and game sessions table.

### 5. Set up the FPS regression watcher (optional)

The watcher runs every 6 hours and compares per-game FPS averages against the prior week, grouped by driver and Proton version. After deploying `elasticsearch/watchers/fps-regression-watcher.json`, activate it in Kibana → Stack Management → Watcher.

---

## Configuration

When the collector is available, it reads configuration from (in priority order):

1. `--config PATH` CLI flag
2. `./gamepulse.toml`
3. `~/.config/gamepulse/gamepulse.toml`
4. `/etc/gamepulse/gamepulse.toml`

A reference config is at [`config/gamepulse.toml`](config/gamepulse.toml):

```toml
[elasticsearch]
endpoint = "https://your-deployment.es.region.cloud.es.io:443"
# api_key = "your-base64-api-key-here"
index_prefix = "gamepulse"
flush_interval_secs = 5
batch_size = 100

[collection]
interval_ms = 1000      # 1 sample/second
cpu = true
memory = true
gpu = true
storage = true
network = false         # enable for multiplayer latency tracking
ebpf = false            # requires kernel 5.8+ and CAP_BPF
frame_timing = true     # FPS/frame time via MangoHud or gamescope
game_detection = true

[privacy]
opt_in_public = false   # share anonymous data in community dashboards
share_ebpf = false
share_network = false
```

See [docs/configuration.md](docs/configuration.md) for the full reference including CLI flags and environment variables.

---

## Running the collector

### Install

```bash
cd collector
pip install -e .
```

Dependencies: Python 3.11+, `httpx`, `psutil`. No root required for basic metrics; eBPF (Phase 5) will need `CAP_BPF`.

### Quick test — no Elasticsearch needed

```bash
gamepulse-collector --debug --once
```

Output (one line per data stream, per tick):

```
[00001] CPU:12.4% GPU:0%/45°C MEM:9842MB FPS:-- IO:R0.01/W0.00MB/s
```

Continuous debug mode — launch a Steam game and watch detection kick in:

```bash
gamepulse-collector --debug
```

### Ship to Elasticsearch

```bash
gamepulse-collector --es-endpoint https://my-deployment.es.region.cloud.es.io \
                    --es-api-key YOUR_API_KEY
```

Or put credentials in `~/.config/gamepulse/gamepulse.toml` (see [config reference](docs/configuration.md)) and run:

```bash
gamepulse-collector
```

### As a systemd service

```bash
# User-level (Steam Deck, no root)
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/gamepulse-collector.service <<EOF
[Unit]
Description=GamePulse telemetry collector
After=network.target

[Service]
ExecStart=%h/.local/bin/gamepulse-collector
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user enable --now gamepulse-collector
```

## Frame timing setup

The collector reads FPS and frame time data from MangoHud's CSV log. Add this to Steam launch options for each game:

```
MANGOHUD=1 MANGOHUD_LOG=1 %command%
```

Or set globally in `~/.config/MangoHud/MangoHud.conf`:

```ini
log_duration=0
output_folder=/tmp/MangoHud
```

On Steam Deck, MangoHud is pre-installed. Enable logging via Quick Access → Performance → Performance Overlay Level 1+, or add `MANGOHUD_LOG=1 %command%` to per-game launch options.

---

## Steam Deck

GamePulse detects Steam Deck LCD vs OLED automatically, reads APU sensors via sysfs, and integrates with gamescope for frame timing without MangoHud. It monitors both the internal NVMe and SD card, tracking which drive the current game is installed on.

Key Steam Deck specifics:
- Use user-mode install (`make install-user`) so the binary survives SteamOS root filesystem updates
- eBPF requires the system-level service with root — user-mode service skips eBPF
- TDP readings from sysfs enable performance-per-watt analysis

See [docs/steam-deck.md](docs/steam-deck.md) for full setup instructions.

---

## Privacy

- **User identity** is a SHA hash of `/etc/machine-id` — stable for session correlation, not reversible to a person
- **No PII ever collected**: no usernames, emails, IPs, or location data
- All data stays in your personal Elasticsearch instance unless you explicitly set `opt_in_public = true`
- Network metrics and eBPF data are opt-out of community sharing by default even when `opt_in_public` is true

---

## Repository structure

```
GamePulse/
├── config/
│   └── gamepulse.toml              # Reference configuration
├── docs/
│   ├── architecture.md             # Component and data flow design
│   ├── configuration.md            # Full config reference
│   ├── ebpf.md                     # eBPF setup and probe descriptions
│   ├── elasticsearch-setup.md      # Detailed ES deployment guide
│   ├── getting-started.md          # End-to-end quickstart
│   ├── metrics-reference.md        # Full field inventory with ES types
│   └── steam-deck.md               # Steam Deck-specific guide
├── elastic/
│   ├── component-templates/        # 12 reusable field mapping templates
│   ├── index-templates/            # 11 per-data-stream index templates
│   ├── ingest-pipelines/           # Enrichment, validation, derived fields
│   └── synthetic-data/
│       └── generate.py             # Realistic test data generator
├── elasticsearch/
│   ├── pipelines/                  # Aggregation pipeline
│   ├── transforms/                 # Community stats continuous transform
│   └── watchers/                   # FPS regression watcher
├── kibana/
│   └── gamepulse-dashboard.ndjson  # 7-panel dashboard export
├── collector/                      # Python prototype collector (Phase 1)
├── agent/                          # Rust production agent (Phase 4)
├── ebpf/                           # eBPF programs in Rust/Aya (Phase 5)
├── Makefile
└── config/gamepulse.toml
```

---

## Roadmap

| Phase | Scope | Status |
|-------|-------|--------|
| **0 — Elasticsearch Foundation** | Component templates, index templates, ingest pipelines, synthetic data generator, Kibana dashboard | **Complete** |
| **1 — Python Collector MVP** | Linux collector: CPU, GPU (AMD + NVIDIA), memory, storage, network, power, audio, MangoHud frame timing, Steam game + Proton detection | **Complete** |
| **2 — Kibana Dashboards** | Session overview, hardware comparison, system health, game library matrix, cross-platform comparison, storage analysis | In development |
| **3 — Windows Support** | PresentMon frame timing, NVML GPU metrics, WMI/PDH system metrics | Planned |
| **4 — Rust Production Agent** | Single binary, <0.5% CPU, <30 MB RAM, Elastic Agent integration | Planned |
| **5 — eBPF Integration** | Block I/O latency, scheduler latency, VFS latency, futex contention, GPU fence waits, Proton translation overhead | Planned |

**Hardware priority:** AMD GPU Linux → NVIDIA Linux → AMD Windows → NVIDIA Windows. Intel Arc and other GPUs welcome via community contributions.

**Game launchers:** Steam only for MVP. GOG and Epic detection planned post-Phase 1.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The most useful contributions right now:

- NVIDIA testing on Linux (the sysfs paths and NVML interface need validation across driver versions)
- Windows hardware collection (WMI paths, PresentMon integration)
- Additional game profiles in the synthetic data generator

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
