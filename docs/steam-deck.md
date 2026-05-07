# Steam Deck Guide

GamePulse is optimised for the Steam Deck. It automatically detects the device model (LCD vs OLED), reads the APU sensors via sysfs, and integrates with gamescope for frame timing.

## Installation

### Desktop Mode (recommended for setup)

Switch to Desktop Mode, open Konsole, and run:

```bash
curl -sSL https://install.gamepulse.dev | bash
```

Or build from source:

```bash
# Install Rust (persists across updates if installed to ~/.cargo)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

git clone https://github.com/gamepulse/agent.git
cd agent
make release
make install-user
```

### Configuration

```bash
vim ~/.config/gamepulse/gamepulse.toml
```

Set your Elasticsearch endpoint and API key. Recommended Steam Deck settings:

```toml
[collection]
interval_ms = 1000
ebpf = false        # User-mode can't use eBPF — use system service for this
network = false      # Enable if playing multiplayer
```

### Start the service

```bash
systemctl --user enable --now gamepulse-agent
```

The agent will now start automatically when you log in, persist across Gaming Mode and Desktop Mode, and survive SteamOS updates (when installed to `~/.local/bin`).

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
MANGOHUD_LOG=1 %command%
```

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

- **eBPF requires root**: the user-mode service can't use eBPF probes. If you want deep telemetry, install the system service instead (`make install` in Desktop Mode, then `sudo systemctl enable gamepulse-agent`).
- **SteamOS updates**: the read-only rootfs means system-level installs (`/usr/bin`) are wiped on updates. User-level installs (`~/.local/bin`) persist.
- **Gaming Mode limitations**: the agent runs fine in Gaming Mode, but you can't see debug output. Check logs with `journalctl --user -u gamepulse-agent` in Desktop Mode.
