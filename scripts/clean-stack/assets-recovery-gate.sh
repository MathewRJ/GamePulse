#!/usr/bin/env bash
# Live, host-run recovery gate for the v2 assets-only transaction engine.
#
# This is intentionally not a CI test and is not run in the constrained Codex
# sandbox.  It starts short-lived loopback-only ES/Kibana 9.4.4 stacks and
# keeps every Docker resource inside the rigsignal-recovery-033 namespace.
# shellcheck disable=SC2329 # legs are dispatched by run_leg after stack setup.
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/clean-stack/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"

readonly NAMESPACE='rigsignal-recovery-033'
readonly ES_VERSION='9.4.4'
readonly KB_VERSION='9.4.4'
readonly VERIFY="$SCRIPT_DIR/assets-recovery-verify.py"

KEEP=0
BUNDLE_INPUT=''
RUN_ROOT=''
BUNDLE=''
AGENT_BINARY=''
CREDENTIAL_ROOT=''
ADMIN_CREDENTIALS_FILE=''
declare -a SUMMARY_ROWS=()

usage() {
  cat >&2 <<'EOF'
Usage: assets-recovery-gate.sh [--bundle PATH] [--keep]

Runs the T-GATE-3 live recovery legs (fresh install, crash-after-Kibana-write
recovery, dashboard-member recovery, pipeline/role detector races) against
isolated Elasticsearch and Kibana 9.4.4 containers ONLY.  T-GATE-2's 9.4.3
saved-object matrix and 9.4.3->9.4.4 upgrade legs are NOT run here; that
cross-version matrix remains a separate release-gate obligation.  Credentials
are generated per leg.  Evidence is retained below /tmp/rigsignal-recovery-033.*
even when Docker resources are removed.  --keep preserves only this gate's
namespaced containers, network, and volumes for diagnosis.
EOF
}

