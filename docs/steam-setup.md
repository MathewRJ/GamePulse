# Steam Integration

## Steam Launch Option

In Steam, right-click the game → **Properties** → **Launch Options**:

```
rigsignal run %command%
```

That's it. RigSignal starts automatically when you launch the game and stops
when you exit. The session is labelled automatically (e.g. `starfield-20260414-154822`).

---

## First-time Setup

Before using the Steam integration, run setup once to configure your Elasticsearch
credentials:

```sh
rigsignal setup
```

This will prompt for your Elasticsearch endpoint and API key, verify the connection,
and write `~/.config/rigsignal/rigsignal.toml`. Your API key is stored only in that
file (mode 600) and is never printed to the terminal after entry.

To get an API key from Elastic Cloud:
1. Open your deployment → **Management** → **Security** → **API keys**
2. Create a key with write access to `metrics-rigsignal.*` and `logs-rigsignal.*`
3. Copy the Base64-encoded key (shown once at creation time)

---

## Manual Start/Stop

If you prefer not to use the Steam integration:

```sh
rigsignal start   # before launching a game
rigsignal stop    # after exiting
```

`rigsignal start` starts the metrics agent (always) and the eBPF kernel daemon
(if you have sudo access). The eBPF daemon is optional — the agent ships all
CPU, GPU, memory, storage, network, audio, power, and frame metrics without it.
eBPF adds scheduler latency, GPU fence, and IRQ probe data.

---

## Checking Status

```sh
rigsignal status
```

Shows:
- Whether the agent and eBPF daemon are running
- The last detected game and session label
- The path to the active config file

To stream live logs:

```sh
journalctl --user -u rigsignal-agent -f
```

---

## How It Works

`rigsignal run %command%` does three things:

1. **Starts** the rigsignal-agent (user service) and rigsignal-ebpf daemon
2. **Executes** the game command, waiting for it to finish
3. **Stops** both services when the game exits — whether via normal exit, crash, or SIGTERM

The agent detects the game via `/proc` environment scanning (looks for `SteamAppId`)
within ~5 seconds of the process appearing. Each session gets an auto-generated label
in the form `<game-slug>-YYYYMMDD-HHMMSS` (e.g. `cyberpunk-2077-20260415-200000`).

---

## Troubleshooting

**"rigsignal: command not found"**
The AUR package installs `rigsignal` to `/usr/bin/rigsignal`. If you installed
from source, ensure `packaging/rigsignal-launcher.sh` is on your PATH.

**"Failed to start rigsignal-agent"**
Check that the systemd user service is installed:
```sh
systemctl --user status rigsignal-agent
```
If not found, install the rigsignal AUR package or copy the service unit manually:
```sh
install -Dm644 packaging/systemd/rigsignal-agent.service \
    ~/.config/systemd/user/rigsignal-agent.service
systemctl --user daemon-reload
```

**"eBPF daemon not started"**
The eBPF daemon requires `sudo` access or a polkit rule granting systemctl
permission for your user. Without it, RigSignal runs in agent-only mode —
all standard metrics (CPU, GPU, frame, etc.) are still collected and shipped.

To grant access without a password prompt, add a sudoers rule:
```
# /etc/sudoers.d/rigsignal-ebpf
%wheel ALL=(ALL) NOPASSWD: /usr/bin/systemctl start rigsignal-ebpf
%wheel ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop rigsignal-ebpf
```

**"Already configured but connection failed"**
Your API key may have expired or the deployment may be paused. Re-run:
```sh
rigsignal setup
```
Enter new credentials when prompted — setup will overwrite the existing config
only after verifying the new connection succeeds.

**Game launches but no data in Kibana**
Check that the agent detected the game:
```sh
journalctl --user -u rigsignal-agent -n 50 | grep "Game detected"
```
If not present, the game may have launched before the agent started (the agent
needs ~5 seconds to begin scanning). This is unlikely with `rigsignal run` since
it waits for the agent to become active before launching the game.
