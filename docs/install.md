# GamePulse — Installation Guide

## Quick start

```bash
# 1. Install the agent (AUR)
yay -S gamepulse

# 2. First-run setup — prompts for ES endpoint + API key
gamepulse setup

# 3. Add to Steam launch options for any game:
gamepulse run %command%
```

That's it. Data starts flowing to Elasticsearch the next time you launch a game.

---

## Choosing a backend: Elastic Cloud vs offline stack

| | Elastic Cloud Serverless | Self-hosted / offline |
|---|---|---|
| Setup effort | Minutes | Hours |
| Cost | Free tier (8 GB) for personal use | Hardware + time |
| Kibana | Included | Install separately |
| Offline gaming PC | Requires internet | Works fully offline |
| Recommended for | Getting started, sharing data | Air-gapped setups |

For the offline stack, the offline branch (not yet forked from main) will bundle Elasticsearch + Kibana natively. Until that branch ships, use Elastic Cloud.

---

## Elastic Cloud setup

### 1. Create a deployment

Sign up at [cloud.elastic.co](https://cloud.elastic.co/). The free tier (8 GB) is sufficient for months of personal gaming data. Create a deployment in any region.

### 2. Get an API key

In Kibana → Stack Management → API Keys, create a key with cluster `monitor` and index `auto_configure` + `create_doc` privileges on `metrics-gamepulse.*`. Note:
- Your **Elasticsearch endpoint** (e.g. `https://gamepulse-af41f9.es.us-central1.gcp.elastic.cloud`)
- The **API key** (base64 encoded, shown once at creation)

For a personal deployment, `all` cluster + index privileges is simpler and fine.

### 3. Run `gamepulse setup`

```bash
gamepulse setup
```

This prompts for your ES endpoint and API key, verifies connectivity, and writes `~/.config/gamepulse/gamepulse.toml` (mode 600). The integration package is installed to your Kibana instance automatically.

---

## Self-hosted Elasticsearch

Any Elasticsearch 8.13+ instance works. Ensure:
- At least 2 GB RAM for ES
- TLS configured (or `tls_skip_verify = true` in `gamepulse.toml` for local dev)
- Kibana accessible for dashboards

Deploy the integration package:

```bash
# Via the Fleet API (Kibana 8.7+)
curl -X POST "https://your-kibana:5601/api/fleet/epm/packages" \
  -H "kbn-xsrf: true" \
  -H "Authorization: ApiKey YOUR_API_KEY" \
  -H "Content-Type: application/zip" \
  --data-binary @gamepulse-0.1.0.zip
```

Or use `elastic-package stack up` from the repo root to start a local 8.13 stack with the package pre-loaded.

---

## Linux distro packages

### Arch Linux / CachyOS / Manjaro (AUR)

```bash
yay -S gamepulse
```

Or manually:

```bash
git clone https://aur.archlinux.org/gamepulse.git
cd gamepulse
makepkg -si
```

### Debian / Ubuntu (.deb)

Coming in Milestone D. Until then, build from source (see below).

### Fedora / RHEL (.rpm)

Coming in Milestone D. Until then, build from source.

### Building from source

Requires Rust 1.77+ and the Aya eBPF toolchain:

```bash
git clone https://github.com/MathewRJ/GamePulse.git
cd GamePulse/src
cargo build --release
sudo cp target/release/gamepulse-agent /usr/local/bin/gamepulse
```

For the eBPF daemon (requires kernel 5.8+ and `CAP_BPF`):

```bash
cd GamePulse/ebpf
RUSTFLAGS="" cargo xtask build-ebpf --release
RUSTFLAGS="" cargo build --release
sudo cp target/release/gamepulse-ebpf /usr/local/bin/
sudo install -m 644 target/bpfel-unknown-none/release/gamepulse-ebpf-probes \
  /usr/lib/gamepulse/gamepulse-ebpf-probes
```

---

## Windows installer

Coming in Milestone E. The plan: MSI installer via WiX, installs `gamepulse.exe` and the Steam launch wrapper. For now, Windows users can build from source with `cargo build --release` (eBPF not available on Windows).

---

## MangoHud setup (required for frame timing)

GamePulse reads frame data from MangoHud's CSV log. Add to Steam launch options:

```
MANGOHUD=1 MANGOHUD_LOG=1 gamepulse run %command%
```

Or set globally in `~/.config/MangoHud/MangoHud.conf`:

```ini
log_duration=0
output_folder=/tmp/MangoHud
```

See `docs/steam-setup.md` for detailed Steam integration instructions.

---

## systemd service (always-on mode)

For continuous collection even outside Steam:

```bash
# User-level agent (no root, no eBPF)
systemctl --user enable --now gamepulse-agent

# System-level eBPF daemon (requires sudo, runs as root with CAP_BPF)
sudo systemctl enable --now gamepulse-ebpf
```

Service files are installed by the AUR package to the correct locations. For manual installs, copy from `packaging/systemd/`.

---

## Configuration

Config is read from (in priority order):
1. `--config PATH` CLI flag
2. `~/.config/gamepulse/gamepulse.toml`
3. `/etc/gamepulse/gamepulse.toml`

`gamepulse setup` writes `~/.config/gamepulse/gamepulse.toml` automatically. See `docs/configuration.md` for the full reference.

---

## Beta install guide

For colleagues onboarding as beta testers, see `docs/BETA-INSTALL.md`. That document will be merged here once .deb/.rpm packaging ships.
