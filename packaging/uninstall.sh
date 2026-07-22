#!/bin/sh
# RigSignal uninstaller
#
# Usage:
#   rigsignal-uninstall [--user-only] [--purge]
#
# --purge also removes RigSignal configuration. Elasticsearch data is never
# touched. DESTDIR stages removal for root-free installer tests.

set -e

USER_ONLY=0
PURGE=0
DESTDIR="${DESTDIR:-}"
for arg in "$@"; do
    case "$arg" in
        --user-only) USER_ONLY=1 ;;
        --purge) PURGE=1 ;;
        *)
            printf '  [err] Unknown option: %s\n' "$arg" >&2
            exit 1
            ;;
    esac
done

removed() { printf '  removed  %s\n' "$*"; }
skipped() { printf '  skipped  %s\n' "$*"; }
info()    { printf '  [info]   %s\n' "$*"; }

stage_path() {
    printf '%s%s' "$DESTDIR" "$1"
}

remove_file() {
    path="$1"
    label="$2"
    if [ -f "$path" ] || [ -L "$path" ]; then
        rm -f "$path"
        removed "$label"
    else
        skipped "$label  (not found)"
    fi
}

remove_system_file() {
    path="$1"
    label="$2"
    if [ -f "$path" ] || [ -L "$path" ]; then
        if [ -n "$DESTDIR" ]; then
            rm -f "$path"
        else
            sudo rm -f "$path"
        fi
        removed "$label"
    else
        skipped "$label  (not found)"
    fi
}

IS_STEAMOS=0
if grep -qiE '^ID=steamos|^VARIANT_ID=steamdeck' /etc/os-release 2>/dev/null; then
    IS_STEAMOS=1
fi

USER_BIN_LOGICAL="${HOME}/.local/bin"
USER_SERVICE_LOGICAL="${HOME}/.config/systemd/user"
USER_CONFIG_LOGICAL="${XDG_CONFIG_HOME:-$HOME/.config}/rigsignal/rigsignal.toml"
USER_BIN=$(stage_path "$USER_BIN_LOGICAL")
USER_SERVICE=$(stage_path "$USER_SERVICE_LOGICAL")
USER_CONFIG=$(stage_path "$USER_CONFIG_LOGICAL")

printf '\n  Removing user-space files...\n'
if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop rigsignal-agent 2>/dev/null || true
    systemctl --user disable rigsignal-agent 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

remove_file "$USER_BIN/rigsignal-agent" "$USER_BIN_LOGICAL/rigsignal-agent"
remove_file "$USER_BIN/rigsignal" "$USER_BIN_LOGICAL/rigsignal"
remove_file "$USER_BIN/rigsignal-uninstall" "$USER_BIN_LOGICAL/rigsignal-uninstall"
remove_file "$USER_SERVICE/rigsignal-agent.service" "$USER_SERVICE_LOGICAL/rigsignal-agent.service"

if [ "$PURGE" = "1" ]; then
    remove_file "$USER_CONFIG" "$USER_CONFIG_LOGICAL"
else
    info "Keeping configuration $USER_CONFIG_LOGICAL (use --purge to remove it)."
fi

EBPF_BIN_LOGICAL="/usr/local/bin/rigsignal-ebpf"
EBPF_LIB_LOGICAL="/usr/local/lib/rigsignal"
EBPF_SVC_LOGICAL="/etc/systemd/system/rigsignal-ebpf.service"
SYSTEM_CONFIG_LOGICAL="/etc/rigsignal/rigsignal.toml"
EBPF_BIN=$(stage_path "$EBPF_BIN_LOGICAL")
EBPF_LIB=$(stage_path "$EBPF_LIB_LOGICAL")
EBPF_SVC=$(stage_path "$EBPF_SVC_LOGICAL")
SYSTEM_CONFIG=$(stage_path "$SYSTEM_CONFIG_LOGICAL")

has_system_files=0
[ -f "$EBPF_BIN" ] && has_system_files=1
[ -d "$EBPF_LIB" ] && has_system_files=1
[ -f "$EBPF_SVC" ] && has_system_files=1
[ "$PURGE" = "1" ] && [ -f "$SYSTEM_CONFIG" ] && has_system_files=1

if [ "$USER_ONLY" = "1" ]; then
    if [ "$has_system_files" = "1" ]; then
        printf '\n  System-wide eBPF files left in place (--user-only).\n'
    fi
elif [ "$has_system_files" = "1" ]; then
    if [ -z "$DESTDIR" ] && ! command -v sudo >/dev/null 2>&1; then
        printf '\n  sudo not available — system-wide eBPF files remain.\n'
    else
        printf '\n  Removing system-wide eBPF files...\n'
        steamos_ro=0
        if [ -z "$DESTDIR" ] && [ "$IS_STEAMOS" = "1" ] && command -v steamos-readonly >/dev/null 2>&1; then
            sudo steamos-readonly disable 2>/dev/null && steamos_ro=1
        fi

        if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl stop rigsignal-ebpf 2>/dev/null || true
            sudo systemctl disable rigsignal-ebpf 2>/dev/null || true
            sudo systemctl daemon-reload 2>/dev/null || true
        fi

        remove_system_file "$EBPF_BIN" "$EBPF_BIN_LOGICAL"
        remove_system_file "$EBPF_SVC" "$EBPF_SVC_LOGICAL"
        if [ -d "$EBPF_LIB" ]; then
            if [ -n "$DESTDIR" ]; then
                rm -rf "$EBPF_LIB"
            else
                sudo rm -rf "$EBPF_LIB"
            fi
            removed "$EBPF_LIB_LOGICAL/"
        fi
        if [ "$PURGE" = "1" ]; then
            remove_system_file "$SYSTEM_CONFIG" "$SYSTEM_CONFIG_LOGICAL"
        else
            info "Keeping configuration $SYSTEM_CONFIG_LOGICAL (use --purge to remove it)."
        fi

        [ "$steamos_ro" = "1" ] && sudo steamos-readonly enable 2>/dev/null || true
    fi
fi

printf '\n  RigSignal uninstalled. Remaining: configuration unless --purge; Elasticsearch data is never touched.\n\n'
