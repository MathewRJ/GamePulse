# Configuration Reference

RigSignal is configured via a TOML file. The agent checks these locations in order:

1. Path specified with `--config PATH` CLI flag
2. `$RIGSIGNAL_CONFIG` environment variable
3. `${XDG_CONFIG_HOME:-~/.config}/rigsignal/rigsignal.toml`
4. `/etc/rigsignal/rigsignal.toml`

Run `rigsignal setup` to create the user config interactively, or create it manually.

---

## `[elasticsearch]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `endpoint` | string | `http://localhost:9200` | Elasticsearch URL |
| `api_key` | string | — | API key (recommended) |
| `username` | string | — | Basic auth username |
| `password` | string | — | Basic auth password |
| `index_prefix` | string | `rigsignal` | Index name prefix |
| `flush_interval_secs` | integer | `5` | How often to bulk-flush to ES |
| `batch_size` | integer | `100` | Documents per bulk request |

`ES_API_KEY` and `ES_URL` environment variables override the TOML values at load time — useful for keyless config files on shared machines.

---

## `[collection]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `interval_ms` | integer | `1000` | Collection interval in milliseconds |
| `cpu` | bool | `true` | CPU utilisation, clocks, temperature, power |
| `memory` | bool | `true` | RAM, swap, game RSS |
| `gpu` | bool | `true` | GPU utilisation, clocks, VRAM, temperature, power |
| `storage` | bool | `true` | Disk I/O throughput |
| `network` | bool | `true` | Network throughput |
| `ebpf` | bool | `false` | eBPF deep telemetry (Linux, kernel 5.8+, CAP\_BPF) |
| `frame_timing` | bool | `true` | FPS and frame time via MangoHud (Linux) / PresentMon (Windows) |
| `game_detection` | bool | `true` | Auto-detect running games |

---

## `[privacy]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `opt_in_public` | bool | `false` | Share anonymous performance data in community dashboards |
| `share_ebpf` | bool | `false` | Include eBPF data in public sharing |
| `share_network` | bool | `false` | Include network metrics in public sharing |

Data never leaves your machine unless `opt_in_public = true`.

---

## `[session]`

Per-session metadata and settings capture.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `label` | string | — | Fixed session label (overrides auto-generated label) |
| `target_pid` | integer | — | Skip auto-detection; monitor this PID |
| `target_name` | string | — | Skip auto-detection; find process by name |

### `[session.settings]` — Tier 1 manual settings capture

Document the in-game settings you're using so they appear alongside performance data.
All fields optional. Overridable via CLI flags (CLI wins over config).

| Key | Type | Description |
|-----|------|-------------|
| `preset` | string | `low` \| `medium` \| `high` \| `ultra` \| `custom` |
| `upscaler_tech` | string | `dlss` \| `fsr` \| `xess` \| `tsr` \| `none` |
| `upscaler_preset` | string | `quality` \| `balanced` \| `performance` \| `ultra_performance` \| `dlaa` \| `native` |
| `frame_gen_tech` | string | `dlss3` \| `fsr3` \| `afmf` \| `lossless-scaling` \| `none` |
| `features_active` | string[] | Active features: `ray_tracing`, `path_tracing`, `direct_storage`, `mesh_shaders`, `hdr`, `vrr` |
| `render_resolution_output` | string | Output resolution, e.g. `3440x1440` |
| `render_vsync` | string | `off` \| `on` \| `adaptive` \| `fast` |
| `notes` | string | Free-text annotation for this session |

---

## CLI flags

| Flag | Description |
|------|-------------|
| `--config PATH` | Config file path |
| `--dry-run` | Collect one cycle, print JSON to stdout, exit without shipping to ES |
| `-v`, `--verbose` | Enable debug logging (equivalent to `--log-level debug`) |
| `--log-level LEVEL` | `error` \| `warn` \| `info` \| `debug` \| `trace`. Overrides `--verbose` and `RIGSIGNAL_LOG` |
| `--print-config` | Print resolved config (credentials redacted) to stdout and exit |
| `--label TEXT` | Session label override |
| `--preset VALUE` | Graphics preset (see `[session.settings]`) |
| `--upscaler TECH[:PRESET]` | e.g. `dlss:quality`, `fsr:balanced`, `xess` |
| `--frame-gen TECH` | Frame generation technology |
| `--features FEATURE,...` | Comma-separated active features |
| `--resolution WxH` | Output resolution, e.g. `3440x1440` |
| `--vsync MODE` | VSync mode |
| `--notes TEXT` | Free-text session notes |
| `--target-pid PID` | Monitor a specific process ID |
| `--target-name NAME` | Find process by name (case-insensitive, first match wins) |

### `diagnose` subcommand

```bash
rigsignal-agent diagnose [--output PATH]
```

Writes a single-file bug report: kernel version, OS, CPU, RAM, GPU (vendor/model/VRAM/driver),
Elasticsearch reachability, resolved config path, and a log of every probe step. API key is
redacted in the output.

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `ES_API_KEY` | Overrides `[elasticsearch].api_key` in TOML |
| `ES_URL` | Overrides `[elasticsearch].endpoint` in TOML |
| `RIGSIGNAL_CONFIG` | Config file path (overridden by `--config`) |
| `RIGSIGNAL_LOG` | Log level: `error` \| `warn` \| `info` \| `debug` \| `trace` |
| `RIGSIGNAL_PROFILES_DIR` | First search path for per-game profile TOML files (D.7) |
| `RIGSIGNAL_PRESENTMON` | Full path to `PresentMon.exe` (Windows; overrides binary-dir and PATH lookup) |

Log level precedence: `--log-level` > `--verbose` > `RIGSIGNAL_LOG` > `info`

---

## Example configuration

```toml
[elasticsearch]
endpoint = "https://my-deployment.es.us-central1.gcp.elastic.cloud"
api_key = "base64-encoded-api-key"

[collection]
interval_ms = 1000
network = true
ebpf = false

[session]
# label = "after-driver-update"    # uncomment for a fixed label

[session.settings]
preset = "ultra"
upscaler_tech = "fsr"
upscaler_preset = "quality"
features_active = ["ray_tracing"]
render_vsync = "off"
notes = "Testing FSR 3 quality mode"
```

---

## Performance impact

- CPU: typically < 0.5% of one core
- Memory: ~15–30 MB RSS
- Disk I/O: reads `/proc` and sysfs only (no writes except ES bulk API)
- Network: small bulk requests every 5 seconds (configurable via `flush_interval_secs`)

Increase `interval_ms` (e.g. `2000`) or disable unused collectors to further reduce overhead.
