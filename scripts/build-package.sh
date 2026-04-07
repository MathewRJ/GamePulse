#!/usr/bin/env bash
# build-package.sh — Build a lean elastic integration package
#
# elastic-package v0.122.0 ignores .elastic-package-ignore during the build
# copy step (it only applies during lint). This script temporarily moves
# dev-only directories to /tmp before building, then restores them.
#
# Directories must be moved OUTSIDE the repo — renaming within the repo
# still causes elastic-package to copy them.
#
# Usage:
#   bash scripts/build-package.sh [elastic-package build flags...]
#
# Examples:
#   bash scripts/build-package.sh
#   bash scripts/build-package.sh --skip-validation

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STASH_DIR="$(mktemp -d /tmp/gamepulse-build-stash.XXXXXX)"
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
            # Recreate parent if needed
            mkdir -p "$(dirname "$abs")"
            mv "$stash" "$abs"
        fi
    done
    rm -rf "$STASH_DIR"
}

trap restore_dirs EXIT

cd "$REPO_ROOT"

echo "==> Stashing dev-only directories to $STASH_DIR ..."
hide_dir .agents
hide_dir collector/.venv

echo "==> Running: elastic-package build $*"
elastic-package build "$@"

echo "==> Restoring stashed directories..."
restore_dirs
trap - EXIT

echo ""
echo "Package:"
ls -lh build/packages/*.zip