while (($#)); do
  case "$1" in
    --bundle) BUNDLE_INPUT="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -z "$BUNDLE_INPUT" || -f "$BUNDLE_INPUT" ]] || { printf 'error: --bundle must be a regular file\n' >&2; exit 2; }

sanitize_text() {
  # The generated passwords contain this stable prefix.  Keep stdout/stderr
  # useful without ever carrying a usable credential into the evidence tree.
  sed -E 's/rgs033-[[:xdigit:]]{32}/<redacted-password>/g'
}

write_leg_summary() {
  local dir="$1" name="$2" result="$3" driver_exit="$4"
  jq -n --arg leg "$name" --arg result "$result" --argjson driver_exit "$driver_exit" \
    '{leg:$leg,result:$result,driver_exit:$driver_exit}' >"$dir/verdict.json"
  {
    printf '# %s\n\n' "$name"
    printf -- '- RESULT: %s\n' "$result"
    printf -- '- DRIVER-EXIT: %s\n' "$driver_exit"
    printf -- '- Per-invocation *.requests.log files contain method/path only; direct-request.log has no Authorization header.\n'
    printf -- '- verification.json is produced by a separate read-only process using direct GETs and records its sanitized request paths.\n'
  } >"$dir/SUMMARY.md"
}

gate_fail() {
  printf 'ASSERT FAIL %s\n' "$*" >&2
  return 1
}

assert_file_contains() {
  local file="$1" expected="$2"
  grep -F -- "$expected" "$file" >/dev/null || gate_fail "missing $expected in ${file##*/}"
}

assert_engine_exit() {
  local expected="$1" actual="$2" label="$3"
  [[ "$expected" == "$actual" ]] || gate_fail "$label: expected engine exit $expected, got $actual"
}

names_for_leg() {
  local leg="$1" suffix slug
  suffix="${BASHPID}-${RANDOM}"
  # Container names become docker-network DNS aliases; a label above 63
  # characters fails getaddrinfo inside Kibana (live-caught: the long
  # saved-object leg name broke ES resolution).  Use a short leg slug.
  slug="$(printf '%s' "$leg" | cksum | cut -d' ' -f1)"
  # Do not use cs_init_names here: all resources must advertise this gate's
  # fixed namespace rather than the generic clean-stack prefix.
  CS_SUFFIX="$suffix"
  CS_NETWORK="${NAMESPACE}-${slug}-net-${suffix}"
  CS_ES_CONTAINER="${NAMESPACE}-${slug}-es-${suffix}"
  CS_KB_CONTAINER="${NAMESPACE}-${slug}-kb-${suffix}"
  CS_ES_DATA_VOLUME="${NAMESPACE}-${leg}-esdata-${suffix}"
  CS_KB_DATA_VOLUME="${NAMESPACE}-${leg}-kbdata-${suffix}"
  export CS_SUFFIX CS_NETWORK CS_ES_CONTAINER CS_KB_CONTAINER CS_ES_DATA_VOLUME CS_KB_DATA_VOLUME
}

curl_status() {
  local output="$1" method="$2" url="$3" data="${4:-}" config status
  config="$(mktemp "$CREDENTIAL_ROOT/curl.XXXXXX")"
  chmod 600 "$config"
  printf 'user = "elastic:%s"\n' "$ELASTIC_PASSWORD" >"$config"
  local args=(--silent --show-error --max-redirs 0 --cacert "$CS_CA_FILE"
              --config "$config" --header 'Content-Type: application/json'
              --request "$method" --output "$output" --write-out '%{http_code}')
  [[ -z "$data" ]] || args+=(--data-binary "@$data")
  set +e
  curl "${args[@]}" "$url"
  status=$?
  set -e
  rm -f -- "$config"
  return "$status"
}

start_stack() {
  local dir="$1" status
  cs_prepare_tls "$dir"
  cs_create_network
  cs_create_named_volumes
  cs_start_elasticsearch_with_volume "docker.elastic.co/elasticsearch/elasticsearch:$ES_VERSION" "$(cs_port_mapping '' 9200)"
  ES_URL="https://localhost:$(cs_published_port "$CS_ES_CONTAINER" 9200/tcp)"
  export ES_URL CS_ES_URL="$ES_URL"
  cs_wait_for_elasticsearch "$ES_URL" elastic "$ELASTIC_PASSWORD" "$dir/es-health.json" || {
    cs_timeout_with_logs Elasticsearch "$CS_ES_CONTAINER"; return 1;
  }
  status="$(curl_status "$dir/kibana-password.json" POST "$ES_URL/_security/user/kibana_system/_password" \
    <(printf '{"password":"%s"}' "$ELASTICSEARCH_PASSWORD"))"
  cs_status_is_success "$status" || gate_fail "could not set kibana_system password"
  cs_start_kibana_with_volume "docker.elastic.co/kibana/kibana:$KB_VERSION" "$(cs_port_mapping '' 5601)"
  KB_URL="https://localhost:$(cs_published_port "$CS_KB_CONTAINER" 5601/tcp)"
  export KB_URL CS_KIBANA_URL="$KB_URL"
  cs_wait_for_kibana "$KB_URL" elastic "$ELASTIC_PASSWORD" "$dir/kibana-health.json" || {
    cs_timeout_with_logs Kibana "$CS_ES_CONTAINER" "$CS_KB_CONTAINER"; return 1;
  }
}

write_credentials() {
  local password="$ELASTIC_PASSWORD"
  ADMIN_CREDENTIALS_FILE="$(mktemp "$CREDENTIAL_ROOT/admin-credentials.XXXXXX.toml")"
  password="${password//\\/\\\\}"
  password="${password//\"/\\\"}"
  umask 077
  printf '[elasticsearch]\nusername = "elastic"\npassword = "%s"\n' "$password" >"$ADMIN_CREDENTIALS_FILE"
  chmod 600 "$ADMIN_CREDENTIALS_FILE"
}

run_engine() {
  local dir="$1" marker="$2" label="$3"
  shift 3
  local output="$dir/${label}.out" audit="$dir/${label}.requests.log" rc
  set +e
  env RIGSIGNAL_HTTP_AUDIT_LOG="$audit" "$@" \
    python3 "$REPO_ROOT/tools/install_assets.py" \
      --assets-only --unsafe-test-injection \
      --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" \
      --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" \
      --admin-credentials-file "$ADMIN_CREDENTIALS_FILE" \
      --agent-binary "$AGENT_BINARY" --profile user --assets-marker "$marker" \
      >"$output" 2>&1
  rc=$?
  set -e
  sanitize_text <"$output" >"$output.sanitized"
  mv "$output.sanitized" "$output"
  printf '%s\n' "$rc" >"$dir/${label}.exit"
  return "$rc"
}

verify_live() {
  local dir="$1" marker="$2" state="$3" pm="$4" mode="${5:-full}"
  local args=(--bundle "$BUNDLE" --es-url "$ES_URL" --kb-url "$KB_URL" --ca-file "$CS_CA_FILE"
              --record "$marker" --record-state "$state"
              --record-pm "$pm" --out "$dir/verification.json")
  if [[ "$mode" == partial ]]; then
    args+=(--allow-absent --minimum-present 1)
  else
    args+=(--no-unexpected-ids)
  fi
  RIGSIGNAL_ASSETS_RECOVERY_PASSWORD="$ELASTIC_PASSWORD" python3 "$VERIFY" "${args[@]}"
}

target_for() {
  local kind="$1" target
  if ! target="$(python3 "$VERIFY" --bundle "$BUNDLE" --print-target "$kind")" \
      || [[ -z "$target" || "$target" == *$'\n'* ]]; then
    gate_fail "could not resolve one $kind hook target"
    return 1
  fi
  printf '%s\n' "$target"
}

direct_foreign_create() {
  local dir="$1" kind="$2" target="$3" name body status endpoint encoded
  name="${target##*/}"
  encoded="${name//%40/@}"
  case "$kind" in
    pipeline)
      endpoint="$ES_URL/_ingest/pipeline/$encoded"
      body="$dir/foreign-pipeline.json"
      printf '{"description":"foreign pre-PUT race fixture","processors":[]}' >"$body"
      ;;
    role)
      endpoint="$ES_URL/_security/role/$encoded"
      body="$dir/foreign-role.json"
      printf '{"cluster":[],"indices":[],"applications":[],"run_as":[],"metadata":{}}' >"$body"
      ;;
    *) gate_fail "unknown direct fixture kind $kind"; return 1 ;;
  esac
  printf 'PUT %s (foreign %s fixture; Authorization redacted)\n' "${endpoint#"$ES_URL"}" "$kind" >>"$dir/direct-requests.log"
  status="$(curl_status "$dir/foreign-${kind}-response.json" PUT "$endpoint" "$body")"
  cs_status_is_success "$status" || gate_fail "foreign $kind create returned HTTP $status"
}

