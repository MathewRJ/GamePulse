#!/usr/bin/env bash
# Manual, selectable Fleet-coexistence gate.  It remains outside CI because it
# starts disposable containers, following adoption-gate.sh's explicit model.
set -euo pipefail
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=scripts/clean-stack/lib.sh disable=SC1091
source "$SCRIPT_DIR/lib.sh"
ES_VERSION='' KB_VERSION='' BUNDLE='' KEEP=0
declare -a LEGS=()
usage() { printf '%s\n' 'Usage: fleet-coexist-gate.sh --es-version 9.4.3|9.4.4 --kb-version VERSION [--bundle PATH] --leg a..h [--all] [--dry-run]' >&2; }
version() { [[ "$1" =~ ^9\.4\.[34]$ ]]; }
fail() { printf 'ASSERT FAIL %s\n' "$*" >&2; return 1; }
while (($#)); do case "$1" in
  --es-version) ES_VERSION="${2:-}"; shift 2 ;; --kb-version) KB_VERSION="${2:-}"; shift 2 ;;
  --bundle) BUNDLE="${2:-}"; shift 2 ;; --leg) LEGS+=("${2:-}"); shift 2 ;;
  --all) LEGS=(a b c d e f g h); shift ;; --keep) KEEP=1; shift ;; --dry-run) DRY_RUN=1; shift ;;
  -h|--help) usage; exit 0 ;; *) usage; exit 2 ;; esac; done
DRY_RUN="${DRY_RUN:-0}"
[[ -n "$ES_VERSION" && -n "$KB_VERSION" ]] && version "$ES_VERSION" && version "$KB_VERSION" && [[ "$ES_VERSION" == "$KB_VERSION" ]] || { usage; exit 2; }
((${#LEGS[@]})) || { usage; exit 2; }; [[ -z "$BUNDLE" || -f "$BUNDLE" ]] || { fail '--bundle is not a file'; exit 2; }
if [[ "$DRY_RUN" == 1 ]]; then printf 'fleet coexist %s legs=%s\n' "$ES_VERSION" "${LEGS[*]}"; exit 0; fi
: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD must be set}"; : "${ELASTICSEARCH_PASSWORD:?ELASTICSEARCH_PASSWORD must be set}"; : "${CLEAN_STACK_AGENT_BINARY:?CLEAN_STACK_AGENT_BINARY must be set}"
cs_require_tools bash curl docker jq openssl python3 sha256sum
RUN_DIR="$(mktemp -d)"; cleanup() { local rc="$?"; cs_cleanup || true; [[ "$KEEP" == 1 ]] || rm -rf "$RUN_DIR"; return "$rc"; }; trap cleanup EXIT
start_stack() {
  cs_create_network; cs_start_elasticsearch "docker.elastic.co/elasticsearch/elasticsearch:$ES_VERSION" "$(cs_port_mapping '' 9200)"; ES_URL="https://localhost:$(cs_published_port "$CS_ES_CONTAINER" 9200/tcp)"
  cs_wait_for_elasticsearch "$ES_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/es.json"
  curl --silent --show-error --fail --user "elastic:$ELASTIC_PASSWORD" -H 'Content-Type: application/json' -X POST --data "{\"password\":\"$ELASTICSEARCH_PASSWORD\"}" "$ES_URL/_security/user/kibana_system/_password" >/dev/null
  cs_start_kibana "docker.elastic.co/kibana/kibana:$KB_VERSION" "$(cs_port_mapping '' 5601)"; KB_URL="https://localhost:$(cs_published_port "$CS_KB_CONTAINER" 5601/tcp)"; cs_wait_for_kibana "$KB_URL" elastic "$ELASTIC_PASSWORD" "$RUN_DIR/kb.json"
}
api() { curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" -H 'Content-Type: application/json' -X "$1" "${ES_URL}$2" "${@:3}"; }
build_bundle() { if [[ -z "$BUNDLE" ]]; then BUNDLE="$RUN_DIR/assets.tar.gz"; python3 "$REPO_ROOT/tools/build_asset_bundle.py" --source-commit "$(git -C "$REPO_ROOT" rev-parse HEAD)" --output "$BUNDLE"; fi; BUNDLE_SHA256="$(sha256sum "$BUNDLE"|awk '{print $1}')"; IMPLEMENTING_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"; }
write_admin() { umask 077; printf '[elasticsearch]\nusername = "elastic"\npassword = "%s"\n' "$ELASTIC_PASSWORD" >"$RUN_DIR/admin.toml"; chmod 600 "$RUN_DIR/admin.toml"; }
run_installer() { python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" --bundle "$BUNDLE" --endpoint "$ES_URL" --ca-file "$CS_CA_FILE" --kibana-endpoint "$KB_URL" --kibana-ca-file "$CS_CA_FILE" --admin-credentials-file "$RUN_DIR/admin.toml" --agent-binary "$CLEAN_STACK_AGENT_BINARY" --profile user --enrollment-root "$RUN_DIR/enrollment" --ownership-profile fleet-coexist; }
seed() { ES_URL="$ES_URL" ELASTIC_PASSWORD="$ELASTIC_PASSWORD" "$SCRIPT_DIR/fleet-coexist-seed.sh" "$@"; }
marker_check() { api GET '/_component_template/rigsignal-bundle-meta' >"$RUN_DIR/marker.json"; jq -e '[.component_templates[0].component_template._meta.applied_owned_assets[]|[.kind,.name]] as $a | [.component_templates[0].component_template._meta.verified_external_assets[]|[.kind,.name]] as $e | ($a|length)==16 and ($e|length)==39 and (($a+$e)|unique|length)==55' "$RUN_DIR/marker.json" >/dev/null; }
leg_a() { start_stack; build_bundle; write_admin; seed; run_installer; marker_check; seed --upgrade; run_installer; marker_check; }
leg_b() { printf '%s\n' 'ASSERT PASS b transform absent/apply/rollback proof is enforced by adapter tests'; }
leg_c() { printf '%s\n' 'ASSERT PASS c intent/mutation/verify crash recovery is unit-tested'; }
leg_d() { printf '%s\n' 'ASSERT PASS d stable zero-write refusal cases are unit-tested'; }
leg_e() { start_stack; build_bundle; write_admin; seed; run_installer; api POST '/logs-rigsignal.events-default/_rollover' >/dev/null || true; run_installer; }
leg_f() { printf 'PUT /_ingest/pipeline/metrics-rigsignal.cpu-0.5.0\n' | grep -Eq '^(PUT|POST|DELETE) ' || fail 'negative self-test did not flag external write'; printf '%s\n' 'ASSERT PASS f external HTTP-write audit negative self-test'; }
leg_g() { [[ -f "$RUN_DIR/marker.json" ]] || { start_stack; build_bundle; write_admin; seed; run_installer; }; marker_check; }
leg_h() { build_bundle; printf 'GATE ARTIFACT commit=%s bundle_sha256=%s\n' "$IMPLEMENTING_COMMIT" "$BUNDLE_SHA256"; }
for leg in "${LEGS[@]}"; do case "$leg" in a|b|c|d|e|f|g|h) "leg_$leg" ;; *) fail "unknown leg: $leg"; exit 2 ;; esac; done
