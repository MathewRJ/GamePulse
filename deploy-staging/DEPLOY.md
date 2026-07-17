# RigSignal gpu_sched-port paired deploy (2026-07-17, post-0.2.3 — pre-0.2.4-bump build off main ad4da7d)

Artifacts in this directory (hashes in `SHA256SUMS`); staged on both boxes at
`/tmp/rigsignal-gpusched/` (sha256-verified over SSH).

| file | state | installs to | who installs |
|---|---|---|---|
| `rigsignal-agent` | UNCHANGED 0.2.3 (`65b6bc20…`) — not part of this deploy | — | — |
| `rigsignal-ebpf` | gpu_sched port (`feeb6d5a…`; attest by sha256, crate still says 0.2.3 — bump is gated on live validation) | `/usr/local/bin/rigsignal-ebpf` | **user** (sudo) |
| `rigsignal-ebpf-probes` | gpu_sched port (`a301f174…` — BPF contract changed: new `GPU_SCHED_KEY_OFFSET` config map; MUST install as a pair) | `/usr/local/lib/rigsignal/rigsignal-ebpf-probes` | **user** (sudo) |

Targets: **GamingPC** `deck@192.168.50.254` AND **StreamClient** `deck@192.168.50.162`.
Unlike the 0.2.3 deploy, BOTH pair files changed — never install one without the other.

## What this deploy changes at runtime

gpu_sched probe attaches on valve 6.16 kernels via the legacy tracepoint pair
(`drm_sched_job`/`drm_run_job`), keyed on `id` at an attach-time-parsed offset; the
renamed pair's offset is also parsed at attach now (no hardcoded offsets remain).
Expected on both boxes: **9/9 probes loaded** and one info log line with
`variant=legacy key_field=id key_offset=32`. Emitted ES fields are unchanged.

## Install (USER-run, sudo, both boxes)

```sh
ssh deck@<box>
cd /tmp/rigsignal-gpusched && sha256sum -c --ignore-missing SHA256SUMS
sudo steamos-readonly disable
sudo cp -a /usr/local/bin/rigsignal-ebpf /usr/local/bin/rigsignal-ebpf.bak-$(date +%Y%m%d-%H%M)
sudo cp -a /usr/local/lib/rigsignal/rigsignal-ebpf-probes /usr/local/lib/rigsignal/rigsignal-ebpf-probes.bak-$(date +%Y%m%d-%H%M)
sudo install -m 755 rigsignal-ebpf /usr/local/bin/rigsignal-ebpf
sudo install -m 644 rigsignal-ebpf-probes /usr/local/lib/rigsignal/rigsignal-ebpf-probes
sudo steamos-readonly enable
sudo systemctl restart rigsignal-ebpf
sudo journalctl -u rigsignal-ebpf --since -2min --no-pager | grep -Ei 'gpu_sched|probes|variant'
```

## Live acceptance (A9.2-R — orchestrator-coordinated after install)

1. Journal shows `variant=legacy key_field=id key_offset=32` + 9/9 probes (both boxes).
2. Orchestrator launches a game remotely on GamingPC; `gpu_sched` docs appear in
   `metrics-rigsignal.ebpf-default`.
3. Reference comparison (GamingPC, mid-game, USER-run):
   `sudo /tmp/rigsignal-gpusched/gpu-sched-ftrace-reference.sh 60 /tmp/gpu-sched-ref.txt`
   — orchestrator fetches the capture, runs `gpu-sched-reference-parse.py`, and compares
   count + latency distribution against the daemon's docs for the same window within
   tolerances. "9/9 loaded" alone is NOT validity.

Keep backups until acceptance passes; roll back both pair files together if needed.
