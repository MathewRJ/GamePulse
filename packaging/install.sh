#!/bin/sh
# RigSignal user-mode installer
#
# Installs to ~/.local/bin/ — no root required, survives SteamOS / immutable-OS updates.
#
# Usage:
#   # Latest channel (mutable; resolves the current release payload):
#   curl -sSfL https://mathewrj.github.io/RigSignal-Integration/install.sh | sh
#   # Reproducible release (pins both this script and its payload):
#   VERSION=<release-version>
#   curl -sSfL "https://github.com/MathewRJ/RigSignal/releases/download/v${VERSION}/install.sh" | sh -s -- --version "${VERSION}"
#
# After install:
#   rigsignal setup    # configure Elasticsearch endpoint + API key
#   rigsignal start    # start the agent
#
# eBPF is disabled by default. To explicitly install its privileged daemon, add
# --with-ebpf. RIGSIGNAL_INSTALL_LOCAL_DIR and DESTDIR are test-only overrides.

set -e

REPO="MathewRJ/RigSignal"
INSTALL_BIN="${HOME}/.local/bin"
INSTALL_SERVICE="${HOME}/.config/systemd/user"
GITHUB_API="https://api.github.com/repos/${REPO}"
GITHUB_RELEASES="https://github.com/${REPO}/releases/download"
DESTDIR="${DESTDIR:-}"
RIGSIGNAL_INSTALL_LOCAL_DIR="${RIGSIGNAL_INSTALL_LOCAL_DIR:-}"

# ── Argument parsing ─────────────────────────────────────────────────────────

VERSION=""
NO_EBPF=1
i=0
for arg in "$@"; do
    i=$((i + 1))
    case "$arg" in
        --version=*) VERSION="${arg#--version=}" ;;
        --version)
            eval "VERSION=\${$(( i + 1 ))}" 2>/dev/null || true
            ;;
        --no-ebpf) NO_EBPF=1 ;;
        --with-ebpf) NO_EBPF=0 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

info()  { printf '  [info] %s\n' "$*"; }
ok()    { printf '    [ok] %s\n' "$*"; }
err()   { printf '   [err] %s\n' "$*" >&2; exit 1; }

stage_path() {
    printf '%s%s' "$DESTDIR" "$1"
}

download() {
    url="$1"; dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -sSfL "$url" -o "$dest"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dest" "$url"
    else
        err "Neither curl nor wget found. Install one and retry."
    fi
}

# ── Architecture detection ────────────────────────────────────────────────────

ARCH=$(uname -m)
case "$ARCH" in
    x86_64|amd64)   ARCH="x86_64" ;;
    *) err "Unsupported architecture: $ARCH. RigSignal release builds support Linux x86_64 only." ;;
esac

OS=$(uname -s)
case "$OS" in
    Linux) ;;
    *) err "This installer is for Linux only. For Windows, download the .msi from GitHub Releases." ;;
esac

# The installed launcher and uninstaller require Python's standard library.
# Check it before release lookup, download, or any installation side effects.
command -v python3 >/dev/null 2>&1 \
    || err "Python 3 is required. Install python3 and retry."
python3 -c 'import sys, ssl, tomllib' >/dev/null 2>&1 \
    || err "A usable Python 3 standard library (including ssl and tomllib) is required. Install or repair python3 and retry."

# ── Resolve version ───────────────────────────────────────────────────────────

if [ -z "$VERSION" ]; then
    info "Fetching latest release version..."
    VERSION=$(download "${GITHUB_API}/releases/latest" - \
        | grep '"tag_name"' \
        | sed 's/.*"tag_name":[[:space:]]*"v\([^"]*\)".*/\1/')
    [ -n "$VERSION" ] || err "Could not determine latest release. Check https://github.com/${REPO}/releases"
fi

info "Installing RigSignal v${VERSION} (${ARCH})"

# ── SteamOS detection ─────────────────────────────────────────────────────────

IS_STEAMOS=0
if [ -f /etc/os-release ]; then
    # SteamOS has ID=steamos or ID_LIKE=arch with VARIANT_ID=steamdeck
    if grep -qiE '^ID=steamos|^VARIANT_ID=steamdeck' /etc/os-release 2>/dev/null; then
        IS_STEAMOS=1
        info "SteamOS detected — installing to ~/.local/bin (survives OS updates)"
    fi
fi

