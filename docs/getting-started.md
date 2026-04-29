# Getting Started with GamePulse

This guide walks you through installing GamePulse, connecting it to Elasticsearch, and seeing your first gaming telemetry data.

## Prerequisites

- A Linux gaming PC (Steam Deck, desktop, or laptop)
- An Elasticsearch instance — [Elastic Cloud](https://cloud.elastic.co/) free tier works for personal use
- Games installed via Steam, Lutris, Heroic (Epic/GOG), or Bottles — or specify any process manually

## Step 1: Install

### Arch Linux / CachyOS / Steam Deck (AUR)

```bash
yay -S gamepulse
```

### Debian / Ubuntu

```bash
sudo apt install ./gamepulse_*.deb   # download from GitHub releases
```

### Fedora / RHEL

```bash
sudo dnf install ./gamepulse-*.rpm   # download from GitHub releases
```

### Build from source

```bash
git clone https://github.com/MathewRJ/GamePulse.git
cd GamePulse/src
cargo build --release
sudo cp target/release/gamepulse-agent /usr/local/bin/gamepulse-agent
sudo install -Dm755 ../packaging/gamepulse-launcher.sh /usr/local/bin/gamepulse
```

## Step 2: Configure

### Option A — Interactive setup (recommended)

```bash
gamepulse setup
```

Prompts for your Elasticsearch endpoint and API key, tests connectivity, and writes
`~/.config/gamepulse/gamepulse.toml` (mode 600).

### Option B — Edit the config file directly

```bash
mkdir -p ~/.config/gamepulse
cat > ~/.config/gamepulse/gamepulse.toml << 'EOF'
[elasticsearch]
endpoint = "https://your-deployment.es.us-central1.gcp.elastic.cloud"
api_key = "your-api-key"
EOF
```

The API key needs `monitor` on the cluster and `auto_configure`/`create_doc`/`create_index`
on `metrics-gamepulse.*` and `logs-gamepulse.*`. See [install.md](install.md) for details.

## Step 3: Verify

```bash
gamepulse-agent diagnose
```

Prints kernel version, GPU info, Elasticsearch reachability, and the resolved config path.
Pass `--output report.txt` to save a bug report.

## Step 4: Start collecting

### Steam launch option (per-game)

In Steam → right-click game → Properties → Launch Options:

```
gamepulse run %command%
```

### Always-on service

```bash
# User-level agent (no root)
systemctl --user enable --now gamepulse-agent

# System-level eBPF daemon (requires sudo, for kernel-level tracing)
sudo systemctl enable --now gamepulse-ebpf
```

### Other launchers

GamePulse auto-detects games from Steam, Lutris, Heroic (Epic/GOG), and Bottles.
No extra configuration is needed — just run the agent alongside your launcher.

To monitor a specific process regardless of launcher:

```bash
gamepulse-agent --target-name cyberpunk2077
# or
gamepulse-agent --target-pid 12345
```

## What happens automatically

Once the agent is running and a game is detected:

1. It identifies the game name, launcher, and graphics API (Vulkan/D3D/OpenGL)
2. If running under Proton, it detects Proton/DXVK/VKD3D versions from `/proc/<pid>/maps`
3. It collects 8 metric streams at 1-second intervals: CPU, GPU, memory, storage,
   network, audio, frame timing, and power
4. When the game exits, it ships a session summary with peak values and FPS percentiles
5. The ES ingest pipeline classifies hardware tier, tags stutters, and detects throttling

## Frame timing (optional)

Frame data (`gamepulse.frame`) requires MangoHud. All other 7 metric streams work without it.

```bash
# Enable MangoHud logging globally
cat >> ~/.config/MangoHud/MangoHud.conf << 'EOF'
log_duration=0
output_folder=/tmp/MangoHud
EOF
```

Or per-game in Steam launch options:
```
MANGOHUD=1 MANGOHUD_LOG=1 gamepulse run %command%
```

On Steam Deck, MangoHud is pre-installed — enable the overlay in Quick Access.

## Next steps

- [Configuration reference](configuration.md) — all TOML fields and CLI flags
- [eBPF deep telemetry](ebpf.md) — kernel-level scheduler, I/O, and GPU tracing
- [Steam Deck setup](steam-deck.md)
- [Full installation guide](install.md)
