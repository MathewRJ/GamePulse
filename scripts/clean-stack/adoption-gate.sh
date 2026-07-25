#!/usr/bin/env bash
# Manual clean-stack gate for Amendment 7/A4 adoption.  It deliberately has no
# CI entry point: run one or more numbered legs while investigating a version.
# shellcheck disable=SC2329
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/clean-stack/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"

DIAGNOSIS_STREAM='logs-rigsignal.diagnosis-default'
ES_VERSION=''
KB_VERSION=''
UPGRADE_ES_VERSION='9.4.4'
UPGRADE_KB_VERSION='9.4.4'
BUNDLE=''
BUNDLE_INPUT=''
KEEP=0
declare -a LEGS=()

usage() {
  cat >&2 <<'EOF'
Usage: adoption-gate.sh --es-version VERSION --kb-version VERSION [options] --leg NAME [--leg NAME ...]

Leg names: 1/refusal, 2/adopt, 3/rerun, 4/shape-negative, 5/flag-misuse,
           6/crash-toctou, 7/m1-shape, 8/fresh, 9/upgrade, 10/proof-set.
Options: --bundle PATH, --keep, --all,
         --upgrade-es-version VERSION --upgrade-kb-version VERSION (leg 9 only).

Legs 1-8 and 10 accept either supported equal version pair.  Leg 9 starts at
9.4.3/9.4.3 and upgrades to 9.4.4/9.4.4 unless both upgrade options override it.
EOF
}

version() { [[ "$1" =~ ^9\.4\.(3|4)$ ]]; }
LEG_RC=0
fail() { LEG_RC=1; printf 'error: %s\n' "$1" >&2; return 1; }
assert_eq() { [[ "$2" == "$3" ]] || fail "$1: expected=$2 actual=$3"; }
assert_file_eq() { cmp -s "$2" "$3" || fail "$1 changed"; }
verdict() { printf 'VERDICT %-16s %s %s\n' "$1" "$2" "$3"; }

while (($#)); do
  case "$1" in
    --es-version) ES_VERSION="${2:-}"; shift 2 ;;
    --kb-version) KB_VERSION="${2:-}"; shift 2 ;;
    --upgrade-es-version) UPGRADE_ES_VERSION="${2:-}"; shift 2 ;;
    --upgrade-kb-version) UPGRADE_KB_VERSION="${2:-}"; shift 2 ;;
    --bundle) BUNDLE_INPUT="${2:-}"; shift 2 ;;
    --leg) LEGS+=("${2:-}"); shift 2 ;;
    --all) LEGS=(1 2 3 4 5 6 7 8 9 10); shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) LEGS+=("$1"); shift ;;
  esac
done

