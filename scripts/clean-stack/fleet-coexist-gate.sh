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
usage() { printf '%s\n' 'Usage: fleet-coexist-gate.sh --es-version 9.4.3|9.4.4 [--kb-version VERSION] --leg a..h [--bundle PATH] [--all]' >&2; }
version() { [[ "$1" =~ ^9\.4\.[34]$ ]]; }
fail() { printf 'ASSERT FAIL %s\n' "$*" >&2; return 1; }
while (($#)); do case "$1" in
  --es-version) ES_VERSION="${2:-}"; shift 2 ;; --kb-version) KB_VERSION="${2:-}"; shift 2 ;;
  --bundle) BUNDLE="${2:-}"; shift 2 ;; --leg) LEGS+=("${2:-}"); shift 2 ;;
  --all) LEGS=(a b c d e f g h); shift ;; --keep) KEEP=1; shift ;;
  -h|--help) usage; exit 0 ;; *) usage; exit 2 ;; esac; done
[[ -n "$ES_VERSION" ]] || { usage; exit 2; }; KB_VERSION="${KB_VERSION:-$ES_VERSION}"
version "$ES_VERSION" && version "$KB_VERSION" && [[ "$ES_VERSION" == "$KB_VERSION" ]] || { usage; exit 2; }
((${#LEGS[@]})) || { usage; exit 2; }; [[ -z "$BUNDLE" || -f "$BUNDLE" ]] || { fail '--bundle is not a file'; exit 2; }
: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD must be set}"; : "${ELASTICSEARCH_PASSWORD:?ELASTICSEARCH_PASSWORD must be set}"; : "${CLEAN_STACK_AGENT_BINARY:?CLEAN_STACK_AGENT_BINARY must be set}"
cs_require_tools bash curl docker jq openssl python3 sha256sum
RUN_DIR="$(mktemp -d)"
cleanup() { local rc="$?"; cs_cleanup || true; [[ "$KEEP" == 1 ]] || rm -rf "$RUN_DIR"; return "$rc"; }
trap cleanup EXIT

start_stack() {
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
installer() { python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment" --ownership-profile fleet-coexist; }
rollback() { python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --rollback "$RUN_DIR/enrollment"; }
seed() { ES_URL="$ES_URL" ELASTIC_PASSWORD="$ELASTIC_PASSWORD" "$SCRIPT_DIR/fleet-coexist-seed.sh" "$@"; }
setup() { start_stack; build_bundle; write_admin; seed; }
marker_check() { api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/marker.json"; jq -e '[.component_templates[0].component_template._meta.applied_owned_assets[]|[.kind,.name]] as $a | [.component_templates[0].component_template._meta.verified_external_assets[]|[.kind,.name]] as $e | ($a|length)==16 and ($e|length)==39 and (($a+$e)|unique|length)==55' "$RUN_DIR/marker.json" >/dev/null; }
expect_refusal() { local code="$1"; shift; if "$@" >"$RUN_DIR/refusal.out" 2>&1; then fail "$code unexpectedly succeeded"; fi; grep -Fx "install refused: $code" "$RUN_DIR/refusal.out" >/dev/null || { cat "$RUN_DIR/refusal.out" >&2; fail "$code wrong refusal"; }; }
external_audit_clean() { local log="$1"; ! grep -E '^(PUT|POST|DELETE) /(_component_template|_index_template|_ingest/pipeline)/(metrics-rigsignal|logs-rigsignal\.events)' "$log"; }
installer_unresolved() { RIGSIGNAL_TEST_UNRESOLVED_ASSET=1 installer; }
installer_bad_health() { RIGSIGNAL_TEST_CLUSTER_HEALTH=red installer; }
installer_bad_ilm() { RIGSIGNAL_TEST_ILM_DELETE_PHASE=1 installer; }

# Dirty Fleet assets + reinstall/upgrade rehearsal.
leg_a() { setup; installer; marker_check; seed --upgrade; installer; marker_check; }

# The real transform inverse is exercised on the running version pair.  If ES
# cannot restore absent _meta, rollback must STOP rather than silently proceed.
leg_b() { setup; installer; rollback; api GET '/_transform/rigsignal-game-timeline' >"$RUN_DIR/transform-after.json" || true; jq -e '(.transforms[0].pivot // .pivot) != null' "$RUN_DIR/transform-after.json" >/dev/null || fail 'transform pivot was not preserved'; }

# Each injected crash is followed by the actual journal rollback, not a unit substitute.
leg_c() { local point rc; for point in after-write-intent dashboard-multipart before-mint-response proof-create; do cs_cleanup || true; rm -rf "$RUN_DIR/enrollment"; start_stack; build_bundle; write_admin; seed; set +e; RIGSIGNAL_TEST_CRASH_AT="$point" installer >"$RUN_DIR/crash-$point.out" 2>&1; rc=$?; set -e; [[ "$rc" == 99 ]] || fail "$point did not crash"; rollback; done; }

# Six live refusal rows; test-only hooks only make an otherwise healthy disposable
# stack present an unrepresentable condition and are inert without their env vars.
leg_d() { setup; installer; expect_refusal omitted_profile_on_coexist env -u RIGSIGNAL_HTTP_AUDIT_LOG python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment"; expect_refusal ownership_profile_mismatch env python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment" --ownership-profile default; expect_refusal ownership_table_unresolved installer_unresolved; expect_refusal cluster_health installer_bad_health; expect_refusal ilm_delete_phase installer_bad_ilm; printf '[elasticsearch]\napi_key = "x"\n' >"$RUN_DIR/api-key.toml"; chmod 600 "$RUN_DIR/api-key.toml"; expect_refusal admin_credential_api_key env CLEAN_STACK_INSTALLER="$REPO_ROOT/tools/install_assets.py" python3 "$REPO_ROOT/tools/install_assets.py" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/api-key.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/api-key-root" --ownership-profile fleet-coexist; }

leg_e() { setup; installer; api POST '/logs-rigsignal.events-default/_rollover' >/dev/null || true; installer; marker_check; }

# Recording transport audit and its mandatory identical-body external PUT negative control.
leg_f() { setup; : >"$RUN_DIR/audit.log"; RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/audit.log" installer; external_audit_clean "$RUN_DIR/audit.log" || fail 'external write audit saw a write'; : >"$RUN_DIR/audit-negative.log"; RIGSIGNAL_HTTP_AUDIT_LOG="$RUN_DIR/audit-negative.log" RIGSIGNAL_TEST_EXTERNAL_WRITE=1 installer || true; grep -E '^(PUT|POST|DELETE) /(_component_template|_index_template|_ingest/pipeline)/(metrics-rigsignal|logs-rigsignal\.events)' "$RUN_DIR/audit-negative.log" >/dev/null || fail 'external write audit negative control did not fire'; }

leg_g() { setup; installer; marker_check; jq -e '[.component_templates[0].component_template._meta|has("installed_assets")|not]' "$RUN_DIR/marker.json" >/dev/null; }
leg_h() { build_bundle; printf 'GATE ARTIFACT commit=%s bundle_sha256=%s\n' "$IMPLEMENTING_COMMIT" "$BUNDLE_SHA256"; }
for leg in "${LEGS[@]}"; do case "$leg" in a|b|c|d|e|f|g|h) "leg_$leg" ;; *) fail "unknown leg: $leg"; exit 2 ;; esac; done