# ── Download ──────────────────────────────────────────────────────────────────

TARBALL="rigsignal-${VERSION}-linux-x86_64.tar.gz"
DOWNLOAD_URL="${GITHUB_RELEASES}/v${VERSION}/${TARBALL}"
CHECKSUM_FILE="${TARBALL}.sha256"
CHECKSUM_URL="${GITHUB_RELEASES}/v${VERSION}/${CHECKSUM_FILE}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

info "Downloading ${TARBALL}..."
if [ -n "$RIGSIGNAL_INSTALL_LOCAL_DIR" ]; then
    # Test-only, network-free fixture override. Do not use this in production.
    cp "${RIGSIGNAL_INSTALL_LOCAL_DIR%/}/${TARBALL}" "$TMP/$TARBALL" \
        || err "Could not copy test tarball from RIGSIGNAL_INSTALL_LOCAL_DIR."
    cp "${RIGSIGNAL_INSTALL_LOCAL_DIR%/}/${CHECKSUM_FILE}" "$TMP/$CHECKSUM_FILE" \
        || err "Could not copy test checksum from RIGSIGNAL_INSTALL_LOCAL_DIR."
else
    download "$DOWNLOAD_URL" "$TMP/$TARBALL"
    download "$CHECKSUM_URL" "$TMP/$CHECKSUM_FILE"
fi

command -v sha256sum >/dev/null 2>&1 || err "sha256sum is required to verify the release tarball."
info "Verifying ${TARBALL} checksum..."
EXPECTED_DIGEST=$(python3 - "$TMP/$CHECKSUM_FILE" "$TARBALL" <<'PY'
import pathlib
import re
import sys

sidecar = pathlib.Path(sys.argv[1]).read_bytes()
basename = sys.argv[2].encode("ascii")
record = re.compile(rb"([0-9a-f]{64}) (?: |\*)" + re.escape(basename) + rb"\n")
match = record.fullmatch(sidecar)
if match is None:
    raise SystemExit(1)
print(match.group(1).decode("ascii"))
PY
) || err "Checksum sidecar must contain exactly one lowercase SHA-256 record for ${TARBALL}; refusing to unpack it."
ACTUAL_DIGEST=$(sha256sum "$TMP/$TARBALL") \
    || err "Could not calculate checksum for ${TARBALL}; refusing to unpack it."
ACTUAL_DIGEST=${ACTUAL_DIGEST%% *}
[ "$ACTUAL_DIGEST" = "$EXPECTED_DIGEST" ] \
    || err "Checksum verification failed for ${TARBALL}; refusing to unpack it."
tar -xzf "$TMP/$TARBALL" -C "$TMP" --strip-components=1

# ── Install binaries ──────────────────────────────────────────────────────────

INSTALLED=""
SKIPPED=""

add_installed() { INSTALLED="${INSTALLED}  + $1\n"; }
add_skipped()   { SKIPPED="${SKIPPED}  - $1\n"; }

INSTALL_BIN_PATH=$(stage_path "$INSTALL_BIN")
INSTALL_SERVICE_PATH=$(stage_path "$INSTALL_SERVICE")

mkdir -p "$INSTALL_BIN_PATH"
install -m 755 "$TMP/rigsignal-agent"  "$INSTALL_BIN_PATH/rigsignal-agent"
install -m 755 "$TMP/rigsignal"        "$INSTALL_BIN_PATH/rigsignal"
install -m 755 "$TMP/rigsignal-uninstall" "$INSTALL_BIN_PATH/rigsignal-uninstall"
add_installed "$INSTALL_BIN/rigsignal-agent  (collector)"
add_installed "$INSTALL_BIN/rigsignal  (launcher CLI)"
add_installed "$INSTALL_BIN/rigsignal-uninstall  (uninstaller)"

# ── Install user systemd service ──────────────────────────────────────────────

if [ -n "$DESTDIR" ]; then
    mkdir -p "$INSTALL_SERVICE_PATH"
    install -m 644 "$TMP/rigsignal-agent.service" "$INSTALL_SERVICE_PATH/rigsignal-agent.service"
    add_installed "$INSTALL_SERVICE/rigsignal-agent.service  (user systemd service; staged)"
elif command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$INSTALL_SERVICE_PATH"
    install -m 644 "$TMP/rigsignal-agent.service" "$INSTALL_SERVICE_PATH/rigsignal-agent.service"
    systemctl --user daemon-reload 2>/dev/null || true
    add_installed "$INSTALL_SERVICE/rigsignal-agent.service  (user systemd service)"