[[ -n "$ES_VERSION" && -n "$KB_VERSION" ]] || { usage; exit 2; }
version "$ES_VERSION" && version "$KB_VERSION" && [[ "$ES_VERSION" == "$KB_VERSION" ]] || fail 'use a supported equal ES/Kibana pair'
((${#LEGS[@]})) || { usage; exit 2; }
[[ -z "$BUNDLE_INPUT" || -f "$BUNDLE_INPUT" ]] || fail '--bundle must be a regular file'
: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD must be set}"
: "${ELASTICSEARCH_PASSWORD:?ELASTICSEARCH_PASSWORD must be set}"
: "${CLEAN_STACK_AGENT_BINARY:?CLEAN_STACK_AGENT_BINARY must name the handshake agent}"

api() {
  local method="$1" path="$2" data_file="${3:-}"
  local args=(--silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD"
              --header 'Content-Type: application/json' --request "$method")
  [[ -z "$data_file" ]] || args+=(--data-binary "@$data_file")
  curl "${args[@]}" "$ES_URL$path"
}

api_to() {
  local out="$1"; shift
  api "$@" >"$out"
}

api_status() {
  local out="$1" method="$2" path="$3" data_file="${4:-}"
  local args=(--silent --show-error --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD"
              --header 'Content-Type: application/json' --request "$method" --output "$out" --write-out '%{http_code}')
  [[ -z "$data_file" ]] || args+=(--data-binary "@$data_file")
  curl "${args[@]}" "$ES_URL$path"
}

start_stack() {
  local es_version="$1" kb_version="$2" with_volume="${3:-0}"
  if [[ "$with_volume" == 1 ]]; then
    cs_start_elasticsearch_with_volume "docker.elastic.co/elasticsearch/elasticsearch:$es_version" "$(cs_port_mapping '' 9200)"
  else
    cs_start_elasticsearch "docker.elastic.co/elasticsearch/elasticsearch:$es_version" "$(cs_port_mapping '' 9200)"
  fi
  ES_URL="https://localhost:$(cs_published_port "$CS_ES_CONTAINER" 9200/tcp)"
  CS_ES_URL="$ES_URL"; export CS_ES_URL
  cs_wait_for_elasticsearch "$ES_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/es-health.json" || {
    cs_timeout_with_logs Elasticsearch "$CS_ES_CONTAINER"; return 1;
  }
  local status
  status="$(api_status "$RUN_DIR/kb-password.json" POST '/_security/user/kibana_system/_password' <(printf '{"password":"%s"}' "$ELASTICSEARCH_PASSWORD"))"
  cs_status_is_success "$status" || fail 'could not bootstrap kibana_system password'
  if [[ "$with_volume" == 1 ]]; then
    cs_start_kibana_with_volume "docker.elastic.co/kibana/kibana:$kb_version" "$(cs_port_mapping '' 5601)"
  else
    cs_start_kibana "docker.elastic.co/kibana/kibana:$kb_version" "$(cs_port_mapping '' 5601)"
  fi
  KB_URL="https://localhost:$(cs_published_port "$CS_KB_CONTAINER" 5601/tcp)"
  CS_KIBANA_URL="$KB_URL"; export CS_KIBANA_URL
  cs_wait_for_kibana "$KB_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/kb-health.json" || {
    cs_timeout_with_logs Kibana "$CS_ES_CONTAINER" "$CS_KB_CONTAINER"; return 1;
  }
}

build_bundle() {
  if [[ -n "$BUNDLE_INPUT" ]]; then BUNDLE="$BUNDLE_INPUT"; return; fi
  BUNDLE="$RUN_DIR/assets.tar.gz"
  python3 "$REPO_ROOT/tools/build_asset_bundle.py" --source-commit "$(git -C "$REPO_ROOT" rev-parse HEAD)" --output "$BUNDLE"
}

write_admin_credentials() {
  local password="$ELASTIC_PASSWORD"
  password="${password//\\/\\\\}"; password="${password//\"/\\\"}"
  umask 077
  printf '[elasticsearch]\nusername = "elastic"\npassword = "%s"\n' "$password" >"$RUN_DIR/admin-credentials.toml"
  chmod 600 "$RUN_DIR/admin-credentials.toml"
}

_run_installer() {
  local root="$1" adopt="${2:-0}"
  # Settle transient cluster tasks from seeding before the installer's §7
  # point-in-time health gate reads them (harness race, not a product concern).
  api GET '/_cluster/health?wait_for_events=languid&wait_for_no_initializing_shards=true&timeout=30s' >/dev/null || true
  local args=(--bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE"
    --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE"
    --admin-credentials-file "$RUN_DIR/admin-credentials.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY"
    --profile user --enrollment-root "$root")
  [[ "$adopt" == 1 ]] && args+=(--adopt-existing-w1-stream)
  local out rc attempt
  for attempt in 1 2 3; do
    if out="$(python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" "${args[@]}" 2>&1)"; then rc=0; else rc=$?; fi
    if [[ "$rc" == 0 ]]; then printf '%s\n' "$out"; return 0; fi
    if [[ "${out##*$'\n'}" == 'install refused: cluster_health' && "$attempt" != 3 ]]; then sleep 10; continue; fi
    printf '%s\n' "$out"; return "$rc"
  done
  return 1
}
run_installer() { _run_installer "$@" || fail 'installer failed'; }

# This is matrix.sh's owned_snapshot/zero-delta discipline, replicated here so
# a selected adoption leg is self-contained.
owned_snapshot() {
  local output="$1" root="$2" item
  : >"$output"
  for item in "/_component_template/logs-rigsignal.diagnosis-mappings" "/_index_template/logs-rigsignal.diagnosis" "/_security/role/rigsignal_shipper" "/_data_stream/$DIAGNOSIS_STREAM" "/_component_template/rigsignal-bundle-meta" '/_security/api_key?owner=false'; do
    curl --silent --show-error --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" --write-out '\nSTATUS:%{http_code}\n' "$ES_URL$item" >>"$output" || true
  done
  for item in state.json credentials.toml handshake.toml shipping-policy-v1.toml candidate/credentials.toml candidate/handshake.toml candidate/shipping-policy-v1.toml candidate/state.json; do
    if [[ -e "$root/$item" ]]; then stat -c "$item:%a:%u:%g:%s" "$root/$item" >>"$output"; sha256sum "$root/$item" >>"$output"; else printf '%s:ABSENT\n' "$item" >>"$output"; fi
  done
  sha256sum "$output" | awk '{print $1}'
}

expect_refusal() {
  local name="$1" root="$2" flag="$3" code="$4" before after output
  output="$RUN_DIR/$name.out"; before="$(owned_snapshot "$RUN_DIR/$name.before" "$root")"
  if _run_installer "$root" "$flag" >"$output" 2>&1; then fail "$name unexpectedly succeeded"; fi
  grep -Fx "install refused: $code" "$output" >/dev/null || { sed -n '1,20p' "$output" >&2; fail "$name wrong refusal"; }
  after="$(owned_snapshot "$RUN_DIR/$name.after" "$root")"; assert_eq "$name owned delta" "$before" "$after"
}

seed_m1() {
  # No enrollment directory is created here.  The two documents deliberately
  # use the frozen W1-positive shape but retain their M1 IDs and hashes.
  local component="$REPO_ROOT/elastic/component-templates/logs-rigsignal.diagnosis-mappings.json"
  local index="$REPO_ROOT/elastic/index-templates/logs-rigsignal.diagnosis.json"
  api PUT '/_component_template/logs-rigsignal.diagnosis-mappings' "$component" >/dev/null
  api PUT '/_index_template/logs-rigsignal.diagnosis' "$index" >/dev/null
  api PUT "/_data_stream/$DIAGNOSIS_STREAM" >/dev/null
  python3 - "$REPO_ROOT/fixtures/diagnosis_event/v1/positive/15-diagnosis-non-finding-conditional.expected.json" "$RUN_DIR" <<'PY'
import json, sys
source = json.load(open(sys.argv[1]))
for number in (1, 2):
    doc = json.loads(json.dumps(source))
    doc["event"]["id"] = f"m1-{number}"
    doc["@timestamp"] = f"2026-01-0{number}T00:00:00.000Z"
    json.dump(doc, open(f"{sys.argv[2]}/m1-{number}.json", "w"), separators=(",", ":"))
PY
  api POST "/$DIAGNOSIS_STREAM/_create/m1-1?refresh=wait_for" "$RUN_DIR/m1-1.json" >/dev/null
  api POST "/$DIAGNOSIS_STREAM/_create/m1-2?refresh=wait_for" "$RUN_DIR/m1-2.json" >/dev/null
}

capture_m1() {
  local out="$1"
  api_to "$out" POST "/$DIAGNOSIS_STREAM/_search" <(printf '%s' '{"query":{"ids":{"values":["m1-1","m1-2"]}},"size":2,"sort":[{"event.id":"asc"}]}')
  jq -S -c '[.hits.hits[]|{_id,_source,_index}]' "$out" >"$out.canonical"
}

backing_uuid() { api GET "/_data_stream/$DIAGNOSIS_STREAM" | jq -r '.data_streams[0].indices[-1].index_uuid'; }
proof_ids() {
  api POST "/$DIAGNOSIS_STREAM/_search" <(printf '%s' '{"query":{"prefix":{"event.id":"provision-"}},"size":100,"_source":true,"sort":[{"event.id":"asc"}]}') \
    | jq -r '.hits.hits[]|._id'
}

assert_exact_proof_set() {
  local expected="$1" request="$RUN_DIR/exact-proof-query.json"
  jq -Rsc '{query:{ids:{values:(split("\n")|map(select(length>0)))}},size:100,_source:true,sort:[{"event.id":"asc"}]}' "$expected" >"$request"
  api_to "$RUN_DIR/exact-proofs.json" POST "/$DIAGNOSIS_STREAM/_search" "$request"
  jq -r '.hits.hits[]|._id' "$RUN_DIR/exact-proofs.json" >"$RUN_DIR/exact-proof-ids"
  assert_file_eq 'exact provision proof-ID set' "$expected" "$RUN_DIR/exact-proof-ids"
  jq -e --rawfile expected "$expected" '($expected | split("\n") | map(select(length>0))) as $ids | [.hits.hits[] | ._source.event.id as $id | select($ids | index($id) != null) | $id] | length == ($ids|length)' "$RUN_DIR/exact-proofs.json" >/dev/null || fail 'exact proof sources do not match their IDs'
}

leg_1() { seed_m1; expect_refusal refusal-no-flag "$RUN_DIR/enrollment" 0 adoption_required; }

leg_2() {
  local before_uuid after_uuid
  seed_m1; capture_m1 "$RUN_DIR/m1.before"; before_uuid="$(backing_uuid)"
  run_installer "$RUN_DIR/enrollment" 1
  capture_m1 "$RUN_DIR/m1.after"; after_uuid="$(backing_uuid)"
  assert_file_eq 'M1 documents' "$RUN_DIR/m1.before.canonical" "$RUN_DIR/m1.after.canonical"
  assert_eq 'adoption backing UUID' "$before_uuid" "$after_uuid"
  [[ "$(proof_ids | wc -l)" == 1 ]] || fail 'adoption did not retain exactly one proof'
}

leg_3() {
  seed_m1; capture_m1 "$RUN_DIR/m1.before"; run_installer "$RUN_DIR/enrollment" 1
  run_installer "$RUN_DIR/enrollment" 0; run_installer "$RUN_DIR/enrollment" 0
  capture_m1 "$RUN_DIR/m1.after"; assert_file_eq 'M1 docs after reruns' "$RUN_DIR/m1.before.canonical" "$RUN_DIR/m1.after.canonical"
  proof_ids >"$RUN_DIR/proof-ids"; [[ "$(sort -u "$RUN_DIR/proof-ids" | wc -l)" == 3 ]] || fail 'expected exactly adoption plus two distinct proofs'
}

seed_with_shape() {
  local name="$1"
  local component="$RUN_DIR/$name-component.json" index="$RUN_DIR/$name-index.json"
  cp "$REPO_ROOT/elastic/component-templates/logs-rigsignal.diagnosis-mappings.json" "$component"
  cp "$REPO_ROOT/elastic/index-templates/logs-rigsignal.diagnosis.json" "$index"
  case "$name" in
    confidence-float) jq '.template.mappings.properties.rigsignal.properties.diagnosis.properties.confidence.type="float"' "$component" >"$component.tmp"; mv "$component.tmp" "$component" ;;
    missing-diagnosis) jq 'del(.template.mappings.properties.rigsignal.properties.diagnosis.properties.disposition)' "$component" >"$component.tmp"; mv "$component.tmp" "$component" ;;
    extra-diagnosis) jq '.template.mappings.properties.rigsignal.properties.diagnosis.properties.extra_gate_probe={"type":"keyword"}' "$component" >"$component.tmp"; mv "$component.tmp" "$component" ;;
    non-strict) jq '.template.mappings.properties.rigsignal.properties.diagnosis.dynamic=false' "$component" >"$component.tmp"; mv "$component.tmp" "$component" ;;
    ignore-malformed) jq '.template.settings.index.mapping.ignore_malformed=true' "$index" >"$index.tmp"; mv "$index.tmp" "$index" ;;
    failure-store) jq '.template.data_stream_options.failure_store.enabled=true' "$index" >"$index.tmp"; mv "$index.tmp" "$index" ;;
    wrong-lifecycle) jq '.template.settings.index.lifecycle.name="wrong-lifecycle"' "$index" >"$index.tmp"; mv "$index.tmp" "$index" ;;
  esac
  api PUT '/_component_template/logs-rigsignal.diagnosis-mappings' "$component" >/dev/null
  api PUT '/_index_template/logs-rigsignal.diagnosis' "$index" >/dev/null
  api PUT "/_data_stream/$DIAGNOSIS_STREAM" >/dev/null
}

