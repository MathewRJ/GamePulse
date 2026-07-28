#!/usr/bin/env bash
# Manual Fleet-coexistence scenario gate.  It intentionally has no CI caller.
# shellcheck disable=SC2329 # lib.sh callbacks are invoked by the clean-stack framework.
set -euo pipefail
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/clean-stack/lib.sh
source "$SCRIPT_DIR/lib.sh"

ES_VERSION='' KB_VERSION='' BUNDLE='' PREDECESSOR_MANIFEST='' KEEP=0
declare -a LEGS=()
usage() { printf '%s\n' 'Usage: fleet-coexist-gate.sh --es-version 9.4.3|9.4.4 [--kb-version VERSION] --leg a..p [--bundle PATH] [--predecessor-manifest PATH] [--all]' 'Solo-screen subset: fleet legs a/b/i/n/o/p plus adoption leg 1.' >&2; }
version() { [[ "$1" =~ ^9\.4\.[34]$ ]]; }
fail() { printf 'ASSERT FAIL %s\n' "$*" >&2; return 1; }
while (($#)); do case "$1" in
  --es-version) ES_VERSION="${2:-}"; shift 2 ;; --kb-version) KB_VERSION="${2:-}"; shift 2 ;;
  --bundle) BUNDLE="${2:-}"; shift 2 ;; --predecessor-manifest) PREDECESSOR_MANIFEST="${2:-}"; shift 2 ;; --leg) LEGS+=("${2:-}"); shift 2 ;;
  --all) LEGS=(a b c d e f g h i j k l m n o p); shift ;; --keep) KEEP=1; shift ;;
  -h|--help) usage; exit 0 ;; *) usage; exit 2 ;; esac; done
[[ -n "$ES_VERSION" ]] || { usage; exit 2; }; KB_VERSION="${KB_VERSION:-$ES_VERSION}"
version "$ES_VERSION" && version "$KB_VERSION" && [[ "$ES_VERSION" == "$KB_VERSION" ]] || { usage; exit 2; }
((${#LEGS[@]})) || { usage; exit 2; }; [[ -z "$BUNDLE" || -f "$BUNDLE" ]] || { fail '--bundle is not a file'; exit 2; }
[[ -z "$PREDECESSOR_MANIFEST" || -f "$PREDECESSOR_MANIFEST" ]] || { fail '--predecessor-manifest is not a file'; exit 2; }
: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD must be set}"; : "${ELASTICSEARCH_PASSWORD:?ELASTICSEARCH_PASSWORD must be set}"; : "${CLEAN_STACK_AGENT_BINARY:?CLEAN_STACK_AGENT_BINARY must be set}"
cs_require_tools bash curl docker jq openssl python3 sha256sum
RUN_DIR="$(mktemp -d)"
cleanup() { local rc="$?"; if [[ "$KEEP" != 1 ]]; then cs_cleanup || true; rm -rf "$RUN_DIR"; else printf 'KEEP: stack alive, RUN_DIR=%s\n' "$RUN_DIR" >&2; fi; return "$rc"; }
trap cleanup EXIT

start_stack() {
  CS_RUN_DIR="$RUN_DIR"; export CS_RUN_DIR
  cs_init_names "fleet-coexist-$(cs_new_suffix)"
  # Fresh TLS material per stack: container names change on every start and
  # cs_prepare_tls's mkdir is not idempotent (leg_c restarts in a loop).
  rm -rf "$RUN_DIR/tls"
  cs_prepare_tls "$RUN_DIR"
  cs_create_network
  cs_start_elasticsearch "docker.elastic.co/elasticsearch/elasticsearch:$ES_VERSION" "$(cs_port_mapping '' 9200)"
  ES_URL="https://localhost:$(cs_published_port "$CS_ES_CONTAINER" 9200/tcp)"
  cs_wait_for_elasticsearch "$ES_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/es.json"
  curl --silent --show-error --fail --user "elastic:$ELASTIC_PASSWORD" -H 'Content-Type: application/json' -X POST --data "{\"password\":\"$ELASTICSEARCH_PASSWORD\"}" "$ES_URL/_security/user/kibana_system/_password" >/dev/null
  cs_start_kibana "docker.elastic.co/kibana/kibana:$KB_VERSION" "$(cs_port_mapping '' 5601)"
  KB_URL="https://localhost:$(cs_published_port "$CS_KB_CONTAINER" 5601/tcp)"
  cs_wait_for_kibana "$KB_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/kb.json"
}
api() { curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" -H 'Content-Type: application/json' -X "$1" "${ES_URL}$2" "${@:3}"; }
build_bundle() { if [[ -z "$BUNDLE" ]]; then BUNDLE="$RUN_DIR/assets.tar.gz"; python3 "$REPO_ROOT/tools/build_asset_bundle.py" --source-commit "$(git -C "$REPO_ROOT" rev-parse HEAD)" --output "$BUNDLE"; fi; BUNDLE_SHA256="$(sha256sum "$BUNDLE" | awk '{print $1}')"; IMPLEMENTING_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"; }
write_admin() { umask 077; printf '[elasticsearch]\nusername = "elastic"\npassword = "%s"\n' "$ELASTIC_PASSWORD" >"$RUN_DIR/admin.toml"; chmod 600 "$RUN_DIR/admin.toml"; }
_installer() {
  local -a args=(--bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment" --ownership-profile fleet-coexist)
  local out rc attempt
  # Adoption flag only on the first install per root: a rerun with committed
  # state correctly refuses adoption_flag_state_present (A4 flag-misuse guard).
  [[ -f "$RUN_DIR/enrollment/state.json" ]] || args+=(--adopt-existing-w1-stream)
  [[ -z "$PREDECESSOR_MANIFEST" ]] || args+=(--predecessor-manifest "$PREDECESSOR_MANIFEST")
  [[ "${RIGSIGNAL_TEST_EXTERNAL_WRITE:-}" != 1 && -z "${RIGSIGNAL_TEST_PAUSE_AT:-}" ]] || args+=(--unsafe-test-injection)
  api GET '/_cluster/health?wait_for_events=languid&wait_for_no_initializing_shards=true&timeout=30s' >/dev/null || true
  for attempt in 1 2 3; do
    if out="$(python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" "${args[@]}" 2>&1)"; then rc=0; else rc=$?; fi
    if [[ "$rc" == 0 ]]; then printf '%s\n' "$out"; return 0; fi
    if [[ "${out##*$'\n'}" == 'install refused: cluster_health' && "$attempt" != 3 ]]; then sleep 10; continue; fi
    printf '%s\n' "$out"; return "$rc"
  done
  return 1
}
installer() { _installer || fail 'installer failed'; }
rollback() { python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --rollback "$RUN_DIR/enrollment"; }
seed_m1_and_streams() {
  local body="$RUN_DIR/m1-anchor-body.json" name
  api PUT '/_component_template/logs-rigsignal.diagnosis-mappings' --data-binary "@$REPO_ROOT/elastic/component-templates/logs-rigsignal.diagnosis-mappings.json" >/dev/null
  api PUT '/_index_template/logs-rigsignal.diagnosis' --data-binary "@$REPO_ROOT/elastic/index-templates/logs-rigsignal.diagnosis.json" >/dev/null
  api PUT '/_index_template/logs-rigsignal.stream' --data-binary "@$REPO_ROOT/elastic/index-templates/logs-rigsignal.stream.json" >/dev/null
  api PUT '/_index_template/metrics-rigsignal.profiles' --data-binary "@$REPO_ROOT/elastic/index-templates/metrics-rigsignal.profiles.json" >/dev/null
  for name in logs-rigsignal.diagnosis-default logs-rigsignal.events-default logs-rigsignal.stream-default metrics-rigsignal.audio-default metrics-rigsignal.cpu-default metrics-rigsignal.ebpf-default metrics-rigsignal.ebpf_thread-default metrics-rigsignal.frame-default metrics-rigsignal.gpu-default metrics-rigsignal.memory-default metrics-rigsignal.network-default metrics-rigsignal.power-default metrics-rigsignal.profiles-default metrics-rigsignal.session-default metrics-rigsignal.storage-default metrics-rigsignal.stream_client-default; do api PUT "/_data_stream/$name" >/dev/null; done
  api POST '/metrics-rigsignal.audio-default/_rollover' >/dev/null
  api POST '/metrics-rigsignal.ebpf-default/_rollover' >/dev/null
  jq -c '.hits.hits[] | {_id,_source}' "$REPO_ROOT/fixtures/fleet-owner-cluster/m1-anchors.json" | while read -r body; do
    name="$(jq -r '._id' <<<"$body")"; jq '._source' <<<"$body" >"$RUN_DIR/m1-$name.json"
    api POST "/logs-rigsignal.diagnosis-default/_create/$name?refresh=wait_for" --data-binary "@$RUN_DIR/m1-$name.json" >/dev/null
  done
}
seed() { ES_URL="$ES_URL" ELASTIC_PASSWORD="$ELASTIC_PASSWORD" "$SCRIPT_DIR/fleet-coexist-seed.sh" "$@"; [[ "${1:-}" == --upgrade ]] || seed_m1_and_streams; }
setup() { start_stack; build_bundle; write_admin; seed; }
marker_check() { api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/marker.json"; jq -e '[.component_templates[0].component_template._meta.applied_owned_assets[]|[.kind,.name]] as $a | [.component_templates[0].component_template._meta.verified_external_assets[]|[.kind,.name]] as $e | ($a|length)==16 and ($e|length)==39 and (($a+$e)|unique|length)==55 and ([.component_templates[0].component_template._meta.applied_owned_assets[].action] | all(. == "create" or . == "update" or . == "import" or . == "noop")) and ([.component_templates[0].component_template._meta.verified_external_assets[].compatibility_projection_sha256] | all(type == "string" and test("^[0-9a-f]{64}$")))' "$RUN_DIR/marker.json" >/dev/null; }
expect_refusal() { local code="$1"; shift; if "$@" >"$RUN_DIR/refusal.out" 2>&1; then fail "$code unexpectedly succeeded"; fi; grep -E "^install refused: ${code}(:|$)" "$RUN_DIR/refusal.out" >/dev/null || { cat "$RUN_DIR/refusal.out" >&2; fail "$code wrong refusal"; }; }
simulation_canary() {
  tar -xOf "$BUNDLE" elastic/index-templates/metrics-rigsignal.cpu.json >"$RUN_DIR/canary-template.json"
  python3 - "$REPO_ROOT" "$RUN_DIR/canary-template.json" "$RUN_DIR/canary-body.json" "$RUN_DIR/canary-index" <<'PY'
import json, pathlib, sys
sys.path.insert(0, sys.argv[1] + "/tools")
import asset_adapters
template = json.load(open(sys.argv[2]))
body, index = asset_adapters.synthetic_simulation_template(template, "gate-canary")
json.dump(body, open(sys.argv[3], "w"), sort_keys=True)
pathlib.Path(sys.argv[4]).write_text(index)
PY
  api POST "/_index_template/_simulate_index/$(<"$RUN_DIR/canary-index")" --data-binary "@$RUN_DIR/canary-body.json" >"$RUN_DIR/canary-bundle-outcome.json"
  api POST '/_index_template/_simulate_index/metrics-rigsignal.cpu-rigsignal-a5-probe' >"$RUN_DIR/canary-live-outcome.json"
  python3 - "$REPO_ROOT" "$RUN_DIR/canary-bundle-outcome.json" "$RUN_DIR/canary-live-outcome.json" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1] + "/tools")
import asset_adapters
bundle = asset_adapters.simulation_outcome(json.load(open(sys.argv[2])))
live = asset_adapters.simulation_outcome(json.load(open(sys.argv[3])))
if bundle == live:
    raise SystemExit("simulate canary did not differ; equality oracle regression is undetected")
PY
}
external_hashes_check() {
  local kind name path n=0 list="$RUN_DIR/external-hash-inputs.tsv"
  : >"$list"
  while IFS=$'\t' read -r kind name; do
    case "$kind" in component_templates) path="/_component_template/$name";; index_templates) path="/_index_template/$name";; pipelines) path="/_ingest/pipeline/$name";; *) fail "unknown external kind $kind";; esac
    api GET "$path" >"$RUN_DIR/external-body-$n.json"
    printf '%s\t%s\t%s\n' "$kind" "$name" "$RUN_DIR/external-body-$n.json" >>"$list"
    n=$((n + 1))
  done < <(jq -r '.component_templates[0].component_template._meta.verified_external_assets[] | [.kind,.name] | @tsv' "$RUN_DIR/marker.json")
  python3 - "$REPO_ROOT" "$RUN_DIR/marker.json" "$list" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1] + "/tools")
