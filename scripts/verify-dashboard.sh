#!/usr/bin/env sh
# verify-dashboard.sh — verify a deployed Kibana dashboard is UI-renderable,
# not just import-valid. Lifted from chatgpt-codex-test/scripts and adapted
# for GamePulse env + integration-package rules.
#
# Usage:
#   scripts/verify-dashboard.sh <dashboard-id> [--expected-panel-types t1,t2,...]
#                                              [--require-dataset-filter]
#                                              [--skip-internal]
#
# Checks performed:
#   1. Saved-objects export round-trip: POST /api/saved_objects/_export with
#      includeReferencesDeep=true. Asserts the dashboard object is present.
#   2. Panel-level Lens invariants: every Lens panel with a formBased datasource
#      has non-empty layers matching the visualization layerId. Every Lens panel
#      with a textBased (ES|QL) datasource has non-empty layers.
#   3. Internal dashboard loader: GET /internal/dashboards/app/<id> with
#      x-elastic-internal-origin: Kibana. A statusCode field means the UI
#      couldn't load it — import-valid but UI-broken.
#   4. (--require-dataset-filter) Every panel references data_stream.dataset
#      somewhere in its embeddableConfig. Required for elastic/integrations
#      package submission.
#   5. (--expected-panel-types) Comma-separated list of expected panel types
#      in the order they appear in panelsJSON.
#
# Exits non-zero on any failure. Tempdir auto-cleans.

set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$script_dir/kibana-lib.sh"

usage() {
  cat >&2 <<EOF
Usage: $0 <dashboard-id> [options]

Options:
  --expected-panel-types TYPES   Comma-separated panel types in panelsJSON order.
  --require-dataset-filter       Fail if any panel lacks data_stream.dataset.
  --skip-internal                Skip internal-loader round-trip (saved-objects only).
  -h, --help                     Show this help.
EOF
}

dashboard_id=""
expected_types=""
require_dataset_filter=0
skip_internal=0

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --expected-panel-types)
      [ $# -ge 2 ] || { echo "--expected-panel-types needs a value" >&2; exit 2; }
      expected_types="$2"; shift 2 ;;
    --require-dataset-filter) require_dataset_filter=1; shift ;;
    --skip-internal) skip_internal=1; shift ;;
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
command -v jq >/dev/null 2>&1 || { echo "jq not found in PATH" >&2; exit 2; }

require_env
base_url="$(kibana_base_url)"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-verify-dashboard.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT

export_file="$tmp_dir/export.ndjson"
internal_file="$tmp_dir/internal.json"

# 1. Export with references.
curl_kibana \
  -X POST "$base_url/api/saved_objects/_export" \
  -H "Content-Type: application/json" \
  --data "{\"objects\":[{\"type\":\"dashboard\",\"id\":\"${dashboard_id}\"}],\"includeReferencesDeep\":true,\"excludeExportDetails\":true}" \
  --output "$export_file"

if ! jq -e --arg id "$dashboard_id" 'select(.type == "dashboard" and .id == $id)' "$export_file" >/dev/null; then
  echo "FAIL export: dashboard $dashboard_id not present in export output" >&2
  echo "--- export payload ---" >&2
  head -c 500 "$export_file" >&2; echo >&2
  exit 1
fi

echo "OK export: dashboard $dashboard_id round-tripped via _export"

# 2. Panel invariants. Only Lens panels are inspected for layer shape; other
#    types (markdown/links/image/controls/search) are treated as opaque.
#    panelsJSON is a JSON-encoded string on the dashboard object — decode with
#    fromjson inside the same jq invocation to preserve type fidelity.
actual_types="$(jq -r --arg id "$dashboard_id" '
  select(.type == "dashboard" and .id == $id)
  | .attributes.panelsJSON | fromjson | map(.type) | join(",")
' "$export_file")"
panel_count="$(jq -r --arg id "$dashboard_id" '
  select(.type == "dashboard" and .id == $id)
  | .attributes.panelsJSON | fromjson | length
