# GamePulse

GamePulse is a gaming performance telemetry platform. A lightweight Rust agent collects FPS, frame times, GPU/CPU temperatures, storage I/O, memory pressure, and more from Linux gaming PCs and ships them to Elasticsearch for analysis in Kibana. Windows support is in active development (Phases B–C). The legacy Python reference implementation (`collector/`) remains for debugging and field validation.

---

## Why GamePulse?

Existing tools (MangoHud, MSI Afterburner, CapFrameX) are local-only. They can't answer questions like:

- Is Proton 9.0-4 actually faster than 9.0-3 for this game on my hardware class?
- Did the new Mesa/driver release cause a regression across the community?
- Is my SD card causing storage stutter vs the internal NVMe?
- What's the performance-per-watt sweet spot on my Steam Deck for this game?

GamePulse ships structured telemetry to Elasticsearch, enabling cross-session, cross-hardware, and cross-configuration comparisons backed by real data.

---

## Quick start

```bash
# Arch Linux / CachyOS / Manjaro
yay -S gamepulse

# First-run setup (prompts for Elasticsearch endpoint + API key)
gamepulse setup

# Add to Steam launch options for any game:
gamepulse run %command%
```

Data starts flowing to Elasticsearch the next time you launch a game through Steam.

---

## What's working today

The Rust production agent (`gamepulse-agent`) is Linux-complete and Elasticsearch-verified — a live 40-minute Starfield session confirmed all 8 metric streams (CPU, GPU, memory, storage, network, audio, power, frame) shipping correctly. The eBPF daemon (`gamepulse-ebpf`, Sprints 1–3) is ES-confirmed for kernel-level scheduler, I/O, GPU fence, futex, IRQ, and VFS probes. Seven Kibana dashboards are built and tested against Elastic Cloud Serverless. Windows collectors are in progress (Phase C); the AUR package is available now.

---

## Status and roadmap

See [`docs/STATUS.md`](docs/STATUS.md) for current state and [`docs/ROADMAP.md`](docs/ROADMAP.md) for milestone structure.

---

## Documentation

- [`docs/install.md`](docs/install.md) — installation guide (Elastic Cloud setup, AUR, .deb/.rpm, building from source)
- [`docs/configuration.md`](docs/configuration.md) — full configuration reference
- [`docs/dashboards.md`](docs/dashboards.md) — dashboard build guide and NDJSON reference
- [`docs/steam-setup.md`](docs/steam-setup.md) — Steam launch options setup
- [`architecture/`](architecture/) — agent, eBPF, and data model architecture docs
- [`docs/SCOPE.md`](docs/SCOPE.md) — strategic project scope

---

## Privacy

- **No PII ever collected**: no usernames, emails, IPs, or location data
- **User identity** is a SHA hash of `/etc/machine-id` — stable for session correlation, not reversible
- All data stays in your personal Elasticsearch instance unless you explicitly opt in to community sharing
- Network metrics and eBPF data are opt-out of community sharing by default

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Most useful contributions right now:

- NVIDIA GPU testing on Linux (sysfs paths and NVML interface need validation across driver versions)
- Windows hardware collection (PDH paths, PresentMon integration)
- Non-CachyOS Arch Linux and Fedora smoke tests

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
