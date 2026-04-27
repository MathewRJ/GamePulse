#!/usr/bin/env sh
# capture-kibana-auth.sh — one-time helper to produce a Playwright
# storage-state JSON from a real Kibana browser-auth session.
#
# Opens a headed Chromium window pointed at $KIBANA_URL, waits for the
# user to log in (including any MFA / OTP), and saves the resulting
# cookies + storage to .gpx/kibana-auth.storage-state.json.
#
# Run again whenever Elastic Cloud expires your session.

set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_dir="$(CDPATH= cd -- "$script_dir/.." && pwd)"
. "$script_dir/kibana-lib.sh"

require_env
base_url="$(kibana_base_url)"

mkdir -p "$repo_dir/.gpx"
state_file="$repo_dir/.gpx/kibana-auth.storage-state.json"
playwright_script="$repo_dir/test/playwright/capture-auth.js"
[ -f "$playwright_script" ] || { echo "Missing $playwright_script" >&2; exit 2; }

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required." >&2
  exit 2
fi

echo "Opening Chromium pointed at $base_url"
echo "Log in (incl. MFA / OTP). When the dashboard list loads, return to this terminal and press Enter."
node "$playwright_script" \
  --base-url "$base_url" \
  --state-file "$state_file"

echo
echo "Saved: $state_file"
echo "Use it via:"
echo "  export KIBANA_BROWSER_AUTH_STATE=$state_file"
echo "  scripts/verify-dashboard-ui.sh <dashboard-id>"