' "$export_file")"
[ -n "$panel_count" ] && [ "$panel_count" != "null" ] || { echo "FAIL: dashboard has no panelsJSON" >&2; exit 1; }
echo "OK panels: $panel_count panel(s), types=[$actual_types]"

if [ -n "$expected_types" ] && [ "$actual_types" != "$expected_types" ]; then
  echo "FAIL expected-panel-types:" >&2
  echo "  expected: $expected_types" >&2
  echo "  actual:   $actual_types" >&2
  exit 1
fi

# Lens-specific: layers must be non-empty, and formBased layerId must resolve.
lens_issues="$(jq -r --arg id "$dashboard_id" '
  select(.type == "dashboard" and .id == $id)
  | .attributes.panelsJSON | fromjson
  | to_entries
  | map(select(.value.type == "lens"))
  | map(
      .value as $p
      | ($p.embeddableConfig.attributes.state) as $state
      | ($state.datasourceStates.formBased // null) as $fb
      | ($state.datasourceStates.textBased // null) as $tb
      | ($state.visualization.layerId // null) as $viz_layer
      | if $fb != null then
          if (($fb.layers // {}) | length) == 0 then
            "panel[\(.key)] (\($p.panelIndex // "?")): formBased has empty layers"
          elif $viz_layer != null and (($fb.layers // {}) | has($viz_layer) | not) then
            "panel[\(.key)] (\($p.panelIndex // "?")): formBased layers missing viz layerId \($viz_layer)"
          else empty end
        elif $tb != null then
          if (($tb.layers // []) | length) == 0 then
            "panel[\(.key)] (\($p.panelIndex // "?")): textBased (ES|QL) layers empty"
          else empty end
        else
          "panel[\(.key)] (\($p.panelIndex // "?")): no datasource state (formBased/textBased)"
        end
    )
  | join("\n")
' "$export_file")"

if [ -n "$lens_issues" ]; then
  echo "FAIL lens invariants:" >&2
  printf '%s\n' "$lens_issues" >&2
  exit 1
fi
echo "OK lens invariants: all Lens panels have resolvable datasource layers"

# 3. data_stream.dataset filter (GamePulse integration rule).
if [ "$require_dataset_filter" -eq 1 ]; then
  bad_panels="$(jq -r --arg id "$dashboard_id" '
    select(.type == "dashboard" and .id == $id)
    | .attributes.panelsJSON | fromjson
    | to_entries
    | map(select((.value | tostring | test("data_stream\\.dataset")) | not))
    | map("panel[\(.key)] (\(.value.panelIndex // "?"), type=\(.value.type))")
    | join("\n")
  ' "$export_file")"
  if [ -n "$bad_panels" ]; then
    echo "FAIL dataset filter: panels missing data_stream.dataset reference" >&2
    printf '%s\n' "$bad_panels" >&2
    exit 1
  fi
  echo "OK dataset filter: every panel references data_stream.dataset"
fi

# 4. Internal dashboard loader (UI-renderability).
if [ "$skip_internal" -eq 0 ]; then
  curl_kibana \
    -X GET "$base_url/internal/dashboards/app/${dashboard_id}" \
    -H "x-elastic-internal-origin: Kibana" \
    --output "$internal_file"
  if jq -e 'has("statusCode")' "$internal_file" >/dev/null 2>&1; then
    echo "FAIL internal loader: dashboard $dashboard_id not UI-renderable" >&2
    echo "--- internal payload ---" >&2
    head -c 500 "$internal_file" >&2; echo >&2
    exit 1
  fi
  echo "OK internal loader: dashboard $dashboard_id renders via /internal/dashboards/app"
fi

echo "PASS $dashboard_id"

# 5. Optional browser-UI gate (chained when KIBANA_BROWSER_AUTH_STATE is set).
#    Catches Lens render failures the API gate cannot see.
if [ -n "${KIBANA_BROWSER_AUTH_STATE:-}" ] && [ -x "$script_dir/verify-dashboard-ui.sh" ]; then
  echo "Chaining browser-UI gate (KIBANA_BROWSER_AUTH_STATE detected)"
  "$script_dir/verify-dashboard-ui.sh" "$dashboard_id"
fi
