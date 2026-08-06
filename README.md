# RigSignal

RigSignal is a gaming telemetry agent for Elastic. A lightweight Rust binary
(`rigsignal-agent`) runs alongside your games, collecting FPS/frame timing, GPU
and CPU utilisation and temperature, memory, storage, network, audio, and
power metrics, and ships them to Elasticsearch as a per-dataset NDJSON spool
for analysis in Kibana. On Linux, an optional eBPF daemon adds kernel-level
scheduler, block I/O, GPU fence, futex, and VFS telemetry. Linux and Windows
are both supported; eBPF is Linux-only.

0.3.0 adds RigSignal's first built-in diagnostic: `rigsignal-agent diagnose
display` (D6) — a CLI check that compares a Gamescope `modes.cfg` override
against the display state Gamescope is actually driving, and reports a
verdict, cited evidence, a confidence score, falsifier, supported scope,
missing evidence, and nearest alternative — not just a frame-time graph.

---

## Why RigSignal?

Existing tools (MangoHud, MSI Afterburner, CapFrameX) are local-only and
measure frame delivery inside the render pipeline. They can't answer
questions like:

- Is Proton 9.0-4 actually faster than 9.0-3 for this game on my hardware class?
- Did the new Mesa/driver release cause a regression across the community?
- Why does my TV show the wrong resolution after a Gamescope session — and is that a display-state problem or a frame-time problem?
- What's the performance-per-watt sweet spot for this game on my setup?

RigSignal ships structured telemetry to Elasticsearch for cross-session and
cross-configuration comparisons, and adds CLI-first diagnostics that compare
configuration against hardware state directly — the first of which is D6.

---

## Quick start

### Linux installer channels (recommended; agent-only by default)

```bash
# Latest channel (mutable; resolves the current release payload)
curl -sSfL https://mathewrj.github.io/RigSignal-Integration/install.sh | sh

# Reproducible release (pins both the installer and payload)
VERSION=<release-version>
curl -sSfL "https://github.com/MathewRJ/RigSignal/releases/download/v${VERSION}/install.sh" | sh -s -- --version "${VERSION}"
```

Installs `rigsignal-agent` and the `rigsignal` launcher to `~/.local/bin`
(no root required) and sets up the user systemd service. The default install
is agent-only; add `--with-ebpf` to install and start the pre-built eBPF
service with one `sudo` prompt.
Works on SteamOS and other read-only-root distros — this is the recommended
path on Steam Deck.

### Arch Linux / CachyOS / Manjaro (AUR, builds from source incl. eBPF)

```bash
yay -S rigsignal-git
```

### Distro packages (agent only, no eBPF)

```bash
sudo pacman -U rigsignal-0.3.3-1-x86_64.pkg.tar.zst   # Arch, pre-built
sudo dpkg -i rigsignal_0.3.3-1_amd64.deb               # Debian / Ubuntu 24.04+
sudo rpm -i rigsignal-0.3.3-1.x86_64.rpm               # Fedora / RHEL / openSUSE
```

Download these from the [Releases page](https://github.com/MathewRJ/RigSignal/releases).
If you want eBPF with a distro package, run the one-line installer alongside
it, or use the AUR package instead.

### Windows

Download the `.msi` from the [Releases page](https://github.com/MathewRJ/RigSignal/releases)
and run it, or install silently from an admin PowerShell:

```powershell
msiexec /i rigsignal-0.3.3-x86_64.msi /qb!
```

This installs `rigsignal-agent.exe` to `C:\Program Files\RigSignal\bin\` and
adds it to the system PATH. There is no `winget` package at this time. eBPF
is Linux-only and not part of the Windows build. Frame timing on Windows
requires [PresentMon](https://github.com/GameTechDev/PresentMon) on PATH
(set `RIGSIGNAL_PRESENTMON` to override the location).

### First run

```bash
# Linux — prompts for your Elasticsearch endpoint + API key
rigsignal setup

