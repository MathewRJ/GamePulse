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
STASH_DIR="$(mktemp -d /tmp/rigsignal-build-stash.XXXXXX)"
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
hide_dir ebpf
hide_dir src
hide_dir target
hide_dir packaging
hide_dir dashboards

echo "==> Running: elastic-package build $*"
elastic-package build "$@"

# Strip elastic/component-templates/ from the zip — the ECS import technical
# preview feature in elastic-package v0.122.0 adds this directory, but the
# local package-registry (v1.37.0 in the 8.13.0 stack) rejects hyphenated
# directory names. These component templates are loaded by Kibana directly and
# are not needed for local registry serving.
ZIP=$(ls build/packages/rigsignal-*.zip 2>/dev/null | head -1)
if [ -n "$ZIP" ]; then
  echo "==> Stripping elastic/component-templates/ from $(basename "$ZIP") ..."
  python3 - "$ZIP" <<'PYEOF'
import sys, zipfile, os, re
path = sys.argv[1]
tmp = path + ".tmp"
# Strip the entire elastic-package v0.122.0 "technical preview" ECS build
# artifacts tree (elastic/<anything>/ inside the zip). The local package-registry
# v1.37.0 predates this feature and rejects any hyphenated directory name.
# The data_stream/ subdirectory is the canonical source for all assets; the
# elastic/ subtree is redundant for local registry serving.
# Pattern: <pkgname>/elastic/<subdir>/ — strip everything except <pkgname>/elastic/ itself.
strip_re = re.compile(r'^[^/]+/elastic/.+')
with zipfile.ZipFile(path, 'r') as src, zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as dst:
    for item in src.infolist():
        if strip_re.match(item.filename):
            print(f"  stripped: {item.filename}")
        else:
            dst.writestr(item, src.read(item.filename))
os.replace(tmp, path)
PYEOF
fi

echo "==> Restoring stashed directories..."
restore_dirs
trap - EXIT

echo ""
echo "Package:"
ls -lh build/packages/*.zip