leg_4() {
  local shape root before after index_name policy status
  for shape in confidence-float missing-diagnosis extra-diagnosis non-strict ignore-malformed failure-store wrong-lifecycle; do
    api DELETE "/_data_stream/$DIAGNOSIS_STREAM" >/dev/null 2>&1 || true
    seed_with_shape "$shape"; root="$RUN_DIR/$shape-root"; before="$(owned_snapshot "$RUN_DIR/$shape.before" "$root")"
    expect_refusal "$shape" "$root" 1 migration_required; after="$(owned_snapshot "$RUN_DIR/$shape.after" "$root")"; assert_eq "$shape zero mutation" "$before" "$after"
  done
  # A divergent backing among several (not merely an altered template).
  api DELETE "/_data_stream/$DIAGNOSIS_STREAM" >/dev/null 2>&1 || true; seed_m1
  api POST "/$DIAGNOSIS_STREAM/_rollover" >/dev/null
  index_name="$(api GET "/_data_stream/$DIAGNOSIS_STREAM" | jq -r '.data_streams[0].indices[0].index_name')"
  printf '%s' '{"properties":{"rigsignal":{"properties":{"diagnosis":{"properties":{"extra_gate_probe":{"type":"keyword"}}}}}}}' >"$RUN_DIR/divergent.json"
  api PUT "/$index_name/_mapping" "$RUN_DIR/divergent.json" >/dev/null
  expect_refusal divergent-backing "$RUN_DIR/divergent-root" 1 migration_required
  # Correct policy name but a delete phase must also fail.
  api DELETE "/_data_stream/$DIAGNOSIS_STREAM" >/dev/null 2>&1 || true; seed_m1
  api_to "$RUN_DIR/policy.json" GET '/_ilm/policy/logs@lifecycle'
  policy="$(jq '{"policy": (."logs@lifecycle".policy | .phases.delete = {"min_age":"1d","actions":{"delete":{}}})}' "$RUN_DIR/policy.json")"
  printf '%s' "$policy" >"$RUN_DIR/delete-policy.json"
  status="$(api_status "$RUN_DIR/delete-policy-put.out" PUT '/_ilm/policy/logs@lifecycle' "$RUN_DIR/delete-policy.json")"
  assert_eq 'delete-lifecycle policy PUT' 200 "$status"
  [[ "$status" != 200 ]] || expect_refusal delete-lifecycle "$RUN_DIR/delete-lifecycle-root" 1 migration_required
}

