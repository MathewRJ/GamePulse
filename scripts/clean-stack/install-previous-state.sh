#!/usr/bin/env bash
# Install the Fleet-free adaptation of the documented 0.3.0-era asset state.

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
ASSET_ROOT="$SCRIPT_DIR/previous-state"
dry_run=0

if [[ "${1:-}" == '--dry-run' ]]; then
  dry_run=1
  shift
fi
if (($#)); then
  printf 'error: unexpected argument: %s\n' "$1" >&2
  exit 2
fi

for required in RIGSIGNAL_ES_URL RIGSIGNAL_ES_AUTH; do
  if [[ -z "${!required:-}" ]]; then
    printf 'error: %s is required\n' "$required" >&2
    exit 1
  fi
done

install_kind() {
  local directory="$1"
  local endpoint="$2"
  local asset name
  for asset in "$ASSET_ROOT/$directory"/*.json; do
    [[ -f "$asset" ]] || continue
    name="${asset##*/}"
    name="${name%.json}"
    if [[ "$dry_run" == 1 ]]; then
      printf 'curl --fail --request PUT %q/%s/%q --data-binary @%q\n' \
        "$RIGSIGNAL_ES_URL" "$endpoint" "$name" "$asset"
    else
      curl --fail --silent --show-error \
        --user "$RIGSIGNAL_ES_AUTH" \
        --header 'Content-Type: application/json' \
        --request PUT \
        --data-binary "@$asset" \
        "${RIGSIGNAL_ES_URL%/}/${endpoint}/${name}" >/dev/null
    fi
  done
}

install_kind component-templates _component_template
install_kind index-templates _index_template
install_kind pipelines _ingest/pipeline
printf 'installed previous-state assets from %s\n' "$ASSET_ROOT"
