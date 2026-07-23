#!/usr/bin/env bash
# TLS/bootstrap adapter used by the clean-stack matrix only.
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"

usage() {
  printf '%s\n' 'Usage: install-wrapper.sh [--bundle PATH] [--enrollment-root PATH]' >&2
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

: "${CS_ES_URL:?CS_ES_URL must be set by the clean-stack harness}"
: "${CS_KIBANA_URL:?CS_KIBANA_URL must be set by the clean-stack harness}"
: "${CS_CA_FILE:?CS_CA_FILE must be set by the clean-stack harness}"
: "${CS_RUN_DIR:?CS_RUN_DIR must be set by the clean-stack harness}"
: "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD must be set by the clean-stack harness}"

if [[ -z "$bundle" ]]; then
  bundle="${CLEAN_STACK_PREBUILT_BUNDLE:-}"
fi
if [[ -z "$bundle" ]]; then
  bundle="${CS_RUN_DIR}/assets.tar.gz"
  python3 "$REPO_ROOT/tools/build_asset_bundle.py" \
    --source-commit "$(git -C "$REPO_ROOT" rev-parse HEAD)" \
    --output "$bundle"
fi
[[ -f "$bundle" ]] || { printf 'error: asset bundle is not a regular file\n' >&2; exit 1; }

if [[ -z "$enrollment_root" ]]; then
  enrollment_root="${CS_RUN_DIR}/enrollment"
fi

admin_credentials="${CS_RUN_DIR}/admin-credentials.toml"
umask 077
escaped_password="${ELASTIC_PASSWORD//\\/\\\\}"
escaped_password="${escaped_password//\"/\\\"}"
printf '[elasticsearch]\nusername = "elastic"\npassword = "%s"\n' "$escaped_password" >"$admin_credentials"
chmod 600 "$admin_credentials"

agent_binary="${CLEAN_STACK_AGENT_BINARY:-$REPO_ROOT/target/debug/rigsignal-agent}"
if [[ -z "${CLEAN_STACK_AGENT_BINARY:-}" ]]; then
  cargo build --manifest-path "$REPO_ROOT/src/Cargo.toml" --locked
fi
[[ -x "$agent_binary" ]] || { printf 'error: rigsignal-agent binary is not executable\n' >&2; exit 1; }

installer="${CLEAN_STACK_INSTALLER:-$REPO_ROOT/tools/install_assets.py}"
if [[ "$installer" == *.py ]]; then
  python3 "$installer" \
    --bundle "$bundle" \
    --endpoint "$CS_ES_URL" \
    --ca-file "$CS_CA_FILE" \
    --kibana-endpoint "$CS_KIBANA_URL" \
    --kibana-ca-file "$CS_CA_FILE" \
    --admin-credentials-file "$admin_credentials" \
    --agent-binary "$agent_binary" \
    --profile user \
    --enrollment-root "$enrollment_root"
else
  "$installer" \
    --bundle "$bundle" \
    --endpoint "$CS_ES_URL" \
    --ca-file "$CS_CA_FILE" \
    --kibana-endpoint "$CS_KIBANA_URL" \
    --kibana-ca-file "$CS_CA_FILE" \
    --admin-credentials-file "$admin_credentials" \
    --agent-binary "$agent_binary" \
    --profile user \
    --enrollment-root "$enrollment_root"
fi