make_incomplete_state() {
  local root="$1"; mkdir -m 700 "$root"
  python3 - "$root" >"$root/state.json" <<'PY'
import hashlib, json, os, sys
root=os.path.realpath(sys.argv[1])
print(json.dumps({"version":1,"phase":"mint_intent","expected_cluster_uuid":"KUrXRgwRRQu-RikmIJhm0Q","target_generation":"0"*64,"role_jcs_sha256":"0"*64,"enrollment_root":root,"active_key_id":None,"pending_revoke_ids":[],"pending_mint_name":"unfinished","candidate_key_id":None}, separators=(",",":")))
PY
  chmod 600 "$root/state.json"
}

leg_5() {
  local root
  expect_refusal flag-stream-absent "$RUN_DIR/absent-root" 1 adoption_flag_stream_absent
  seed_m1; run_installer "$RUN_DIR/committed-root" 1; expect_refusal flag-committed "$RUN_DIR/committed-root" 1 adoption_flag_state_present
  make_incomplete_state "$RUN_DIR/incomplete-root"; expect_refusal flag-incomplete "$RUN_DIR/incomplete-root" 1 adoption_flag_state_present
  root="$RUN_DIR/malformed-root"; mkdir -m 700 "$root"; printf '{}' >"$root/state.json"; chmod 600 "$root/state.json"; expect_refusal flag-malformed "$root" 1 enrollment_remediation_required
  root="$RUN_DIR/orphan-root"; mkdir -m 700 "$root"; printf x >"$root/credentials.toml"; chmod 600 "$root/credentials.toml"; expect_refusal flag-orphan "$root" 1 enrollment_remediation_required
  root="$RUN_DIR/candidate-root"; mkdir -m 700 "$root" "$root/candidate"; expect_refusal flag-candidate "$root" 1 enrollment_remediation_required
  root="$RUN_DIR/stage-root"; mkdir -m 700 "$root" "$RUN_DIR/.rigsignal-publication-stage-root"; expect_refusal flag-stage "$root" 1 enrollment_remediation_required
}