# Add to Steam launch options for any game:
rigsignal run %command%
```

Windows has no `rigsignal` launcher yet in this release — configure
`rigsignal-agent.exe` by hand (see [`docs/install.md`](docs/install.md)) and
run it directly, or wrap it in a Steam launch option.

MangoHud (Linux) is configured automatically by the one-line installer to
write frame-timing CSVs — no extra setup needed for `rigsignal.fps.*` data.

### Diagnose a display problem (Linux, Gamescope)

If a game or TV looks wrong after a Gamescope session — wrong resolution,
letterboxing, a config that survived a reboot it shouldn't have:

```bash
rigsignal-agent diagnose display
```

Exit codes are scriptable: `0` for `ok`/`not-applicable`, `1` for a real
finding, `2` for an incomplete or invalid invocation. See
[`docs/diagnose-display.md`](docs/diagnose-display.md) for the full
verdict/evidence/confidence/falsifier/scope/missing-evidence/alternative
contract and a real incident replay.

RigSignal also includes D3, `rigsignal-agent diagnose gpu-boot`, for a GPU
that disappears after a warm boot. It compares authoritative PCI sysfs state
with retained boot journals and uses an explicit per-slot baseline. See
[`docs/diagnose-gpu-boot.md`](docs/diagnose-gpu-boot.md).

---

## Connecting to Elasticsearch

RigSignal requires an Elasticsearch endpoint. [Elastic Cloud Serverless](https://www.elastic.co/cloud)
is the recommended, quickest path; self-managed Elasticsearch 8.10+ also
works (the integration uses TSDS index templates, which require 8.10+).

In Kibana → **Stack Management → API Keys**, create a key with:
- Index privileges: `create_index`, `create`, `write`, `view_index_metadata` on `metrics-rigsignal.*` **and** `logs-rigsignal.*` (the agent ships both metrics and logs data streams)
- Cluster privileges: `monitor`

Then run `rigsignal setup` (Linux) and enter the endpoint and API key when
prompted, or set `ES_URL` / `ES_API_KEY` environment variables (both
platforms honour these; they take precedence over the config file).

See [`docs/install.md`](docs/install.md) for the full installation guide,
including self-hosted Elasticsearch/Kibana setup, package managers, systemd
service management, and troubleshooting.

`rigsignal assets install --repair` can reconcile only a proven RigSignal-owned Elasticsearch object. It cannot rewrite a present divergent Kibana saved object, space, or role: delete it in Kibana and rerun the installer so its guarded create path can recreate it.

---

## What RigSignal ships today

The Rust agent runs one collection cycle per tick across CPU, GPU, memory,
storage, network, audio, power, and frame-timing collectors, plus session and
event streams — 12 metrics data streams and one logs data stream in total,
all under the `metrics-rigsignal.*` / `logs-rigsignal.*` naming convention
(see [`docs/metrics-reference.md`](docs/metrics-reference.md) for the field
list). Output is a per-dataset NDJSON spool that is finalized and shipped to
Elasticsearch, with startup recovery and retention pruning for durability.
On Linux, an optional eBPF daemon adds kernel-level scheduler latency, block
I/O, GPU fence, futex, IRQ, and VFS probes. Windows collectors cover the same
8 metric streams with some fields not yet populated (see the Windows caveats
in [`docs/install.md`](docs/install.md)) and no eBPF equivalent.

0.3.0 adds the first diagnostic detector, D6 (`diagnose display`), described
above. A Kibana dashboard suite ships alongside the agent, covering game
performance, system health, software/config context, engine internals, and
a streaming-lab view for Remote Play sessions.

---

## Status and roadmap

See [`docs/STATUS.md`](docs/STATUS.md) for current release state and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for milestone structure.

---

## Documentation

| Guide | Link | Audience |
|---|---|---|
| Getting started | [`docs/getting-started.md`](docs/getting-started.md) | First install + first session |
| Installation | [`docs/install.md`](docs/install.md) | Full install guide: Elastic Cloud, self-hosted, AUR, packages, systemd |
| Assets install contract | [`docs/assets-install-exit-contract.md`](docs/assets-install-exit-contract.md) | Assets/full-flow exit codes, refusal taxonomy, and recovery surfaces |
| Asset recovery | [`docs/RECOVERY.md`](docs/RECOVERY.md) | Safe recovery boundaries for interrupted asset installation |
| `diagnose display` (D6) | [`docs/diagnose-display.md`](docs/diagnose-display.md) | Display mode-override diagnostic — usage, verdicts, exit codes |
| `diagnose gpu-boot` (D3) | [`docs/diagnose-gpu-boot.md`](docs/diagnose-gpu-boot.md) | PCI/journal GPU boot-enumeration diagnostic — explicit slot baseline, findings, and recovery |
| Metrics reference | [`docs/metrics-reference.md`](docs/metrics-reference.md) | Field-by-field reference for every data stream |
| Configuration reference | [`docs/configuration.md`](docs/configuration.md) | All config options |
| eBPF kernel telemetry | [`docs/ebpf.md`](docs/ebpf.md) | eBPF daemon setup and probe reference |
| Steam Deck | [`docs/steam-deck.md`](docs/steam-deck.md) | SteamOS-specific install and eBPF persistence |
| Dashboard guide | [`docs/dashboards.md`](docs/dashboards.md) | Dashboard build and NDJSON reference |
| Steam launch options | [`docs/steam-setup.md`](docs/steam-setup.md) | Per-game Steam integration |
| Architecture | [`docs/architecture.md`](docs/architecture.md) | Agent, eBPF, and data model internals |
| Elastic Fleet integration | [`docs/README.md`](docs/README.md) | Adding RigSignal to Kibana Fleet |

---

## Privacy

- `rigsignal.session.opt_in_public` defaults to `false` — public/community
  data sharing is not enabled by default and is not currently implemented.
- Data is sent only to the Elasticsearch endpoint you configure. No
  telemetry is sent to RigSignal's developers.
- Some fields carry local, privacy-sensitive identifiers (for example, Steam
  Remote Play peer name/id); see the "Privacy boundary" note in
  [`docs/metrics-reference.md`](docs/metrics-reference.md) for exactly which
  fields and their handling.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Most useful contributions right now:

- NVIDIA GPU testing on Linux (sysfs paths and NVML interface need validation across driver versions)
- Fedora and non-CachyOS Arch Linux platform-parity testing (see the platform matrix in `docs/STATUS.md`)
- Windows metric-parity gaps: `power.battery_rate_w` is not yet populated on Windows (`audio.quantum` is a Linux-only PipeWire concept); per-game CPU utilisation and per-game storage I/O are not implemented on any platform yet

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
