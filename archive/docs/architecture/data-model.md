# GamePulse — Data Model

This is a stub for the data model architecture document. See `data_stream/*/fields/fields.yml` for authoritative field definitions.

## Data streams

Each data stream maps to a Elasticsearch data stream at `metrics-gamepulse.<dataset>-default`.

### gamepulse.cpu
Per-second CPU metrics: utilisation, clocks, temperatures, RAPL power, governor, boost state.
See `data_stream/cpu/fields/fields.yml`.

### gamepulse.gpu
Per-second GPU metrics: utilisation, clocks, VRAM, temperatures (edge/hotspot/memory), power, fan.
See `data_stream/gpu/fields/fields.yml`.

### gamepulse.memory
Per-second memory metrics: system RAM, swap, page faults, game process RSS.
See `data_stream/memory/fields/fields.yml`.

### gamepulse.storage
Per-second storage metrics: throughput (MB/s), IOPS, latency, queue depth, I/O wait.
See `data_stream/storage/fields/fields.yml`.

### gamepulse.network
Per-second network metrics: throughput, TCP retransmits, connection type.
See `data_stream/network/fields/fields.yml`.

### gamepulse.audio
Per-second audio metrics: backend (PipeWire/PulseAudio/ALSA), xruns, latency, sample rate.
See `data_stream/audio/fields/fields.yml`.

### gamepulse.power
Per-second power metrics: TDP cap, battery, AC state, platform profile.
See `data_stream/power/fields/fields.yml`.

### gamepulse.frame
Per-second frame timing metrics from MangoHud: avg FPS, 1%/0.1% lows, frametime, stutter count.
See `data_stream/frame/fields/fields.yml`.

### gamepulse.session
Session lifecycle documents: hardware snapshot (GPU model, VRAM, driver version, CPU, RAM),
game detection (name, Steam app ID, graphics API, Proton version), session summary (avg FPS,
peak temps, bottleneck, total frames).
See `data_stream/session/fields/fields.yml`.

### gamepulse.ebpf
eBPF probe data: scheduler latency histograms, block I/O latency, GPU scheduler events,
futex contention, IRQ latency, VFS latency, GPU fence waits, stutter correlation.
See `data_stream/ebpf/fields/fields.yml`.

## Common fields

Every document across all streams carries:
- `@timestamp` — collection time (RFC 3339)
- `gamepulse.session.id` — UUID linking all docs from one agent run
- `gamepulse.session.label` — human-readable label (auto-generated or user-set)
- `gamepulse.game.name` — detected game name (absent when no game running)
- `host.*` — ECS host fields (hostname, OS, kernel)

## Secondary indices

- `gamepulse-game-timeline` — ES transform pivot aggregating session stream data per game+session.
  Data view ID: `gp-dv-timeline`. Source: `metrics-gamepulse.session-default`.
