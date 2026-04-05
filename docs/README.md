# GamePulse

Gaming telemetry for Elastic. GamePulse captures FPS, GPU/CPU metrics, frame timing
percentiles, and kernel-level scheduler, I/O, and GPU fence data via eBPF — giving you
insight into *why* a game performs the way it does, not just that it stuttered.

## Data streams

| Data stream | Type | Content |
|---|---|---|
| `metrics-gamepulse.frame` | metrics (TSDS) | FPS, frame times, stutter events, bottleneck |
| `metrics-gamepulse.gpu` | metrics (TSDS) | Utilisation, clocks, temps, power, VRAM |
| `metrics-gamepulse.cpu` | metrics (TSDS) | Per-core utilisation, clocks, temps, power |
| `metrics-gamepulse.memory` | metrics (TSDS) | System + game-process memory, swap, page faults |
| `metrics-gamepulse.storage` | metrics (TSDS) | I/O throughput, IOPS, latency percentiles |
| `metrics-gamepulse.network` | metrics (TSDS) | Throughput, packet rates, TCP retransmits |
| `metrics-gamepulse.power` | metrics (TSDS) | Battery, AC, TDP (handhelds/laptops) |
| `metrics-gamepulse.audio` | metrics (TSDS) | Backend, latency, xruns |
| `metrics-gamepulse.session` | metrics (TSDS) | Hardware/software environment snapshot |
| `metrics-gamepulse.ebpf` | metrics (TSDS) | Scheduler latency, I/O tracing, GPU fences |
| `logs-gamepulse.events` | logs | Shader compiles, stutters, crashes |

## Requirements

- **Linux**: kernel ≥ 5.8 for eBPF telemetry (BTF support required)
- **GPU**: AMD via sysfs; NVIDIA via nvidia-smi
- **Frame timing**: MangoHud ≥ 0.7 with `output_folder` configured
- **Elasticsearch**: Elastic Cloud Serverless or self-managed ≥ 8.14

## Configuration

The collector is configured via `~/.config/gamepulse/config.toml`. See the
[configuration reference](configuration.md) for all options.

Three collection profiles are available:

- **Standard** — surface metrics (FPS, GPU, CPU, memory, storage, audio, network)
- **Developer** — surface + eBPF kernel telemetry (requires `CAP_BPF + CAP_PERFMON`)
- **Minimal** — FPS, GPU temp/util, CPU util only (battery-conscious mode)
