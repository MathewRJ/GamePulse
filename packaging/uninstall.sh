#!/bin/sh
# RigSignal uninstaller
#
# Removes everything installed by install.sh, in reverse order.
#
# Usage:
#   rigsignal-uninstall           # remove everything (prompts for sudo for eBPF)
#   rigsignal-uninstall --user-only   # remove user-space files only (no sudo needed)
#
# What gets removed:
#   User-space (no sudo):
#     ~/.local/bin/rigsignal-agent
#     ~/.local/bin/rigsignal
#     ~/.config/systemd/user/rigsignal-agent.service
#   System-wide (sudo required, skipped with --user-only):
#     /usr/local/bin/rigsignal-ebpf
#     /usr/local/lib/rigsignal/  (entire directory)
#     /etc/systemd/system/rigsignal-ebpf.service

set -e

# ── Argument parsing ──────────────────────────────────────────────────────────

USER_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --user-only) USER_ONLY=1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

removed() { printf '  removed  %s\n' "$*"; }
skipped() { printf '  skipped  %s\n' "$*"; }
info()    { printf '  [info]   %s\n' "$*"; }

remove_file() {
    path="$1"
    if [ -f "$path" ] || [ -L "$path" ]; then
        rm -f "$path"
        removed "$path"
    else
        skipped "$path  (not found)"
    fi
}

sudo_remove_file() {
    path="$1"
    if [ -f "$path" ] || [ -L "$path" ]; then
        sudo rm -f "$path"
        removed "$path"
    else
        skipped "$path  (not found)"
    fi
}

# ── SteamOS detection ─────────────────────────────────────────────────────────

IS_STEAMOS=0
if grep -qiE '^ID=steamos|^VARIANT_ID=steamdeck' /etc/os-release 2>/dev/null; then
    IS_STEAMOS=1
fi

# ── User-space removal (no sudo) ──────────────────────────────────────────────

printf '\n  Removing user-space files...\n'

USER_BIN="${HOME}/.local/bin"
USER_SERVICE="${HOME}/.config/systemd/user"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop rigsignal-agent 2>/dev/null || true
    systemctl --user disable rigsignal-agent 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

remove_file "$USER_BIN/rigsignal-agent"
remove_file "$USER_BIN/rigsignal"
remove_file "$USER_SERVICE/rigsignal-agent.service"

# ── System-wide removal (sudo) ────────────────────────────────────────────────

EBPF_BIN="/usr/local/bin/rigsignal-ebpf"
EBPF_LIB="/usr/local/lib/rigsignal"
EBPF_SVC="/etc/systemd/system/rigsignal-ebpf.service"

_has_system_files=0
[ -f "$EBPF_BIN" ] && _has_system_files=1
[ -d "$EBPF_LIB" ] && _has_system_files=1
[ -f "$EBPF_SVC" ] && _has_system_files=1

if [ "$USER_ONLY" = "1" ]; then
    if [ "$_has_system_files" = "1" ]; then
        printf '\n  System-wide eBPF files left in place (--user-only):\n'
        [ -f "$EBPF_BIN" ] && skipped "$EBPF_BIN"
        [ -d "$EBPF_LIB" ] && skipped "$EBPF_LIB/"
        [ -f "$EBPF_SVC" ] && skipped "$EBPF_SVC"
        printf '  To remove them: run without --user-only\n'
    fi
elif [ "$_has_system_files" = "1" ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        printf '\n  sudo not available — skipping system-wide eBPF files.\n'
        printf '  To remove manually:\n'
        printf '    sudo rm -f %s %s\n' "$EBPF_BIN" "$EBPF_SVC"
        printf '    sudo rm -rf %s\n' "$EBPF_LIB"
    else
        printf '\n  Removing system-wide eBPF files (requires sudo)...\n'

        _steamos_ro=0
        if [ "$IS_STEAMOS" = "1" ] && command -v steamos-readonly >/dev/null 2>&1; then
            sudo steamos-readonly disable 2>/dev/null && _steamos_ro=1
        fi

        if command -v systemctl >/dev/null 2>&1; then
            sudo systemctl stop rigsignal-ebpf 2>/dev/null || true
            sudo systemctl disable rigsignal-ebpf 2>/dev/null || true
            sudo systemctl daemon-reload 2>/dev/null || true
        fi

        sudo_remove_file "$EBPF_BIN"
        sudo_remove_file "$EBPF_SVC"
        if [ -d "$EBPF_LIB" ]; then
            sudo rm -rf "$EBPF_LIB"
            removed "$EBPF_LIB/"
        fi

        [ "$_steamos_ro" = "1" ] && sudo steamos-readonly enable 2>/dev/null || true
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────

printf '\n  RigSignal uninstalled.\n\n'
