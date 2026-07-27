#!/usr/bin/env bash
# Seed and inspect dashboard-origin fixtures for fleet-coexist gate legs J--M.
set -euo pipefail

: "${KB_URL:?KB_URL is required}"
: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD is required}"

BUNDLE=''
while (($#)); do
  case "$1" in
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    *) break ;;
  esac
done
[[ -f "$BUNDLE" ]] || { printf '%s\n' 'dashboard-origin-seed: --bundle is required' >&2; exit 2; }

usage() {
  printf '%s\n' 'Usage: dashboard-origin-seed.sh --bundle FILE new-all|old-all|derivatives|alias|one|delete|delete-all|export|replay ...' >&2
}
prefix() { [[ "$1" == default ]] || printf '/s/%s' "$1"; }
kb() {
  local method="$1" path="$2"
  shift 2
  curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" \
    -H 'kbn-xsrf: true' -H 'Content-Type: application/json' -X "$method" "${KB_URL}$(prefix "${DASH_SPACE:-default}")${path}" "$@"
}
kb_form() {
  # Multipart variant for _import (a JSON Content-Type header breaks --form).
  local method="$1" path="$2" file="$3"
  curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" \
    -H 'kbn-xsrf: true' -X "$method" "${KB_URL}$(prefix "${DASH_SPACE:-default}")${path}" \
    --form "file=@${file};type=application/ndjson;filename=import.ndjson"
}
space_create() {
  # /api/spaces/space is a ROOT-level API — never space-scoped (POSTing to
  # /s/<space>/api/spaces/space 400s; solo legs j/k failure at pin 0e3689c).
  curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" \
    -H 'kbn-xsrf: true' -H 'Content-Type: application/json' -X POST \
    "${KB_URL}/api/spaces/space" --data-binary "{\"id\":\"$1\",\"name\":\"$1\"}" >/dev/null 2>&1 || true
}
bundle_lines() {
  local file
  for file in rigsignal-engine.ndjson rigsignal-flamegraph-dashboard.ndjson rigsignal-game-perf.ndjson rigsignal-home.ndjson rigsignal-software.ndjson rigsignal-streaming-lab.ndjson rigsignal-system-health.ndjson; do
    tar -xOf "$BUNDLE" "dashboards/v0.3.1/$file"
  done
}
derivative_objects() {
  local file
  for file in rigsignal-engine.ndjson rigsignal-flamegraph-dashboard.ndjson rigsignal-game-perf.ndjson rigsignal-home.ndjson rigsignal-software.ndjson; do
    tar -xOf "$BUNDLE" "dashboards/v0.3.1/$file"
  done | jq -cs '
    map(if .type == "dashboard" then
      .id |= ({
        "rigsignal-pkg-engine":"rigsignal-engine",
        "rigsignal-pkg-flamegraph-dashboard":"rigsignal-flamegraph-dashboard",
        "rigsignal-pkg-game-perf":"rigsignal-game-perf",
        "rigsignal-pkg-home":"rigsignal-home",
        "rigsignal-pkg-software":"rigsignal-software"
      }[.] // .)
    else . end) | unique_by([.type, .id])[]'
}
new_objects() { bundle_lines | jq -c '{type,id,attributes:(.attributes // {}),references:(.references // [])}'; }
old_objects() {
  new_objects | jq -c '
    .id |= ({
      "rigsignal-pkg-engine":"rigsignal-engine",
      "rigsignal-pkg-flamegraph-dashboard":"rigsignal-flamegraph-dashboard",
      "rigsignal-pkg-game-perf":"rigsignal-game-perf",
      "rigsignal-pkg-home":"rigsignal-home",
      "rigsignal-pkg-software":"rigsignal-software",
      "rigsignal-pkg-streaming-lab":"rigsignal-streaming-lab",
      "rigsignal-pkg-system-health":"rigsignal-system-health",
      "rigsignal-pkg-metrics-ebpf":"metrics-rigsignal.ebpf*",
      "rigsignal-pkg-metrics-session":"metrics-rigsignal.session*",
      "rigsignal-pkg-flamegraph-data-view":"rigsignal-flamegraph-data-view",
      "rigsignal-pkg-sl-d1-host-data-view":"sl-d1-host-data-view",
      "rigsignal-pkg-sl-d1-stream-data-view":"sl-d1-stream-data-view",
      "rigsignal-pkg-flamegraph-top-function-delta":"rigsignal-flamegraph-top-function-delta",
      "rigsignal-pkg-managed":"fleet-managed-gaming",
      "rigsignal-pkg-bundle":"fleet-pkg-rigsignal-gaming",
      "rigsignal-pkg-flamegraph-vega-diff":"rigsignal-flamegraph-vega-diff",
      "rigsignal-pkg-flamegraph-vega-live-diff":"rigsignal-flamegraph-vega-live-diff",
      "rigsignal-pkg-flamegraph-vega-single":"rigsignal-flamegraph-vega-single"
    }[.] // .)' | awk '!seen[$0]++'
}
create_objects() {
  local space="$1" rows="$2" row type id body
  space_create "$space"
  while IFS= read -r row; do
    type="$(jq -r '.type' <<<"$row")"; id="$(jq -r '.id' <<<"$row")"
    # The per-object create API takes type/id in the URL; its strict body
    # schema rejects extra top-level keys (whole-row body 400s — solo leg-j
    # round-3 failure at pin 0498f28). Send only {attributes, references}.
    body="$(jq -c '{attributes,references}' <<<"$row")"
    DASH_SPACE="$space" kb POST "/api/saved_objects/$type/$id?overwrite=true" --data-binary "$body" >/dev/null
  done <<<"$rows"
}
case "${1:-}" in
  new-all) create_objects "$2" "$(new_objects)" ;;
  old-all) create_objects "$2" "$(old_objects)" ;;
  space) space_create "$2" ;;
  derivatives)
    # createNewCopies mints UUID ids/originIds and rewrites all closure references.
    space_create default
    derivative_file="${TMPDIR:-/tmp}/dashboard-origin-derivatives.ndjson"
    derivative_response="${TMPDIR:-/tmp}/dashboard-origin-derivatives-response.json"
    derivative_objects >"$derivative_file"
    derivative_expected="$(wc -l <"$derivative_file")"
    curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" -H 'kbn-xsrf: true' \
      -X POST "${KB_URL}/api/saved_objects/_import?createNewCopies=true" -F "file=@${derivative_file};type=application/ndjson;filename=import.ndjson" >"$derivative_response"
    jq -e --argjson expected "$derivative_expected" \
      '.success == true and .successCount == $expected and ((.errors // []) | length == 0)' \
      "$derivative_response" >/dev/null || { cat "$derivative_response" >&2; printf '%s\n' 'dashboard-origin derivatives import was incomplete' >&2; exit 1; }
    DASH_SPACE=default kb GET '/api/saved_objects/_find?type=dashboard&per_page=1000' >"${TMPDIR:-/tmp}/dashboard-origin-derivatives-find.json"
    jq -e '[.saved_objects[] | select((.originId // "") != "")] | length >= 5' \
      "${TMPDIR:-/tmp}/dashboard-origin-derivatives-find.json" >/dev/null || { cat "${TMPDIR:-/tmp}/dashboard-origin-derivatives-find.json" >&2; printf '%s\n' 'dashboard-origin derivatives did not create five default-space origin copies' >&2; exit 1; }
    ;;
  alias)
    # legacy-url-alias is a HIDDEN type: the public create API does not accept
    # it. Mint a REAL alias via Kibana's own machinery instead — proven on
    # 9.4.3 in kibana-943-experiment case 4: a compatibilityMode import that
    # must regenerate (cross-space literal conflict, the case-7b trigger)
    # creates the alias in the target space. Then delete every object,
    # leaving ONLY the alias (Leg-J(i): the alias is the only seed).
    alias_target="$2"; alias_id="$4"; alias_donor="dashboard-origin-alias-donor"
    space_create "$alias_target"; space_create "$alias_donor"
    DASH_SPACE="$alias_donor" kb POST "/api/saved_objects/dashboard/${alias_id}?overwrite=true" \
      --data-binary '{"attributes":{"title":"dashboard-origin alias donor"},"references":[]}' >/dev/null
    alias_ndjson="$(mktemp)"
    printf '{"type":"dashboard","id":"%s","attributes":{"title":"dashboard-origin alias seed"},"references":[]}\n' \
      "$alias_id" >"$alias_ndjson"
    alias_resp="$(DASH_SPACE="$alias_target" kb_form POST '/api/saved_objects/_import?compatibilityMode=true&overwrite=true' "$alias_ndjson")"
    jq -e '.success == true' <<<"$alias_resp" >/dev/null \
      || { printf 'alias mint import failed: %s\n' "$alias_resp" >&2; exit 1; }
    alias_uuid="$(jq -r '.successResults[0].destinationId // empty' <<<"$alias_resp")"
    [[ -n "$alias_uuid" ]] \
      || { printf 'alias mint did not regenerate (no destinationId): %s\n' "$alias_resp" >&2; exit 1; }
    # Deleting the alias's TARGET object cascade-deletes the alias (verified
    # live 2026-07-27 on the kept leg-j stack), so the regenerated uuid host
    # object MUST remain. It is inert to the preflight anyway: a uuid literal
    # id matches no bundle id, and the typed-origin sweep is target-space
    # scoped. Delete only the bundle-id LITERAL (which would otherwise add a
    # literal_id_exists_elsewhere reason), leaving alias_match as the sole
    # preflight-visible reason for bundle ids — exactly what Leg-J(i)/D-2
    # asserts.
    DASH_SPACE="$alias_donor" kb DELETE "/api/saved_objects/dashboard/${alias_id}" >/dev/null
    # Independent-oracle verification via ES (same transport case 4 used;
    # deliberately NOT the installer's own _find, which Leg-J(i) exists to
    # prove): the alias doc must exist and reference alias_id.
    : "${ES_URL:?ES_URL required for alias verification}"
    alias_es="$(curl --silent --show-error --fail ${CS_CA_FILE:+--cacert "$CS_CA_FILE"} \
      --user "elastic:$ELASTIC_PASSWORD" -H 'Content-Type: application/json' \
      "${ES_URL}/.kibana*/_search" --data-binary \
      "{\"query\":{\"term\":{\"type\":\"legacy-url-alias\"}},\"size\":100}")"
    jq -e --arg id "$alias_id" \
      '[.hits.hits[]._source["legacy-url-alias"].sourceId // empty | select(. == $id)] | length >= 1' \
      <<<"$alias_es" >/dev/null \
      || { printf 'alias not found in .kibana after mint: %s\n' "$alias_es" >&2; exit 1; }
    ;;
  one)
    space_create "$2"
    DASH_SPACE="$2" kb POST "/api/saved_objects/$3/$4?overwrite=true" \
      --data-binary "{\"attributes\":{\"title\":\"dashboard-origin seed $4\"},\"references\":[]}" >/dev/null
    ;;
  delete)
    DASH_SPACE="$2" kb DELETE "/api/saved_objects/$3/$4" >/dev/null ;;
  delete-all)
    while IFS=$'\t' read -r type id; do
      DASH_SPACE="$2" kb DELETE "/api/saved_objects/$type/$id" >/dev/null
    done < <(new_objects | jq -r '[.type,.id] | @tsv' | awk '!seen[$0]++')
    ;;
  export)
    # legacy-url-alias is a hidden, non-exportable type (public _export 400s on
    # it — solo leg-j failure at pin 5a2364c); alias presence is asserted via
    # _find in the gate legs instead. An EMPTY space is a legitimate baseline:
    # Kibana _export returns 400 "No objects to export" on zero matches, so
    # run without --fail, tolerate exactly that error as an empty baseline,
    # and fail loudly on anything else.
    export_body="$(curl --silent --show-error --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" \
      -H 'kbn-xsrf: true' -H 'Content-Type: application/json' -X POST \
      "${KB_URL}$(prefix "$2")/api/saved_objects/_export" --data-binary \
      '{"type":["dashboard","index-pattern","search","tag","visualization"],"excludeExportDetails":true}')"
    if jq -e 'select(.statusCode? == 400)' <<<"$(printf '%s' "$export_body" | head -1)" >/dev/null 2>&1; then
      if grep -qi 'no objects' <<<"$export_body"; then
        : >"$3"
      else
        printf 'dashboard-origin export failed: %s\n' "$export_body" >&2; exit 1
      fi
    else
      printf '%s\n' "$export_body" \
        | jq -S -c 'del(.created_at,.updated_at,.version,.coreMigrationVersion,.typeMigrationVersion,.migrationVersion)' | sort >"$3"
    fi
    ;;
  replay)
    while IFS= read -r payload; do
      method="$(jq -r '.method' <<<"$payload")"; path="$(jq -r '.path' <<<"$payload")"
      config="$(mktemp)"
      jq -r '.headers | to_entries[] | "header = \(.key): \(.value)"' <<<"$payload" >"$config"
      curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" \
        --config "$config" -X "$method" "${KB_URL}${path}" >/dev/null
    done <"$2"
    ;;
  *) usage; exit 2 ;;
esac
