#!/bin/sh
# GamePulse user-mode installer
#
# Installs to ~/.local/bin/ — no root required, survives SteamOS / immutable-OS updates.
#
# Usage:
#   curl -sSfL https://mathewrj.github.io/GamePulse-Integration/install.sh | sh
#   curl -sSfL https://mathewrj.github.io/GamePulse-Integration/install.sh | sh -s -- --version 0.2.0
#
# After install:
#   gamepulse setup    # configure Elasticsearch endpoint + API key
#   gamepulse start    # start the agent

set -e

REPO="MathewRJ/GamePulse"
INSTALL_BIN="${HOME}/.local/bin"
INSTALL_SERVICE="${HOME}/.config/systemd/user"
GITHUB_API="https://api.github.com/repos/${REPO}"
GITHUB_RELEASES="https://github.com/${REPO}/releases/download"

# ── Argument parsing ─────────────────────────────────────────────────────────

VERSION=""
NO_EBPF=0
i=0
for arg in "$@"; do
    i=$((i + 1))
    case "$arg" in
        --version=*) VERSION="${arg#--version=}" ;;
        --version)
            eval "VERSION=\${$(( i + 1 ))}" 2>/dev/null || true
            ;;
        --no-ebpf) NO_EBPF=1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

info()  { printf '  [info] %s\n' "$*"; }
ok()    { printf '    [ok] %s\n' "$*"; }
err()   { printf '   [err] %s\n' "$*" >&2; exit 1; }

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
    aarch64|arm64)  ARCH="aarch64" ;;
    *) err "Unsupported architecture: $ARCH. Only x86_64 and aarch64 are supported." ;;
esac

OS=$(uname -s)
case "$OS" in
    Linux) ;;
    *) err "This installer is for Linux only. For Windows, download the .msi from GitHub Releases." ;;
esac

# ── Resolve version ───────────────────────────────────────────────────────────

if [ -z "$VERSION" ]; then
    info "Fetching latest release version..."
    VERSION=$(download "${GITHUB_API}/releases/latest" - \
        | grep '"tag_name"' \
        | sed 's/.*"tag_name":[[:space:]]*"v\([^"]*\)".*/\1/')
    [ -n "$VERSION" ] || err "Could not determine latest release. Check https://github.com/${REPO}/releases"
fi

info "Installing GamePulse v${VERSION} (${ARCH})"

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

TARBALL="gamepulse-${VERSION}-linux-${ARCH}.tar.gz"
DOWNLOAD_URL="${GITHUB_RELEASES}/v${VERSION}/${TARBALL}"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

info "Downloading ${TARBALL}..."
download "$DOWNLOAD_URL" "$TMP/$TARBALL"
tar -xzf "$TMP/$TARBALL" -C "$TMP" --strip-components=1

# ── Install binaries ──────────────────────────────────────────────────────────

INSTALLED=""
SKIPPED=""

add_installed() { INSTALLED="${INSTALLED}  + $1\n"; }
add_skipped()   { SKIPPED="${SKIPPED}  - $1\n"; }

mkdir -p "$INSTALL_BIN"
install -m 755 "$TMP/gamepulse-agent"  "$INSTALL_BIN/gamepulse-agent"
install -m 755 "$TMP/gamepulse"        "$INSTALL_BIN/gamepulse"
add_installed "$INSTALL_BIN/gamepulse-agent  (collector)"
add_installed "$INSTALL_BIN/gamepulse  (launcher CLI)"

# ── Install user systemd service ──────────────────────────────────────────────

if command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$INSTALL_SERVICE"
    install -m 644 "$TMP/gamepulse-agent.service" "$INSTALL_SERVICE/gamepulse-agent.service"
    systemctl --user daemon-reload 2>/dev/null || true
    add_installed "$INSTALL_SERVICE/gamepulse-agent.service  (user systemd service)"
else
    info "systemctl not found — skipping service install (non-systemd system)"
    add_skipped "systemd user service (systemctl not found)"
fi

# ── Install eBPF daemon (optional, requires sudo) ─────────────────────────────
# The eBPF daemon captures kernel-level scheduler, I/O, and GPU fence events.
# It is only present in the tarball when built with nightly Rust + bpf-linker.
# If absent, the agent runs without kernel-level telemetry — all other streams
# (CPU, GPU, memory, frame timing, etc.) are unaffected.

EBPF_BIN="$TMP/gamepulse-ebpf"
EBPF_PROBES="$TMP/gamepulse-ebpf-probes"

if [ "$NO_EBPF" = "1" ]; then
    info "Skipping eBPF daemon (--no-ebpf)"
    add_skipped "gamepulse-ebpf (--no-ebpf flag)"
