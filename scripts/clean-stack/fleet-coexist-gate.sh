#!/usr/bin/env bash
# Manual Fleet-coexistence scenario gate.  It intentionally has no CI caller.
# shellcheck disable=SC2329
set -euo pipefail
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/clean-stack/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"

ES_VERSION='' KB_VERSION='' BUNDLE='' KEEP=0
declare -a LEGS=()
usage() { printf '%s\n' 'Usage: fleet-coexist-gate.sh --es-version 9.4.3|9.4.4 [--kb-version VERSION] --leg a..i [--bundle PATH] [--all]' >&2; }
version() { [[ "$1" =~ ^9\.4\.[34]$ ]]; }
fail() { printf 'ASSERT FAIL %s\n' "$*" >&2; return 1; }
while (($#)); do case "$1" in
  --es-version) ES_VERSION="${2:-}"; shift 2 ;; --kb-version) KB_VERSION="${2:-}"; shift 2 ;;
  --bundle) BUNDLE="${2:-}"; shift 2 ;; --leg) LEGS+=("${2:-}"); shift 2 ;;
  --all) LEGS=(a b c d e f g h i); shift ;; --keep) KEEP=1; shift ;;
  -h|--help) usage; exit 0 ;; *) usage; exit 2 ;; esac; done
[[ -n "$ES_VERSION" ]] || { usage; exit 2; }; KB_VERSION="${KB_VERSION:-$ES_VERSION}"
version "$ES_VERSION" && version "$KB_VERSION" && [[ "$ES_VERSION" == "$KB_VERSION" ]] || { usage; exit 2; }
((${#LEGS[@]})) || { usage; exit 2; }; [[ -z "$BUNDLE" || -f "$BUNDLE" ]] || { fail '--bundle is not a file'; exit 2; }
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
  [[ "${RIGSIGNAL_TEST_EXTERNAL_WRITE:-}" != 1 ]] || args+=(--unsafe-test-injection)
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

# Dirty Fleet assets + reinstall/upgrade rehearsal.
leg_a() { setup; installer || fail 'installer failed'; marker_check; cp "$RUN_DIR/marker.json" "$RUN_DIR/marker-before-upgrade.json"; simulation_canary; external_hashes_check; jq -e '[.component_templates[0].component_template._meta.applied_owned_assets[].action] | all(. == "create" or . == "update" or . == "import" or . == "noop") and any(. != "noop")' "$RUN_DIR/marker.json" >/dev/null; seed --upgrade; installer || fail 'installer failed'; marker_check; external_hashes_check; jq -e --slurpfile before "$RUN_DIR/marker-before-upgrade.json" '([.component_templates[0].component_template._meta.verified_external_assets[] | [.kind,.name,.owner_metadata,.live_body_sha256]]) != ([$before[0].component_templates[0].component_template._meta.verified_external_assets[] | [.kind,.name,.owner_metadata,.live_body_sha256]])' "$RUN_DIR/marker.json" >/dev/null || fail 'upgrade did not capture moved owner metadata/live body'; jq -e '[.component_templates[0].component_template._meta.applied_owned_assets[].action] | all(. == "noop")' "$RUN_DIR/marker.json" >/dev/null; api PUT '/_component_template/.fleet_globals-1' --data-binary '{"template":{"settings":{"index.default_pipeline":"rigsignal-a5-dominance-canary"}}}' >/dev/null; expect_refusal 'external asset compatibility' installer; }

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
  grep -Fx 'rollback completed from journaled intents; pipeline retained: in use as default pipeline for adopted stream indices' "$RUN_DIR/leg-b-transform-rollback.log" >/dev/null || fail 'retained adopted-stream pipeline was not reported'
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
  grep -Fx 'rollback completed from journaled intents; pipeline retained: in use as default pipeline for adopted stream indices' "$RUN_DIR/leg-b-transform-rollback.log" >/dev/null || fail 'retained adopted-stream pipeline was not reported'
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
    RIGSIGNAL_TEST_CRASH_AT="$point" installer >"$RUN_DIR/crash-$point.out" 2>&1
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
    set +e
    RIGSIGNAL_TEST_ROLLOVER_AT="after-fleet-snapshot:$stream" installer >"$RUN_DIR/in-transaction-rollover-$stream.out" 2>&1
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

leg_e() { setup; api PUT '/_data_stream/logs-rigsignal.events-default' >/dev/null; api GET '/_data_stream/logs-rigsignal.events-default' >"$RUN_DIR/rollover-before.json"; installer; api POST '/logs-rigsignal.events-default/_rollover' >/dev/null; api GET '/_data_stream/logs-rigsignal.events-default' >"$RUN_DIR/rollover-after.json"; jq -e '(.data_streams[0].indices|length) > 1' "$RUN_DIR/rollover-after.json" >/dev/null; installer; marker_check; }

# Two completed transactions share one root.  Rolling back N=2 must retain
# N=1's archived proofs and restore N=1's marker rather than deleting it.
leg_i() { setup; api GET '/_data_stream/*rigsignal*' >"$RUN_DIR/streams-before-rollback.json"; jq -e '(.data_streams|length) == 16 and ([.data_streams[].indices[]]|length) == 18' "$RUN_DIR/streams-before-rollback.json" >/dev/null || fail 'pre-rollback stream set is not 16/18'; installer || fail 'installer failed'; marker_check; cp "$RUN_DIR/enrollment/fleet-coexist-journal.json" "$RUN_DIR/transaction-1.json"; cp "$RUN_DIR/marker.json" "$RUN_DIR/marker-1.json"; seed --upgrade; installer || fail 'installer failed'; cp "$RUN_DIR/enrollment/fleet-coexist-journal.json" "$RUN_DIR/transaction-2.json"; rollback 2>&1 | tee "$RUN_DIR/leg-i-rollback.log"; grep -Fx 'rollback completed from journaled intents' "$RUN_DIR/leg-i-rollback.log" >/dev/null || fail 'txn-2 rollback did not complete plainly (pipeline intent is noop in txn 2)'; api GET '/_data_stream/*rigsignal*' >"$RUN_DIR/streams-after-rollback.json"; jq -S '[.data_streams[]|{name,indices:[.indices[]|{index_name,index_uuid}]}]' "$RUN_DIR/streams-before-rollback.json" >"$RUN_DIR/streams-before.canonical"; jq -S '[.data_streams[]|{name,indices:[.indices[]|{index_name,index_uuid}]}]' "$RUN_DIR/streams-after-rollback.json" >"$RUN_DIR/streams-after.canonical"; cmp -s "$RUN_DIR/streams-before.canonical" "$RUN_DIR/streams-after.canonical" || fail 'rollback changed or newly created a RigSignal stream'; jq -e '(.transactions|length) == 1 and .transactions[0].apply_ok == true' "$RUN_DIR/enrollment/fleet-coexist-journal.json" >/dev/null; while read -r event_id; do jq -n --arg id "$event_id" '{query:{ids:{values:[$id]}},size:2}' >"$RUN_DIR/proof-query.json"; api POST '/logs-rigsignal.diagnosis-default/_search' --data-binary "@$RUN_DIR/proof-query.json" >"$RUN_DIR/proof-$event_id.json"; jq -e --arg id "$event_id" '.hits.hits | length == 1 and .[0]._id == $id' "$RUN_DIR/proof-$event_id.json" >/dev/null || fail "transaction-1 proof missing: $event_id"; done < <(jq -r '.proofs[].event_id' "$RUN_DIR/transaction-1.json"); while read -r event_id; do jq -n --arg id "$event_id" '{query:{ids:{values:[$id]}},size:2}' >"$RUN_DIR/proof-query.json"; api POST '/logs-rigsignal.diagnosis-default/_search' --data-binary "@$RUN_DIR/proof-query.json" | jq -e '.hits.hits|length == 0' >/dev/null || fail "transaction-2 proof survived rollback: $event_id"; done < <(jq -r '.proofs[].event_id' "$RUN_DIR/transaction-2.json"); api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/marker-restored.json"; jq -e '.component_templates[0].component_template._meta.ownership_profile == "fleet-coexist"' "$RUN_DIR/marker-restored.json" >/dev/null; expect_refusal transaction_already_rolled_back rollback; installer || fail 'reinstall after rollback failed'; }

# Recording transport audit and its mandatory identical-body external PUT negative control.
leg_f() { setup; : >"$RUN_DIR/audit.log"; RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/audit.log" installer || fail 'installer failed'; external_audit_clean "$RUN_DIR/audit.log" || fail 'external write audit saw a write'; : >"$RUN_DIR/audit-negative.log"; RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/audit-negative.log" RIGSIGNAL_TEST_EXTERNAL_WRITE=1 installer || true; grep -E "$EXTERNAL_WRITE_RE" "$RUN_DIR/audit-negative.log" >/dev/null || fail 'external write audit negative control did not fire'; }

leg_g() { setup; installer; marker_check; jq -e '[.component_templates[0].component_template._meta|has("installed_assets")|not]' "$RUN_DIR/marker.json" >/dev/null; }
leg_h() { build_bundle; printf 'GATE ARTIFACT commit=%s bundle_sha256=%s\n' "$IMPLEMENTING_COMMIT" "$BUNDLE_SHA256"; }
for leg in "${LEGS[@]}"; do case "$leg" in a|b|c|d|e|f|g|h|i) "leg_$leg" ;; *) fail "unknown leg: $leg"; exit 2 ;; esac; done