assert_detector_halt() {
  local dir kind target marker audit last_write
  dir="$1"; kind="$2"; target="$3"; marker="$4"; audit="$dir/detector.requests.log"
  assert_file_contains "$dir/detector.out" 'partial-remote-possible'
  [[ -f "${marker}.diagnostic.json" ]] || gate_fail "$kind detector evidence missing"
  jq -e --arg target "$target" --arg detector "$5" \
    '.target == $target and .detector == $detector' "${marker}.diagnostic.json" >/dev/null ||
    gate_fail "$kind detector evidence is not specific"
  # The detector's own guarded PUT is the final mutation.  Reads after it are
  # expected; any later mutating audit line is a release-blocking regression.
  last_write="$(grep -nE '^(POST|PUT|DELETE) ' "$audit" | tail -n 1 | cut -d: -f1 || true)"
  [[ -n "$last_write" ]] || gate_fail "$kind detector audit has no mutation"
  if tail -n "+$((last_write + 1))" "$audit" | grep -qE '^(POST|PUT|DELETE) '; then
    gate_fail "$kind wrote after detector-positive boundary"
  fi
}

leg_fresh() {
  local dir marker rc
  dir="$1"; marker="$dir/marker/assets-marker.json"
  mkdir -p "$dir/marker"
  write_credentials
  if run_engine "$dir" "$marker" fresh; then rc=0; else rc=$?; fi
  assert_engine_exit 0 "$rc" fresh
  verify_live "$dir" "$marker" installed false
  jq -e '.state == "installed" and (.targets|length == 66) and (.progress|not)' "$marker" >"$dir/record-inspection.json"
}