elif [ -f "$EBPF_BIN" ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        info "eBPF daemon found but sudo not available — skipping system install."
        info "To install manually: sudo cp $EBPF_BIN /usr/local/bin/gamepulse-ebpf"
        add_skipped "gamepulse-ebpf (sudo not available)"
    else
        info "Installing eBPF daemon (kernel tracing — requires sudo)..."

        # On SteamOS the root filesystem is read-only; disable it briefly.
        _steamos_ro=0
        if [ "$IS_STEAMOS" = "1" ] && command -v steamos-readonly >/dev/null 2>&1; then
            sudo steamos-readonly disable 2>/dev/null && _steamos_ro=1
        fi

        if sudo install -m 755 "$EBPF_BIN" /usr/local/bin/gamepulse-ebpf 2>/dev/null; then
            add_installed "/usr/local/bin/gamepulse-ebpf  (eBPF kernel daemon)"

            if [ -f "$EBPF_PROBES" ]; then
                sudo mkdir -p /usr/local/lib/gamepulse
                sudo install -m 644 "$EBPF_PROBES" /usr/local/lib/gamepulse/gamepulse-ebpf-probes
                add_installed "/usr/local/lib/gamepulse/gamepulse-ebpf-probes  (eBPF probe bytecode)"
            fi

            if [ -f "$TMP/gamepulse-ebpf.service" ] && command -v systemctl >/dev/null 2>&1; then
                sudo install -m 644 "$TMP/gamepulse-ebpf.service" /etc/systemd/system/gamepulse-ebpf.service
                sudo systemctl daemon-reload 2>/dev/null || true
                add_installed "/etc/systemd/system/gamepulse-ebpf.service  (system service)"
            fi

            # Write system config so the eBPF daemon can connect to ES immediately.
            # If the user already ran 'gamepulse setup', copy their credentials.
            # Otherwise write a placeholder; 'gamepulse setup' will update it.
            sudo mkdir -p /etc/gamepulse
            _user_cfg="${XDG_CONFIG_HOME:-$HOME/.config}/gamepulse/gamepulse.toml"
            if [ -f "$_user_cfg" ]; then
                sudo install -m 600 "$_user_cfg" /etc/gamepulse/gamepulse.toml
                add_installed "/etc/gamepulse/gamepulse.toml  (eBPF daemon config — copied from user config)"
            else
                sudo install -m 600 "$TMP/gamepulse.toml" /etc/gamepulse/gamepulse.toml 2>/dev/null || \
                    printf '[elasticsearch]\nendpoint = ""\n' | sudo tee /etc/gamepulse/gamepulse.toml >/dev/null
                add_installed "/etc/gamepulse/gamepulse.toml  (eBPF daemon config — run 'gamepulse setup' to fill in credentials)"
            fi

            # Enable and start the eBPF service so kernel tracing is active immediately.
            if command -v systemctl >/dev/null 2>&1; then
                sudo systemctl enable --now gamepulse-ebpf 2>/dev/null || true
                add_installed "gamepulse-ebpf.service  (enabled + started)"
            fi
        else
            info "sudo install failed — eBPF skipped. Re-run with sudo access to enable kernel tracing."
            add_skipped "gamepulse-ebpf (sudo install failed)"
        fi

        [ "$_steamos_ro" = "1" ] && sudo steamos-readonly enable 2>/dev/null || true
    fi
else
    info "eBPF daemon not included in this release — kernel tracing unavailable."
    info "Agent-only mode: FPS, CPU, GPU, memory, frame timing streams are unaffected."
    add_skipped "gamepulse-ebpf (not included in this release)"
fi

# ── MangoHud config (frame timing CSV) ───────────────────────────────────────
# Ensure MangoHud writes frame timing CSVs so the agent can read them.
# We write only the two keys we need; if the file already exists we append
# missing keys rather than overwriting user customisations.
_mh_conf_dir="${XDG_CONFIG_HOME:-$HOME/.config}/MangoHud"
_mh_conf="$_mh_conf_dir/MangoHud.conf"
mkdir -p "$_mh_conf_dir"
if ! grep -q "output_folder" "$_mh_conf" 2>/dev/null; then
    printf 'output_folder=%s\n' "$HOME/.local/share/MangoHud" >> "$_mh_conf"
fi
if ! grep -q "autostart_log" "$_mh_conf" 2>/dev/null; then
    printf 'autostart_log=1\n' >> "$_mh_conf"
fi
add_installed "$_mh_conf  (MangoHud frame logging)"

# ── PATH setup ────────────────────────────────────────────────────────────────

# Check if ~/.local/bin is already in PATH
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

# ── Summary ───────────────────────────────────────────────────────────────────

printf '\n'
printf '  GamePulse v%s installed.\n' "$VERSION"
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
    printf '    1. If gamepulse is not found, open a new terminal or run:\n'
    printf '         export PATH="%s:$PATH"\n' "$INSTALL_BIN"
    printf '    2. Run setup:\n'
else
    printf '    1. Run setup:\n'
fi
printf '         gamepulse setup\n'
printf '    2. Add to Steam launch options:\n'
printf '         gamepulse run %%command%%\n'
if printf '%b' "$INSTALLED" | grep -q "gamepulse-ebpf.service  (enabled"; then
    printf '    3. eBPF kernel telemetry is active. To check status:\n'
    printf '         sudo systemctl status gamepulse-ebpf\n'
elif printf '%b' "$INSTALLED" | grep -q "gamepulse-ebpf"; then
    printf '    3. Enable eBPF kernel telemetry at boot:\n'
    printf '         sudo systemctl enable --now gamepulse-ebpf\n'
fi
printf '\n'
