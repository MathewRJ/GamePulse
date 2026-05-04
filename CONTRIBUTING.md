# Contributing to GamePulse

Thank you for your interest in contributing to GamePulse! This project aims to build an open, community-driven gaming telemetry platform, and contributions of all kinds are welcome.

## Ways to Contribute

- **Report bugs** — open an issue with reproduction steps, your hardware/OS info, and `gamepulse-agent --debug --once` output
- **Request features** — describe the use case, not just the feature
- **Submit telemetry** — run the agent and opt in to public sharing to grow the community dataset
- **Write code** — see Development below
- **Add GPU support** — Intel Arc, mobile GPUs, and Windows AMD (ADL/ADLX) all need work
- **Add game config parsers** — parse in-game graphics settings for popular titles
- **Improve documentation** — user guides, architecture docs, API docs
- **Create Kibana dashboards** — if you build a useful dashboard, share the NDJSON export

## Development

### Prerequisites

- Rust 1.75+ (install via [rustup](https://rustup.rs))
- Linux for full functionality (Windows builds are supported but with reduced metrics)
- For eBPF development: kernel 5.8+, `bpftool`, and `cargo install bpf-linker`

### Building

```bash
git clone https://github.com/MathewRJ/GamePulse.git
cd GamePulse
cargo build          # Debug build
cargo build --release # Optimised build
cargo test           # Run tests
```

### Project Structure

```
src/
├── main.rs              # Entry point, CLI, main loop
├── config.rs            # TOML configuration
├── analytics.rs         # Session summarization, comparison queries
├── lifecycle.rs         # Game session state machine
├── session.rs           # Session documents
├── collector/           # Metric collectors
│   ├── cpu.rs           # CPU metrics
│   ├── gpu/             # GPU metrics (AMD sysfs, NVIDIA NVML)
│   ├── memory.rs        # Memory metrics
│   ├── storage.rs       # Storage I/O and device classification
│   ├── network.rs       # Network metrics
│   ├── frametime.rs     # FPS and frame timing
│   └── process.rs       # Per-game process metrics
├── detector/            # Game and compatibility detection
│   ├── game.rs          # Steam game auto-detection
│   └── proton.rs        # Proton/DXVK/VKD3D version detection
├── ebpf/                # eBPF deep telemetry
│   ├── mod.rs           # Probe manager
│   └── probes/          # Individual probe implementations
├── enricher/            # System environment collection
├── shipper/             # Elasticsearch shipping
│   └── elasticsearch.rs # Bulk API, index templates, ILM
└── platform/            # OS-specific code
```

### Code Style

- Run `cargo fmt` before committing
- Run `cargo clippy -- -D warnings` to catch issues
- Keep platform-specific code behind `#[cfg(target_os = "...")]` blocks
- Every collector should gracefully degrade — return `None` rather than crash
- Use `tracing` for logging, not `println!`

### Adding a New Collector

1. Create `src/collector/your_collector.rs`
2. Define a metrics struct with `#[derive(Serialize, Deserialize)]`
3. Add the module to `src/collector/mod.rs`
4. Add the field to `CollectedMetrics` (with `skip_serializing_if = "Option::is_none"`)
5. Add to `CollectorManager::new()` and `collect_all()`
6. Add ES field mappings in `src/shipper/elasticsearch.rs`

### Adding a New eBPF Probe

1. Create the userspace loader in `src/ebpf/probes/your_probe.rs`
2. Create the BPF program in `gamepulse-ebpf/src/your_probe.rs`
3. Register in `src/ebpf/probes/mod.rs`
4. Add to the probe factory list in `src/ebpf/mod.rs`

### Commit Messages

Use conventional commits:

```
feat: add Intel Arc GPU support via sysfs
fix: handle missing hwmon for APU temperature
docs: add Steam Deck setup guide
perf: reduce /proc scanning frequency to 5s
```

## Pull Request Process

1. Fork the repo and create a feature branch from `main`
2. Write tests for new functionality where possible
3. Ensure `cargo fmt`, `cargo clippy`, and `cargo test` all pass
4. Update documentation if you've changed behaviour
5. Open a PR with a clear description of what and why

## Code of Conduct

Be kind, be constructive, be inclusive. We're here to make gaming better for everyone.

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
