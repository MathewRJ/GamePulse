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
i=0
for arg in "$@"; do
    i=$((i + 1))
    case "$arg" in
        --version=*) VERSION="${arg#--version=}" ;;
        --version)
            # next arg is the value — handled by eval trick below
            eval "VERSION=\${$(( i + 1 ))}" 2>/dev/null || true
            ;;
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

mkdir -p "$INSTALL_BIN"
install -m 755 "$TMP/gamepulse-agent"  "$INSTALL_BIN/gamepulse-agent"
install -m 755 "$TMP/gamepulse"        "$INSTALL_BIN/gamepulse"
ok "Binaries installed to $INSTALL_BIN"

# ── Install user systemd service ──────────────────────────────────────────────

if command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$INSTALL_SERVICE"
    install -m 644 "$TMP/gamepulse-agent.service" "$INSTALL_SERVICE/gamepulse-agent.service"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "Systemd user service installed ($INSTALL_SERVICE/gamepulse-agent.service)"
else
    info "systemctl not found — skipping service install (non-systemd system)"
fi

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
printf '\n'
