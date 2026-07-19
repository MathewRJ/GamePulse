RigSignal ships a real-time gaming performance telemetry agent that captures CPU, GPU, memory, storage, network, audio, power, and frame-timing metrics while you play and streams them to Elasticsearch for live dashboards and historical analysis.

## What's in this release

**Linux packages** (`.deb`, `.rpm`, `.pkg.tar.zst`):
- `rigsignal-agent` — the telemetry agent binary
- `rigsignal` — unified launcher CLI (`setup / start / stop / status / run %command%`)
- systemd user unit (`rigsignal-agent.service`)
- Example config at `/etc/rigsignal/rigsignal.toml`
- Three starter game profiles (Starfield, Cyberpunk 2077, Baldur's Gate 3)

**Windows package** (`.msi`):
- `rigsignal-agent.exe` installed to `C:\Program Files\RigSignal\bin\` (added to system PATH)
- Example config at `C:\Program Files\RigSignal\config\rigsignal.toml.example`
- Three starter game profiles at `C:\Program Files\RigSignal\profiles\`

> **eBPF probes** (deep kernel-level GPU/CPU scheduler, block I/O, futex, VFS metrics) are **not** included in these packages — they require a nightly Rust toolchain and `bpf-linker`, and are Linux-only. Arch/CachyOS users who want eBPF should install from AUR (`yay -S rigsignal-git`) which builds everything from source. Windows is an eBPF-equivalent-free build by design.

---

## Requirements

- **Linux** x86_64, glibc 2.39+ (Ubuntu 24.04, Fedora 40, Arch, CachyOS, SteamOS 3.6+) — full feature set
- **Windows** 10 / 11 x86_64 — agent only, no eBPF; some metrics partial (see "Windows caveats" below)
- An Elasticsearch endpoint — the free tier on [Elastic Cloud Serverless](https://www.elastic.co/cloud) works
- An API key with `write` access to `metrics-rigsignal.*` data streams (see Quick Start)
- **MangoHud** (Linux) / **PresentMon** (Windows) — optional, required for frame timing (`rigsignal.fps.*` fields)

---

## Installation

### Arch Linux / CachyOS / Manjaro

Install the pre-built package from this release:
```sh
sudo pacman -U rigsignal-0.2.5-1-x86_64.pkg.tar.zst
```
Or install from AUR (includes eBPF probes, builds from source):
```sh
yay -S rigsignal-git
```

### Debian / Ubuntu (24.04+)

```sh
sudo dpkg -i rigsignal_0.2.5-1_amd64.deb
```

### Fedora / RHEL / openSUSE

```sh
sudo rpm -i rigsignal-0.2.5-1.x86_64.rpm
```

### Windows 10 / 11

Double-click the `.msi` and accept the UAC prompt, or install silently from an admin PowerShell:
```powershell
msiexec /i rigsignal-0.2.5-x86_64.msi /qb!
```

The installer adds `C:\Program Files\RigSignal\bin\` to the system PATH. Open a **new** terminal so `rigsignal-agent` is on PATH:
```powershell
rigsignal-agent --version
```

To uninstall:
```powershell
msiexec /x rigsignal-0.2.5-x86_64.msi /qb!
```
or use *Settings → Apps → Installed apps → RigSignal → Uninstall*.

#### Windows caveats

- No `rigsignal` launcher CLI — Windows has no systemd analog. Configure the agent by hand and run `rigsignal-agent` directly (or wrap it in a Steam launch option, see Quick Start §5).
- `gpu.temperature_c` is reported via WMI ACPI thermal zones (best-effort, may be absent on some boards).
- `cpu.game_utilisation_pct`, `storage.game_io`, `audio.quantum`, `power.battery_rate_w` are **not** populated on Windows in this release — the Linux equivalents require eBPF / hwmon / pw-metadata, which have no direct Windows counterpart yet.
- Frame timing requires [PresentMon](https://github.com/GameTechDev/PresentMon). Place `PresentMon.exe` on PATH or set `RIGSIGNAL_PRESENTMON=C:\path\to\PresentMon.exe`. Without it, all other 7 collectors continue normally and `rigsignal.fps.*` fields are empty.

---

## Quick Start

### 1. Get an Elasticsearch endpoint

Sign up for a free [Elastic Cloud Serverless](https://www.elastic.co/cloud) project. Copy the **Elasticsearch endpoint URL** from the project overview — it looks like `https://your-project.es.us-central1.gcp.elastic.cloud`.

### 2. Create an API key

In Kibana → **Stack Management → API Keys**, create a key with these privileges:
- Index privileges: `create_index`, `create`, `write`, `view_index_metadata` on `metrics-rigsignal.*`
- Cluster privileges: `monitor`

### 3. Run first-time setup

**Linux:**
```sh
rigsignal setup
```
Writes endpoint + API key to `~/.config/rigsignal/rigsignal.toml` and verifies connectivity.

**Windows** (no `rigsignal setup` yet — manual config):
```powershell
$cfgDir = "$env:APPDATA\RigSignal"
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
Copy-Item "C:\Program Files\RigSignal\config\rigsignal.toml.example" "$cfgDir\rigsignal.toml"
notepad "$cfgDir\rigsignal.toml"   # fill in endpoint + api_key, save
```

Or skip the file and use environment variables (Windows + Linux both honour these):
```powershell
[Environment]::SetEnvironmentVariable('ES_URL',     'https://your-project.es.us-central1.gcp.elastic.cloud', 'User')
[Environment]::SetEnvironmentVariable('ES_API_KEY', 'your-api-key-here',                                      'User')
```

### 4. Test with a dry run

```sh
rigsignal-agent --dry-run
```

You should see your hardware snapshot (CPU model, GPU, RAM) printed and `collectors ready` in the log. No data is written to Elasticsearch in dry-run mode.

### 5. Steam integration (Linux, recommended)

In Steam, right-click a game → **Properties → Launch Options**:
```
rigsignal run %command%
```

The agent starts automatically when the game launches and stops cleanly when you quit. Session data appears in Elasticsearch within a few seconds.

> **Windows:** the launcher script is Linux-only in this release. Run `rigsignal-agent.exe` from any terminal before launching the game; it auto-detects Steam, Lutris, Heroic, Bottles, or use `--target-name <ProcessName>` / `--target-pid <PID>` to pin a specific process.

### 6. Manual start / stop

**Linux** (systemd-managed):
```sh
rigsignal start    # start the agent in the background via systemd
rigsignal status   # show current session label and service state
rigsignal stop     # stop the agent gracefully
```

**Windows** (no service yet — run the binary directly):
```powershell
# Foreground (Ctrl+C to stop):
rigsignal-agent

# Background (PowerShell job):
$j = Start-Job { rigsignal-agent }
# ... play ...
Stop-Job $j; Remove-Job $j
```

---

## Configuration

The agent searches for config in this order (first found wins):

**Linux:**
1. `$RIGSIGNAL_CONFIG` env var (full path)
2. `~/.config/rigsignal/rigsignal.toml`
3. `/etc/rigsignal/rigsignal.toml`

**Windows:**
1. `%RIGSIGNAL_CONFIG%` env var (full path)
2. `%APPDATA%\RigSignal\rigsignal.toml` (per-user — recommended)
3. `%PROGRAMDATA%\RigSignal\rigsignal.toml` (system-wide, all users)

The MSI installs a starting template at `C:\Program Files\RigSignal\config\rigsignal.toml.example` — copy it to one of the locations above and fill in your endpoint + API key.

Minimal config:
```toml
[elasticsearch]
endpoint = "https://your-project.es.us-central1.gcp.elastic.cloud"
api_key  = "your-api-key-here"
```

You can also pass credentials via environment variables (takes precedence over config):
```sh
export ES_URL="https://your-project.es.us-central1.gcp.elastic.cloud"
export ES_API_KEY="your-api-key-here"
rigsignal-agent
```

Full config reference: `rigsignal-agent --help` and the example at `/etc/rigsignal/rigsignal.toml`.

---

## Game Profiles

RigSignal ships three starter profiles that auto-apply when the matching game is detected:

| Game | Steam App ID | Auto-configured |
|---|---|---|
| Starfield | 1716740 | FSR 2, ray tracing, ultra preset |
| Cyberpunk 2077 | 1091500 | DLSS / FSR 3 / XeSS, path tracing |
| Baldur's Gate 3 | 1086940 | Vulkan |

Profiles live at `/usr/share/rigsignal/profiles/` (system) or `~/.config/rigsignal/profiles/` (user, takes precedence). Copy and edit one to create your own.

---

## Troubleshooting

**"No Elasticsearch endpoint configured"**
Run `rigsignal setup` or set `ES_URL` and `ES_API_KEY` environment variables.

**"ES ping failed" at startup**
Check that your API key is valid and the endpoint URL ends in `.es.` (not `.kb.`). Run `rigsignal-agent diagnose` for a full connectivity report.

**No frame timing data (`rigsignal.fps.*` fields are null)**
MangoHud must be installed and on `$PATH`. On Arch: `sudo pacman -S mangohud`. On Ubuntu: `sudo apt install mangohud`. The agent will log a one-time warning if MangoHud is not found.

**Agent doesn't detect my game**
The agent detects Steam games automatically. For non-Steam games (Lutris, Heroic, Bottles) detection is also automatic. For anything else, pin the target manually:
```sh
rigsignal-agent --target-name "MyGame"
# or
rigsignal-agent --target-pid 12345
```

**Permissions error on `/proc`**
The agent reads `/proc/<pid>/maps` and `/proc/<pid>/environ` for settings auto-detection. This works without elevated privileges on standard desktop kernels. If you have a hardened kernel with restricted `/proc` access, run `rigsignal-agent --log-level debug` to see which reads are failing.

**Session label counter is stuck at 1**
The counter is stored in `$XDG_STATE_HOME/rigsignal/session-counters.json` (default `~/.local/state/rigsignal/`). If the file is read-only or on a read-only filesystem, the agent falls back gracefully but can't increment. Check file permissions.

**systemd unit fails to start**
If you installed a dev build to `/usr/local/bin/` instead of `/usr/bin/`, the unit's `ExecStart` path won't match. Create a drop-in override:
```sh
mkdir -p ~/.config/systemd/user/rigsignal-agent.service.d
cat > ~/.config/systemd/user/rigsignal-agent.service.d/override.conf <<EOF
[Service]
ExecStart=
ExecStart=/usr/local/bin/rigsignal-agent
EOF
systemctl --user daemon-reload
```

---

## FAQ

**Does RigSignal work with non-Steam games?**
Yes. Lutris, Heroic (Epic/GOG), and Bottles are auto-detected. Any other game can be targeted by process name (`--target-name`) or PID (`--target-pid`). Automatic game detection via behavioural classification (GPU activity + fullscreen signals) is planned for a future release.

**Does it work on Steam Deck?**
Yes, for the standard metric streams. Install via the AUR PKGBUILD or the `.pkg.tar.zst` from this release. eBPF probes are not available on SteamOS's locked kernel, but CPU, GPU, memory, power, and frame timing all work.

**Does it send any data without my consent?**
No. `opt_in_public = false` is the default. Data is sent only to the Elasticsearch endpoint you configure. No telemetry is sent to RigSignal developers.

**What is the performance overhead?**
The agent is designed to be invisible. Typical overhead is <0.3% CPU and <5 MB RSS. The collection interval defaults to 1 second and is configurable. Frame timing via MangoHud adds no overhead beyond MangoHud's own cost.

**I want eBPF probes — what do I need?**
eBPF gives you GPU scheduler latency, CPU runqueue latency, block I/O latency, futex contention, VFS latency, and more. Currently Linux only. Install from AUR (`yay -S rigsignal-git`) which builds the eBPF daemon from source using the nightly Rust toolchain and `bpf-linker`. Pre-built eBPF packages are planned for a future release.

**Can I contribute game profiles?**
Yes — PRs welcome at [github.com/MathewRJ/RigSignal](https://github.com/MathewRJ/RigSignal). A profile is a small TOML file; see `profiles/starfield.toml` for the format.

**What Elasticsearch version is required?**
Elastic Cloud Serverless (recommended) or self-managed Elasticsearch 8.10+. The integration uses TSDS (Time Series Data Stream) index templates which require 8.10+.