leg_6() {
  local root="$RUN_DIR/crash-root" status index_name injector='' pending_mint_name
  seed_m1; capture_m1 "$RUN_DIR/crash-m1.before"
  if RIGSIGNAL_TEST_CRASH_AT=candidate-write run_installer "$root" 1 >"$RUN_DIR/crash.out" 2>&1; then status=0; else status=$?; fi
  [[ "$status" == 99 ]] || fail 'candidate-write crash hook did not terminate installer'
  pending_mint_name="$(jq -r '.pending_mint_name|@uri' "$root/state.json")"
  if run_installer "$root" 0 >"$RUN_DIR/crash-retry.out" 2>&1; then fail 'candidate-write no-flag retry unexpectedly succeeded'; fi
  grep -Fx 'install refused: adoption_required' "$RUN_DIR/crash-retry.out" >/dev/null || fail 'candidate-write retry did not re-dispatch adoption_required'
  [[ ! -d "$root/candidate" ]] || fail 'recovery left candidate directory'
  [[ ! -e "$root/state.json" ]] || fail 'recovery left null-active state'
  api GET "/_security/api_key?name=$pending_mint_name&active_only=true" | jq -e '.api_keys == []' >/dev/null \
    || fail 'recovery left candidate API key active'
  run_installer "$root" 1 || fail 'candidate-write adoption retry did not complete'
  capture_m1 "$RUN_DIR/crash-m1.after"
  assert_file_eq 'candidate-write recovery preserved M1 documents' "$RUN_DIR/crash-m1.before.canonical" "$RUN_DIR/crash-m1.after.canonical"
  [[ "$(proof_ids | wc -l)" == 1 ]] || fail 'candidate-write adoption retry did not retain one proof'
  # Polling candidate_verified makes the mutation occur after Step 8; the
  # loop persists it until the immediate pre-Step-9 shared predicate observes it.
  root="$RUN_DIR/drift-root"; api DELETE "/_data_stream/$DIAGNOSIS_STREAM" >/dev/null; seed_m1
  ( while [[ ! -f "$root/state.json" ]] || ! grep -q '"phase":"candidate_verified"' "$root/state.json"; do sleep 0.01; done
    index_name="$(api GET "/_data_stream/$DIAGNOSIS_STREAM" | jq -r '.data_streams[0].indices[-1].index_name')"
    printf '%s' '{"properties":{"rigsignal":{"properties":{"diagnosis":{"properties":{"toctou_gate":{"type":"keyword"}}}}}}}' >"$RUN_DIR/toctou.json"
    api PUT "/$index_name/_mapping" "$RUN_DIR/toctou.json" >/dev/null
  ) &
  injector=$!
  if run_installer "$root" 1 >"$RUN_DIR/toctou.out" 2>&1; then
    kill "$injector" 2>/dev/null || true
    wait "$injector" || true
    injector=''
    fail 'TOCTOU installer unexpectedly succeeded'
  else
    # The injector may still be polling if the installer failed before Step 8.
    # Always reap it before making the fence assertions.
    kill "$injector" 2>/dev/null || true
    wait "$injector" || true
    injector=''
  fi
  grep -Fx 'install failed: pre-publication fence:' "$RUN_DIR/toctou.out" >/dev/null || fail 'TOCTOU did not fail at fence'
  [[ ! -e "$root/credentials.toml" ]] || fail 'TOCTOU published credentials'
  [[ ! -e "$root/handshake.toml" ]] || fail 'TOCTOU published configuration'
}

