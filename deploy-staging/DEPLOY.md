# GamingPC eBPF paired install

These two files are one map-contract pair and must be installed together:

- `rigsignal-ebpf` → `/usr/local/bin/rigsignal-ebpf`
- `rigsignal-ebpf-probes` → `/usr/local/lib/rigsignal/rigsignal-ebpf-probes`

Do not run these commands from the build host automatically. The GamingPC user runs them.

## Transfer from the build host

```sh
cd /home/dev/coding/RigSignal/worktrees/codex-ebpf-rebuild/deploy-staging
sha256sum -c SHA256SUMS
ssh deck@192.168.50.254 'mkdir -p /tmp/rigsignal-ebpf-rebuild'
scp rigsignal-ebpf rigsignal-ebpf-probes SHA256SUMS \
  deck@192.168.50.254:/tmp/rigsignal-ebpf-rebuild/
```

## Install on GamingPC (user-run)

```sh
cd /tmp/rigsignal-ebpf-rebuild
sha256sum -c SHA256SUMS
sudo systemctl cat rigsignal-ebpf
# Confirm ExecStart uses /usr/local/bin/rigsignal-ebpf and
# /usr/local/lib/rigsignal/rigsignal-ebpf-probes before continuing.

DEPLOY_START="$(date --iso-8601=seconds)"
sudo systemctl stop rigsignal-ebpf
sudo steamos-readonly disable
sudo cp -a /usr/local/bin/rigsignal-ebpf \
  /usr/local/bin/rigsignal-ebpf.bak-"$(date +%Y%m%d-%H%M%S)"
sudo cp -a /usr/local/lib/rigsignal/rigsignal-ebpf-probes \
  /usr/local/lib/rigsignal/rigsignal-ebpf-probes.bak-"$(date +%Y%m%d-%H%M%S)"
sudo install -m 755 rigsignal-ebpf /usr/local/bin/rigsignal-ebpf
sudo install -m 644 rigsignal-ebpf-probes \
  /usr/local/lib/rigsignal/rigsignal-ebpf-probes
sudo steamos-readonly enable
sudo systemctl restart rigsignal-ebpf
sudo systemctl --no-pager --full status rigsignal-ebpf
```

This restarts the system eBPF service used in the 0.2.3 installation; it does
not replace or restart the separate user `rigsignal-agent` service.

## Post-install verification on GamingPC

```sh
sudo sha256sum /usr/local/bin/rigsignal-ebpf \
  /usr/local/lib/rigsignal/rigsignal-ebpf-probes
sudo journalctl -u rigsignal-ebpf --since "$DEPLOY_START" --no-pager \
  | grep -Ei 'probe|loaded|attach'
if sudo journalctl -u rigsignal-ebpf --since "$DEPLOY_START" --no-pager \
  | grep -E 'could not (seed|update) GAME_PIDS|could not insert TID into GAME_PIDS'; then
  echo 'Unexpected GAME_PIDS seed warning found'
else
  echo 'No GAME_PIDS seed warnings since paired install'
fi
```

Keep the two backups until a live game session confirms eBPF documents are
produced. Restore both backups together if rollback is required.
