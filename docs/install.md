# RigSignal — Installation Guide

## Quick start

```bash
# Latest channel (mutable; resolves the current release payload)
curl -sSfL https://mathewrj.github.io/RigSignal-Integration/install.sh | sh

# Explicitly opt in to the privileged eBPF daemon on the latest channel
curl -sSfL https://mathewrj.github.io/RigSignal-Integration/install.sh | sh -s -- --with-ebpf

# Reproducible release (pins both the installer and payload)
VERSION=<release-version>
curl -sSfL "https://github.com/MathewRJ/RigSignal/releases/download/v${VERSION}/install.sh" | sh -s -- --version "${VERSION}"

# Arch Linux / CachyOS / Manjaro (AUR, builds from source incl. eBPF)
yay -S rigsignal-git
```

Windows: download the `.msi` from the [GitHub Releases page](https://github.com/MathewRJ/RigSignal/releases)
and run it. There is no `winget` package at this time.

Then:

```bash
# Prompts for your Elasticsearch endpoint + API key
rigsignal setup

# Add to Steam launch options for any game:
rigsignal run %command%
```

Data starts flowing to Elasticsearch the next time you launch a game.

The default install is agent-only (`--no-ebpf`) and does not request `sudo`.
To enable kernel telemetry later, rerun the installer with `--with-ebpf`.

## Install the released asset bundle

The launcher installs dashboards, templates, pipelines, transforms, and related
Kibana assets separately from telemetry setup. It downloads the bundle matching
the installed RigSignal release, verifies its adjacent SHA-256 sidecar, and then
has the packaged engine verify the bundle manifest:

```bash
# Latest installed release, prompting for the administrator's native user login
rigsignal assets install

# A pinned installed release is selected automatically from its agent build stamp.
# Supply values explicitly for automation; no prompt is made with --non-interactive.
rigsignal assets install --endpoint https://es.example.invalid \
  --ca-file ./http_ca.crt --ca-sha256 "$(sha256sum ./http_ca.crt | awk '{print $1}')" \
  --kibana-endpoint https://kibana.example.invalid \
  --admin-credentials-file ./elastic-admin.toml --non-interactive
```

The administrator input is one-shot and is never added to RigSignal
configuration, state, logs, or the journal. It must be a native Elasticsearch
user credential—API-key TOML is deliberately refused:

```toml
[elasticsearch]
username = "elastic"
password = "…"
```

The command creates a protected temporary copy for the engine's two reads and
removes it on success, failure, or interruption. The resulting assets use the
`user`/default ownership profile. `--ownership-profile fleet-coexist` is not an
assets shortcut: use the full packaged engine flow for that transaction.

Endpoint, CA, and Kibana endpoint resolution is explicit flag, then valid
persisted launcher configuration, then prompt. `--non-interactive` turns any
missing value into an actionable failure. A CA pin is valid only with an
explicit CA file. A resolved Kibana endpoint is atomically persisted and any
installed eBPF system configuration is synchronized; the administrator password
is not persisted. A pre-mutation local refusal (exit 2) is safe to correct and
rerun. Successful same-bundle reruns are marker-driven. 0.3.3 adds
partial-apply recovery: after an exit 4 (remote state may be partial),
follow `docs/RECOVERY.md` — preserve the installer output and transaction
record, inspect the named remote object and coordinate with its owner,
restore manually where required, then rerun the same command. `--repair`
reconciles only a proven RigSignal-owned Elasticsearch object; it cannot
rewrite a present divergent Kibana saved object, space, or role — delete it
in Kibana, then rerun. Use `--repair`, `--upgrade`, or `--allow-downgrade`
only for their corresponding explicit transitions.

`rigsignal-git` intentionally has no guessed GitHub release mapping. Use an
offline bundle and its exact adjacent sidecar instead:

```bash
rigsignal assets install --bundle ./rigsignal-assets-<version>.tar.gz \
  --admin-credentials-file ./elastic-admin.toml
```

Offline mode copies both the archive and `.sha256` file into a private snapshot
before hashing, so changing the source path after launch cannot affect the
engine input.

### Manual Linux download with checksum verification

Download the tarball and its adjacent checksum file from the same release, then
verify before unpacking:

```bash
VERSION=0.3.4
ARCH=x86_64
BASE="https://github.com/MathewRJ/RigSignal/releases/download/v${VERSION}"
curl -fLO "$BASE/rigsignal-${VERSION}-linux-${ARCH}.tar.gz"
curl -fLO "$BASE/rigsignal-${VERSION}-linux-${ARCH}.tar.gz.sha256"
sha256sum -c "rigsignal-${VERSION}-linux-${ARCH}.tar.gz.sha256"
tar -xzf "rigsignal-${VERSION}-linux-${ARCH}.tar.gz"
```

The one-line installer performs the same checksum verification before unpacking.

---

## Choosing a backend

| | Elastic Cloud | Self-hosted (local) |
|---|---|---|
| Setup effort | Minutes | 15–30 min |
| Cost | Free trial / free tier | Free (open source) |
| Kibana | Included | Install separately (same version) |
| Works offline | No (requires internet) | Yes |
| Recommended for | Getting started fast | Privacy, air-gapped, no cloud |

Both options are free. Elastic Cloud is the quickest path. Self-hosted gives you full control and works with no internet connection after setup.

---

## Elastic Cloud setup

### 1. Create a deployment

Sign up at [cloud.elastic.co](https://cloud.elastic.co/). The free tier (8 GB) is sufficient for months of personal gaming data. Create a deployment in any region.

### 2. Get an API key

In Kibana → Stack Management → API Keys, create a key with:
- Index privileges: `create_index`, `create`, `write`, `view_index_metadata` on `metrics-rigsignal.*` **and** `logs-rigsignal.*` (the agent ships both metrics and logs data streams)
- Cluster privileges: `monitor`

Note:
- Your **Elasticsearch endpoint** (e.g. `https://your-project.es.us-central1.gcp.elastic.cloud`)
- The **API key** (base64 encoded, shown once at creation)

For a personal deployment, `all` cluster + index privileges is simpler and fine.

### 3. Run `rigsignal setup`

```bash
rigsignal setup
```

This prompts for your ES endpoint and API key, verifies connectivity, and writes `${XDG_CONFIG_HOME:-~/.config}/rigsignal/rigsignal.toml` (mode 600). Import the Kibana dashboards separately — see "Import the dashboards" below.

---

## Self-hosted Elasticsearch

Download and install guide: [elastic.co/downloads/elasticsearch](https://www.elastic.co/downloads/elasticsearch) / [Installing Elasticsearch](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/installing-elasticsearch)

Requirements: Elasticsearch **9.4.3 – 9.4.4** with Kibana at the same version, and at least 2 GB RAM. This is the *tested supported range* (G4 gate, 2026-07-22): fresh install, asset upgrade, and in-place stack upgrade are all verified at both endpoints by the repeatable clean-stack matrix (`scripts/clean-stack/matrix.sh`, see `docs/QA-MATRIX.md`). Older 8.x/9.x versions may work (TSDS needs 8.10+) but are NOT tested or supported; the range widens only when the matrix passes at a new endpoint.

### 1. Start Elasticsearch

**tar.gz (Linux):**

```bash
tar -xzf elasticsearch-*.tar.gz
cd elasticsearch-*/
./bin/elasticsearch
```

**Docker:**

```bash
docker run -d --name elasticsearch \
  -p 9200:9200 -p 9300:9300 \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=true" \
  -e "ELASTIC_PASSWORD=changeme" \
  docker.elastic.co/elasticsearch/elasticsearch:8.18.0
```

On first start, Elasticsearch generates a `elastic` superuser password and a Kibana enrollment token — save both. It listens on `https://localhost:9200` (TLS is enabled by default since ES 8.0).

### 2. Create a RigSignal API key

```bash
curl -u elastic:<your-password> \
  -X POST "https://localhost:9200/_security/api_key" \
  -H "Content-Type: application/json" \
  --cacert elasticsearch-*/config/certs/http_ca.crt \
  -d '{
    "name": "rigsignal",
    "role_descriptors": {
      "rigsignal_writer": {
        "cluster": ["monitor"],
        "indices": [{
          "names": ["metrics-rigsignal.*", "logs-rigsignal.*"],
          "privileges": ["create_index", "create", "write", "view_index_metadata"]
        }]
      }
    }
  }'
```

The response includes an `encoded` field — that base64 string is your API key.

### 3. Run `rigsignal setup`

```bash
rigsignal setup
# Elasticsearch endpoint: https://localhost:9200
# API key: <the encoded value from above>
```

For a locally-issued Elasticsearch certificate, provide its CA PEM bundle during
setup. The optional digest authenticates the exact bytes before setup makes a
network request:

```bash
rigsignal setup --ca-file ./http_ca.crt
rigsignal setup --ca-file ./http_ca.crt --ca-sha256 "$(sha256sum ./http_ca.crt | awk '{print $1}')"
```

Setup structurally validates a non-empty, strict PEM certificate bundle:
certificate blocks may be concatenated with whitespace, but any other leading,
embedded, or trailing data is refused before a network request. X.509 validity
and trust authority are deferred to the Elasticsearch TLS handshake and agent
preflight; hostname verification remains enabled. It stores the accepted bytes at
`${XDG_CONFIG_HOME:-$HOME/.config}/rigsignal/certs/elasticsearch-ca.pem` and records that durable path
as `elasticsearch.ca_cert` in `${XDG_CONFIG_HOME:-$HOME/.config}/rigsignal/rigsignal.toml`. Replace a
CA by rerunning `rigsignal setup --ca-file <path>` (and an optional pin).

When the optional eBPF daemon is installed, setup installs the accepted CA
snapshot at `/etc/rigsignal/certs/elasticsearch-ca.pem` and rewrites only the
system TOML `ca_cert` path to that `/etc` location. For ordinary installation,
durability, and restart failures it restores the staged `/etc` files before
rolling back the matching user files. If it cannot prove the `/etc` restoration,
it leaves the user transaction in place and reports the failure rather than
claiming a cross-scope rollback; rerun setup after correcting the system error.
It never leaves a home-relative certificate path for the root daemon.

### 4. Install Kibana (for dashboards)

Download Kibana from [elastic.co/downloads/kibana](https://www.elastic.co/downloads/kibana) — must be the same version as Elasticsearch.

```bash
tar -xzf kibana-*.tar.gz
cd kibana-*/
./bin/kibana
# When prompted, paste the enrollment token printed by Elasticsearch on first start
```

Kibana listens on `http://localhost:5601` by default. Log in as `elastic` with the password from step 1.

### 5. Import the dashboards

There is no published Fleet integration package for RigSignal yet — the
elastic/integrations submission ([PR #18878](https://github.com/elastic/integrations/pull/18878))
is deferred pending a maintainer architecture rework, and this repository does
not currently build a `fields.yml`/Fleet package. The agent writes directly to
`metrics-rigsignal.*` / `logs-rigsignal.*` data streams (see `rigsignal setup`
above) without needing Fleet.

To get the Kibana dashboards, import the NDJSON files under
[`dashboards/`](../dashboards/) via Kibana → Stack Management → Saved Objects →
Import. See [`docs/dashboards.md`](dashboards.md) for the current dashboard list.

---

## Linux distro packages

All distro packages below are **agent-only** — no eBPF daemon. If you want eBPF
with a distro package, also run the one-line installer (above), or use AUR
(which builds eBPF from source).

### Arch Linux / CachyOS / Manjaro

Pre-built package from the release (agent only):

```bash
sudo pacman -U rigsignal-0.3.4-1-x86_64.pkg.tar.zst
```

Or AUR, which builds from source and includes eBPF:

```bash
yay -S rigsignal-git
```

Or manually from AUR:

```bash
git clone https://aur.archlinux.org/rigsignal-git.git
cd rigsignal-git
makepkg -si
```

Both package paths install the spool-retention timer as a global systemd user
unit. Enable it once after installing:

```bash
sudo systemctl --global enable rigsignal-spool-retention.timer
```

When upgrading from the legacy package, its post-upgrade hook hashes the former
`/etc/rigsignal/rigsignal.toml` example. It removes only known pristine
examples; a modified file (including a pacman's `.pacsave`) is left in place
with an announcement for the operator. It never copies credentials into a user
config—run `rigsignal setup` for that.

### Debian / Ubuntu 24.04+ (.deb)

Download the latest `.deb` from the [GitHub releases page](https://github.com/MathewRJ/RigSignal/releases):

```bash
sudo dpkg -i rigsignal_0.3.4-1_amd64.deb
```

The package installs `rigsignal-agent` and `rigsignal` (launcher) to `/usr/bin/`, the systemd user unit, and an example config to `/usr/share/rigsignal/examples/rigsignal.toml.example`. Run `rigsignal setup` to create the credential-bearing config in `${XDG_CONFIG_HOME:-~/.config}/rigsignal/rigsignal.toml`.

### Fedora / RHEL / openSUSE (.rpm)

Download the latest `.rpm` from the [GitHub releases page](https://github.com/MathewRJ/RigSignal/releases):

```bash
sudo rpm -i rigsignal-0.3.4-1.x86_64.rpm
```

### Building from source

Requires Rust 1.77+ and the Aya eBPF toolchain:

```bash
git clone https://github.com/MathewRJ/RigSignal.git
cd RigSignal/src
cargo build --release
sudo cp target/release/rigsignal-agent /usr/local/bin/rigsignal-agent
# Install the launcher wrapper as 'rigsignal'
sudo install -Dm755 ../packaging/rigsignal-launcher.sh /usr/local/bin/rigsignal
```

For the eBPF daemon (requires kernel 5.8+ and `CAP_BPF`):

```bash
cd RigSignal/ebpf
RUSTFLAGS="" cargo xtask build-ebpf --release
RUSTFLAGS="" cargo build --release
sudo cp target/release/rigsignal-ebpf /usr/local/bin/
sudo install -m 644 target/bpfel-unknown-none/release/rigsignal-ebpf-probes \
  /usr/lib/rigsignal/rigsignal-ebpf-probes
```

---

## Windows installer

Windows is agent-only: assets are installed from a Linux administrator host.

Download `rigsignal-0.3.4-x86_64.msi` from the
[GitHub Releases page](https://github.com/MathewRJ/RigSignal/releases) and run it,
or install silently from an admin PowerShell:

```powershell
msiexec /i rigsignal-0.3.4-x86_64.msi /qb!
```

This installs `rigsignal-agent.exe` to `C:\Program Files\RigSignal\bin\` (added
to the system PATH), an example config at
`C:\Program Files\RigSignal\config\rigsignal.toml.example`, and three starter
game profiles. The installer also bundles Intel GameTechDev PresentMon v2.4.1
(MIT license) for Windows frame timing — set `RIGSIGNAL_PRESENTMON` to a
different path to override it.

There is no Windows service or `rigsignal` launcher CLI in this release —
Windows has no systemd analog. Run `rigsignal-agent.exe` directly from a
terminal (foreground), or wrap it in a Steam launch option. eBPF is not
available on Windows; all other metric streams are supported, with some gaps
documented in [`RELEASE_NOTES.md`](../.github/RELEASE_NOTES.md#windows-caveats).

To uninstall: `msiexec /x rigsignal-0.3.4-x86_64.msi /qb!` or *Settings → Apps
→ Installed apps → RigSignal → Uninstall*.

---

## MangoHud setup (optional — needed for frame timing data)

The agent ships all 8 metric streams regardless. MangoHud is only needed to populate
`rigsignal.frame` (FPS, frame time, 1%/0.1% lows, stutter). All other streams (CPU, GPU,
memory, storage, network, audio, power) work without it.

To enable frame data, add to Steam launch options:

```
MANGOHUD=1 MANGOHUD_LOG=1 rigsignal run %command%
```

Or set globally in `~/.config/MangoHud/MangoHud.conf`:

```ini
log_duration=0
output_folder=/tmp/MangoHud
```

See [`docs/steam-setup.md`](steam-setup.md) for detailed Steam integration instructions.

---

## systemd service (always-on mode)

For continuous collection even outside Steam:

```bash
# User-level agent (no root, no eBPF)
systemctl --user enable --now rigsignal-agent

# System-level eBPF daemon (requires sudo, runs as root with CAP_BPF)
sudo systemctl enable --now rigsignal-ebpf
```

The AUR package installs these units. The one-line installer installs only the
user agent by default; pass `--with-ebpf` to explicitly install and enable the
privileged eBPF service. For manual installs, copy the unit files from the
release tarball or `packaging/systemd/`.

> A tarball-installed `~/.config/systemd/user/rigsignal-agent.service` shadows
> the packaged unit in `/usr/lib/systemd/user/`. Remove the leftover user unit
> after switching to a distro package, then run `systemctl --user daemon-reload`.

> Likewise, remove any tarball-installed
> `~/.config/systemd/user/rigsignal-spool-retention.*` units after switching to
> a distro package. They shadow the packaged global retention units and may
> still invoke an old helper from `~/.local/bin/`. Then run
> `systemctl --user daemon-reload`.

> **Dev installs (build from source):** The unit's `ExecStart` defaults to `/usr/bin/rigsignal-agent`, but a source build installs to `/usr/local/bin/`. Create a drop-in to override:
> ```bash
> mkdir -p ~/.config/systemd/user/rigsignal-agent.service.d
> cat > ~/.config/systemd/user/rigsignal-agent.service.d/override.conf <<'EOF'
> [Service]
> ExecStart=
> ExecStart=/usr/local/bin/rigsignal-agent
> EOF
> systemctl --user daemon-reload
> ```

---

## Configuration

Config is read from (in priority order):
1. `--config PATH` CLI flag
2. `${XDG_CONFIG_HOME:-~/.config}/rigsignal/rigsignal.toml`
3. `/etc/rigsignal/rigsignal.toml`

`rigsignal setup` writes `${XDG_CONFIG_HOME:-~/.config}/rigsignal/rigsignal.toml` automatically. See `docs/configuration.md` for the full reference.

---

## Uninstalling Linux user installs

The one-line installer also installs `rigsignal-uninstall` alongside the
launcher. It stops and disables the user unit, removes RigSignal binaries and
unit files, and leaves configuration in place by default:

```bash
rigsignal-uninstall
```

Use `rigsignal-uninstall --purge` to also remove RigSignal configuration, or
`--user-only` to leave any privileged eBPF files in place. Elasticsearch data is
never removed. `DESTDIR` and `RIGSIGNAL_INSTALL_LOCAL_DIR` are test-only
installer overrides used by the repository's root-free packaging tests.

---

## Minimum API key permissions

The API key you provide needs:
- Cluster privileges: `monitor`
- Index privileges: `create_index`, `create`, `write`, `view_index_metadata` on `metrics-rigsignal.*` **and** `logs-rigsignal.*` (the agent ships both metrics and logs data streams)

For a personal deployment, `all` cluster + index privileges is simpler and fine.

---

## Verifying your setup

After configuring, run the diagnostics subcommand before starting a game:

```bash
rigsignal-agent diagnose
```

This outputs kernel version, GPU info, Elasticsearch reachability (with the API key redacted),
and the resolved config path. Use `--output report.txt` to save it for bug reports.

On Linux, if you use Gamescope, you can also check for display mode-override
problems directly:

```bash
rigsignal-agent diagnose display
```

See [`docs/diagnose-display.md`](diagnose-display.md) for the verdict/evidence/exit-code contract.

---

## Contributor tooling

The Elastic Agent Builder MCP server lets Claude Code and claude.ai query Elasticsearch directly during dashboard builds and field validation. This is optional developer convenience — it is not required to run RigSignal.

Setup instructions and the API key creation recipe are in `.agents/skills/elastic-mcp-setup/SKILL.md`. The template config is in `.mcp.json.example` at the repo root; copy it to `.mcp.json` (gitignored) and set `RIGSIGNAL_MCP_API_KEY` before restarting Claude Code.
