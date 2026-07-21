# RigSignal — Elastic Integration

> **This document is the Elastic Fleet integration guide** — it covers adding RigSignal to Kibana Fleet and is bundled into the Elastic Package Registry.
> For installation, quick start, and project overview, see the [project README](https://github.com/MathewRJ/RigSignal#readme).

RigSignal collects gaming performance telemetry — FPS, GPU/CPU metrics, frame timing
percentiles, audio, storage, network, power, and kernel-level scheduler and I/O data via
eBPF — and ships it to Elasticsearch so you can understand why a game performs the way
it does, not just that it stuttered.

## Compatibility

- **Linux**: full support, including eBPF kernel telemetry
- **Windows**: supported; eBPF data stream is not available on Windows
- **Kibana**: `^8.13` or `^9.0`
- **Elasticsearch**: `8.10+` (TSDS index templates)
- **Subscription**: Basic

## Requirements

- The RigSignal agent binary must be installed and running on the gaming machine.
- Elastic Agent must be deployed on the same machine, or have network access to the
  configured log path.
- **eBPF metrics** (Linux only): kernel 5.8 or later with BTF enabled, and the agent
  process must have `CAP_BPF` and `CAP_PERFMON` capabilities.
- **Frame timing**: MangoHud must be installed and configured with `output_folder` set
  (Linux), or PresentMon must be available (Windows).

## Setup

**1. Install the RigSignal agent**

Follow the platform instructions in the [installation guide](install.md).

**2. Configure Elasticsearch credentials**

Run the interactive setup to provide the Elasticsearch endpoint and API key.
The RigSignal agent ships telemetry directly to Elasticsearch via the Bulk API —
no Elastic Agent involvement is required for data ingestion. This integration
provides the index templates, ingest pipelines, and dashboards.

```bash
rigsignal setup
```

**3. Start the agent**

As a user-level systemd service:

```bash
systemctl --user enable --now rigsignal-agent
```

Or as a Steam launch option (per-game):

```
rigsignal run %command%
```

**4. Add the RigSignal integration in Kibana Fleet**

In Kibana, go to **Fleet > Integrations**, search for **RigSignal**, and click **Add RigSignal**.

Choose the policy template that matches your platform:

| Template | Use when |
|---|---|
| **RigSignal — Linux** | Linux gaming machine (all streams except eBPF) |
| **RigSignal — Windows** | Windows gaming machine |
| **RigSignal — eBPF Kernel Telemetry** | Linux, kernel 5.8+, eBPF daemon running |

You can add multiple templates to the same Elastic Agent policy (e.g. Linux + eBPF).

**5. Apply the policy**

Assign the policy to the Elastic Agent on your gaming machine and save. The dashboards
will populate as soon as the RigSignal agent starts shipping data.

## Configuration

The integration itself requires no configuration for normal use — the RigSignal agent
ships data directly to Elasticsearch using the credentials from `rigsignal setup`.

Each policy template exposes a **paths** variable (hidden by default) for advanced
setups where agent output is redirected to a file and read by Elastic Agent.

| Template | Default path |
|---|---|
| RigSignal — Linux | `/var/log/rigsignal/*.log` |
| RigSignal — Windows | `C:\ProgramData\RigSignal\logs\*.log` |
| RigSignal — eBPF | `/var/log/rigsignal/ebpf*.log` |

## Data streams

| Data stream | Type | Description |
|---|---|---|
| `audio` | metrics | Audio backend, configured scheduling latency, and sink details per session. |
| `cpu` | metrics | Per-core and total CPU utilisation, clock speeds, temperature, and power draw. |
| `ebpf` | metrics | Kernel-level scheduler latency, block I/O tracing, GPU fence and submit latency, and memory pressure via eBPF probes. Linux only. |
| `events` | logs | Discrete session events, including Steam Remote Play connection transitions. |
| `frame` | metrics | Instantaneous and rolling FPS, frame time percentiles (1% and 0.1% lows), stutter counts, and CPU/GPU bottleneck classification. |
| `gpu` | metrics | GPU core utilisation, clock speed, VRAM usage, temperature (edge and hotspot), power draw, fan speed, and voltage. |
| `memory` | metrics | System RAM usage, availability, and the game process resident set size. |
| `network` | metrics | Upload and download throughput in bytes per second. |
| `power` | metrics | AC connection state, battery percentage, and battery discharge rate (portable devices). |
| `session` | metrics | Hardware and software environment snapshot recorded at game start: CPU/GPU model, driver versions, graphics API, Proton/DXVK/VKD3D versions, and in-game settings. |
| `storage` | metrics | System-level disk read and write throughput in bytes per second. |

## Dashboards

- **RigSignal — Home**: Session overview showing recent games, aggregate FPS trends, and hardware summary.
- **RigSignal — Games**: Per-game performance comparison across sessions and hardware configurations.
- **RigSignal — Hardware**: GPU, CPU, and thermal metrics broken down per session.
- **RigSignal — Engine**: Frame timing detail, shader compilation events, and stutter analysis.
- **RigSignal — Environment**: Performance impact of kernel version, GPU driver, and Proton/DXVK version.
- **RigSignal — Compare**: Side-by-side comparison of two sessions with aligned time axes.
- **RigSignal — Game Library**: All tracked games with median FPS and the hardware context each was played on.

## Troubleshooting

**No data appears after setup**

- Confirm the agent is running: `rigsignal-agent diagnose`
- Confirm the **Paths** variable in Fleet matches the actual log file location.
- Check Elastic Agent logs in Fleet for file input errors.

**Frame timing data is missing**

MangoHud is not installed or `output_folder` is not set in `MangoHud.conf`. All other
metric streams work without MangoHud. See [getting-started.md](getting-started.md) for
MangoHud configuration steps.

**eBPF data is missing on Linux**

Run `rigsignal-agent diagnose` to check kernel BTF availability and capability state.
The eBPF daemon requires kernel 5.8 or later with BTF, and either `CAP_BPF`/`CAP_PERFMON`
or execution as root via `sudo systemctl enable --now rigsignal-ebpf`.

**eBPF stream is empty on Windows**

This is expected. eBPF kernel probes are not available on Windows. All other data streams
function normally.

## Steam Deck

RigSignal runs on the Steam Deck and is designed to survive SteamOS updates.

**Installation (Desktop Mode):**

```bash
curl -sSfL https://mathewrj.github.io/RigSignal-Integration/install.sh | sh
rigsignal setup
```

This installs the agent and launcher to `~/.local/bin/` and the systemd user service to
`~/.config/systemd/user/` — both on the persistent home partition. SteamOS resets `/usr`
on every OS update, but `~/.local/` survives, so **no reinstall is needed after a SteamOS
update** for the agent itself.

**eBPF on SteamOS:**

eBPF probes are a special case. The eBPF daemon (`rigsignal-ebpf`) must run as a system
service with root/`CAP_BPF`, which means it lives in `/usr/` and **gets wiped on every
SteamOS update**. To restore eBPF after an update:

```bash
yay -S rigsignal-git          # rebuilds and reinstalls from AUR
sudo systemctl enable --now rigsignal-ebpf
```

Without eBPF, all 11 other data streams (CPU, GPU, memory, frame timing, storage, network,
audio, power, session, hardware, and more) continue to work normally. eBPF adds per-thread
scheduler latency and kernel-level stutter attribution — useful for deep analysis but not
required for day-to-day gaming telemetry.

See [docs/steam-deck.md](steam-deck.md) for the full Steam Deck guide.

## Known limitations

- eBPF probes require Linux kernel 5.8 or later with BTF enabled, and elevated process
  capabilities (`CAP_BPF` and `CAP_PERFMON`).
- Frame timing requires MangoHud (Linux) or PresentMon (Windows). FPS data will not
  appear in the `frame` data stream if neither is present.
- `rigsignal.cpu.game_utilisation_pct` (process-scoped CPU via cgroup) is not yet
  available on Windows.
- Hardware enrichment fields under `rigsignal.hardware.*` are not yet populated on Windows.
