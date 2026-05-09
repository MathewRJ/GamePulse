# Steam Deck Guide

GamePulse is optimised for the Steam Deck. It automatically detects the device model (LCD vs OLED), reads the APU sensors via sysfs, and integrates with gamescope for frame timing.

## Installation

### Desktop Mode (recommended for setup)

Switch to Desktop Mode, open Konsole, and run:

```bash
curl -sSfL https://mathewrj.github.io/GamePulse-Integration/install.sh | sh
```

This installs `gamepulse-agent` and the `gamepulse` launcher to `~/.local/bin/`. If the release
includes the eBPF daemon, the installer will prompt for `sudo` to place it in `/usr/local/bin/`
and enable `gamepulse-ebpf.service` immediately — kernel tracing is active without any follow-up
step. MangoHud is also configured to write frame-timing CSVs automatically.

Because `~/.local/` lives on the persistent home partition, **the agent survives SteamOS updates**.
The eBPF daemon lives on the read-only root filesystem and will need reinstalling after a SteamOS
major update — re-run the installer after each OS update to restore kernel tracing.

### Configuration

```bash
gamepulse setup
```

This prompts for your Elasticsearch endpoint and API key, writes `~/.config/gamepulse/gamepulse.toml`,
and syncs credentials to `/etc/gamepulse/gamepulse.toml` for the eBPF daemon (restarting it if
running). Re-run `gamepulse setup` any time you rotate your API key.

Optional settings you can add manually to `~/.config/gamepulse/gamepulse.toml`:

```toml
[collection]
interval_ms = 1000
network = false      # Enable if playing multiplayer
```

### Start the service

```bash
systemctl --user enable --now gamepulse-agent
```

The agent starts automatically on login and persists across Gaming Mode and Desktop Mode. The eBPF
daemon (`gamepulse-ebpf.service`) is enabled and started by the installer — no separate step needed.

## Storage metrics on Steam Deck

GamePulse classifies and monitors both the internal NVMe and SD card:

- **Internal NVMe**: detected as `nvme` type, typically PCIe Gen4 x4 on the OLED, Gen3 x4 on LCD
- **SD card**: detected as `sd_card` type, with UHS speed class (UHS-I on LCD, UHS-I on OLED) and application class (A1/A2)

When you have games installed on both drives, GamePulse detects which drive the currently running game is installed on and focuses I/O monitoring there. The session document records both drives for comparison.

This is particularly valuable for comparing game loading times and stutter between NVMe and SD card installations.

## Frame timing

On Steam Deck, frame timing works automatically through two sources:

1. **Gamescope stats** — gamescope (the Steam Deck compositor) exposes frame timing data. GamePulse reads this when running in Gaming Mode.

2. **MangoHud** — the performance overlay built into SteamOS. To enable logging, go to the Quick Access menu (the `...` button) → Performance → Performance Overlay Level 1+.

For the most detailed frame timing data, add to your per-game launch options in Steam:

```
MANGOHUD=1 %command%
```

`MANGOHUD=1` is required on Steam Deck — the host `mangohud` binary is ignored by the Steam Linux
Runtime container. `MANGOHUD=1` lets pressure-vessel inject its own MangoHud layer, which writes
CSVs correctly. The installer sets `autostart_log=1` in `~/.config/MangoHud/MangoHud.conf` so
logging starts automatically without pressing F2.

## TDP and power

GamePulse reads the Steam Deck's power envelope (TDP) from sysfs. Combined with FPS data, this enables performance-per-watt analysis — answering questions like "what TDP setting gives me stable 40fps in this game?"

## What gets detected automatically

When you launch a game on Steam Deck, GamePulse automatically captures:

- Device model (Steam Deck LCD or OLED)
- Power source (AC or battery)
- TDP setting
- Game name and Steam App ID
- Proton version, DXVK version, VKD3D-Proton version
- gamescope version
- Mesa driver version
- Which drive the game is installed on (NVMe vs SD card)
- Filesystem type and mount options (btrfs with compression, etc.)

## Known limitations on Steam Deck

- **eBPF requires root**: the installer handles this automatically (prompts for `sudo` and enables `gamepulse-ebpf.service`). If eBPF was skipped during install, re-run the installer with `sudo` access to enable kernel tracing.
- **SteamOS updates**: the read-only rootfs means system-level installs (`/usr/bin`) are wiped on updates. User-level installs (`~/.local/bin`) persist.
- **Gaming Mode limitations**: the agent runs fine in Gaming Mode, but you can't see debug output. Check logs with `journalctl --user -u gamepulse-agent` in Desktop Mode.