import asset_adapters
marker = json.load(open(sys.argv[2]))["component_templates"][0]["component_template"]["_meta"]
pins = {(x["kind"], x["name"]): x["compatibility_projection_sha256"] for x in marker["verified_external_assets"]}
for line in open(sys.argv[3]):
    kind, name, path = line.rstrip("\n").split("\t")
    actual = asset_adapters.sha256(asset_adapters.compatibility_projection(kind, json.load(open(path))))
    if pins[(kind, name)] != actual:
        raise SystemExit("marker external hash mismatch: " + kind + "/" + name)
PY
}
# Exactly the 39 external asset paths (13 components @package, 13 stream index
# templates, 13 pipelines -0.5.0). Owned assets that share the metrics- prefix
# (metrics-rigsignal.profiles) or logs- prefix (logs-rigsignal.stream,
# diagnosis chain) must NOT match: the installer legitimately writes them.
EXTERNAL_STREAMS='(logs-rigsignal\.events|metrics-rigsignal\.(audio|cpu|ebpf|ebpf_thread|frame|gpu|memory|network|power|session|storage|stream_client))'
EXTERNAL_WRITE_RE="^(PUT|POST|DELETE) (/_component_template/${EXTERNAL_STREAMS}(@|%40)package|/_index_template/${EXTERNAL_STREAMS}|/_ingest/pipeline/${EXTERNAL_STREAMS}-0\.5\.0)\$"
external_audit_clean() { local log="$1"; ! grep -E "$EXTERNAL_WRITE_RE" "$log"; }
installer_unresolved() { RIGSIGNAL_TEST_UNRESOLVED_ASSET=1 _installer; }
installer_bad_health() { RIGSIGNAL_TEST_CLUSTER_HEALTH=red _installer; }
installer_bad_ilm() { RIGSIGNAL_TEST_ILM_DELETE_PHASE=1 _installer; }

# Generate a set-shaped manifest from the same projection functions the
# installer uses.  The Workflow-side generator is the operator tool; keeping
# this tiny in-gate version local makes the chain leg self-contained.
make_predecessor_manifest() {
  local output="$1"
  python3 - "$REPO_ROOT" "$BUNDLE" "$output" <<'PY'
import hashlib, sys
from pathlib import Path
repo, bundle_path, output = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo / "tools"))
import install_assets as install

bundle = install.load_bundle(bundle_path)
assets = {}
for asset in bundle.assets:
    if (asset.kind, asset.name) in install._EXTERNAL_ASSET_KEYS:
        continue
    if asset.kind == "dashboard":
        absent = hashlib.sha256(install.jcs([[kind, ident, "ABSENT"] for kind, ident, _ in install._dashboard_expected_objects(asset)])).hexdigest()
        current = hashlib.sha256(install.jcs([[kind, ident, body] for kind, ident, body in install._dashboard_expected_objects(asset)])).hexdigest()
    else:
        absent = install.asset_adapters.dashboard_absent_hash()
        current = install.asset_adapters.sha256(install.asset_adapters.get_projection(asset.kind, install.parse_json(asset.data, asset.path)))
    entry = {"id": "gate-current-set", "approved_sha256": sorted({absent, current})}
    if (asset.kind, asset.name) == ("pipelines", "logs-rigsignal.stream@pipeline"):
        entry["comment"] = "Includes post-install body for retained-pipeline retry."
    assets[asset.kind + "/" + asset.name] = entry
