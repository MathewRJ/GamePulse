# Getting Started with GamePulse

This guide walks you through installing GamePulse, connecting it to Elasticsearch, and seeing your first gaming telemetry data.

## Prerequisites

- A Linux gaming PC (Steam Deck, desktop, or laptop)
- An Elasticsearch instance (Elastic Cloud free tier works great for testing)
- Games installed via Steam (other launchers coming soon)

## Step 1: Install

### Option A: One-line installer (recommended)

```bash
curl -sSL https://install.gamepulse.dev | bash
```

This auto-detects your hardware, prompts for Elasticsearch credentials, and sets up a systemd service.

### Option B: Build from source

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

# Build
git clone https://github.com/gamepulse/agent.git
cd agent
cargo build --release
```

### Option C: Package manager

```bash
# Arch Linux / Steam Deck
yay -S gamepulse-agent

# Debian / Ubuntu
sudo dpkg -i gamepulse-agent_0.1.0_amd64.deb
```

## Step 2: Set up Elasticsearch

If you don't have an Elasticsearch instance, the easiest option is [Elastic Cloud](https://cloud.elastic.co/) — the free tier includes enough storage for personal use.

Once you have an instance, you need two things:
1. The **endpoint URL** (e.g. `https://my-deployment.es.us-central1.gcp.cloud.es.io:9243`)
2. An **API key** (create one in Kibana → Stack Management → API Keys)

### Deploy index templates and dashboards

```bash
./scripts/setup-elasticsearch.sh YOUR_ES_ENDPOINT YOUR_API_KEY
```

This creates ILM policies, index templates, ingest pipelines, and analytics transforms.

## Step 3: Configure

Edit the config file:

```bash
# System install
sudo vim /etc/gamepulse/gamepulse.toml

# User install (Steam Deck)
vim ~/.config/gamepulse/gamepulse.toml

# Source build
cp config/gamepulse.toml ~/.config/gamepulse/gamepulse.toml
vim ~/.config/gamepulse/gamepulse.toml
```

At minimum, set the Elasticsearch endpoint and API key:

```toml
[elasticsearch]
endpoint = "https://my-deployment.es.us-central1.gcp.cloud.es.io:9243"
api_key = "your-api-key-here"
```

See [Configuration Reference](configuration.md) for all options.

## Step 4: Test

Run a quick test without Elasticsearch:

```bash
gamepulse-agent --debug --once
```

You should see a JSON dump of system metrics (CPU, GPU, memory, storage). If a game is running, you'll also see game detection info.

For continuous monitoring:

```bash
gamepulse-agent --debug
```

Output looks like:

```
CPU:45% GPU:92%/72°C MEM:12400/16384MB FPS:60 (1%:52 0.1%:44) ft:16.7ms IO:R85/W2MB/s PROC:RSS8200MB/24thr [Starfield]
```

## Step 5: Start the service

```bash
# System-wide (with eBPF support)
sudo systemctl enable --now gamepulse-agent

# User-level (Steam Deck, no root needed)
systemctl --user enable --now gamepulse-agent
```

## Step 6: Enable frame timing

For FPS and frame time data, launch games with MangoHud logging. Add this to Steam launch options for each game:

```
MANGOHUD=1 MANGOHUD_LOG=1 %command%
```

Or set it globally in `~/.config/MangoHud/MangoHud.conf`:

```ini
log_duration=0
output_folder=/tmp/MangoHud
```

On Steam Deck, MangoHud is pre-installed — just enable the performance overlay in Quick Access.

## Step 7: View data in Kibana

Import the pre-built dashboards:

```bash
curl -X POST "https://your-kibana:5601/api/saved_objects/_import" \
  -H "kbn-xsrf: true" \
  --form file=@kibana/gamepulse-dashboard.ndjson
```

Then open Kibana → Dashboards → "[GamePulse] Gaming Performance".

## What happens automatically

Once the agent is running:

1. It detects when you launch a game via Steam
2. It identifies the game name, Steam App ID, and graphics API
3. If running under Proton, it detects Proton/DXVK/VKD3D versions
4. It starts collecting metrics at 1-second intervals
5. When the game exits, it ships a session summary
6. The ES ingest pipeline classifies hardware tier, tags stutters, and detects throttling

## Next steps

- [Configure the agent](configuration.md) for your needs
- [Set up eBPF](ebpf.md) for deep kernel-level tracing
- [Steam Deck specific setup](steam-deck.md)
- [Elasticsearch setup details](install.md)
