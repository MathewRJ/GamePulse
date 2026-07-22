#!/usr/bin/env bash
# Run the ES/Kibana compatibility spike against the canonical v0.3.1 dashboards.

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/clean-stack/lib.sh
. "$SCRIPT_DIR/lib.sh"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/clean-stack/spike.sh [--keep] [--dry-run] ES_VERSION [KB_VERSION]

Run an isolated single-node Elasticsearch and Kibana stack, import every
dashboards/v0.3.1/*.ndjson file, and write spike-report-ES_VERSION.json.

Arguments must be exact X.Y.Z image tags. KB_VERSION defaults to ES_VERSION.

Options:
  --keep     Do not remove this run's containers or network on exit.
  --dry-run  Print the docker commands without executing anything.
  -h, --help Show this help text.

Environment:
  CLEAN_STACK_ES_PORT       Fixed host port for Elasticsearch (default: random).
  CLEAN_STACK_KB_PORT       Fixed host port for Kibana (default: random).
  CLEAN_STACK_TIMEOUT_SECONDS  Per-service startup timeout (default: 180).
EOF
}

is_version() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

print_import_table() {
  local results_file="$1"
  local file success_count errors

  printf '%-42s %-12s %s\n' 'file' 'successCount' 'errors (type, id, error.type)'
  while IFS=$'\t' read -r file success_count errors; do
    printf '%-42s %-12s %s\n' "$file" "$success_count" "$errors"
  done < <(jq -r '
    . as $result
    | ($result.errors
       | if length == 0 then "none"
         else map("type=\(.type // "-"), id=\(.id // "-"), error.type=\(.error.type // "-")") | join("; ")
         end) as $errors
    | "\(.file)\t\(.successCount)\t\($errors)"
  ' "$results_file")
}

append_import_result() {
  local file_name="$1"
  local http_status="$2"
  local response_file="$3"

  if ! jq -e . "$response_file" >/dev/null 2>&1; then
    jq -cn --arg file "$file_name" --arg http_status "$http_status" '
      {
        file: $file,
        httpStatus: $http_status,
        successCount: 0,
        errors: [{type: "response", id: null, error: {type: "invalid_json"}}]
      }
    ' >>"$IMPORT_RESULTS_FILE"
    return 0
  fi

  jq -c --arg file "$file_name" --arg http_status "$http_status" '
    {
      file: $file,
      httpStatus: $http_status,
      successCount: (.successCount // 0),
      errors: (
        if ($http_status | test("^2")) then
          [(.errors // [])[] | {
            type: (.type // null),
            id: (.id // null),
            error: {type: (.error.type // null)}
          }]
        else
          [{type: "http", id: null, error: {type: ("HTTP_" + $http_status)}}]
        end
      )
    }
  ' "$response_file" >>"$IMPORT_RESULTS_FILE"
}

write_report() {
  local finished_at
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  jq -n \
    --arg started_at "$STARTED_AT" \
    --arg finished_at "$finished_at" \
    --arg es_image "$ES_IMAGE" \
    --arg kb_image "$KB_IMAGE" \
    --argjson es_digests "$ES_DIGESTS_JSON" \
    --argjson kb_digests "$KB_DIGESTS_JSON" \
    --slurpfile imports "$IMPORT_RESULTS_FILE" \
    --argjson esql "$ESQL_RESULT_JSON" '
      {
        timestamps: {startedAt: $started_at, finishedAt: $finished_at},
        images: {
          elasticsearch: {tag: $es_image, repoDigests: $es_digests},
          kibana: {tag: $kb_image, repoDigests: $kb_digests}
        },
        imports: $imports,
        esqlProbe: $esql
      }
    ' >"$REPORT_PATH"
}

cleanup() {
  local exit_status="$?"
  cs_cleanup || true
  if [[ -n "$RUN_DIR" && "$keep" != 1 ]]; then
    rm -rf "$RUN_DIR"
  elif [[ -n "$RUN_DIR" && "$keep" == 1 ]]; then
    printf 'keeping run dir for debugging: %s\n' "$RUN_DIR"
  fi
  return "$exit_status"
}

keep=0
dry_run=0
positionals=()
while (($#)); do
  case "$1" in
    --keep) keep=1 ;;
    --dry-run) dry_run=1 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; positionals+=("$@"); break ;;
    -*) printf 'error: unknown option: %s\n' "$1" >&2; usage; exit 2 ;;
    *) positionals+=("$1") ;;
  esac
  shift
done

if ((${#positionals[@]} < 1 || ${#positionals[@]} > 2)); then
  usage
  exit 2
fi

ES_VERSION="${positionals[0]}"
KB_VERSION="${positionals[1]:-$ES_VERSION}"
if ! is_version "$ES_VERSION" || ! is_version "$KB_VERSION"; then
  printf 'error: ES_VERSION and KB_VERSION must be exact X.Y.Z tags (not latest)\n' >&2
  usage
  exit 2
fi

export CS_KEEP="$keep"
export CS_DRY_RUN="$dry_run"
cs_init_names "$(cs_new_suffix)"
ES_IMAGE="docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}"
KB_IMAGE="docker.elastic.co/kibana/kibana:${KB_VERSION}"
ES_PORT_MAPPING="$(cs_port_mapping "${CLEAN_STACK_ES_PORT:-}" 9200)"
KB_PORT_MAPPING="$(cs_port_mapping "${CLEAN_STACK_KB_PORT:-}" 5601)"
REPORT_PATH="$REPO_ROOT/spike-report-${ES_VERSION}.json"
RUN_DIR=''
IMPORT_RESULTS_FILE=''
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ES_DIGESTS_JSON='[]'
KB_DIGESTS_JSON='[]'
ESQL_RESULT_JSON='{"success":false,"reason":"not-run"}'

# Values are exported so docker receives them through --env without commands or logs
# ever containing their values.
ELASTIC_PASSWORD="rgs-${RANDOM}${RANDOM}${RANDOM}${RANDOM}-A1"
ELASTICSEARCH_PASSWORD="rgs-${RANDOM}${RANDOM}${RANDOM}${RANDOM}-B2"
export ELASTIC_PASSWORD ELASTICSEARCH_PASSWORD

trap cleanup EXIT

if [[ "$dry_run" == '1' ]]; then
  printf 'Dry run; generated resource suffix: %s\n' "$CS_SUFFIX"
  cs_create_network
  cs_start_elasticsearch "$ES_IMAGE" "$ES_PORT_MAPPING"
  cs_start_kibana "$KB_IMAGE" "$KB_PORT_MAPPING"
  cs_docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ES_IMAGE"
  cs_docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$KB_IMAGE"
  exit 0
fi

cs_require_tools bash curl jq docker
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rigsignal-clean-stack.XXXXXX")"
IMPORT_RESULTS_FILE="$RUN_DIR/import-results.ndjson"
shopt -s nullglob
dashboard_files=("$REPO_ROOT"/dashboards/v0.3.1/*.ndjson)
if ((${#dashboard_files[@]} == 0)); then
  printf 'error: no dashboard files found in dashboards/v0.3.1\n' >&2
  exit 1
fi

cs_create_network
cs_start_elasticsearch "$ES_IMAGE" "$ES_PORT_MAPPING"
ES_PORT="$(cs_published_port "$CS_ES_CONTAINER" '9200/tcp')"
ES_URL="http://${CS_BIND_ADDRESS}:${ES_PORT}"

ES_HEALTH_FILE="$RUN_DIR/es-health.json"
if ! cs_wait_for_elasticsearch "$ES_URL" elastic "$ELASTIC_PASSWORD" "$ES_HEALTH_FILE"; then
  cs_timeout_with_logs Elasticsearch "$CS_ES_CONTAINER"
  exit 1
fi

KIBANA_PASSWORD_RESPONSE="$RUN_DIR/kibana-password.json"
password_status="$(cs_http_to_file "$KIBANA_PASSWORD_RESPONSE" \
  --user "elastic:${ELASTIC_PASSWORD}" \
  --header 'Content-Type: application/json' \
  --request POST \
  --data "{\"password\":\"${ELASTICSEARCH_PASSWORD}\"}" \
  "${ES_URL}/_security/user/kibana_system/_password")" || {
  printf 'error: Elasticsearch API became unreachable while configuring kibana_system\n' >&2
  exit 1
}
if ! cs_status_is_success "$password_status"; then
  printf 'error: failed to configure kibana_system password (HTTP %s)\n' "$password_status" >&2
  cs_dump_logs "$CS_ES_CONTAINER"
  exit 1
fi

cs_start_kibana "$KB_IMAGE" "$KB_PORT_MAPPING"
KB_PORT="$(cs_published_port "$CS_KB_CONTAINER" '5601/tcp')"
KB_URL="http://${CS_BIND_ADDRESS}:${KB_PORT}"

KB_STATUS_FILE="$RUN_DIR/kibana-status.json"
if ! cs_wait_for_kibana "$KB_URL" elastic "$ELASTIC_PASSWORD" "$KB_STATUS_FILE"; then
  cs_timeout_with_logs Kibana "$CS_ES_CONTAINER" "$CS_KB_CONTAINER"
  exit 1
fi

ES_DIGESTS_JSON="$(cs_repo_digests_json "$ES_IMAGE")"
KB_DIGESTS_JSON="$(cs_repo_digests_json "$KB_IMAGE")"
printf 'Elasticsearch image: %s\n' "$ES_IMAGE"
printf 'Elasticsearch repo digests: %s\n' "$ES_DIGESTS_JSON"
printf 'Kibana image: %s\n' "$KB_IMAGE"
printf 'Kibana repo digests: %s\n' "$KB_DIGESTS_JSON"

: >"$IMPORT_RESULTS_FILE"
for dashboard_file in "${dashboard_files[@]}"; do
  dashboard_name="${dashboard_file##*/}"
  import_response="$RUN_DIR/${dashboard_name}.json"
  import_status="$(cs_http_to_file "$import_response" \
    --user "elastic:${ELASTIC_PASSWORD}" \
    --header 'kbn-xsrf: clean-stack-spike' \
    --request POST \
    --form "file=@${dashboard_file};type=application/ndjson" \
    "${KB_URL}/api/saved_objects/_import?overwrite=true")" || {
    printf 'error: Kibana import API became unreachable for %s\n' "$dashboard_name" >&2
    exit 1
  }
  append_import_result "$dashboard_name" "$import_status" "$import_response"