leg_saved_object_recovery() {
  local dir marker target rc
  dir="$1"; marker="$dir/marker/assets-marker.json"
  mkdir -p "$dir/marker"; write_credentials
  target="$(target_for saved-object)"
  if run_engine "$dir" "$marker" crash "RIGSIGNAL_TEST_HALT_AT=after-target-verification:$target"; then rc=0; else rc=$?; fi
  assert_engine_exit 4 "$rc" saved-object-injected-failure
  verify_live "$dir" "$marker" installing true partial
  cp "$marker" "$dir/installing-record.json"
  if run_engine "$dir" "$marker" resume; then rc=0; else rc=$?; fi
  assert_engine_exit 0 "$rc" saved-object-resume
  verify_live "$dir" "$marker" installed false
}

leg_dashboard_member_recovery() {
  local dir marker target rc
  dir="$1"; marker="$dir/marker/assets-marker.json"
  mkdir -p "$dir/marker"; write_credentials
  target="$(target_for dashboard-member)"
  if run_engine "$dir" "$marker" dashboard-member "RIGSIGNAL_TEST_HALT_AT=after-target-verification:$target"; then rc=0; else rc=$?; fi
  assert_engine_exit 4 "$rc" dashboard-member-injected-failure
  verify_live "$dir" "$marker" installing true partial
  cp "$marker" "$dir/dashboard-member-installing-record.json"
  if run_engine "$dir" "$marker" dashboard-member-resume; then rc=0; else rc=$?; fi
  assert_engine_exit 0 "$rc" dashboard-member-resume
  verify_live "$dir" "$marker" installed false
}

leg_detector() {
  local dir kind detector marker target pid rc
  dir="$1"; kind="$2"; detector="$3"; marker="$dir/marker/assets-marker.json"
  mkdir -p "$dir/marker"; write_credentials
  target="$(target_for "$kind")"
  env RIGSIGNAL_HTTP_AUDIT_LOG="$dir/detector.requests.log" \
      RIGSIGNAL_TEST_PAUSE_AT="before-transaction-put:$target" \
      RIGSIGNAL_TEST_PAUSE_SENTINEL="$dir/resume" \
      python3 "$REPO_ROOT/tools/install_assets.py" \
        --assets-only --unsafe-test-injection --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" \
        --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" \
        --admin-credentials-file "$ADMIN_CREDENTIALS_FILE" --agent-binary "$AGENT_BINARY" \
        --profile user --assets-marker "$marker" >"$dir/detector.out" 2>&1 &
  pid=$!
  for _ in $(seq 1 200); do
    if grep -F "RIGSIGNAL_TEST_PAUSE_REACHED before-transaction-put" "$dir/detector.out" >/dev/null 2>&1; then break; fi
    sleep 0.05
  done
  grep -F "RIGSIGNAL_TEST_PAUSE_REACHED before-transaction-put" "$dir/detector.out" >/dev/null || {
    kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; gate_fail "$kind did not reach pre-PUT pause"; return 1;
  }
  direct_foreign_create "$dir" "$kind" "$target"
  # ES pipeline timestamps are millisecond-granular; make the foreign create
  # and the resumed engine PUT observably distinct for this positive detector
  # reproduction rather than relying on scheduler timing.
  sleep 0.05
  : >"$dir/resume"
  set +e; wait "$pid"; rc=$?; set -e
  sanitize_text <"$dir/detector.out" >"$dir/detector.out.sanitized"; mv "$dir/detector.out.sanitized" "$dir/detector.out"
  printf '%s\n' "$rc" >"$dir/detector.exit"
  assert_engine_exit 4 "$rc" "$kind-detector"
  verify_live "$dir" "$marker" installing true partial
  assert_detector_halt "$dir" "$kind" "$target" "$marker" "$detector"
}

