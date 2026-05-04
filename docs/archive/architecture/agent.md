# GamePulse — Rust Agent Architecture

This is a stub for the Rust agent architecture document. See `src/main.rs` and `src/session.rs` as the authoritative source.

## Overview

The Rust agent (`gamepulse-agent`) is the production-primary collector. It runs as a user systemd service, collects metrics at 1 Hz from all hardware subsystems, and ships them to Elasticsearch.

## Main loop

`src/main.rs` drives a 1-second tick loop:
1. Call `session.poll()` — detect game start/end via `/proc` + Steam ACF files
2. Call all 8 collectors in sequence
3. Deep-merge collector outputs with base doc (session.id, game.name, host fields)
4. Bulk-ship merged docs to ES via the shipper

On SIGTERM/SIGINT: flush remaining docs, ship session summary doc, exit.

## Collector trait

Each collector implements a `collect()` method returning `Option<serde_json::Value>`. `None` means no data this tick (e.g. first CPU tick with no delta, or no MangoHud log present). The main loop skips `None` results.

Current collectors (`src/collectors/`):
- `cpu.rs` — `/proc/stat` delta, hwmon temp, cpufreq clock, RAPL power
- `gpu_amd.rs` — sysfs/hwmon, discovers dGPU by max VRAM heuristic
- `memory.rs` — `/proc/meminfo`, game process RSS
- `storage.rs` — `/proc/diskstats` delta, Steam library device detection
- `network.rs` — `/proc/net/dev` delta
- `power.rs` — AMD TDP cap (hwmon), battery, AC, platform profile
- `audio.rs` — PipeWire/PulseAudio/ALSA backend detection, xruns
- `mangohud.rs` — MangoHud CSV log tail, FPS stats, stutter count

## Session lifecycle (`src/session.rs`)

- `poll()` scans `/proc` for Steam game PIDs every tick
- On game detected: write `/tmp/gamepulse/session.json`, emit session-start doc
- On game exit: remove session.json, emit session-end + summary doc
- `last_known_game` tracked to populate summary doc after game exits
- `session.label` auto-generated as `<game-slug>-YYYYMMDD-HHMMSS` on game detect; manual override via `--label` or `[session].label` config

## Host enricher (`src/host.rs`)

Runs once at startup. Captures: GPU model, VRAM, driver version, Mesa version, CPU model, RAM. Selects discrete GPU by max-VRAM heuristic (avoids iGPU on multi-GPU systems).

## eBPF integration

The eBPF daemon (`gamepulse-ebpf`) is a separate binary in `ebpf/`. It reads `/tmp/gamepulse/session.json` via inotify to correlate probes with the current session. See `architecture/ebpf.md` for details.

Phase B will merge eBPF as a feature-flagged module: `cargo build --features ebpf`.

## Shipper (`src/shipper.rs`)

Sends docs to `POST /_bulk` on the Elasticsearch Bulk API. Auth via API key. Matches the Python collector's auth/index format exactly.