leg_7() { seed_m1; run_installer "$RUN_DIR/enrollment" 1; api GET "/_data_stream/$DIAGNOSIS_STREAM" | jq -e '.data_streams[0].failure_store.enabled == false' >/dev/null; }

leg_10() {
  seed_m1; run_installer "$RUN_DIR/enrollment" 1; proof_ids >"$RUN_DIR/adoption-proof"
  run_installer "$RUN_DIR/enrollment" 0; proof_ids >"$RUN_DIR/all-proofs"
  comm -13 "$RUN_DIR/adoption-proof" "$RUN_DIR/all-proofs" >"$RUN_DIR/rerun-proof"
  cat "$RUN_DIR/adoption-proof" "$RUN_DIR/rerun-proof" | sort -u >"$RUN_DIR/expected-proofs"
  [[ "$(wc -l <"$RUN_DIR/expected-proofs")" == 2 ]] || fail 'expected one accepted proof per invocation'
  assert_exact_proof_set "$RUN_DIR/expected-proofs"
  grep -Eq '^m1-' "$RUN_DIR/expected-proofs" && fail 'legacy M1 document entered proof set'
  capture_m1 "$RUN_DIR/m1"; [[ "$(jq 'length' "$RUN_DIR/m1.canonical")" == 2 ]] || fail 'legacy M1 docs missing'
}