run_leg() {
  local name="$1" function_name="$2" driver_exit result dir
  shift 2
  dir="$RUN_ROOT/$name"; mkdir -p "$dir"
  set +e
  (
    # The outer status capture must not turn off errexit for the leg itself:
    # a failed target lookup or stack readiness check must prevent a malformed
    # injection invocation against a partially ready stack.
    set -e
    CS_KEEP="$KEEP"; CS_DRY_RUN=0; export CS_KEEP CS_DRY_RUN
    names_for_leg "$name"
    ELASTIC_PASSWORD="rgs033-$(openssl rand -hex 16)"
    ELASTICSEARCH_PASSWORD="rgs033-$(openssl rand -hex 16)"
    export ELASTIC_PASSWORD ELASTICSEARCH_PASSWORD
    trap cs_cleanup EXIT
    start_stack "$dir"
    "$function_name" "$dir" "$@"
  ) >"$dir/driver.out" 2>&1
  driver_exit=$?
  set -e
  sanitize_text <"$dir/driver.out" >"$dir/driver.out.sanitized"; mv "$dir/driver.out.sanitized" "$dir/driver.out"
  if [[ "$driver_exit" == 0 ]]; then result=PASS; else result=FAIL; fi
  write_leg_summary "$dir" "$name" "$result" "$driver_exit"
  SUMMARY_ROWS+=("$name|$result|$driver_exit")
  printf 'LEG %-30s %s (driver %s)\n' "$name" "$result" "$driver_exit"
}

write_summary() {
  local row name result rc overall=0
  {
    printf '# RigSignal assets recovery gate — %s\n\n' "$NAMESPACE"
    printf 'ES/Kibana: %s/%s.  This evidence is intentionally host-run.\n\n' "$ES_VERSION" "$KB_VERSION"
    printf '| Leg | Result | DRIVER-EXIT |\n|---|---|---:|\n'
    for row in "${SUMMARY_ROWS[@]}"; do
      IFS='|' read -r name result rc <<<"$row"
      printf '| %s | %s | %s |\n' "$name" "$result" "$rc"
      [[ "$result" == PASS ]] || overall=1
    done
    printf '\nDRIVER-EXIT: %s\n' "$overall"
  } >"$RUN_ROOT/SUMMARY.md"
  return "$overall"
}

cs_require_tools bash curl docker jq openssl python3 cargo sed
[[ -x "$VERIFY" || -f "$VERIFY" ]] || { printf 'error: verifier missing\n' >&2; exit 1; }
RUN_ROOT="$(mktemp -d "/tmp/${NAMESPACE}.XXXXXX")"
chmod 700 "$RUN_ROOT"
CREDENTIAL_ROOT="$(mktemp -d "/tmp/${NAMESPACE}-credentials.XXXXXX")"
chmod 700 "$CREDENTIAL_ROOT"
trap 'rm -rf -- "$CREDENTIAL_ROOT"' EXIT
if [[ -n "$BUNDLE_INPUT" ]]; then
  BUNDLE="$BUNDLE_INPUT"
else
  BUNDLE="$RUN_ROOT/rigsignal-assets.tar.gz"
  python3 "$REPO_ROOT/tools/build_asset_bundle.py" --source-commit "$(git -C "$REPO_ROOT" rev-parse HEAD)" --output "$BUNDLE"
fi
if [[ -n "${CLEAN_STACK_AGENT_BINARY:-}" ]]; then
  AGENT_BINARY="$CLEAN_STACK_AGENT_BINARY"
else
  cargo build --manifest-path "$REPO_ROOT/src/Cargo.toml" --locked
  AGENT_BINARY="$REPO_ROOT/target/debug/rigsignal-agent"
fi
[[ -x "$AGENT_BINARY" ]] || { printf 'error: rigsignal agent is not executable\n' >&2; exit 1; }

printf 'Evidence directory: %s\n' "$RUN_ROOT"
run_leg fresh-assets-only leg_fresh
run_leg saved-object-crash-and-recovery leg_saved_object_recovery
run_leg partial-within-one-dashboard-import leg_dashboard_member_recovery
run_leg pipeline-pre-put-detector leg_detector pipeline 'created<modified'
run_leg role-pre-put-detector leg_detector role 'created:false'
if write_summary; then
  printf 'ASSETS-RECOVERY-GATE-PASS evidence=%s\n' "$RUN_ROOT"
  exit 0
fi
printf 'ASSETS-RECOVERY-GATE-FAIL evidence=%s\n' "$RUN_ROOT" >&2
exit 1
