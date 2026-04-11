# GamePulse Beta Installation Guide

Version 0.1.0 — closed beta

---

## Prerequisites

### Hardware
- Linux x86_64 (kernel 6.x+ recommended for eBPF probes)
- AMD GPU primary (NVIDIA: limited support, no eBPF GPU probes)
- Steam installed (required for game detection)
- MangoHud installed and configured (required for frame timing data)

### Elastic Stack
- Elastic Cloud Serverless (recommended) **or** self-hosted Elastic 8.13+
- An API key with index write + ingest pipeline manage permissions
- Kibana access (8.7+ for zip upload method)

---

## Step 1 — Install the Integration Package

Download `gamepulse-0.1.0.zip` from the GitHub releases page.

### Method A — Kibana Fleet UI (recommended)

1. Open Kibana → **Integrations** (Fleet)
2. Click **Upload integration** (top-right area, or Integrations search page)
3. Upload `gamepulse-0.1.0.zip`
4. Click **Install GamePulse**

This installs all 11 data stream index templates and the ingest pipeline into your Elastic deployment. GamePulse will now appear in the Integrations catalog.

### Method B — Direct API (developers, CLI preferred)

Requires `curl` or any HTTP client that can POST a zip file:

```bash
# Elastic Cloud Serverless
curl -X POST \
  "https://<your-deployment-id>.kb.<region>.gcp.elastic.cloud/api/fleet/epm/packages" \
  -H "Content-Type: application/zip" \
  -H "kbn-xsrf: reporting" \
  -H "Authorization: ApiKey <your-kibana-api-key>" \
  --data-binary @gamepulse-0.1.0.zip

# Self-hosted Kibana 8.13
curl -k -X POST \
  "https://your-kibana:5601/api/fleet/epm/packages" \
  -H "Content-Type: application/zip" \
  -H "kbn-xsrf: reporting" \
  -H "Authorization: Basic $(echo -n 'user:pass' | base64)" \
  --data-binary @gamepulse-0.1.0.zip
```

**Note on custom registry:** Elastic Cloud Serverless does not support custom Package Registry URLs — use Method A or B above. Self-hosted Fleet can be configured with a custom registry by pointing `xpack.fleet.registryUrl` in `kibana.yml` to your registry server.

---

## Step 2 — Install the GamePulse Agent

### AUR (CachyOS / Arch Linux)

```bash
# Download the built package from GitHub releases
yay -U gamepulse-0.1.0-1-x86_64.pkg.tar.zst

# Or install from source
git clone https://github.com/MathewRJ/GamePulse.git
cd GamePulse/packaging
makepkg -si
```

### Manual install (other distros)

Download the pre-built binaries from GitHub releases:
- `gamepulse-agent` → `/usr/bin/gamepulse-agent`
- `gamepulse-ebpf` → `/usr/bin/gamepulse-ebpf`
- `gamepulse-ebpf-probes` ELF → `/usr/lib/gamepulse/gamepulse-ebpf-probes`

Install systemd units from `packaging/systemd/`:
```bash
sudo cp packaging/systemd/gamepulse-agent.service /etc/systemd/user/
sudo cp packaging/systemd/gamepulse-ebpf.service /etc/systemd/system/
```

---

## Step 3 — Configure

Create the config file:

```bash
mkdir -p ~/.config/gamepulse
cat > ~/.config/gamepulse/gamepulse.toml <<EOF
[elasticsearch]
endpoint = "https://<your-deployment-id>.es.<region>.gcp.elastic.cloud"
api_key = "<your-api-key>"

[collection]
cpu = true
gpu = true
memory = true
storage = true
network = true
audio = true
frame = true
power = true
EOF
```

**Minimum API key permissions** required for the key in your config:
- `monitor` on cluster
- `auto_configure`, `create_doc`, `create_index` on `metrics-gamepulse.*` and `logs-gamepulse.*`

---

## Step 4 — Start the Services

```bash
# Metrics agent (runs as your user)
systemctl --user enable --now gamepulse-agent

# eBPF kernel probe daemon (runs as root — requires modern kernel)
sudo systemctl enable --now gamepulse-ebpf
```

Verify both are running:
```bash
systemctl --user status gamepulse-agent
sudo systemctl status gamepulse-ebpf
```

---

## Step 5 — Verify Data is Flowing

After starting a game, check Elasticsearch for documents:

```bash
# Quick check — should show recent docs after ~5 seconds
curl -X POST \
  "https://<your-deployment>.es.<region>.gcp.elastic.cloud/metrics-gamepulse.*/_search" \
  -H "Authorization: ApiKey <key>" \
  -H "Content-Type: application/json" \
  -d '{"size":1,"sort":[{"@timestamp":"desc"}]}'
```

In Kibana: search for **GamePulse** in the Integrations section to find the pre-built dashboards, or create a Data View for `metrics-gamepulse.*`.

---

## Known Limitations (v0.1.0)

| Limitation | Details |
|---|---|
| AMD GPU only | NVIDIA support planned; GPU and eBPF GPU probes are AMD-specific |
| Linux only | Windows planned for v0.2.0 |
| eBPF probes need kernel 6.x+ | Tracepoints required: `sched/*`, `irq/*`, `block/*`, `drm_scheduler/*` |
| `ccx_cross_count` always 0 | Expected on single-CCD CPUs (9800X3D, etc.) — not a bug |
| Game RSS unreliable under Proton | `memory.game_rss_mb` tracks the Proton launcher, not the game process |
| MangoHud required for frame data | Without MangoHud, `gamepulse.frame` data stream will be empty |
| eBPF daemon needs root | `sudo systemctl` for `gamepulse-ebpf`; the agent runs as a normal user |

---

## Troubleshooting

**Agent starts but no data in Elasticsearch:**
- Check the config file: `~/.config/gamepulse/gamepulse.toml`
- Verify the API key has write permissions to `metrics-gamepulse.*`
- Run in dry-run mode: `gamepulse-agent --dry-run --config ~/.config/gamepulse/gamepulse.toml`

**eBPF daemon fails to start:**
- Check kernel version: `uname -r` — needs 6.x+
- Check capabilities: the systemd unit uses `AmbientCapabilities=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN`
- Check tracepoint availability: `ls /sys/kernel/tracing/events/sched/`

**Game not detected:**
- Steam must be running
- Check: `cat /tmp/gamepulse/session.json` after launching a game

**MangoHud frame data missing:**
- Verify MangoHud is configured: `MANGOHUD=1 mangohud glxgears`
- Check the log path: `ls ~/.local/share/MangoHud/` (or the path set in `MANGOHUD_OUTPUT`)

---

## Feedback

Please file issues at: https://github.com/MathewRJ/GamePulse/issues

Include:
- Your GPU model and driver version (`vulkaninfo | grep deviceName`)
- Kernel version (`uname -r`)
- Whether you're running Proton and which version
- Any relevant logs: `journalctl --user -u gamepulse-agent -n 50`