else
    info "systemctl not found — skipping service install (non-systemd system)"
    add_skipped "systemd user service (systemctl not found)"
fi

# ── Install eBPF daemon (optional, requires sudo) ─────────────────────────────
# The eBPF daemon captures kernel-level scheduler, I/O, and GPU fence events.
# It is only present in the tarball when built with nightly Rust + bpf-linker.
# If absent, the agent runs without kernel-level telemetry — all other streams
# (CPU, GPU, memory, frame timing, etc.) are unaffected.

EBPF_BIN="$TMP/rigsignal-ebpf"
EBPF_PROBES="$TMP/rigsignal-ebpf-probes"

if [ "$NO_EBPF" = "1" ]; then
    info "Skipping eBPF daemon by default (opt in with --with-ebpf)"
    add_skipped "rigsignal-ebpf (opt in with --with-ebpf)"
elif [ -f "$EBPF_BIN" ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        info "eBPF daemon found but sudo not available — skipping system install."
        info "To install manually: sudo cp $EBPF_BIN /usr/local/bin/rigsignal-ebpf"
        add_skipped "rigsignal-ebpf (sudo not available)"
    else
        info "Installing eBPF daemon (kernel tracing — requires sudo)..."

        # On SteamOS the root filesystem is read-only; disable it briefly.
        _steamos_ro=0
        if [ "$IS_STEAMOS" = "1" ] && command -v steamos-readonly >/dev/null 2>&1; then
            sudo steamos-readonly disable 2>/dev/null && _steamos_ro=1
        fi

        if sudo install -m 755 "$EBPF_BIN" /usr/local/bin/rigsignal-ebpf 2>/dev/null; then
            add_installed "/usr/local/bin/rigsignal-ebpf  (eBPF kernel daemon)"

            if [ -f "$EBPF_PROBES" ]; then
                sudo mkdir -p /usr/local/lib/rigsignal
                sudo install -m 644 "$EBPF_PROBES" /usr/local/lib/rigsignal/rigsignal-ebpf-probes
                add_installed "/usr/local/lib/rigsignal/rigsignal-ebpf-probes  (eBPF probe bytecode)"
            fi

            if [ -f "$TMP/rigsignal-ebpf.service" ] && command -v systemctl >/dev/null 2>&1; then
                sudo install -m 644 "$TMP/rigsignal-ebpf.service" /etc/systemd/system/rigsignal-ebpf.service
                sudo systemctl daemon-reload 2>/dev/null || true
                add_installed "/etc/systemd/system/rigsignal-ebpf.service  (system service)"
            fi

            # Write system config so the eBPF daemon can connect to ES immediately.
            # If the user already ran 'rigsignal setup', copy their credentials.
            # Otherwise write a placeholder; 'rigsignal setup' will update it.
            sudo mkdir -p /etc/rigsignal
            _user_cfg="${XDG_CONFIG_HOME:-$HOME/.config}/rigsignal/rigsignal.toml"
            if [ -f "$_user_cfg" ]; then
                sudo install -m 600 "$_user_cfg" /etc/rigsignal/rigsignal.toml
                add_installed "/etc/rigsignal/rigsignal.toml  (eBPF daemon config — copied from user config)"
            else
                sudo install -m 600 "$TMP/rigsignal.toml" /etc/rigsignal/rigsignal.toml 2>/dev/null || \
                    printf '[elasticsearch]\nendpoint = ""\n' | sudo tee /etc/rigsignal/rigsignal.toml >/dev/null
                add_installed "/etc/rigsignal/rigsignal.toml  (eBPF daemon config — run 'rigsignal setup' to fill in credentials)"
            fi

            # Enable and start the eBPF service so kernel tracing is active immediately.
            if command -v systemctl >/dev/null 2>&1; then
                sudo systemctl enable --now rigsignal-ebpf 2>/dev/null || true
                add_installed "rigsignal-ebpf.service  (enabled + started)"
            fi
        else
            info "sudo install failed — eBPF skipped. Re-run with sudo access to enable kernel tracing."
            add_skipped "rigsignal-ebpf (sudo install failed)"
        fi

        [ "$_steamos_ro" = "1" ] && sudo steamos-readonly enable 2>/dev/null || true
    fi
else
    info "eBPF daemon not included in this release — kernel tracing unavailable."
    info "Agent-only mode: FPS, CPU, GPU, memory, frame timing streams are unaffected."
    add_skipped "rigsignal-ebpf (not included in this release)"
fi

# ── MangoHud config (frame timing CSV) ───────────────────────────────────────
# Ensure MangoHud writes frame timing CSVs so the agent can read them.
# We write only the two keys we need; if the file already exists we append
# missing keys rather than overwriting user customisations.
_mh_conf_dir="${XDG_CONFIG_HOME:-$HOME/.config}/MangoHud"
_mh_conf="$_mh_conf_dir/MangoHud.conf"
_mh_conf_path=$(stage_path "$_mh_conf")
_mh_conf_dir_path=$(stage_path "$_mh_conf_dir")
mkdir -p "$_mh_conf_dir_path"
if ! grep -q "output_folder" "$_mh_conf_path" 2>/dev/null; then
    printf 'output_folder=%s\n' "$HOME/.local/share/MangoHud" >> "$_mh_conf_path"
fi
if ! grep -q "autostart_log" "$_mh_conf_path" 2>/dev/null; then
    printf 'autostart_log=1\n' >> "$_mh_conf_path"
fi
add_installed "$_mh_conf  (MangoHud frame logging)"

# ── PATH setup ────────────────────────────────────────────────────────────────

# Check if ~/.local/bin is already in PATH
if [ -z "$DESTDIR" ]; then
case ":${PATH}:" in
    *":${INSTALL_BIN}:"*) ;;
    *)
        info "Adding $INSTALL_BIN to PATH..."
        # Fish shell (SteamOS default in Desktop Mode)
        FISH_CONFIG="${HOME}/.config/fish/config.fish"
        if [ -d "${HOME}/.config/fish" ] || command -v fish >/dev/null 2>&1; then
            if ! grep -qF "fish_add_path.*\.local/bin\|set.*PATH.*\.local/bin" "$FISH_CONFIG" 2>/dev/null; then
                mkdir -p "$(dirname "$FISH_CONFIG")"
                printf '\nfish_add_path "%s"\n' "$INSTALL_BIN" >> "$FISH_CONFIG"
                ok "Added to fish PATH ($FISH_CONFIG)"
            fi
        fi
        # Bash / sh fallback
        BASH_PROFILE="${HOME}/.bash_profile"
        if [ -f "$BASH_PROFILE" ] || [ -f "${HOME}/.bashrc" ]; then
            TARGET="${HOME}/.bashrc"
            [ -f "$BASH_PROFILE" ] && TARGET="$BASH_PROFILE"
            if ! grep -qF ".local/bin" "$TARGET" 2>/dev/null; then
                printf '\nexport PATH="%s:$PATH"\n' "$INSTALL_BIN" >> "$TARGET"
                ok "Added to bash PATH ($TARGET)"
            fi
        fi
        ;;
