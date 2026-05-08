# GamePulse

GamePulse is a gaming performance telemetry platform. A lightweight Rust agent collects FPS, frame times, GPU/CPU temperatures, storage I/O, memory pressure, and more from Linux gaming PCs and ships them to Elasticsearch for analysis in Kibana. Windows support is available (Phases B–C complete). The legacy Python reference implementation (`collector/`) remains for debugging and field validation.

---

## Why GamePulse?

Existing tools (MangoHud, MSI Afterburner, CapFrameX) are local-only. They can't answer questions like:

- Is Proton 9.0-4 actually faster than 9.0-3 for this game on my hardware class?
- Did the new Mesa/driver release cause a regression across the community?
- Is my SD card causing storage stutter vs the internal NVMe?
- What's the performance-per-watt sweet spot on my Steam Deck for this game?

GamePulse ships structured telemetry to Elasticsearch, enabling cross-session, cross-hardware, and cross-configuration comparisons backed by real data.

---

## Quick start

### Arch Linux / CachyOS / Manjaro

```bash
yay -S gamepulse-git
```

### Other Linux (one-liner)

```bash
curl -sSfL https://mathewrj.github.io/GamePulse-Integration/install.sh | sh
```

### Windows

```powershell
winget install MathewRJ.GamePulse
```

Or download the MSI from the [Releases page](https://github.com/MathewRJ/GamePulse/releases).

### First run

```bash
# Prompts for your Elasticsearch endpoint + API key
gamepulse setup

# Add to Steam launch options for any game:
gamepulse run %command%
```

Data starts flowing to Elasticsearch the next time you launch a game through Steam.

---

## Connecting to Elasticsearch

GamePulse requires an Elasticsearch endpoint. You have two options:

### Option A — Elastic Cloud (easiest, free tier available)

Sign up at [cloud.elastic.co](https://cloud.elastic.co/). The free trial gives you a fully managed deployment with Kibana included. After creating a deployment:

1. Note your **Elasticsearch endpoint** (shown on the deployment overview page)
2. In Kibana → Stack Management → API Keys, create a key with `auto_configure` + `create_doc` + `create_index` on `metrics-gamepulse.*` and `logs-gamepulse.*`
3. Run `gamepulse setup` and enter the endpoint and API key when prompted

### Option B — Local Elasticsearch (free, runs on your own machine)

Elasticsearch is free to self-host. Download the latest release from:

- **Download:** [elastic.co/downloads/elasticsearch](https://www.elastic.co/downloads/elasticsearch)
- **Install guide:** [Installing Elasticsearch](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/installing-elasticsearch)

Quick setup on Linux (tar.gz):

```bash
# Extract and start
tar -xzf elasticsearch-*.tar.gz
cd elasticsearch-*/
./bin/elasticsearch
```

On first start, Elasticsearch prints a generated `elastic` superuser password and an enrollment token — save both. It listens on `https://localhost:9200` by default (TLS enabled since ES 8.0).

Create an API key for GamePulse (run this in a new terminal):

```bash
curl -u elastic:<your-generated-password> \
  -X POST "https://localhost:9200/_security/api_key" \
  -H "Content-Type: application/json" \
  --cacert elasticsearch-*/config/certs/http_ca.crt \
  -d '{
    "name": "gamepulse",
    "role_descriptors": {
      "gamepulse_writer": {
        "cluster": ["monitor"],
        "indices": [{
          "names": ["metrics-gamepulse.*", "logs-gamepulse.*"],
          "privileges": ["auto_configure", "create_doc", "create_index"]
        }]
      }
    }
  }'
```

Then run setup:

```bash
gamepulse setup
# Endpoint: https://localhost:9200
# API key: <the encoded value from the curl output above>
```

If you get a TLS error with a self-signed cert, add `tls_skip_verify = true` to `~/.config/gamepulse/gamepulse.toml` (fine for local dev, not for shared instances).

For Kibana, download it separately from [elastic.co/downloads/kibana](https://www.elastic.co/downloads/kibana) and enroll it using the enrollment token printed at Elasticsearch startup. Both need to be the same version.

See [`docs/install.md`](docs/install.md) for the full installation guide including package managers (.deb/.rpm), systemd setup, and the Fleet API integration package install.

---

## What's working today

The Rust production agent (`gamepulse-agent`) is Linux-complete and Elasticsearch-verified — a live 40-minute Starfield session confirmed all 8 metric streams (CPU, GPU, memory, storage, network, audio, power, frame) shipping correctly. The eBPF daemon (`gamepulse-ebpf`, Sprints 1–3) is ES-confirmed for kernel-level scheduler, I/O, GPU fence, futex, IRQ, and VFS probes. Windows collectors are implemented for all 8 streams (Phases B–C complete, some platform gaps documented in `docs/STATUS.md`). Seven Kibana dashboards are built and tested against Elastic Cloud Serverless. An integration package submission is in progress at [elastic/integrations#18878](https://github.com/elastic/integrations/pull/18878).

---

## Status and roadmap

See [`docs/STATUS.md`](docs/STATUS.md) for current state and [`docs/ROADMAP.md`](docs/ROADMAP.md) for milestone structure.

---

## Documentation

- [`docs/install.md`](docs/install.md) — full installation guide (Elastic Cloud, local self-hosted, AUR, .deb/.rpm, building from source)
- [`docs/configuration.md`](docs/configuration.md) — full configuration reference
- [`docs/dashboards.md`](docs/dashboards.md) — dashboard build guide and NDJSON reference
- [`docs/steam-setup.md`](docs/steam-setup.md) — Steam launch options setup
- [`architecture/`](architecture/) — agent, eBPF, and data model architecture docs
- [`docs/SCOPE.md`](docs/SCOPE.md) — strategic project scope

---

## Privacy

- **No PII ever collected**: no usernames, emails, IPs, or location data
- **User identity** is a SHA hash of `/etc/machine-id` — stable for session correlation, not reversible
- All data stays in your personal Elasticsearch instance unless you explicitly opt in to community sharing
- Network metrics and eBPF data are opt-out of community sharing by default

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Most useful contributions right now:

- NVIDIA GPU testing on Linux (sysfs paths and NVML interface need validation across driver versions)
- Windows hardware collection (PDH paths, PresentMon integration)
- Non-CachyOS Arch Linux and Fedora smoke tests

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
