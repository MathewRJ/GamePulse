#!/usr/bin/env bash
# Run the one-shot installer adoption path against a prepared clean-stack stream.
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  printf '%s\n' 'Usage: adoption-gate.sh --bundle PATH --enrollment-root PATH' >&2
}

bundle=''
enrollment_root=''
while (($#)); do
  case "$1" in
    --bundle)
      shift
      [[ $# -gt 0 ]] || { usage; exit 2; }
      bundle="$1"
      ;;
    --enrollment-root)
      shift
      [[ $# -gt 0 ]] || { usage; exit 2; }
      enrollment_root="$1"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

[[ -n "$bundle" && -f "$bundle" ]] || cs_usage_error 'a regular --bundle is required'
[[ -n "$enrollment_root" ]] || cs_usage_error '--enrollment-root is required'
: "${CS_ES_URL:?CS_ES_URL must be set by the clean-stack harness}"
: "${CS_KIBANA_URL:?CS_KIBANA_URL must be set by the clean-stack harness}"
: "${CS_CA_FILE:?CS_CA_FILE must be set by the clean-stack harness}"
: "${CS_RUN_DIR:?CS_RUN_DIR must be set by the clean-stack harness}"
: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD must be set by the clean-stack harness}"
: "${CLEAN_STACK_AGENT_BINARY:?CLEAN_STACK_AGENT_BINARY must name the handshake agent}"

admin_credentials="${CS_RUN_DIR}/admin-credentials.toml"
umask 077
escaped_password="${ELASTIC_PASSWORD//\\/\\\\}"
escaped_password="${escaped_password//\"/\\\"}"
printf '[elasticsearch]\nusername = "elastic"\npassword = "%s"\n' "$escaped_password" >"$admin_credentials"
chmod 600 "$admin_credentials"

python3 "${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}" \
  --bundle "$bundle" \
  --endpoint "$CS_ES_URL" \
  --ca-file "$CS_CA_FILE" \
  --kibana-endpoint "$CS_KIBANA_URL" \
  --kibana-ca-file "$CS_CA_FILE" \
  --admin-credentials-file "$admin_credentials" \
  --agent-binary "$CLEAN_STACK_AGENT_BINARY" \
  --profile user \
  --enrollment-root "$enrollment_root" \
  --adopt-existing-w1-stream