done

PROBE_INDEX="rigsignal-esql-probe-${CS_SUFFIX}"
PROBE_RESPONSE="$RUN_DIR/esql-probe.json"
probe_index_response="$RUN_DIR/esql-index.json"
probe_index_status="$(cs_http_to_file "$probe_index_response" \
  --user "elastic:${ELASTIC_PASSWORD}" \
  --header 'Content-Type: application/json' \
  --request PUT \
  --data '{"source":"rigsignal-clean-stack-spike"}' \
  "${ES_URL}/${PROBE_INDEX}/_doc/1?refresh=wait_for")" || {
  printf 'error: Elasticsearch API became unreachable while creating the ES|QL probe index\n' >&2
  exit 1
}

if cs_status_is_success "$probe_index_status"; then
  esql_status="$(cs_http_to_file "$PROBE_RESPONSE" \
    --user "elastic:${ELASTIC_PASSWORD}" \
    --header 'Content-Type: application/json' \
    --request POST \
    --data "{\"query\":\"FROM ${PROBE_INDEX} | LIMIT 1\"}" \
    "${ES_URL}/_query")" || {
    printf 'error: Elasticsearch ES|QL API became unreachable\n' >&2
    exit 1
  }
  if jq -e . "$PROBE_RESPONSE" >/dev/null 2>&1; then
    ESQL_RESULT_JSON="$(jq -c --arg status "$esql_status" '{httpStatus: $status, success: (($status | tonumber) >= 200 and ($status | tonumber) < 300), response: .}' "$PROBE_RESPONSE")"
  else
    ESQL_RESULT_JSON="$(jq -cn --arg status "$esql_status" '{httpStatus: $status, success: false, response: "invalid JSON"}')"
  fi
else
  ESQL_RESULT_JSON="$(jq -cn --arg status "$probe_index_status" '{success: false, probeIndexHttpStatus: $status, reason: "probe index creation failed"}')"
fi

write_report
print_import_table "$IMPORT_RESULTS_FILE"
printf 'ES|QL probe: %s\n' "$ESQL_RESULT_JSON"
printf 'Report written: %s\n' "$REPORT_PATH"