leg_9() {
  [[ "$ES_VERSION" == 9.4.3 && "$KB_VERSION" == 9.4.3 && "$UPGRADE_ES_VERSION" == 9.4.4 && "$UPGRADE_KB_VERSION" == 9.4.4 ]] || fail 'leg 9 requires 9.4.3 -> 9.4.4'
  seed_m1; capture_m1 "$RUN_DIR/m1.before"; run_installer "$RUN_DIR/enrollment" 1
  cs_docker_quiet stop "$CS_KB_CONTAINER"; cs_docker_quiet rm "$CS_KB_CONTAINER"
  # shellcheck disable=SC2034 # read by cs_cleanup in the sourced lifecycle helpers
  CS_KB_CREATED=0
  cs_docker_quiet stop "$CS_ES_CONTAINER"; cs_docker_quiet rm "$CS_ES_CONTAINER"
  # shellcheck disable=SC2034 # read by cs_cleanup in the sourced lifecycle helpers
  CS_ES_CREATED=0
  start_stack "$UPGRADE_ES_VERSION" "$UPGRADE_KB_VERSION" 1
  api POST "/$DIAGNOSIS_STREAM/_rollover" >/dev/null
  run_installer "$RUN_DIR/enrollment" 0
  capture_m1 "$RUN_DIR/m1.after"; assert_file_eq 'upgrade preserved M1 documents' "$RUN_DIR/m1.before.canonical" "$RUN_DIR/m1.after.canonical"
  [[ -n "$(backing_uuid)" ]] || fail 'rollover did not expose new backing UUID'
}

leg_8() { run_installer "$RUN_DIR/enrollment" 0; [[ -f "$RUN_DIR/enrollment/state.json" ]] || fail 'fresh install did not commit enrollment'; }

canonical_leg() {
  case "$1" in
    1|refusal) printf 1 ;; 2|adopt) printf 2 ;; 3|rerun) printf 3 ;; 4|shape-negative) printf 4 ;;
    5|flag-misuse) printf 5 ;; 6|crash-toctou) printf 6 ;; 7|m1-shape) printf 7 ;;
    8|fresh) printf 8 ;; 9|upgrade) printf 9 ;; 10|proof-set) printf 10 ;;
    *) return 1 ;;
  esac
}

run_leg() {
  local requested="$1" leg function_name status
  leg="$(canonical_leg "$requested")" || { verdict "$requested" FAIL 'unknown leg'; return 2; }
  RUN_DIR="$(mktemp -d "/tmp/rigsignal-adoption-gate-$leg.XXXXXX")"; chmod 700 "$RUN_DIR"
  CS_RUN_DIR="$RUN_DIR"; export CS_RUN_DIR
  cs_init_names "adoption-$leg-$(cs_new_suffix)"; cs_prepare_tls "$RUN_DIR"; cs_create_network
  if [[ "$leg" == 9 ]]; then cs_create_named_volumes; start_stack "$ES_VERSION" "$KB_VERSION" 1; else start_stack "$ES_VERSION" "$KB_VERSION"; fi
  build_bundle; write_admin_credentials; function_name="leg_$leg"; LEG_RC=0
  # Invoke the leg through a conditional so an assertion/helper failure cannot
  # escape this runner before it emits the leg's verdict.
  if "$function_name"; then status=0; else status=$?; fi
  (( LEG_RC == 0 )) || status=1
  if [[ "$status" == 0 ]]; then verdict "$leg" PASS 'contract assertions satisfied'; else verdict "$leg" FAIL 'see run directory and assertion above'; fi
  cs_cleanup || true
  if [[ "$KEEP" == 1 || "$status" != 0 ]]; then printf 'RUN_DIR %s\n' "$RUN_DIR"; else rm -rf "$RUN_DIR"; fi
  return "$status"
}

cs_require_tools bash curl docker jq openssl python3 sha256sum cmp grep sort wc
overall=0
for requested in "${LEGS[@]}"; do
  if run_leg "$requested"; then :; else overall=1; fi
done
exit "$overall"
