GamePulse ships a real-time gaming performance telemetry agent that captures CPU, GPU, memory, storage, network, audio, power, and frame-timing metrics while you play and streams them to Elasticsearch for live dashboards and historical analysis.

## What's in this release

- `gamepulse-agent` — the telemetry agent binary
- `gamepulse` — unified launcher CLI (`setup / start / stop / status / run %command%`)
- systemd user unit (`gamepulse-agent.service`)
- Example config at `/etc/gamepulse/gamepulse.toml`
- Three starter game profiles (Starfield, Cyberpunk 2077, Baldur's Gate 3)

> **eBPF probes** (deep kernel-level GPU/CPU scheduler, block I/O, futex, VFS metrics) are **not** included in these packages — they require a nightly Rust toolchain and `bpf-linker`. Arch/CachyOS users who want eBPF should install from AUR (`yay -S gamepulse-git`) which builds everything from source.

---

## Requirements

- Linux x86_64, **glibc 2.39+** (Ubuntu 24.04, Fedora 40, Arch, CachyOS, SteamOS 3.6+)
- An Elasticsearch endpoint — the free tier on [Elastic Cloud Serverless](https://www.elastic.co/cloud) works
- An API key with `write` access to `metrics-gamepulse.*` data streams (see Quick Start)
- MangoHud — optional, required for frame timing (`gamepulse.fps.*` fields)

---

## Installation

### Arch Linux / CachyOS / Manjaro

Install the pre-built package from this release:
```sh
sudo pacman -U gamepulse-0.1.0-1-x86_64.pkg.tar.zst
```
Or install from AUR (includes eBPF probes, builds from source):
```sh
yay -S gamepulse-git
```

### Debian / Ubuntu (24.04+)

```sh
sudo dpkg -i gamepulse-agent_0.1.0-1_amd64.deb
```

### Fedora / RHEL / openSUSE

```sh
sudo rpm -i gamepulse-agent-0.1.0-1.x86_64.rpm
```

---

## Quick Start

### 1. Get an Elasticsearch endpoint

Sign up for a free [Elastic Cloud Serverless](https://www.elastic.co/cloud) project. Copy the **Elasticsearch endpoint URL** from the project overview — it looks like `https://your-project.es.us-central1.gcp.elastic.cloud`.

### 2. Create an API key

In Kibana → **Stack Management → API Keys**, create a key with these privileges:
- Index privileges: `create_index`, `create`, `write`, `view_index_metadata` on `metrics-gamepulse.*`
- Cluster privileges: `monitor`

### 3. Run first-time setup

```sh
gamepulse setup
```

This writes your endpoint and API key to `~/.config/gamepulse/gamepulse.toml` and verifies connectivity.

### 4. Test with a dry run

```sh
gamepulse-agent --dry-run
```

You should see your hardware snapshot (CPU model, GPU, RAM) printed and `collectors ready` in the log. No data is written to Elasticsearch in dry-run mode.

### 5. Steam integration (recommended)

In Steam, right-click a game → **Properties → Launch Options**:
```
gamepulse run %command%
```

The agent starts automatically when the game launches and stops cleanly when you quit. Session data appears in Elasticsearch within a few seconds.

### 6. Manual start / stop

```sh
gamepulse start    # start the agent in the background via systemd
gamepulse status   # show current session label and service state
gamepulse stop     # stop the agent gracefully
```

---

## Configuration

The agent searches for config in this order (first found wins):
1. `$GAMEPULSE_CONFIG` env var (full path)
2. `~/.config/gamepulse/gamepulse.toml`
3. `/etc/gamepulse/gamepulse.toml`

Minimal config:
```toml
[elasticsearch]
endpoint = "https://your-project.es.us-central1.gcp.elastic.cloud"
api_key  = "your-api-key-here"
```

You can also pass credentials via environment variables (takes precedence over config):
```sh
export ES_URL="https://your-project.es.us-central1.gcp.elastic.cloud"
export ES_API_KEY="your-api-key-here"
gamepulse-agent
```

Full config reference: `gamepulse-agent --help` and the example at `/etc/gamepulse/gamepulse.toml`.

---

## Game Profiles

GamePulse ships three starter profiles that auto-apply when the matching game is detected:

| Game | Steam App ID | Auto-configured |
|---|---|---|
| Starfield | 1716740 | FSR 2, ray tracing, ultra preset |
| Cyberpunk 2077 | 1091500 | DLSS / FSR 3 / XeSS, path tracing |
| Baldur's Gate 3 | 1086940 | Vulkan |

Profiles live at `/usr/share/gamepulse/profiles/` (system) or `~/.config/gamepulse/profiles/` (user, takes precedence). Copy and edit one to create your own.

---

## Troubleshooting

**"No Elasticsearch endpoint configured"**
Run `gamepulse setup` or set `ES_URL` and `ES_API_KEY` environment variables.

**"ES ping failed" at startup**
Check that your API key is valid and the endpoint URL ends in `.es.` (not `.kb.`). Run `gamepulse-agent diagnose` for a full connectivity report.

**No frame timing data (`gamepulse.fps.*` fields are null)**
MangoHud must be installed and on `$PATH`. On Arch: `sudo pacman -S mangohud`. On Ubuntu: `sudo apt install mangohud`. The agent will log a one-time warning if MangoHud is not found.

**Agent doesn't detect my game**
The agent detects Steam games automatically. For non-Steam games (Lutris, Heroic, Bottles) detection is also automatic. For anything else, pin the target manually:
```sh
gamepulse-agent --target-name "MyGame"
# or
gamepulse-agent --target-pid 12345
```

**Permissions error on `/proc`**
The agent reads `/proc/<pid>/maps` and `/proc/<pid>/environ` for settings auto-detection. This works without elevated privileges on standard desktop kernels. If you have a hardened kernel with restricted `/proc` access, run `gamepulse-agent --log-level debug` to see which reads are failing.

**Session label counter is stuck at 1**
The counter is stored in `$XDG_STATE_HOME/gamepulse/session-counters.json` (default `~/.local/state/gamepulse/`). If the file is read-only or on a read-only filesystem, the agent falls back gracefully but can't increment. Check file permissions.

**systemd unit fails to start**
If you installed a dev build to `/usr/local/bin/` instead of `/usr/bin/`, the unit's `ExecStart` path won't match. Create a drop-in override:
```sh
mkdir -p ~/.config/systemd/user/gamepulse-agent.service.d
cat > ~/.config/systemd/user/gamepulse-agent.service.d/override.conf <<EOF
[Service]
ExecStart=
ExecStart=/usr/local/bin/gamepulse-agent
EOF
systemctl --user daemon-reload
```

---

## FAQ

**Does GamePulse work with non-Steam games?**
Yes. Lutris, Heroic (Epic/GOG), and Bottles are auto-detected. Any other game can be targeted by process name (`--target-name`) or PID (`--target-pid`). Automatic game detection via behavioural classification (GPU activity + fullscreen signals) is planned for a future release.

**Does it work on Steam Deck?**
Yes, for the standard metric streams. Install via the AUR PKGBUILD or the `.pkg.tar.zst` from this release. eBPF probes are not available on SteamOS's locked kernel, but CPU, GPU, memory, power, and frame timing all work.

**Does it send any data without my consent?**
No. `opt_in_public = false` is the default. Data is sent only to the Elasticsearch endpoint you configure. No telemetry is sent to GamePulse developers.

**What is the performance overhead?**
The agent is designed to be invisible. Typical overhead is <0.3% CPU and <5 MB RSS. The collection interval defaults to 1 second and is configurable. Frame timing via MangoHud adds no overhead beyond MangoHud's own cost.

**I want eBPF probes — what do I need?**
eBPF gives you GPU scheduler latency, CPU runqueue latency, block I/O latency, futex contention, VFS latency, and more. Currently Linux only. Install from AUR (`yay -S gamepulse-git`) which builds the eBPF daemon from source using the nightly Rust toolchain and `bpf-linker`. Pre-built eBPF packages are planned for a future release.

**Can I contribute game profiles?**
Yes — PRs welcome at [github.com/MathewRJ/GamePulse](https://github.com/MathewRJ/GamePulse). A profile is a small TOML file; see `profiles/starfield.toml` for the format.

**What Elasticsearch version is required?**
Elastic Cloud Serverless (recommended) or self-managed Elasticsearch 8.10+. The integration uses TSDS (Time Series Data Stream) index templates which require 8.10+.