Path(output).write_bytes(install.jcs({"version": 1, "assets": dict(sorted(assets.items()))}) + b"\n")
PY
}

owned_template_matches_bundle() {
  local name="$1"
  local live="$RUN_DIR/$name-live.json"
  api GET "/_index_template/$name" >"$live"
  python3 - "$REPO_ROOT" "$BUNDLE" "$name" "$live" <<'PY'
import sys
from pathlib import Path
repo, bundle_path, name, live_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo / "tools"))
import install_assets as install
bundle = install.load_bundle(bundle_path)
asset = next(item for item in bundle.assets if item.kind == "index_templates" and item.name == str(name))
expected = install.asset_adapters.get_projection(asset.kind, install.parse_json(asset.data, asset.path))
live = install.asset_adapters.get_projection(asset.kind, install.parse_json(live_path.read_bytes(), str(live_path)))
if install.jcs(expected) != install.jcs(live):
    raise SystemExit("owned template differs from bundle projection: " + str(name))
PY
}

stream_backing_snapshot() { api GET "/_data_stream/$1" | jq -S '[.data_streams[0].indices[]|{index_name,index_uuid}]'; }

# Dashboard-origin legs use a deliberately separate fixture helper so the
# old/new identity fixtures cannot bleed into the Fleet owner seeder.
origin_seed() { KB_URL="$KB_URL" ELASTIC_PASSWORD="$ELASTIC_PASSWORD" ES_URL="${ES_URL:-}" CS_CA_FILE="${CS_CA_FILE:-}" "$SCRIPT_DIR/dashboard-origin-seed.sh" --bundle "$BUNDLE" "$@"; }
origin_reset() { cs_cleanup || true; rm -rf "$RUN_DIR/enrollment" "$RUN_DIR/default-enrollment"; setup; }
kb_get() { local space="$1" path="$2"; curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" -H 'kbn-xsrf: true' "${KB_URL}$([[ "$space" == default ]] || printf '/s/%s' "$space")$path"; }
space_absent() { ! kb_get "$1" "/api/spaces/space/$1" >/dev/null 2>&1; }
origin_refusal() { local reason="$1"; shift; if "$@" >"$RUN_DIR/origin-refusal.out" 2>&1; then fail "origin refusal unexpectedly succeeded"; fi; grep -E '^install refused: saved_object_topology_conflict(:|$)' "$RUN_DIR/origin-refusal.out" >/dev/null || { cat "$RUN_DIR/origin-refusal.out" >&2; fail 'wrong topology refusal'; }; grep -F "$reason" "$RUN_DIR/origin-refusal.out" >/dev/null || { cat "$RUN_DIR/origin-refusal.out" >&2; fail "topology reason missing: $reason"; }; }
origin_unverifiable() { if "$@" >"$RUN_DIR/origin-refusal.out" 2>&1; then fail 'unverifiable topology unexpectedly succeeded'; fi; grep -E '^install refused: saved_object_topology_unverifiable(:|$)' "$RUN_DIR/origin-refusal.out" >/dev/null || { cat "$RUN_DIR/origin-refusal.out" >&2; fail 'wrong unverifiable refusal'; }; }
origin_export() { origin_seed export "$1" "$2"; }
origin_assert_clean_refusal() {
  space_absent rigsignal || fail 'topology refusal created rigsignal space'
  [[ ! -e "$RUN_DIR/enrollment" ]] || fail 'topology refusal created any journal/profile/body root'
  if api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/origin-marker.json" 2>&1; then fail 'topology refusal wrote remote marker'; fi
  origin_export default "$RUN_DIR/default-after-refusal.ndjson"
  cmp -s "$RUN_DIR/default-before-refusal.ndjson" "$RUN_DIR/default-after-refusal.ndjson" || fail 'topology refusal changed default objects'
}
origin_dashboard_count() {
  local space="$1" type total=0
  for type in dashboard index-pattern search tag visualization; do
    total=$((total + $(kb_get "$space" "/api/saved_objects/_find?type=$type&per_page=1000" | jq '[.saved_objects[] | select(.id | startswith("rigsignal-pkg-"))] | length')))
  done
  printf '%s\n' "$total"
}
origin_export_legacy() { origin_export "$1" "$2.all"; jq -c 'select(.id | startswith("rigsignal-pkg-") | not)' "$2.all" >"$2"; }
origin_assert_full_accounting() {
  local log="$1"
  sed -n 's/^RIGSIGNAL_DASHBOARD_IMPORT_RESULT //p' "$log" >"$RUN_DIR/import-results.jsonl"
  jq -s '([.[].results[]] | length == 29) and ([.[].results[] | .destinationId_present] | any | not)' "$RUN_DIR/import-results.jsonl" | grep -Fx true >/dev/null || fail 'dashboard import results were not 29 zero-destinationId occurrences'
  [[ "$(origin_dashboard_count rigsignal)" == 15 && "$(origin_dashboard_count default)" == 3 ]] || fail 'dashboard unique-object target split is not 15/3'
  marker_check
}
# Noop-rerun shape (v8 D-9): capture lines are emitted only for non-noop
# dashboard files, so an unchanged rerun must produce ZERO of them while the
# live 15/3 split and marker stay intact (round-12 catch: asserting the
# 29-row install shape on the rerun log).
origin_assert_noop_accounting() {
  local log="$1"
  [[ "$(grep -c '^RIGSIGNAL_DASHBOARD_IMPORT_RESULT ' "$log")" == 0 ]] || fail 'noop rerun emitted dashboard import captures'
  [[ "$(origin_dashboard_count rigsignal)" == 15 && "$(origin_dashboard_count default)" == 3 ]] || fail 'noop rerun changed the 15/3 target split'
  marker_check
}
origin_missing() { ! kb_get "$1" "/api/saved_objects/$2/$3" >/dev/null 2>&1; }
default_installer() {
  local -a args=(--bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/default-enrollment" --adopt-existing-w1-stream)
  [[ -z "${RIGSIGNAL_TEST_PAUSE_AT:-}" ]] || args+=(--unsafe-test-injection)
  python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" "${args[@]}"
}
origin_installer() {
  local -a args=(--bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment" --ownership-profile fleet-coexist)
  [[ -f "$RUN_DIR/enrollment/state.json" ]] || args+=(--adopt-existing-w1-stream)
  [[ -z "${RIGSIGNAL_TEST_PAUSE_AT:-}" ]] || args+=(--unsafe-test-injection)
  python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" "${args[@]}"
}
origin_pause_install() {
  local log="$1"; shift
  rm -f "$RUN_DIR/pause.resume"
  : >"$RUN_DIR/origin-http.log"
  RIGSIGNAL_TEST_PAUSE_AT=after-topology-preflight RIGSIGNAL_TEST_PAUSE_SENTINEL="$RUN_DIR/pause.resume" RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/origin-http.log" "$@" >"$log" 2>&1 &
  ORIGIN_PID=$!
  local n
  for ((n = 0; n < 200; n++)); do grep -Fx 'RIGSIGNAL_TEST_PAUSE_REACHED after-topology-preflight' "$log" >/dev/null 2>&1 && return 0; sleep 0.05; done
  fail 'topology pause hook was not reached'
}
origin_resume() { : >"$RUN_DIR/pause.resume"; wait "$ORIGIN_PID"; }

# Dirty Fleet assets + reinstall/upgrade rehearsal.
leg_a() { setup; installer || fail 'installer failed'; marker_check; cp "$RUN_DIR/marker.json" "$RUN_DIR/marker-before-upgrade.json"; simulation_canary; external_hashes_check; jq -e '[.component_templates[0].component_template._meta.applied_owned_assets[].action] | all(. == "create" or . == "update" or . == "import" or . == "noop") and any(. != "noop")' "$RUN_DIR/marker.json" >/dev/null; seed --upgrade; installer || fail 'installer failed'; marker_check; external_hashes_check; jq -e --slurpfile before "$RUN_DIR/marker-before-upgrade.json" '([.component_templates[0].component_template._meta.verified_external_assets[] | [.kind,.name,.owner_metadata,.live_body_sha256]]) != ([$before[0].component_templates[0].component_template._meta.verified_external_assets[] | [.kind,.name,.owner_metadata,.live_body_sha256]])' "$RUN_DIR/marker.json" >/dev/null || fail 'upgrade did not capture moved owner metadata/live body'; jq -e '[.component_templates[0].component_template._meta.applied_owned_assets[].action] | all(. == "noop")' "$RUN_DIR/marker.json" >/dev/null; api PUT '/_component_template/.fleet_globals-1' --data-binary '{"template":{"settings":{"index.default_pipeline":"rigsignal-a5-dominance-canary"}}}' >/dev/null; expect_refusal 'external asset compatibility' _installer; }

# The real transform inverse is exercised on the running version pair.  The
# fallback is explicitly accepted only for ES rejecting an absent-_meta restore.
transform_get() { api GET '/_transform/rigsignal-game-timeline' >"$1"; }
transform_stats() { api GET '/_transform/rigsignal-game-timeline/_stats' >"$1"; }
transform_pivot_matches_bundle() { jq -e --slurpfile bundle "$RUN_DIR/transform-bundle.json" '(.transforms[0] // .).pivot == $bundle[0].pivot' "$1" >/dev/null; }
transform_meta_absent() { jq -e '((.transforms[0] // .) | has("_meta") | not)' "$1" >/dev/null; }
transform_meta_matches_bundle() { jq -e --slurpfile bundle "$RUN_DIR/transform-bundle.json" '(.transforms[0] // .)._meta == $bundle[0]._meta' "$1" >/dev/null; }
transform_started() { jq -e '(.transforms[0].state // .state) == "started"' "$1" >/dev/null; }
transform_assert_state() { transform_pivot_matches_bundle "$1" || fail "transform pivot changed: $1"; transform_stats "$2"; transform_started "$2" || fail "transform stopped: $2"; }
seed_owned_transform_baseline() {
  local body="$RUN_DIR/owned-transform-baseline.json" stats="$RUN_DIR/owned-transform-baseline-stats.json" attempt
  # This transform is bundle-owned, so its owner-cluster baseline belongs to
  # leg_b rather than the 39-external-asset Fleet seeder.
  api GET '/_data_stream/metrics-rigsignal.session-default' >/dev/null 2>&1 || api PUT '/_data_stream/metrics-rigsignal.session-default' >/dev/null
  jq 'del(._meta)' "$REPO_ROOT/elastic/transforms/rigsignal-game-timeline.json" >"$body"
  api PUT '/_transform/rigsignal-game-timeline' --data-binary "@$body" >/dev/null
  api POST '/_transform/rigsignal-game-timeline/_start' >/dev/null
  for ((attempt = 0; attempt < 30; attempt++)); do
    transform_stats "$stats"
    if transform_started "$stats"; then
      return 0
    fi
    sleep 1
  done
  fail 'owner transform did not reach started state'
}
leg_b() {
  setup
  seed_owned_transform_baseline
  tar -xOf "$BUNDLE" elastic/transforms/rigsignal-game-timeline.json >"$RUN_DIR/transform-bundle.json"
  transform_get "$RUN_DIR/transform-before.json"
  transform_meta_absent "$RUN_DIR/transform-before.json" || fail 'transform _meta was present before apply'
  transform_assert_state "$RUN_DIR/transform-before.json" "$RUN_DIR/transform-before-stats.json"
  installer || fail 'installer failed'
  transform_get "$RUN_DIR/transform-applied.json"
  transform_meta_matches_bundle "$RUN_DIR/transform-applied.json" || fail 'transform _meta did not match bundle after apply'
  transform_assert_state "$RUN_DIR/transform-applied.json" "$RUN_DIR/transform-applied-stats.json"
  rollback 2>&1 | tee -a "$RUN_DIR/leg-b-transform-rollback.log"
  grep -qE '^rollback completed from journaled intents; pipeline retained: in use as default pipeline for adopted stream indices; logs-rigsignal\.stream@pipeline; referencing_indices: \["\.ds-[^"]+"\]$' "$RUN_DIR/leg-b-transform-rollback.log" >/dev/null || fail 'retained adopted-stream pipeline was not reported'
  transform_get "$RUN_DIR/transform-after.json"
  if transform_meta_absent "$RUN_DIR/transform-after.json"; then
    transform_assert_state "$RUN_DIR/transform-after.json" "$RUN_DIR/transform-after-stats.json"
    printf 'leg_b transform restore branch: restored-absent-meta\n'
  else
    grep -Fx 'rollback completed from journaled intents; transform _meta absence could not be restored: verify-only cosmetic drift accepted' "$RUN_DIR/leg-b-transform-rollback.log" >/dev/null || fail 'transform fallback was not reported after failed absence proof'
    transform_meta_matches_bundle "$RUN_DIR/transform-after.json" || fail 'transform fallback left non-cosmetic drift'
    transform_assert_state "$RUN_DIR/transform-after.json" "$RUN_DIR/transform-after-stats.json"
    printf 'leg_b transform restore branch: verify-only-cosmetic-drift\n'
  fi

  # Default-inert installer guard: emulate an ES validation rejection when a
  # version cannot naturally reject the absent-_meta restore request.
  cs_cleanup || true
  rm -rf "$RUN_DIR/enrollment"
  setup
  seed_owned_transform_baseline
  transform_get "$RUN_DIR/transform-fallback-before.json"
  transform_meta_absent "$RUN_DIR/transform-fallback-before.json" || fail 'transform _meta was present before fallback rehearsal'
  transform_assert_state "$RUN_DIR/transform-fallback-before.json" "$RUN_DIR/transform-fallback-before-stats.json"
  RIGSIGNAL_TEST_TRANSFORM_META_RESTORE_REJECT=1 installer || fail 'installer failed'
  jq -e '.intents[] | select(.kind == "transforms") | .verify_only == true and .verify_only_reason == "meta_absent_restore_unproven_preapply"' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'transform pre-apply proof gate did not choose verify-only'
  rollback 2>&1 | tee -a "$RUN_DIR/leg-b-transform-rollback.log"
  grep -qE '^rollback completed from journaled intents; pipeline retained: in use as default pipeline for adopted stream indices; logs-rigsignal\.stream@pipeline; referencing_indices: \["\.ds-[^"]+"\]$' "$RUN_DIR/leg-b-transform-rollback.log" >/dev/null || fail 'retained adopted-stream pipeline was not reported'
  transform_get "$RUN_DIR/transform-verify-only.json"
  transform_meta_matches_bundle "$RUN_DIR/transform-verify-only.json" || fail 'transform verify-only fallback did not retain accepted _meta drift'
  transform_assert_state "$RUN_DIR/transform-verify-only.json" "$RUN_DIR/transform-verify-only-stats.json"; printf 'leg_b transform restore branch: verify-only\n'
}

# Each injected crash is followed by the actual journal rollback, not a unit substitute.
leg_c() {
  local point rc
  for point in after-write-intent dashboard-multipart before-mint-response candidate-write candidate-verify published-state proof-create; do
    cs_cleanup || true
    rm -rf "$RUN_DIR/enrollment"
    start_stack
    build_bundle
    write_admin
    seed
    set +e
    RIGSIGNAL_TEST_CRASH_AT="$point" _installer >"$RUN_DIR/crash-$point.out" 2>&1
    rc=$?
    set -e
    [[ "$rc" == 99 ]] || fail "$point did not crash"
    rollback
  done

  # Default-inert installer guard: inject a tracked-stream rollover immediately
  # after the pre-transaction Fleet snapshot and before any publication point.
  cs_cleanup || true
  rm -rf "$RUN_DIR/enrollment"
  start_stack
  build_bundle
  write_admin
  seed
  for stream in metrics-rigsignal.session-default logs-rigsignal.diagnosis-default; do
    # Fresh root per iteration: the prior iteration's correct mid-flight abort
    # leaves a non-apply_ok journal that refuses transaction_recovery_required
    # before the drift check could even run.
    rm -rf "$RUN_DIR/enrollment"
    set +e
    RIGSIGNAL_TEST_ROLLOVER_AT="after-fleet-snapshot:$stream" _installer >"$RUN_DIR/in-transaction-rollover-$stream.out" 2>&1
    rc=$?
    set -e
    [[ "$rc" != 0 ]] || fail "in-transaction rollover unexpectedly succeeded: $stream"
    grep -Fx 'install failed: fleet stream verification:' "$RUN_DIR/in-transaction-rollover-$stream.out" >/dev/null || fail "in-transaction rollover did not fail closed: $stream"
  done
  [[ ! -e "$RUN_DIR/enrollment/credentials.toml" && ! -e "$RUN_DIR/enrollment/state.json" ]] || fail 'in-transaction rollover wrote publication state'
  if api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/rollover-marker.json" 2>&1; then fail 'in-transaction rollover wrote marker'; fi
  rollback
  jq -e '.rollback_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'in-transaction rollover did not run journaled rollback'
}

# Six live refusal rows; test-only hooks only make an otherwise healthy disposable
# stack present an unrepresentable condition and are inert without their env vars.
leg_d() { setup; installer; expect_refusal omitted_profile_on_coexist env -u RIGSIGNAL_HTTP_AUDIT_LOG python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment"; expect_refusal ownership_profile_mismatch env python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment" --ownership-profile default; expect_refusal ownership_table_unresolved installer_unresolved; expect_refusal cluster_health installer_bad_health; expect_refusal ilm_delete_phase installer_bad_ilm; printf '[elasticsearch]\napi_key = "x"\n' >"$RUN_DIR/api-key.toml"; chmod 600 "$RUN_DIR/api-key.toml"; expect_refusal admin_credential_api_key env CLEAN_STACK_INSTALLER="$REPO_ROOT/tools/install_assets.py" python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/api-key.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/api-key-root" --ownership-profile fleet-coexist --adopt-existing-w1-stream; }

leg_e() { setup; api GET '/_data_stream/logs-rigsignal.events-default' >/dev/null 2>&1 || api PUT '/_data_stream/logs-rigsignal.events-default' >/dev/null; api GET '/_data_stream/logs-rigsignal.events-default' >"$RUN_DIR/rollover-before.json"; installer; api POST '/logs-rigsignal.events-default/_rollover' >/dev/null; api GET '/_data_stream/logs-rigsignal.events-default' >"$RUN_DIR/rollover-after.json"; jq -e '(.data_streams[0].indices|length) > 1' "$RUN_DIR/rollover-after.json" >/dev/null; installer; marker_check; }

# Two completed transactions share one root.  Rolling back N=2 must retain
# N=1's archived proofs and restore N=1's marker rather than deleting it.
leg_i() { setup; api GET '/_data_stream/*rigsignal*' >"$RUN_DIR/streams-before-rollback.json"; jq -e '(.data_streams|length) == 16 and ([.data_streams[].indices[]]|length) == 18' "$RUN_DIR/streams-before-rollback.json" >/dev/null || fail 'pre-rollback stream set is not 16/18'; installer || fail 'installer failed'; marker_check; cp "$RUN_DIR/enrollment/fleet-coexist-journal.json" "$RUN_DIR/transaction-1.json"; cp "$RUN_DIR/marker.json" "$RUN_DIR/marker-1.json"; seed --upgrade; installer || fail 'installer failed'; cp "$RUN_DIR/enrollment/fleet-coexist-journal.json" "$RUN_DIR/transaction-2.json"; rollback 2>&1 | tee "$RUN_DIR/leg-i-rollback.log"; grep -Fx 'rollback completed from journaled intents' "$RUN_DIR/leg-i-rollback.log" >/dev/null || fail 'txn-2 rollback did not complete plainly (pipeline intent is noop in txn 2)'; api GET '/_data_stream/*rigsignal*' >"$RUN_DIR/streams-after-rollback.json"; jq -S '[.data_streams[]|{name,indices:[.indices[]|{index_name,index_uuid}]}]' "$RUN_DIR/streams-before-rollback.json" >"$RUN_DIR/streams-before.canonical"; jq -S '[.data_streams[]|{name,indices:[.indices[]|{index_name,index_uuid}]}]' "$RUN_DIR/streams-after-rollback.json" >"$RUN_DIR/streams-after.canonical"; cmp -s "$RUN_DIR/streams-before.canonical" "$RUN_DIR/streams-after.canonical" || fail 'rollback changed or newly created a RigSignal stream'; jq -e '(.transactions|length) == 1 and .transactions[0].apply_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null; while read -r event_id; do jq -n --arg id "$event_id" '{query:{ids:{values:[$id]}},size:2}' >"$RUN_DIR/proof-query.json"; api POST '/logs-rigsignal.diagnosis-default/_search' --data-binary "@$RUN_DIR/proof-query.json" >"$RUN_DIR/proof-$event_id.json"; jq -e --arg id "$event_id" '.hits.hits | length == 1 and .[0]._id == $id' "$RUN_DIR/proof-$event_id.json" >/dev/null || fail "transaction-1 proof missing: $event_id"; done < <(jq -r '.proofs[].event_id' "$RUN_DIR/transaction-1.json"); while read -r event_id; do jq -n --arg id "$event_id" '{query:{ids:{values:[$id]}},size:2}' >"$RUN_DIR/proof-query.json"; api POST '/logs-rigsignal.diagnosis-default/_search' --data-binary "@$RUN_DIR/proof-query.json" | jq -e '.hits.hits|length == 0' >/dev/null || fail "transaction-2 proof survived rollback: $event_id"; done < <(jq -r '.proofs[].event_id' "$RUN_DIR/transaction-2.json"); api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/marker-restored.json"; jq -e '.component_templates[0].component_template._meta.ownership_profile == "fleet-coexist"' "$RUN_DIR/marker-restored.json" >/dev/null; expect_refusal transaction_already_rolled_back rollback; installer || fail 'reinstall after rollback failed'; }

# Recording transport audit and its mandatory identical-body external PUT negative control.
leg_f() { setup; : >"$RUN_DIR/audit.log"; RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/audit.log" installer || fail 'installer failed'; external_audit_clean "$RUN_DIR/audit.log" || fail 'external write audit saw a write'; : >"$RUN_DIR/audit-negative.log"; RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/audit-negative.log" RIGSIGNAL_TEST_EXTERNAL_WRITE=1 installer || true; grep -E "$EXTERNAL_WRITE_RE" "$RUN_DIR/audit-negative.log" >/dev/null || fail 'external write audit negative control did not fire'; }

leg_g() { setup; installer; marker_check; jq -e '[.component_templates[0].component_template._meta|has("installed_assets")|not]' "$RUN_DIR/marker.json" >/dev/null; }
leg_h() { build_bundle; printf 'GATE ARTIFACT commit=%s bundle_sha256=%s\n' "$IMPLEMENTING_COMMIT" "$BUNDLE_SHA256"; }

# J: W-B refuses every origin/literal topology collision before either local or
# remote mutation.  Each variant is deliberately a fresh stack/root.
leg_j() {
  setup
  origin_export default "$RUN_DIR/default-before-refusal.ndjson"
  origin_seed new-all donor
  origin_refusal literal_id_exists_elsewhere _installer
  origin_assert_clean_refusal
  origin_seed delete-all donor
  _installer >"$RUN_DIR/leg-j-rerun.log" 2>&1 || fail 'Leg-J clean rerun did not succeed'
  origin_assert_full_accounting "$RUN_DIR/leg-j-rerun.log"

  origin_reset
  origin_export default "$RUN_DIR/default-before-refusal.ndjson"
  # D-2: this is intentionally the only seed.  Reason matching is mandatory.
  origin_seed alias donor dashboard rigsignal-pkg-engine
  if _installer >"$RUN_DIR/leg-j-alias.out" 2>&1; then fail 'alias-only seed unexpectedly installed'; fi
  grep -E '^install refused: saved_object_topology_conflict(:|$)' "$RUN_DIR/leg-j-alias.out" >/dev/null || fail 'alias-only seed had wrong token'
  if ! grep -E '^install refused: saved_object_topology_conflict: [^:]+: alias_match space=donor$' "$RUN_DIR/leg-j-alias.out" >/dev/null; then
    api POST '/.kibana*/_search' --data-binary '{"query":{"term":{"type":"legacy-url-alias"}}}' >"$RUN_DIR/leg-j-alias-fallback.json"
    fail 'alias-only refusal was not exclusively alias_match'
  fi
  origin_assert_clean_refusal

  origin_reset
  origin_seed one donor tag rigsignal-pkg-engine
  # The role must be ES-omnipotent (cluster/indices all) so the installer's
  # pre-W-B read steps (dispatch_clean_root, remote-profile fence) succeed and
  # execution actually REACHES W-B step 0 — but the role is not NAMED
  # superuser, so the _authenticate roles check trips privilege_unverified
  # there (round-7 catch: an empty-privilege role 403'd before W-B and
  # produced a different refusal). Kibana visibility stays partial (default
  # space only) for the filtered-200 evidence capture below.
  api PUT '/_security/role/rigsignal-origin-restricted' --data-binary '{"cluster":["all"],"indices":[{"names":["*"],"privileges":["all"],"allow_restricted_indices":false}],"applications":[{"application":"kibana-.kibana","privileges":["feature_dashboard.read"],"resources":["space:default"]}]}' >/dev/null
  api POST '/_security/user/rigsignal-origin-restricted' --data-binary '{"password":"restricted-password","roles":["rigsignal-origin-restricted"]}' >/dev/null
  curl --silent --show-error --fail --max-redirs 0 --user 'rigsignal-origin-restricted:restricted-password' -H 'kbn-xsrf: true' "$KB_URL/api/spaces/space" >"$RUN_DIR/leg-j-restricted-spaces.json"
  jq -e 'type == "array" and length > 0 and all(.[]; .id != "donor")' "$RUN_DIR/leg-j-restricted-spaces.json" >/dev/null || fail 'restricted user did not produce partial-200 spaces evidence'
  printf '[elasticsearch]\nusername = "rigsignal-origin-restricted"\npassword = "restricted-password"\n' >"$RUN_DIR/restricted.toml"; chmod 600 "$RUN_DIR/restricted.toml"
  # This credential trips privilege_unverified at step 0 (ES role-name check) by
  # design; the captured spaces response above is the filtered-200 path evidence.
  # --adopt-existing-w1-stream mirrors _installer's first-install flag: the
  # harness-seeded W1 stream otherwise refuses adoption_required at
  # dispatch_clean_root, which by design precedes W-B (round-8 catch).
  origin_unverifiable python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/restricted.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment" --ownership-profile fleet-coexist --adopt-existing-w1-stream
  grep -E '^install refused: saved_object_topology_unverifiable: privilege_unverified(:|$)' "$RUN_DIR/origin-refusal.out" >/dev/null || fail 'restricted credential did not refuse privilege_unverified'

  origin_reset
  origin_export default "$RUN_DIR/default-before-refusal.ndjson"
  origin_seed one donor tag rigsignal-pkg-managed
  origin_refusal literal_id_exists_elsewhere _installer
  origin_assert_clean_refusal
}

# K: legacy identities and default-origin derivatives coexist with the new
# namespace; the qualified fault is deliberately the later shared-child file.
leg_k() {
  setup
  origin_seed old-all donor
  origin_seed derivatives
  origin_export donor "$RUN_DIR/leg-k-donor-before.ndjson"; origin_export_legacy default "$RUN_DIR/leg-k-default-before.ndjson"
  _installer >"$RUN_DIR/leg-k-install.log" 2>&1 || fail 'Leg-K new-id install failed'
  origin_assert_full_accounting "$RUN_DIR/leg-k-install.log"
  origin_export donor "$RUN_DIR/leg-k-donor-after.ndjson"; origin_export_legacy default "$RUN_DIR/leg-k-default-after.ndjson"
  cmp -s "$RUN_DIR/leg-k-donor-before.ndjson" "$RUN_DIR/leg-k-donor-after.ndjson" || fail 'Leg-K changed donor legacy seeds'
  cmp -s "$RUN_DIR/leg-k-default-before.ndjson" "$RUN_DIR/leg-k-default-after.ndjson" || fail 'Leg-K changed default derivative seeds'
  _installer >"$RUN_DIR/leg-k-rerun.log" 2>&1 || fail 'Leg-K noop rerun failed'
  origin_assert_noop_accounting "$RUN_DIR/leg-k-rerun.log"

  origin_reset; origin_seed old-all donor; origin_seed derivatives
  if RIGSIGNAL_TEST_CRASH_AT='after-remote-mutation:dashboard/rigsignal-home.ndjson' _installer >"$RUN_DIR/leg-k-fault.log" 2>&1; then fail 'Leg-K qualified later-file fault did not crash'; fi
  grep -F 'rigsignal-home.ndjson' "$RUN_DIR/leg-k-fault.log" >/dev/null || fail 'Leg-K fault did not reach named later dashboard file'
  rollback >"$RUN_DIR/leg-k-rollback.log" 2>&1 || fail 'Leg-K rollback failed'
  # The success line's benign variants continue with '; ...' (transform
  # verify-only / retained pipeline) — K(e)'s crash lands after transforms
  # applied, so the rollback legitimately prints a ';'-suffixed variant.
  # D-11's '(:|$)' form is for REFUSAL tokens, not this line (round-14 catch).
  # 'recovery incomplete' would also match the ';' form, so exclude it
  # explicitly: K(e)'s sweep must leave zero unverified orphans.
  grep -E '^rollback completed from journaled intents(;|$)' "$RUN_DIR/leg-k-rollback.log" >/dev/null || fail 'Leg-K rollback reporter mismatch'
  ! grep -F 'recovery incomplete:' "$RUN_DIR/leg-k-rollback.log" >/dev/null || fail 'Leg-K rollback left unverified orphans'
  [[ "$(origin_dashboard_count rigsignal)" == 0 ]] || fail 'Leg-K rollback left shared-child or regenerated objects'
  kb_get rigsignal '/api/saved_objects/_find?type=legacy-url-alias&per_page=1000' | jq -e '.total == 0' >/dev/null || fail 'Leg-K rollback left an alias delta'
}

# L: a retained {apply_ok:false,rollback_ok:true} transaction is archivable and
# its next install is a fresh, fully-accounted transaction (D-12 invocation).
leg_l() {
  setup
  if RIGSIGNAL_TEST_CRASH_AT=after-remote-mutation _installer >"$RUN_DIR/leg-l-crash.log" 2>&1; then fail 'Leg-L after-remote-mutation fault did not crash'; fi
  rollback >"$RUN_DIR/leg-l-first-rollback.log" 2>&1 || fail 'Leg-L first rollback failed'
  jq -e '.apply_ok == false and .rollback_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-L did not retain the deadlock live shape'
  _installer >"$RUN_DIR/leg-l-reinstall.log" 2>&1 || fail 'Leg-L reinstall from retained root failed'
  origin_assert_full_accounting "$RUN_DIR/leg-l-reinstall.log"
  jq -e '(.transactions|length) == 1 and .transactions[0].apply_ok == false and .transactions[0].rollback_ok == true and .apply_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-L did not archive then open a fresh transaction'
  rollback >"$RUN_DIR/leg-l-second-rollback.log" 2>&1 || fail 'Leg-L second rollback failed'
  jq -e '.rollback_ok == true and (.transactions|length) == 1 and .transactions[0].rollback_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-L second rollback did not stay in active transaction'
}

# M: pause-created collisions, lost responses, and per-delete resume cover both
# space_prefix branches.  The assertions consume the captured request log.
leg_m() {
  setup
  # Pre-create the (empty) target space: on a fresh stack the preflight's
  # per-space sweep only visits EXISTING spaces, so without this the
  # rigsignal-scoped _find is legitimately never issued (design: space-absent
  # outcome) and the line-437 scoped-path assertion would test nothing
  # (solo leg-m failure at pin 0e3689c). An empty pre-existing target space
  # is legitimate topology and makes the assertion prove the /s/rigsignal
  # find branch actually executed.
  origin_seed space-bundle
  origin_pause_install "$RUN_DIR/leg-m-i.out" origin_installer
  origin_seed one donor dashboard rigsignal-pkg-engine
  if origin_resume; then fail 'Leg-M(i) regenerated import unexpectedly succeeded'; fi
  grep -E '^install refused: saved_object_id_regenerated(:|$)' "$RUN_DIR/leg-m-i.out" >/dev/null || fail 'Leg-M(i) wrong regeneration refusal'
  uuid="$(python3 - "$RUN_DIR/leg-m-i.out" <<'PY'
import ast
import sys

prefix = "install refused: saved_object_id_regenerated: "
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.startswith(prefix):
        continue
    payload = line[len(prefix):].partition(" space=")[0]
    try:
        regenerated = ast.literal_eval(payload)
    except (SyntaxError, ValueError):
        continue
    for item in regenerated:
        if isinstance(item, tuple) and len(item) >= 3 and isinstance(item[2], str):
            print(item[2])
            raise SystemExit
PY
)"
  [[ -n "$uuid" ]] || fail 'Leg-M(i) did not expose a regenerated UUID'
  origin_missing rigsignal dashboard "$uuid" || fail 'Leg-M(i) cleanup did not explicitly leave destination 404'
  grep -E "^DELETE /s/rigsignal/api/saved_objects/dashboard/${uuid}$" "$RUN_DIR/origin-http.log" >/dev/null || fail 'Leg-M(i) cleanup did not use scoped path'
  grep -E '^GET /s/rigsignal/api/saved_objects/_find\?type=dashboard&per_page=1000$' "$RUN_DIR/origin-http.log" >/dev/null || fail 'Leg-M(i) preflight did not use scoped find path'
  jq -e '.apply_ok == false' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-M(i) journal was not retained unfinished'
  rollback >"$RUN_DIR/leg-m-i-rollback.log" 2>&1 || fail 'Leg-M(i) rollback failed'
  jq -e '.rollback_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-M(i) rollback did not persist'
  origin_seed delete donor dashboard rigsignal-pkg-engine
  _installer >"$RUN_DIR/leg-m-i-reinstall.log" 2>&1 || fail 'Leg-M(i) rollback-then-reinstall failed'
  origin_assert_full_accounting "$RUN_DIR/leg-m-i-reinstall.log"

  origin_reset
  RIGSIGNAL_TEST_CRASH_AT='after-dashboard-response-before-regen-check:dashboard/rigsignal-engine.ndjson' origin_pause_install "$RUN_DIR/leg-m-ii.out" origin_installer
  origin_seed one donor dashboard rigsignal-pkg-engine
  : >"$RUN_DIR/pause.resume"
  if wait "$ORIGIN_PID"; then fail 'Leg-M(ii) crash hook did not exit'; fi
  RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/leg-m-ii-rollback-http.log" rollback >"$RUN_DIR/leg-m-ii-rollback.log" 2>&1 || fail 'Leg-M(ii) sweep rollback failed'
  grep -E '^GET /s/rigsignal/api/saved_objects/_find\?type=dashboard&per_page=1000$' "$RUN_DIR/leg-m-ii-rollback-http.log" >/dev/null || fail 'Leg-M(ii) sweep find was not scoped'
  grep -E '^DELETE /s/rigsignal/api/saved_objects/dashboard/[^/]+$' "$RUN_DIR/leg-m-ii-rollback-http.log" >/dev/null || fail 'Leg-M(ii) sweep delete was not scoped'
  kb_get rigsignal '/api/saved_objects/_find?type=dashboard&per_page=1000' | jq -e '[.saved_objects[] | select(.originId == "rigsignal-pkg-engine")] | length == 0' >/dev/null || fail 'Leg-M(ii) sweep left UUID orphan'
  ! grep -F 'unverified-orphan:' "$RUN_DIR/leg-m-ii-rollback.log" >/dev/null || fail 'Leg-M(ii) sweep left an unverified orphan'
  origin_seed delete donor dashboard rigsignal-pkg-engine
  _installer >"$RUN_DIR/leg-m-ii-reinstall.log" 2>&1 || fail 'Leg-M(ii) clean reinstall failed'
  origin_assert_full_accounting "$RUN_DIR/leg-m-ii-reinstall.log"

  origin_reset
  RIGSIGNAL_TEST_CRASH_AT='after-dashboard-response-before-regen-check:dashboard/rigsignal-engine.ndjson' origin_pause_install "$RUN_DIR/leg-m-prime.out" origin_installer
  origin_seed one donor dashboard rigsignal-pkg-engine; : >"$RUN_DIR/pause.resume"
  if wait "$ORIGIN_PID"; then fail 'Leg-M(iii-prime) import crash did not fire'; fi
  if RIGSIGNAL_TEST_CRASH_AT=after-regen-cleanup-delete rollback >"$RUN_DIR/leg-m-prime-crash.log" 2>&1; then fail 'Leg-M(iii-prime) delete crash did not fire'; fi
  rollback >"$RUN_DIR/leg-m-prime-resume.log" 2>&1 || fail 'Leg-M(iii-prime) same-transaction rollback did not resume'
  jq -e '.rollback_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-M(iii-prime) resumed rollback did not persist'
  kb_get rigsignal '/api/saved_objects/_find?type=dashboard&per_page=1000' | jq -e '[.saved_objects[] | select(.originId == "rigsignal-pkg-engine")] | length == 0' >/dev/null || fail 'Leg-M(iii-prime) resumed sweep left UUID orphan'
  ! grep -F 'unverified-orphan:' "$RUN_DIR/leg-m-prime-resume.log" >/dev/null || fail 'Leg-M(iii-prime) resumed sweep was not converged'

  origin_reset
  RIGSIGNAL_TEST_CRASH_AT='after-dashboard-response-before-regen-check:dashboard/rigsignal-streaming-lab.ndjson' origin_pause_install "$RUN_DIR/leg-m-iii.out" default_installer
  origin_seed one donor dashboard rigsignal-pkg-streaming-lab; : >"$RUN_DIR/pause.resume"
  if wait "$ORIGIN_PID"; then fail 'Leg-M(iii) default crash did not fire'; fi
  if RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/origin-http.log" default_installer >"$RUN_DIR/leg-m-iii-refusal.log" 2>&1; then fail 'Leg-M(iii) lost-response rerun unexpectedly succeeded'; fi
  grep -E '^install refused: saved_object_topology_conflict(:|$)' "$RUN_DIR/leg-m-iii-refusal.log" >/dev/null || fail 'Leg-M(iii) wrong refusal token'
  grep -F 'target_origin_derivative' "$RUN_DIR/leg-m-iii-refusal.log" >/dev/null || fail 'Leg-M(iii) did not name UUID orphan'
  grep -F 'literal_id_exists_elsewhere' "$RUN_DIR/leg-m-iii-refusal.log" >/dev/null || fail 'Leg-M(iii) did not name foreign literal seed'
  grep -F 'RIGSIGNAL_OPERATOR_ACTION resolve or remove foreign literal object' "$RUN_DIR/leg-m-iii-refusal.log" >/dev/null || fail 'Leg-M(iii) did not require foreign-seed resolution'
  sed -n 's/^RIGSIGNAL_REMEDIATION //p' "$RUN_DIR/leg-m-iii-refusal.log" >"$RUN_DIR/leg-m-remediation.jsonl"
  [[ -s "$RUN_DIR/leg-m-remediation.jsonl" ]] || fail 'Leg-M(iii) emitted no machine remediation payload'
  # One-arg all(f) iterates the INPUT's values (it would test .method against
  # the string "DELETE" and error); the payload rows are objects, so slurp and
  # use the two-arg form (solo leg-m round-17 catch — a harness jq defect, the
  # emitted payloads were correct).
  jq -s -e 'length > 0 and all(.[]; .method == "DELETE" and (.path | startswith("/api/saved_objects/")) and .headers == {"kbn-xsrf":"true"})' "$RUN_DIR/leg-m-remediation.jsonl" >/dev/null || fail 'Leg-M(iii) remediation payload is malformed'
  grep -E '^GET /api/saved_objects/_find\?type=dashboard&per_page=1000$' "$RUN_DIR/origin-http.log" >/dev/null || fail 'Leg-M(iii) default find was not unscoped'
  origin_seed replay "$RUN_DIR/leg-m-remediation.jsonl"
  if default_installer >"$RUN_DIR/leg-m-iii-negative.log" 2>&1; then fail 'Leg-M(iii) UUID-only cleanup accepted foreign seed'; fi
  grep -E '^install refused: saved_object_topology_conflict(:|$)' "$RUN_DIR/leg-m-iii-negative.log" >/dev/null || fail 'Leg-M(iii) UUID-only cleanup wrong token'
  grep -F 'literal_id_exists_elsewhere' "$RUN_DIR/leg-m-iii-negative.log" >/dev/null || fail 'Leg-M(iii) UUID-only cleanup did not retain literal refusal'
  origin_seed delete donor dashboard rigsignal-pkg-streaming-lab
  default_installer >"$RUN_DIR/leg-m-iii-success.log" 2>&1 || fail 'Leg-M(iii) remediation replay did not permit rerun'
}

# N: Existing Fleet streams whose owned winners are semantically old must be
# classified L3 and receive the bundle's sanctioned projection—not rejected by
# an obsolete byte-equality fence.
leg_n() {
  setup
  jq 'del(.template.mappings.properties.stream)' "$REPO_ROOT/elastic/index-templates/logs-rigsignal.stream.json" >"$RUN_DIR/logs-rigsignal.stream-old.json"
  jq 'del(.template.mappings.properties["node.width"])' "$REPO_ROOT/elastic/index-templates/metrics-rigsignal.profiles.json" >"$RUN_DIR/metrics-rigsignal.profiles-old.json"
  api PUT '/_index_template/logs-rigsignal.stream' --data-binary "@$RUN_DIR/logs-rigsignal.stream-old.json" >/dev/null
  api PUT '/_index_template/metrics-rigsignal.profiles' --data-binary "@$RUN_DIR/metrics-rigsignal.profiles-old.json" >/dev/null
  _installer >"$RUN_DIR/leg-n-install.log" 2>&1 || fail 'Leg-N installer refused sanctioned owned-template update'
  cat "$RUN_DIR/leg-n-install.log"
  for stream in logs-rigsignal.stream-default metrics-rigsignal.profiles-default; do
    jq -e --arg stream "$stream" '.fleet_fence.plan[$stream] | (.classification.status == "L3") and ((.projection.ops | length) > 0) and (.classification.winner_evidence.matching_set | type == "array") and (.classification.winner_evidence.matching_set | length > 0) and (.classification.winner_evidence.max_priority | type == "number") and (.classification.winner_evidence.unique == true) and (.classification | has("winning_template"))' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail "Leg-N missing L3 projection/winner evidence: $stream"
  done
  owned_template_matches_bundle logs-rigsignal.stream
  owned_template_matches_bundle metrics-rigsignal.profiles
  printf 'Leg-N proof: non-empty L3 D on pre-existing streams proves the old byte-equality fence would have refused its own sanctioned write.\n'
}

# O: a rollover after the candidate proof is an honest late-fence abort.  P6
# reverses only installer intents and preserves the external backing change.
leg_o() {
  setup
  stream_backing_snapshot logs-rigsignal.stream-default >"$RUN_DIR/leg-o-before-backing.json"
  set +e
  RIGSIGNAL_TEST_ROLLOVER_AT='before-publication:logs-rigsignal.stream-default' _installer >"$RUN_DIR/leg-o-install.log" 2>&1
  rc=$?
  set -e
  [[ "$rc" != 0 ]] || fail 'Leg-O late rollover unexpectedly succeeded'
  grep -Fx 'install failed: pre-publication fence:' "$RUN_DIR/leg-o-install.log" >/dev/null || { cat "$RUN_DIR/leg-o-install.log" >&2; fail 'Leg-O did not report pre-publication fence'; }
  for artifact in credentials.toml handshake.toml shipping-policy-v1.toml; do [[ ! -e "$RUN_DIR/enrollment/$artifact" ]] || fail "Leg-O published $artifact after late fence"; done
  stream_backing_snapshot logs-rigsignal.stream-default >"$RUN_DIR/leg-o-post-rollover-backing.json"
  cmp -s "$RUN_DIR/leg-o-before-backing.json" "$RUN_DIR/leg-o-post-rollover-backing.json" && fail 'Leg-O hook did not roll over the tracked stream'
  rollback 2>&1 | tee "$RUN_DIR/leg-o-rollback.log"
  grep -Fx 'rollback completed from journaled intents; external_rollover_observed' "$RUN_DIR/leg-o-rollback.log" >/dev/null || fail 'Leg-O P6 did not report external rollover'
  jq -e '.apply_ok == false and .rollback_ok == true and .fleet_fence.external_rollover_observed == true and (.fleet_fence.external_rollovers | length) > 0 and .fleet_fence.failure.layer == "late"' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-O journal did not classify honest rollover abort'
  stream_backing_snapshot logs-rigsignal.stream-default >"$RUN_DIR/leg-o-after-rollback-backing.json"
  cmp -s "$RUN_DIR/leg-o-post-rollover-backing.json" "$RUN_DIR/leg-o-after-rollback-backing.json" || fail 'Leg-O rollback restored rather than preserved external rollover backing list'
}

# P: P3 refuses a non-approved predecessor before any cluster write, while the
# set-valued manifest permits the P6-retained pipeline state on a retry.
leg_p() {
  setup
  make_predecessor_manifest "$RUN_DIR/predecessor-good.json"
  jq '.assets["index_templates/logs-rigsignal.stream"].approved_sha256 = ["0000000000000000000000000000000000000000000000000000000000000000"]' "$RUN_DIR/predecessor-good.json" >"$RUN_DIR/predecessor-bad.json"
  : >"$RUN_DIR/leg-p-refusal-audit.log"
  set +e
  PREDECESSOR_MANIFEST="$RUN_DIR/predecessor-bad.json" RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/leg-p-refusal-audit.log" _installer >"$RUN_DIR/leg-p-mismatch.log" 2>&1
  rc=$?
  set -e
  [[ "$rc" != 0 ]] || fail 'Leg-P predecessor mismatch unexpectedly succeeded'
  grep -F 'predecessor manifest mismatch' "$RUN_DIR/leg-p-mismatch.log" >/dev/null || { cat "$RUN_DIR/leg-p-mismatch.log" >&2; fail 'Leg-P mismatch did not surface predecessor barrier'; }
  ! grep -E '^(PUT|DELETE) ' "$RUN_DIR/leg-p-refusal-audit.log" >/dev/null || fail 'Leg-P mismatch wrote a cluster asset'
  if api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/leg-p-marker.json" 2>&1; then fail 'Leg-P mismatch wrote marker'; fi
  # The first half deliberately leaves its local unfinished journal as refusal
  # evidence.  Start Part 2 with a fresh enrollment root so recovery policy is
  # not mistaken for predecessor-barrier behavior.
  rm -rf "$RUN_DIR/enrollment"
  PREDECESSOR_MANIFEST="$RUN_DIR/predecessor-good.json" installer || fail 'Leg-P correct predecessor manifest did not install'
  rollback 2>&1 | tee "$RUN_DIR/leg-p-rollback.log"
  grep -F 'pipeline retained:' "$RUN_DIR/leg-p-rollback.log" >/dev/null || fail 'Leg-P did not exercise retained-pipeline exception'
  jq -e '[.intents[] | select(.kind == "pipelines" and .name == "logs-rigsignal.stream@pipeline") | .pipeline_retained_in_use] | length == 1' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null || fail 'Leg-P journal lacks retained pipeline record'
  PREDECESSOR_MANIFEST="$RUN_DIR/predecessor-good.json" installer || fail 'Leg-P post-retained-pipeline retry did not pass predecessor barrier'
}

for leg in "${LEGS[@]}"; do case "$leg" in a|b|c|d|e|f|g|h|i|j|k|l|m|n|o|p) "leg_$leg" ;; *) fail "unknown leg: $leg"; exit 2 ;; esac; done
