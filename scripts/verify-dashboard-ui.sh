#!/usr/bin/env sh
# verify-dashboard-ui.sh — browser-rendering gate for a Kibana dashboard.
#
# Complements scripts/verify-dashboard.sh (which only proves the dashboard
# round-trips via _export and the internal loader returns a non-error
# payload). This script opens the dashboard in headless Chromium with a
# real browser-auth session and asserts:
#
#   1. The dashboard title element renders.
#   2. Every panel title from the saved-object's panelsJSON renders.
#   3. The page body contains none of the well-known Lens / Kibana
#      embeddable failure strings.
#   4. A full-page screenshot is saved as evidence.
#
# This is the gap that import-valid-but-UI-broken Lens datasource bugs slip
# through. Uses a Playwright-based headless browser verification framework.
#
# Usage:
#   scripts/verify-dashboard-ui.sh <dashboard-id> [--artifact-dir DIR]
#                                                [--timeout-ms MS]
#
# Required env:
#   KIBANA_URL                     Kibana base URL (same one verify-dashboard.sh uses)
#   ES_API_KEY                     Used to call /api/saved_objects/_export
#   KIBANA_BROWSER_AUTH_STATE      Path to a Playwright storage-state JSON
#                                  captured from a real browser-auth session.
#                                  Use scripts/capture-kibana-auth.sh once to
#                                  produce this file.
#
# Exits non-zero on any failure. Browser-auth state is intentionally a hard
# requirement — we do not fall back to ApiKey headers for browser routes.

set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_dir="$(CDPATH= cd -- "$script_dir/.." && pwd)"
. "$script_dir/kibana-lib.sh"

usage() {
  cat >&2 <<EOF
Usage: $0 <dashboard-id> [--artifact-dir DIR] [--timeout-ms MS]

Env required:
  KIBANA_URL, ES_API_KEY, KIBANA_BROWSER_AUTH_STATE
EOF
}

dashboard_id=""
artifact_dir="$repo_dir/artifacts/dashboard-ui"
timeout_ms="60000"

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --artifact-dir)
      [ $# -ge 2 ] || { echo "--artifact-dir needs a value" >&2; exit 2; }
      artifact_dir="$2"; shift 2 ;;
    --timeout-ms)
      [ $# -ge 2 ] || { echo "--timeout-ms needs a value" >&2; exit 2; }
      timeout_ms="$2"; shift 2 ;;
    --) shift; break ;;
    -*) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    *)
      if [ -z "$dashboard_id" ]; then
        dashboard_id="$1"; shift
      else
        echo "Unexpected positional arg: $1" >&2; usage; exit 2
      fi
      ;;
  esac
done

[ -n "$dashboard_id" ] || { usage; exit 2; }

require_env
base_url="$(kibana_base_url)"

auth_refresh_guidance() {
  cat >&2 <<'EOF'
Browser authentication state is required for Kibana UI verification.

Elastic Cloud sessions can expire behind MFA or emailed OTP. The verifier
fails here rather than attempting repeated automated logins through MFA.

To produce a fresh storage-state file:

  scripts/capture-kibana-auth.sh
  export KIBANA_BROWSER_AUTH_STATE=.gpx/kibana-auth.storage-state.json
  scripts/verify-dashboard-ui.sh <dashboard-id>

The state file is gitignored. Refresh it whenever Elastic Cloud invalidates
your session.
EOF
}

if [ -z "${KIBANA_BROWSER_AUTH_STATE:-}" ]; then
  echo "Missing KIBANA_BROWSER_AUTH_STATE." >&2
  auth_refresh_guidance
  exit 2
fi
if [ ! -f "$KIBANA_BROWSER_AUTH_STATE" ]; then
  echo "Browser auth state file not found: $KIBANA_BROWSER_AUTH_STATE" >&2
  auth_refresh_guidance
  exit 2
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required for the Playwright verifier." >&2
  exit 2
fi

mkdir -p "$artifact_dir"
playwright_script="$repo_dir/test/playwright/verify-dashboard-ui.js"
[ -f "$playwright_script" ] || { echo "Missing $playwright_script" >&2; exit 2; }

echo "Running UI verification for dashboard $dashboard_id"
node "$playwright_script" \
  --base-url "$base_url" \
  --dashboard-id "$dashboard_id" \
  --storage-state "$KIBANA_BROWSER_AUTH_STATE" \
  --artifact-dir "$artifact_dir" \
  --es-api-key "$ES_API_KEY" \
  --timeout-ms "$timeout_ms"
