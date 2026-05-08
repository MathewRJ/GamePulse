# GamePulse — Installation Guide

## Quick start

```bash
# Arch Linux / CachyOS / Manjaro
yay -S gamepulse-git

# Other Linux (one-liner)
curl -sSfL https://mathewrj.github.io/GamePulse-Integration/install.sh | sh

# Windows
winget install MathewRJ.GamePulse
```

Then:

```bash
# Prompts for your Elasticsearch endpoint + API key
gamepulse setup

# Add to Steam launch options for any game:
gamepulse run %command%
```

Data starts flowing to Elasticsearch the next time you launch a game.

---

## Choosing a backend

| | Elastic Cloud | Self-hosted (local) |
|---|---|---|
| Setup effort | Minutes | 15–30 min |
| Cost | Free trial / free tier | Free (open source) |
| Kibana | Included | Install separately (same version) |
| Works offline | No (requires internet) | Yes |
| Recommended for | Getting started fast | Privacy, air-gapped, no cloud |

Both options are free. Elastic Cloud is the quickest path. Self-hosted gives you full control and works with no internet connection after setup.

---

## Elastic Cloud setup

### 1. Create a deployment

Sign up at [cloud.elastic.co](https://cloud.elastic.co/). The free tier (8 GB) is sufficient for months of personal gaming data. Create a deployment in any region.

### 2. Get an API key

In Kibana → Stack Management → API Keys, create a key with cluster `monitor` and index `auto_configure` + `create_doc` privileges on `metrics-gamepulse.*`. Note:
- Your **Elasticsearch endpoint** (e.g. `https://your-deployment.es.us-central1.gcp.elastic.cloud`)
- The **API key** (base64 encoded, shown once at creation)

For a personal deployment, `all` cluster + index privileges is simpler and fine.

### 3. Run `gamepulse setup`

```bash
gamepulse setup
```

This prompts for your ES endpoint and API key, verifies connectivity, and writes `~/.config/gamepulse/gamepulse.toml` (mode 600). The integration package is installed to your Kibana instance automatically.

---

## Self-hosted Elasticsearch

Download and install guide: [elastic.co/downloads/elasticsearch](https://www.elastic.co/downloads/elasticsearch) / [Installing Elasticsearch](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/installing-elasticsearch)

Requirements: Elasticsearch 8.13+, at least 2 GB RAM, Kibana (same version) for dashboards.

### 1. Start Elasticsearch

**tar.gz (Linux):**

```bash
tar -xzf elasticsearch-*.tar.gz
cd elasticsearch-*/
./bin/elasticsearch
```

**Docker:**

```bash
docker run -d --name elasticsearch \
  -p 9200:9200 -p 9300:9300 \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=true" \
  -e "ELASTIC_PASSWORD=changeme" \
  docker.elastic.co/elasticsearch/elasticsearch:8.18.0
```

On first start, Elasticsearch generates a `elastic` superuser password and a Kibana enrollment token — save both. It listens on `https://localhost:9200` (TLS is enabled by default since ES 8.0).

### 2. Create a GamePulse API key

```bash
curl -u elastic:<your-password> \
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

The response includes an `encoded` field — that base64 string is your API key.

### 3. Run `gamepulse setup`

```bash
gamepulse setup
# Elasticsearch endpoint: https://localhost:9200
# API key: <the encoded value from above>
```

If you see a TLS certificate error with a locally-issued cert, add this to `~/.config/gamepulse/gamepulse.toml`:

```toml
[elasticsearch]
tls_skip_verify = true
```

This is fine for a local dev machine. Do not use it for shared or remote instances.

### 4. Install Kibana (for dashboards)

Download Kibana from [elastic.co/downloads/kibana](https://www.elastic.co/downloads/kibana) — must be the same version as Elasticsearch.

```bash
tar -xzf kibana-*.tar.gz
cd kibana-*/
./bin/kibana
# When prompted, paste the enrollment token printed by Elasticsearch on first start
```

Kibana listens on `http://localhost:5601` by default. Log in as `elastic` with the password from step 1.

### 5. Install the GamePulse integration package

Once Kibana is running, install the integration package via the Fleet API:

```bash
curl -X POST "https://localhost:5601/api/fleet/epm/packages" \
  -u elastic:<your-password> \
  -H "kbn-xsrf: true" \
  -H "Content-Type: application/zip" \
  --data-binary @gamepulse-0.1.0.zip
```

Or navigate to Kibana → Fleet → Integrations → search "GamePulse" if the package is available in the registry.

> For contributors: `elastic-package stack up` from the repo root starts a local stack with the package pre-loaded automatically.

---

## Linux distro packages

### Arch Linux / CachyOS / Manjaro (AUR)

```bash
yay -S gamepulse-git
```

Or manually:

```bash
git clone https://aur.archlinux.org/gamepulse-git.git
cd gamepulse-git
makepkg -si
```

### Debian / Ubuntu (.deb)

Download the latest `.deb` from the [GitHub releases page](https://github.com/MathewRJ/GamePulse/releases):

```bash
sudo apt install ./gamepulse_*.deb
```

The package installs `gamepulse-agent` and `gamepulse` (launcher) to `/usr/bin/`, the systemd user unit, and an example config to `/etc/gamepulse/gamepulse.toml`.

### Fedora / RHEL (.rpm)

Download the latest `.rpm` from the [GitHub releases page](https://github.com/MathewRJ/GamePulse/releases):

```bash
sudo dnf install ./gamepulse-*.rpm
```

### Building from source

Requires Rust 1.77+ and the Aya eBPF toolchain:

```bash
git clone https://github.com/MathewRJ/GamePulse.git
cd GamePulse/src
cargo build --release
sudo cp target/release/gamepulse-agent /usr/local/bin/gamepulse-agent
# Install the launcher wrapper as 'gamepulse'
sudo install -Dm755 ../packaging/gamepulse-launcher.sh /usr/local/bin/gamepulse
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

The Windows MSI installer shipped in Milestone E and is available on the
[GitHub Releases page](https://github.com/MathewRJ/GamePulse/releases).
Download `gamepulse-<version>-x86_64-windows.msi` and run the installer — it installs
`gamepulse.exe` to `Program Files\GamePulse\` and registers the Windows Service.
A portable zip (`gamepulse-<version>-windows-x64.zip`) is also available for users
who prefer not to use the installer. eBPF is not available on Windows; all other
metric streams are supported.

---

## MangoHud setup (optional — needed for frame timing data)

The agent ships all 8 metric streams regardless. MangoHud is only needed to populate
`gamepulse.frame` (FPS, frame time, 1%/0.1% lows, stutter). All other streams (CPU, GPU,
memory, storage, network, audio, power) work without it.

To enable frame data, add to Steam launch options:

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

> **Dev installs (build from source):** The unit's `ExecStart` defaults to `/usr/bin/gamepulse-agent`, but a source build installs to `/usr/local/bin/`. Create a drop-in to override:
> ```bash
> mkdir -p ~/.config/systemd/user/gamepulse-agent.service.d
> cat > ~/.config/systemd/user/gamepulse-agent.service.d/override.conf <<'EOF'
> [Service]
> ExecStart=
> ExecStart=/usr/local/bin/gamepulse-agent
> EOF
> systemctl --user daemon-reload
> ```

---

## Configuration

Config is read from (in priority order):
1. `--config PATH` CLI flag
2. `~/.config/gamepulse/gamepulse.toml`
3. `/etc/gamepulse/gamepulse.toml`

`gamepulse setup` writes `~/.config/gamepulse/gamepulse.toml` automatically. See `docs/configuration.md` for the full reference.

---

## Minimum API key permissions

The API key you provide needs:
- `monitor` privilege on the cluster
- `auto_configure`, `create_doc`, `create_index` on indices `metrics-gamepulse.*` and `logs-gamepulse.*`

For a personal deployment, `all` cluster + index privileges is simpler and fine.

---

## Verifying your setup

After configuring, run the diagnostics subcommand before starting a game:

```bash
gamepulse-agent diagnose
```

This outputs kernel version, GPU info, Elasticsearch reachability (with the API key redacted),
and the resolved config path. Use `--output report.txt` to save it for bug reports.

---

## Contributor tooling

The Elastic Agent Builder MCP server lets Claude Code and claude.ai query Elasticsearch directly during dashboard builds and field validation. This is optional developer convenience — it is not required to run GamePulse.

Setup instructions and the API key creation recipe are in `.agents/skills/elastic-mcp-setup/SKILL.md`. The template config is in `.mcp.json.example` at the repo root; copy it to `.mcp.json` (gitignored) and set `GAMEPULSE_MCP_API_KEY` before restarting Claude Code.
