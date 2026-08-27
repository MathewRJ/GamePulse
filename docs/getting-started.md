# Getting Started with RigSignal

This guide walks you through installing RigSignal, connecting it to Elasticsearch, and seeing your first gaming telemetry data.

## Prerequisites

- A Linux gaming PC (Steam Deck, desktop, or laptop)
- An Elasticsearch instance — [Elastic Cloud](https://cloud.elastic.co/) offers a 14-day free trial (no credit card required) for evaluation; the trial deployment is suspended at expiry and its data is permanently deleted 30 days later, so it suits evaluation rather than ongoing personal use
- Games installed via Steam, Lutris, Heroic (Epic/GOG), or Bottles — or specify any process manually

## Step 1: Install

### Arch Linux / CachyOS / Steam Deck (AUR)

```bash
yay -S rigsignal
```

### Debian / Ubuntu

```bash
sudo apt install ./rigsignal_*.deb   # download from GitHub releases
```

### Fedora / RHEL

```bash
sudo dnf install ./rigsignal-*.rpm   # download from GitHub releases
```

### Build from source

```bash
git clone https://github.com/MathewRJ/RigSignal.git
cd RigSignal/src
cargo build --release
sudo cp target/release/rigsignal-agent /usr/local/bin/rigsignal-agent
sudo install -Dm755 ../packaging/rigsignal-launcher.sh /usr/local/bin/rigsignal
```

## Step 2: Configure

### Option A — Interactive setup (recommended)

```bash
rigsignal setup
```

Prompts for your Elasticsearch endpoint and API key, tests connectivity, and writes
`${XDG_CONFIG_HOME:-~/.config}/rigsignal/rigsignal.toml` (mode 600).

### Option B — Edit the config file directly

```bash
mkdir -p ~/.config/rigsignal
cat > ~/.config/rigsignal/rigsignal.toml << 'EOF'
[elasticsearch]
endpoint = "https://your-deployment.es.us-central1.gcp.elastic.cloud"
api_key = "your-api-key"
EOF
```

The API key needs `monitor` on the cluster and `auto_configure`/`create_doc`/`create_index`
on `metrics-rigsignal.*` and `logs-rigsignal.*`. See [install.md](install.md) for details.

## Step 3: Verify

```bash
rigsignal-agent diagnose
```

Prints kernel version, GPU info, Elasticsearch reachability, and the resolved config path.
Pass `--output report.txt` to save a bug report.

## Step 4: Start collecting

### Steam launch option (per-game)

In Steam → right-click game → Properties → Launch Options:

```
rigsignal run %command%
```

### Always-on service

```bash
# User-level agent (no root)
systemctl --user enable --now rigsignal-agent

# System-level eBPF daemon (requires sudo, for kernel-level tracing)
sudo systemctl enable --now rigsignal-ebpf
```

### Other launchers

RigSignal auto-detects games from Steam, Lutris, Heroic (Epic/GOG), and Bottles.
No extra configuration is needed — just run the agent alongside your launcher.

To monitor a specific process regardless of launcher:

```bash
rigsignal-agent --target-name cyberpunk2077
# or
rigsignal-agent --target-pid 12345
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

Frame data (`rigsignal.frame`) requires MangoHud. All other 7 metric streams work without it.

```bash
# Enable MangoHud logging globally
cat >> ~/.config/MangoHud/MangoHud.conf << 'EOF'
log_duration=0
output_folder=/tmp/MangoHud
EOF
```

Or per-game in Steam launch options:
```
MANGOHUD=1 MANGOHUD_LOG=1 rigsignal run %command%
```

On Steam Deck, MangoHud is included by default — enable the overlay in Quick Access.

## Next steps

- [Configuration reference](configuration.md) — all TOML fields and CLI flags
- [eBPF deep telemetry](ebpf.md) — kernel-level scheduler, I/O, and GPU tracing
- [Steam Deck setup](steam-deck.md)
- [Full installation guide](install.md)
