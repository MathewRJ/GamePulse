# GamePulse

GamePulse collects gaming performance telemetry — FPS, GPU/CPU metrics, frame timing
percentiles, audio, storage, network, power, and kernel-level scheduler and I/O data via
eBPF — and ships it to Elasticsearch so you can understand why a game performs the way
it does, not just that it stuttered.

## Compatibility

- **Linux**: full support, including eBPF kernel telemetry
- **Windows**: supported; eBPF data stream is not available on Windows
- **Kibana**: `^8.13` or `^9.0`
- **Elasticsearch**: `^8.13`
- **Subscription**: Basic

## Requirements

- The GamePulse agent binary must be installed and running on the gaming machine.
- Elastic Agent must be deployed on the same machine, or have network access to the
  configured log path.
- **eBPF metrics** (Linux only): kernel 5.8 or later with BTF enabled, and the agent
  process must have `CAP_BPF` and `CAP_PERFMON` capabilities.
- **Frame timing**: MangoHud must be installed and configured with `output_folder` set
  (Linux), or PresentMon must be available (Windows).

## Setup

**1. Install the GamePulse agent**

Follow the platform instructions in the [installation guide](install.md).

**2. Configure Elasticsearch credentials**

Run the interactive setup to provide the agent-side Elasticsearch endpoint and API key.
These credentials are used by the GamePulse agent to write log files locally. The Elastic
integration reads those log files — it does not connect to Elasticsearch directly.

```bash
gamepulse setup
```

**3. Start the agent**

As a user-level systemd service:

```bash
systemctl --user enable --now gamepulse-agent
```

Or as a Steam launch option (per-game):

```
gamepulse run %command%
```

**4. Add the GamePulse integration in Kibana Fleet**

In Kibana, go to **Fleet > Integrations**, search for **GamePulse**, and click **Add GamePulse**.

**5. Configure the log path**

Set the **Paths** variable to match the location where the GamePulse agent writes its log
files. The default is `/var/log/gamepulse/*.log`. On Windows, use
`C:\ProgramData\GamePulse\logs\*.log`.

**6. Apply the policy**

Assign the policy to the Elastic Agent running on your gaming machine and save.

## Configuration

The Elastic integration has one configurable input variable.

**paths** (text, multi-value)

Glob paths to the GamePulse log files produced by the agent.

- Default (Linux): `/var/log/gamepulse/*.log`
- Default (Windows): `C:\ProgramData\GamePulse\logs\*.log`

Separate multiple paths with a newline in the Fleet UI.

## Data streams

| Data stream | Type | Description |
|---|---|---|
| `audio` | metrics | Audio backend, latency, and buffer underrun (xrun) counts per session. |
| `cpu` | metrics | Per-core and total CPU utilisation, clock speeds, temperature, and power draw. |
| `ebpf` | metrics | Kernel-level scheduler latency, block I/O tracing, GPU fence and submit latency, and memory pressure via eBPF probes. Linux only. |
| `events` | logs | Discrete session events: shader compilation, save operations, crashes, and stutter detections. |
| `frame` | metrics | Instantaneous and rolling FPS, frame time percentiles (1% and 0.1% lows), stutter counts, and CPU/GPU bottleneck classification. |
| `gpu` | metrics | GPU core utilisation, clock speed, VRAM usage, temperature (edge and hotspot), power draw, fan speed, and voltage. |
| `memory` | metrics | System RAM usage, availability, and the game process resident set size. |
| `network` | metrics | Upload and download throughput in bytes per second. |
| `power` | metrics | AC connection state, battery percentage, and battery discharge rate (portable devices). |
| `session` | metrics | Hardware and software environment snapshot recorded at game start: CPU/GPU model, driver versions, graphics API, Proton/DXVK/VKD3D versions, and in-game settings. |
| `storage` | metrics | System-level disk read and write throughput in bytes per second. |

## Dashboards

- **GamePulse — Home**: Session overview showing recent games, aggregate FPS trends, and hardware summary.
- **GamePulse — Games**: Per-game performance comparison across sessions and hardware configurations.
- **GamePulse — Hardware**: GPU, CPU, and thermal metrics broken down per session.
- **GamePulse — Engine**: Frame timing detail, shader compilation events, and stutter analysis.
- **GamePulse — Environment**: Performance impact of kernel version, GPU driver, and Proton/DXVK version.
- **GamePulse — Compare**: Side-by-side comparison of two sessions with aligned time axes.
- **GamePulse — Game Library**: All tracked games with median FPS and the hardware context each was played on.

## Troubleshooting

**No data appears after setup**

- Confirm the agent is running: `gamepulse-agent diagnose`
- Confirm the **Paths** variable in Fleet matches the actual log file location.
- Check Elastic Agent logs in Fleet for file input errors.

**Frame timing data is missing**

MangoHud is not installed or `output_folder` is not set in `MangoHud.conf`. All other
metric streams work without MangoHud. See [getting-started.md](getting-started.md) for
MangoHud configuration steps.

**eBPF data is missing on Linux**

Run `gamepulse-agent diagnose` to check kernel BTF availability and capability state.
The eBPF daemon requires kernel 5.8 or later with BTF, and either `CAP_BPF`/`CAP_PERFMON`
or execution as root via `sudo systemctl enable --now gamepulse-ebpf`.

**eBPF stream is empty on Windows**

This is expected. eBPF kernel probes are not available on Windows. All other data streams
function normally.

## Known limitations

- eBPF probes require Linux kernel 5.8 or later with BTF enabled, and elevated process
  capabilities (`CAP_BPF` and `CAP_PERFMON`).
- Frame timing requires MangoHud (Linux) or PresentMon (Windows). FPS data will not
  appear in the `frame` data stream if neither is present.
- `gamepulse.cpu.game_utilisation_pct` (process-scoped CPU via cgroup) is not yet
  available on Windows.
- Hardware enrichment fields under `gamepulse.hardware.*` are not yet populated on Windows.
