#!/usr/bin/env bash
# test-asset.sh — Run elastic-package test asset without large dev directories
#
# elastic-package v0.122.0 copies the entire repo root when building the
# package internally for asset tests. .elastic-package-ignore only applies
# during lint, not the build copy step. This script temporarily moves large
# dev-only directories to /tmp before running the test, then restores them.
#
# Usage:
#   bash scripts/test-asset.sh [elastic-package test asset flags...]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STASH_DIR="$(mktemp -d /tmp/gamepulse-test-stash.XXXXXX)"
HIDDEN=()

hide_dir() {
    local rel="$1"
    local abs="$REPO_ROOT/$rel"
    if [ -d "$abs" ]; then
        local stash="$STASH_DIR/$(echo "$rel" | tr '/' '_')"
        mv "$abs" "$stash"
        HIDDEN+=("$rel:$stash")
        echo "  hidden: $rel → $stash"
    fi
}

restore_dirs() {
    for entry in "${HIDDEN[@]:-}"; do
        local rel="${entry%%:*}"
        local stash="${entry##*:}"
        local abs="$REPO_ROOT/$rel"
        if [ -n "$rel" ] && [ -d "$stash" ]; then
            mkdir -p "$(dirname "$abs")"
            mv "$stash" "$abs"
        fi
    done
    rm -rf "$STASH_DIR"
}

trap restore_dirs EXIT

cd "$REPO_ROOT"

echo "==> Stashing large dev-only directories to $STASH_DIR ..."
hide_dir .agents
hide_dir collector/.venv
hide_dir ebpf
hide_dir src
hide_dir target
hide_dir packaging
hide_dir dashboards

echo "==> Running: elastic-package test asset $*"
elastic-package test asset "$@"

echo "==> Restoring stashed directories..."
restore_dirs
trap - EXIT