esac
fi

# ── Summary ───────────────────────────────────────────────────────────────────

printf '\n'
printf '  RigSignal v%s installed.\n' "$VERSION"
printf '\n'
printf '  Installed:\n'
printf '%b' "$INSTALLED"
if [ -n "$SKIPPED" ]; then
    printf '\n'
    printf '  Not installed:\n'
    printf '%b' "$SKIPPED"
fi
printf '\n'
printf '  Next steps:\n'
if [ "$IS_STEAMOS" = "1" ]; then
    printf '    1. If rigsignal is not found, open a new terminal or run:\n'
    printf '         export PATH="%s:$PATH"\n' "$INSTALL_BIN"
    printf '    2. Run setup:\n'
else
    printf '    1. Run setup:\n'
fi
printf '         rigsignal setup\n'
printf '    2. Add to Steam launch options:\n'
printf '         rigsignal run %%command%%\n'
printf '    3. To uninstall this user install:\n'
printf '         rigsignal-uninstall\n'
if printf '%b' "$INSTALLED" | grep -q "rigsignal-ebpf.service  (enabled"; then
    printf '    4. eBPF kernel telemetry is active. To check status:\n'
    printf '         sudo systemctl status rigsignal-ebpf\n'
elif printf '%b' "$INSTALLED" | grep -q "rigsignal-ebpf"; then
    printf '    4. To enable eBPF kernel telemetry, reinstall with --with-ebpf.\n'
fi
printf '\n'
