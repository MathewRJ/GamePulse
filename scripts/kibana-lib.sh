#!/usr/bin/env sh
# kibana-lib.sh — minimal Kibana curl helper for RigSignal dashboard scripts.
# Source, don't execute: `. "$(dirname "$0")/kibana-lib.sh"`
#
# Reads env:
#   KIBANA_URL   — required; full base URL (e.g., https://foo.kb.region.gcp.elastic.cloud)
#   KIBANA_SPACE — optional; defaults to "default"
#   ES_API_KEY   — required; same key works for both ES and Kibana on Serverless

set -eu

require_env() {
  missing=""
  [ -n "${KIBANA_URL:-}" ]  || missing="${missing} KIBANA_URL"
  [ -n "${ES_API_KEY:-}" ]  || missing="${missing} ES_API_KEY"
  if [ -n "$missing" ]; then
    echo "Missing required environment variables:${missing}" >&2
    exit 2
  fi
  KIBANA_SPACE="${KIBANA_SPACE:-default}"
  KIBANA_AUTH_KEY="$ES_API_KEY"
}

kibana_base_url() {
  base="${KIBANA_URL%/}"
  if [ "$KIBANA_SPACE" = "default" ]; then
    printf "%s" "$base"
  else
    printf "%s/s/%s" "$base" "$KIBANA_SPACE"
  fi
}

curl_kibana() {
  curl --fail-with-body --silent --show-error \
    -H "Authorization: ApiKey ${KIBANA_AUTH_KEY}" \
    -H "kbn-xsrf: true" \
    "$@"
}
