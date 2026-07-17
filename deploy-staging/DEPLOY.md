# RigSignal 0.2.3 paired deploy (2026-07-17)

Artifacts in this directory (hashes in `SHA256SUMS`):

| file | version | installs to | who installs |
|---|---|---|---|
| `rigsignal-agent` | 0.2.3 (`--version` attests) | `~/.local/bin/rigsignal-agent` | orchestrator over SSH (user service) |
| `rigsignal-ebpf` | 0.2.3 (no `--version` flag; attest by sha256) | `/usr/local/bin/rigsignal-ebpf` | **user** (sudo) |
| `rigsignal-ebpf-probes` | unchanged from 07-16 (`4a25554f…` — seed fix is userspace-only) | `/usr/local/lib/rigsignal/rigsignal-ebpf-probes` | **user** (sudo; re-install is a same-hash no-op) |

Targets: **GamingPC** `deck@192.168.50.254` AND **StreamClient** `deck@192.168.50.162`
(both run `rigsignal-agent.service` (user) + `rigsignal-ebpf.service` (system)).

## What 0.2.3 changes at runtime

Agent: PipeWire audio enrichment (item 4), plus items 1/2/3/6/7/8 already validated
on-box 2026-07-16. eBPF daemon: running-game seed fix — games already running at daemon
start now get GAME_PIDS coverage (environ-scan union, per-thread children walk, 1024-TID
cap, 30 s active refresh).

## Agent (orchestrator-run, both boxes) — DONE over SSH, see session log

```sh
scp rigsignal-agent SHA256SUMS deck@<box>:/tmp/rigsignal-0.2.3/
ssh deck@<box>
  cd /tmp/rigsignal-0.2.3 && sha256sum -c --ignore-missing SHA256SUMS
  cp -a ~/.local/bin/rigsignal-agent ~/.local/bin/rigsignal-agent.bak-$(date +%Y%m%d)
  install -m 755 rigsignal-agent ~/.local/bin/rigsignal-agent
  systemctl --user restart rigsignal-agent
  rigsignal-agent --version   # must print 0.2.3
```

## eBPF daemon (USER-run, both boxes)

Same procedure as the 07-16 paired install (backups, `steamos-readonly disable`,
`install`, restart, journal check) — see the version of this file at commit 4039f80 for
the full command block. Only `rigsignal-ebpf` differs by hash; installing the staged
`rigsignal-ebpf-probes` over the existing one is a no-op (identical hash), which keeps
the pair rule satisfied without a probe-contract risk.

Post-install seed-fix verification (per box): with a game ALREADY running, restart
`rigsignal-ebpf` and confirm within one interval:

```sh
sudo journalctl -u rigsignal-ebpf --since -3min --no-pager | grep -E 'seed_source|session detected'
# expect seed_source=recorded pids|environ scan|union with tid_count > 0
```

then check `metrics-rigsignal.ebpf-default` receives docs for that session.

Keep backups until a live game session confirms eBPF documents are produced.
Restore both pair files together if rollback is required.
