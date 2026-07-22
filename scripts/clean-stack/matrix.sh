#!/usr/bin/env bash
# Clean-stack G4 matrix.
set -euo pipefail
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/clean-stack/lib.sh
. "$SCRIPT_DIR/lib.sh"
CPU_INDEX=metrics-rigsignal.cpu-default
EVENTS_INDEX=logs-rigsignal.events-default
usage() { printf '%s\n' 'Usage: matrix.sh [--keep] [--dry-run] [--bundle PATH] fresh ES_VERSION|upgrade ES_VERSION|stackupgrade FROM_ES TO_ES' >&2; }
version() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; }
assert_equal() { [[ "$3" == "$2" ]] || { printf 'ASSERT FAIL %s: expected=%q actual=%q\n' "$1" "$2" "$3" >&2; return 1; }; printf 'ASSERT PASS %s\n' "$1"; }
cleanup() { local status="$?"; cs_cleanup || true; [[ -z "$RUN_DIR" || "$keep" == 1 ]] || rm -rf "$RUN_DIR"; return "$status"; }
request() {
  local name="$1" file="$2" status; shift 2
  if ! status="$(cs_http_to_file "$file" --user "elastic:$ELASTIC_PASSWORD" "$@")"; then assert_equal "$name-reachable" success unreachable; return 1; fi
  if cs_status_is_success "$status"; then assert_equal "$name-http" success success; else assert_equal "$name-http" success "$status"; fi
}
kb_request() {
  local name="$1" file="$2" status; shift 2
  if ! status="$(cs_http_to_file "$file" --user "elastic:$ELASTIC_PASSWORD" --header 'kbn-xsrf: clean-stack-matrix' "$@")"; then assert_equal "$name-reachable" success unreachable; return 1; fi
  if cs_status_is_success "$status"; then assert_equal "$name-http" success success; else assert_equal "$name-http" success "$status"; fi
}
start_stack() {
  local v="$1" volumes="$2" ep='' kp=''
  [[ -v CLEAN_STACK_ES_PORT ]] && ep="$CLEAN_STACK_ES_PORT"; [[ -v CLEAN_STACK_KB_PORT ]] && kp="$CLEAN_STACK_KB_PORT"
  if [[ "$volumes" == 1 ]]; then cs_start_elasticsearch_with_volume "docker.elastic.co/elasticsearch/elasticsearch:$v" "$(cs_port_mapping "$ep" 9200)"; else cs_start_elasticsearch "docker.elastic.co/elasticsearch/elasticsearch:$v" "$(cs_port_mapping "$ep" 9200)"; fi
  ES_URL="http://$CS_BIND_ADDRESS:$(cs_published_port "$CS_ES_CONTAINER" 9200/tcp)"
  cs_wait_for_elasticsearch "$ES_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/es-health.json" || { cs_timeout_with_logs Elasticsearch "$CS_ES_CONTAINER"; return 1; }
  local status
  status="$(cs_http_to_file "$RUN_DIR/kb-password.json" --user "elastic:$ELASTIC_PASSWORD" --header 'Content-Type: application/json' --request POST --data "{\"password\":\"$ELASTICSEARCH_PASSWORD\"}" "$ES_URL/_security/user/kibana_system/_password")"
  cs_status_is_success "$status" || return 1
  if [[ "$volumes" == 1 ]]; then cs_start_kibana_with_volume "docker.elastic.co/kibana/kibana:$v" "$(cs_port_mapping "$kp" 5601)"; else cs_start_kibana "docker.elastic.co/kibana/kibana:$v" "$(cs_port_mapping "$kp" 5601)"; fi
  KB_URL="http://$CS_BIND_ADDRESS:$(cs_published_port "$CS_KB_CONTAINER" 5601/tcp)"
  cs_wait_for_kibana "$KB_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/kb-health.json" || { cs_timeout_with_logs Kibana "$CS_ES_CONTAINER" "$CS_KB_CONTAINER"; return 1; }
}
build_bundle() {
  if [[ -n "$bundle_path" ]]; then BUNDLE="$bundle_path"; else BUNDLE="$RUN_DIR/assets.tar.gz"; python3 "$REPO_ROOT/tools/build_asset_bundle.py" --source-commit "$(git -C "$REPO_ROOT" rev-parse HEAD)" --output "$BUNDLE"; fi
  BUNDLE_VERSION="$(python3 - "$BUNDLE" <<'PY'
import json, sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as f: print(json.load(f.extractfile("manifest.json"))["bundle_version"])
PY
)"
}
install_current() { RIGSIGNAL_ES_URL="$ES_URL" RIGSIGNAL_KB_URL="$KB_URL" RIGSIGNAL_ES_AUTH="elastic:$ELASTIC_PASSWORD" python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE"; }
install_previous() { RIGSIGNAL_ES_URL="$ES_URL" RIGSIGNAL_ES_AUTH="elastic:$ELASTIC_PASSWORD" "$SCRIPT_DIR/install-previous-state.sh"; }
ingest() {
  local now; now="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
  jq --arg timestamp "$now" '. + {"@timestamp":$timestamp}' "$REPO_ROOT/fixtures/clean-stack/cpu-doc.json" >"$RUN_DIR/cpu.json"
  jq --arg timestamp "$now" '. + {"@timestamp":$timestamp}' "$REPO_ROOT/fixtures/clean-stack/events-doc.json" >"$RUN_DIR/events.json"
  # TSDS derives _id from dimensions+timestamp; a client-supplied _id is rejected (400).
  request cpu-ingest "$RUN_DIR/cpu-ingest.json" --header 'Content-Type: application/json' --request POST --data-binary "@$RUN_DIR/cpu.json" "$ES_URL/$CPU_INDEX/_doc?refresh=wait_for"
  request events-ingest "$RUN_DIR/events-ingest.json" --header 'Content-Type: application/json' --request POST --data-binary "@$RUN_DIR/events.json" "$ES_URL/$EVENTS_INDEX/_doc/rigsignal-matrix-events-sentinel?op_type=create&refresh=wait_for"
}
hash_source() { jq -cS '._source' "$1" | sha256sum | awk '{print $1}'; }
# TSDS sentinel has an auto-generated _id: locate it by its dimension marker instead.
cpu_sentinel_fetch() {
  # Called via command substitution: ASSERT PASS lines must not pollute the hash,
  # so all assert output is routed to stderr; only the hash reaches stdout.
  local name="$1" out="$2" hits
  request "$name" "$out" --header 'Content-Type: application/json' --request POST \
    --data '{"query":{"term":{"host.name":"rigsignal-matrix-host"}},"size":2}' \
    "$ES_URL/$CPU_INDEX/_search" 1>&2
  hits="$(jq -r '.hits.total.value' "$out")"
  assert_equal "$name-unique-sentinel" "1" "$hits" 1>&2
  jq -cS '.hits.hits[0]._source' "$out" | sha256sum | awk '{print $1}'
}
record() {
  CPU_HASH="$(cpu_sentinel_fetch cpu-before "$RUN_DIR/cpu-before.json")"
  request events-before "$RUN_DIR/events-before.json" --request GET "$ES_URL/$EVENTS_INDEX/_doc/rigsignal-matrix-events-sentinel"; EVENTS_HASH="$(hash_source "$RUN_DIR/events-before.json")"
  request cpu-count-before "$RUN_DIR/cpu-count-before.json" --request GET "$ES_URL/$CPU_INDEX/_count"; CPU_COUNT="$(jq -r .count "$RUN_DIR/cpu-count-before.json")"
  request events-count-before "$RUN_DIR/events-count-before.json" --request GET "$ES_URL/$EVENTS_INDEX/_count"; EVENTS_COUNT="$(jq -r .count "$RUN_DIR/events-count-before.json")"
}
survival() {
  local actual
  actual="$(cpu_sentinel_fetch cpu-after "$RUN_DIR/cpu-after.json")"; assert_equal cpu-sentinel-source-hash "$CPU_HASH" "$actual"
  request events-after "$RUN_DIR/events-after.json" --request GET "$ES_URL/$EVENTS_INDEX/_doc/rigsignal-matrix-events-sentinel"; actual="$(hash_source "$RUN_DIR/events-after.json")"; assert_equal events-sentinel-source-hash "$EVENTS_HASH" "$actual"
  request cpu-count-after "$RUN_DIR/cpu-count-after.json" --request GET "$ES_URL/$CPU_INDEX/_count"; actual="$(jq -r .count "$RUN_DIR/cpu-count-after.json")"; assert_equal cpu-sentinel-count "$CPU_COUNT" "$actual"
  request events-count-after "$RUN_DIR/events-count-after.json" --request GET "$ES_URL/$EVENTS_INDEX/_count"; actual="$(jq -r .count "$RUN_DIR/events-count-after.json")"; assert_equal events-sentinel-count "$EVENTS_COUNT" "$actual"
}
esql() {
  local name="$1" query="$2" expected="$3" actual
  jq -cn --arg query "$query" '{query:$query}' >"$RUN_DIR/$name-request.json"
  request "$name" "$RUN_DIR/$name-result.json" --header 'Content-Type: application/json' --request POST --data-binary "@$RUN_DIR/$name-request.json" "$ES_URL/_query"
  actual="$(jq -r 'if ((.values|length)==1 and (.values[0]|length)==1) then .values[0][0]|tostring else "__invalid__" end' "$RUN_DIR/$name-result.json")"; assert_equal "$name" "$expected" "$actual"
}
asserts() {
  local title actual component pipeline
  esql cpu-marker "FROM $CPU_INDEX | WHERE host.name == \"rigsignal-matrix-host\" | KEEP rigsignal.cpu.total_utilisation_pct" 42.42
  esql events-value "FROM $EVENTS_INDEX | WHERE host.name == \"rigsignal-matrix-host\" | KEEP rigsignal.stream.client.event" connected
  kb_request dashboard-find "$RUN_DIR/dashboards.json" --request GET "$KB_URL/api/saved_objects/_find?type=dashboard&per_page=1000"; actual="$(jq -r .total "$RUN_DIR/dashboards.json")"; assert_equal dashboard-canonical-total 7 "$actual"
  for title in 'RigSignal: Engine & Diagnostics' 'RigSignal Flamegraph Profiles' 'RigSignal: Game Performance' 'RigSignal: Overview' 'RigSignal: Software Stack' 'RigSignal Streaming Lab' 'RigSignal: System Health'; do actual="$(jq -r --arg title "$title" '[.saved_objects[]|select(.attributes.title==$title)]|length' "$RUN_DIR/dashboards.json")"; assert_equal "dashboard-title-$title" 1 "$actual"; done
  request cpu-template-fetch "$RUN_DIR/template.json" --request GET "$ES_URL/_index_template/metrics-rigsignal.cpu"; actual="$(jq -r '(.index_templates|length==1)|tostring' "$RUN_DIR/template.json")"; assert_equal cpu-template-exists true "$actual"
  component="$(jq -r '[.index_templates[0].index_template.composed_of[]|select(endswith(".cpu@package"))][0]' "$RUN_DIR/template.json")"; assert_equal cpu-template-default-component metrics-rigsignal.cpu@package "$component"
  request cpu-component-fetch "$RUN_DIR/component.json" --request GET "$ES_URL/_component_template/$component"; pipeline="$(jq -r '.component_templates[0].component_template.template.settings.index.default_pipeline' "$RUN_DIR/component.json")"; assert_equal cpu-default-pipeline-name metrics-rigsignal.cpu-0.5.0 "$pipeline"
  request cpu-default-pipeline-fetch "$RUN_DIR/pipeline.json" --request GET "$ES_URL/_ingest/pipeline/$pipeline"; actual="$(jq -r 'has("metrics-rigsignal.cpu-0.5.0")|tostring' "$RUN_DIR/pipeline.json")"; assert_equal cpu-default-pipeline-exists true "$actual"
  request bundle-marker-fetch "$RUN_DIR/marker.json" --request GET "$ES_URL/_component_template/rigsignal-bundle-meta"; actual="$(jq -r '.component_templates[0].component_template._meta.bundle_version' "$RUN_DIR/marker.json")"; assert_equal bundle-marker-current-version "$BUNDLE_VERSION" "$actual"
  jq -cn --arg query "FROM $CPU_INDEX | STATS cpu_utilisation = AVG(rigsignal.cpu.total_utilisation_pct) BY Host = TO_LOWER(host.name) | SORT Host" '{query:$query}' >"$RUN_DIR/render.json"; request render-proof-cpu-panel "$RUN_DIR/render-result.json" --header 'Content-Type: application/json' --request POST --data-binary "@$RUN_DIR/render.json" "$ES_URL/_query"; actual="$(jq -r 'if (.values|length)>0 then "1" else "0" end' "$RUN_DIR/render-result.json")"; assert_equal render-proof-cpu-panel 1 "$actual"
}
dry_plan() {
  printf 'Dry run; generated resource suffix: %s\n' "$CS_SUFFIX"; cs_create_network
  if [[ "$mode" == stackupgrade ]]; then cs_create_named_volumes; cs_start_elasticsearch_with_volume "docker.elastic.co/elasticsearch/elasticsearch:$one" "$(cs_port_mapping '' 9200)"; cs_start_kibana_with_volume "docker.elastic.co/kibana/kibana:$one" "$(cs_port_mapping '' 5601)"; else cs_start_elasticsearch "docker.elastic.co/elasticsearch/elasticsearch:$one" "$(cs_port_mapping '' 9200)"; cs_start_kibana "docker.elastic.co/kibana/kibana:$one" "$(cs_port_mapping '' 5601)"; fi
  [[ "$mode" == upgrade ]] && printf '%s --dry-run\n' "$SCRIPT_DIR/install-previous-state.sh" || printf 'python3 tools/build_asset_bundle.py; python3 tools/install_assets.py --bundle <run-bundle>\n'
  printf 'jq injects @timestamp; curl ingests fixtures and runs all named assertions\n'
  if [[ "$mode" == upgrade ]]; then printf 'python3 tools/build_asset_bundle.py; python3 tools/install_assets.py --bundle <run-bundle>; curl verifies sentinel hashes/counts and reruns all assertions\n'; fi
  if [[ "$mode" == stackupgrade ]]; then cs_docker_quiet stop "$CS_KB_CONTAINER"; cs_docker_quiet rm "$CS_KB_CONTAINER"; cs_docker_quiet stop "$CS_ES_CONTAINER"; cs_docker_quiet rm "$CS_ES_CONTAINER"; cs_start_elasticsearch_with_volume "docker.elastic.co/elasticsearch/elasticsearch:$two" "$(cs_port_mapping '' 9200)"; cs_start_kibana_with_volume "docker.elastic.co/kibana/kibana:$two" "$(cs_port_mapping '' 5601)"; printf 'curl reruns all assertions\n'; fi
}
keep=0; dry_run=0; bundle_path=''
while [[ $# -gt 0 && "$1" == -* ]]; do case "$1" in --keep) keep=1 ;; --dry-run) dry_run=1 ;; --bundle) shift; [[ $# -gt 0 ]] || { usage; exit 2; }; bundle_path="$1" ;; *) usage; exit 2 ;; esac; shift; done
[[ $# -gt 0 ]] || { usage; exit 2; }; mode="$1"; shift
case "$mode" in
  fresh|upgrade) if [[ $# != 1 ]] || ! version "$1"; then usage; exit 2; fi; one="$1"; two='' ;;
  stackupgrade) if [[ $# != 2 ]] || ! version "$1" || ! version "$2"; then usage; exit 2; fi; one="$1"; two="$2" ;;
  *) usage; exit 2 ;;
esac
export CS_KEEP="$keep" CS_DRY_RUN="$dry_run"; cs_init_names "$(cs_new_suffix)"; RUN_DIR=''; ELASTIC_PASSWORD="rgs-$RANDOM$RANDOM-A1"; ELASTICSEARCH_PASSWORD="rgs-$RANDOM$RANDOM-B2"; export ELASTIC_PASSWORD ELASTICSEARCH_PASSWORD; trap cleanup EXIT
if [[ "$dry_run" == 1 ]]; then dry_plan; exit 0; fi
cs_require_tools bash curl docker jq python3 sha256sum; RUN_DIR="$(mktemp -d /tmp/rigsignal-clean-stack-matrix.XXXXXX)"
case "$mode" in fresh) cs_create_network; start_stack "$one" 0; build_bundle; install_current; ingest; asserts ;; upgrade) cs_create_network; start_stack "$one" 0; install_previous; ingest; record; build_bundle; install_current; survival; asserts ;; stackupgrade) cs_create_network; cs_create_named_volumes; start_stack "$one" 1; build_bundle; install_current; ingest; cs_docker_quiet stop "$CS_KB_CONTAINER"; cs_docker_quiet rm "$CS_KB_CONTAINER"; CS_KB_CREATED=0; cs_docker_quiet stop "$CS_ES_CONTAINER"; cs_docker_quiet rm "$CS_ES_CONTAINER"; CS_ES_CREATED=0; start_stack "$two" 1; asserts ;; esac
