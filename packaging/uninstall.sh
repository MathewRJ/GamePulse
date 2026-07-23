#!/bin/sh
# RigSignal uninstaller
#
# Usage:
#   rigsignal-uninstall [--user-only] [--purge --endpoint HTTPS_URL --ca-file PATH
#                       --admin-credentials-file PATH --enrollment-root PATH]
#
# --purge also removes RigSignal configuration. Elasticsearch data is never
# touched. DESTDIR stages removal for root-free installer tests.

set -e

USER_ONLY=0
PURGE=0
PURGE_ENDPOINT=''
PURGE_CA_FILE=''
PURGE_ADMIN_FILE=''
PURGE_ROOT=''
DESTDIR="${DESTDIR:-}"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --user-only) USER_ONLY=1 ;;
        --purge) PURGE=1 ;;
        --endpoint|--ca-file|--admin-credentials-file|--enrollment-root)
            option="$1"; shift
            if [ "$#" -eq 0 ]; then
                printf '  [err] Missing value for %s\n' "$option" >&2; exit 1
            fi
            case "$option" in
                --endpoint) PURGE_ENDPOINT="$1" ;;
                --ca-file) PURGE_CA_FILE="$1" ;;
                --admin-credentials-file) PURGE_ADMIN_FILE="$1" ;;
                --enrollment-root) PURGE_ROOT="$1" ;;
            esac
            ;;
        *)
            printf '  [err] Unknown option: %s\n' "$1" >&2
            exit 1
            ;;
    esac
    shift
done

purge_fail() { printf 'uninstall purge failed: shipper API key revocation:\n' >&2; exit 1; }
purge_output_fail() { printf 'uninstall purge failed: enrollment output:\n' >&2; exit 1; }

protected_file() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    [ "$(stat -c '%u' "$1" 2>/dev/null)" = "$(id -u)" ] || return 1
    [ $(( $(stat -c '%a' "$1" 2>/dev/null) & 077 )) -eq 0 ]
}

purge_authorization() {
    python3 - "$PURGE_ADMIN_FILE" <<'PY'
import base64, sys, tomllib
with open(sys.argv[1], 'rb') as f: data = tomllib.load(f)
v = data.get('elasticsearch')
if not isinstance(v, dict): raise SystemExit(1)
if set(v) == {'api_key'} and isinstance(v['api_key'], str): print('ApiKey ' + v['api_key'])
elif set(v) == {'username', 'password'} and all(isinstance(v[k], str) for k in v):
    print('Basic ' + base64.b64encode((v['username'] + ':' + v['password']).encode()).decode())
else: raise SystemExit(1)
PY
}

purge_ids() {
    python3 - "$PURGE_ROOT/state.json" <<'PY'
import json, re, sys
def reject(pairs):
 d={}
 for k,v in pairs:
  if k in d: raise ValueError()
  d[k]=v
 return d
with open(sys.argv[1], encoding='utf-8') as f: s=json.load(f, object_pairs_hook=reject)
keys={'version','phase','expected_cluster_uuid','target_generation','role_jcs_sha256','active_key_id','pending_revoke_ids','pending_mint_name','candidate_key_id'}
if set(s) != keys: raise ValueError()
ids=[s['active_key_id'], *s['pending_revoke_ids'], s['candidate_key_id']]
for item in ids:
 if item is not None:
  if not isinstance(item,str) or len(item.encode())>1024: raise ValueError()
  print(item)
if s['pending_mint_name'] is not None:
 if not isinstance(s['pending_mint_name'],str) or len(s['pending_mint_name'].encode())>255: raise ValueError()
 print('NAME:' + s['pending_mint_name'])
PY
}

run_purge() {
    [ -n "$PURGE_ENDPOINT" ] && [ -n "$PURGE_CA_FILE" ] && [ -n "$PURGE_ADMIN_FILE" ] && [ -n "$PURGE_ROOT" ] || purge_fail
    case "$PURGE_ENDPOINT" in https://* ) ;; *) purge_fail ;; esac
    protected_file "$PURGE_CA_FILE" && protected_file "$PURGE_ADMIN_FILE" || purge_fail
    [ -d "$PURGE_ROOT" ] && [ ! -L "$PURGE_ROOT" ] || purge_fail
    auth="$(purge_authorization)" || purge_fail
    ids_and_name="$(purge_ids)" || purge_fail
    ids="$(printf '%s\n' "$ids_and_name" | sed -n '/^NAME:/!p' | sort -u)"
    mint_name="$(printf '%s\n' "$ids_and_name" | sed -n 's/^NAME://p')"
    if [ -n "$mint_name" ]; then
        discovered="$(curl --silent --show-error --fail --max-redirs 0 --cacert "$PURGE_CA_FILE" \
            --header "Authorization: $auth" "$PURGE_ENDPOINT/_security/api_key?name=$mint_name" \
            | python3 -c 'import json,sys; print("\\n".join(x["id"] for x in json.load(sys.stdin).get("api_keys",[]) if isinstance(x.get("id"),str)))')" || purge_fail
        ids="$(printf '%s\n%s\n' "$ids" "$discovered" | sed '/^$/d' | sort -u)"
    fi
    if [ -n "$ids" ]; then
        request_body="$(printf '%s\n' "$ids" | python3 -c 'import json,sys; print(json.dumps({"ids":[x.rstrip("\\n") for x in sys.stdin if x.strip()]}))')"
        response="$(curl --silent --show-error --fail --max-redirs 0 --cacert "$PURGE_CA_FILE" \
            --header "Authorization: $auth" --header 'Content-Type: application/json' --request DELETE \
            --data "$request_body" "$PURGE_ENDPOINT/_security/api_key")" || purge_fail
        printf '%s' "$response" | python3 -c 'import json,sys; v=json.load(sys.stdin); wanted=set(sys.argv[1:]); got=set(v.get("invalidated_api_keys",[]))|set(v.get("previously_invalidated_api_keys",[])); raise SystemExit(0 if wanted <= got else 1)' $ids || purge_fail
    fi
    # Confirmation succeeded before deletion.  Never issue a shared-asset delete.
    rm -f "$PURGE_ROOT/credentials.toml" "$PURGE_ROOT/handshake.toml" \
        "$PURGE_ROOT/shipping-policy-v1.toml" "$PURGE_ROOT/state.json" "$PURGE_ROOT/candidate-credentials.toml" || purge_output_fail
    rmdir "$PURGE_ROOT/candidate" 2>/dev/null || true
}

if [ "$PURGE" = "1" ]; then run_purge; fi

removed() { printf '  removed  %s\n' "$*"; }
skipped() { printf '  skipped  %s\n' "$*"; }
info()    { printf '  [info]   %s\n' "$*"; }

stage_path() {
    printf '%s%s' "$DESTDIR" "$1"
}

remove_file() {
    path="$1"
    label="$2"
    if [ -f "$path" ] || [ -L "$path" ]; then
        rm -f "$path"
        removed "$label"
    else
        skipped "$label  (not found)"
    fi
}

remove_system_file() {
    path="$1"
    label="$2"
    if [ -f "$path" ] || [ -L "$path" ]; then
        if [ -n "$DESTDIR" ]; then
            rm -f "$path"
        else
            sudo rm -f "$path"
        fi
        removed "$label"
    else
        skipped "$label  (not found)"
    fi
}

IS_STEAMOS=0
if grep -qiE '^ID=steamos|^VARIANT_ID=steamdeck' /etc/os-release 2>/dev/null; then
    IS_STEAMOS=1
fi

USER_BIN_LOGICAL="${HOME}/.local/bin"
USER_SERVICE_LOGICAL="${HOME}/.config/systemd/user"
USER_CONFIG_LOGICAL="${XDG_CONFIG_HOME:-$HOME/.config}/rigsignal/rigsignal.toml"
USER_BIN=$(stage_path "$USER_BIN_LOGICAL")
USER_SERVICE=$(stage_path "$USER_SERVICE_LOGICAL")
USER_CONFIG=$(stage_path "$USER_CONFIG_LOGICAL")

printf '\n  Removing user-space files...\n'
if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop rigsignal-agent 2>/dev/null || true
    systemctl --user disable rigsignal-agent 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

remove_file "$USER_BIN/rigsignal-agent" "$USER_BIN_LOGICAL/rigsignal-agent"
remove_file "$USER_BIN/rigsignal" "$USER_BIN_LOGICAL/rigsignal"
remove_file "$USER_BIN/rigsignal-uninstall" "$USER_BIN_LOGICAL/rigsignal-uninstall"
remove_file "$USER_SERVICE/rigsignal-agent.service" "$USER_SERVICE_LOGICAL/rigsignal-agent.service"

if [ "$PURGE" = "1" ]; then
    remove_file "$USER_CONFIG" "$USER_CONFIG_LOGICAL"
else
    info "Keeping configuration $USER_CONFIG_LOGICAL (use --purge to remove it)."
fi

EBPF_BIN_LOGICAL="/usr/local/bin/rigsignal-ebpf"
EBPF_LIB_LOGICAL="/usr/local/lib/rigsignal"
EBPF_SVC_LOGICAL="/etc/systemd/system/rigsignal-ebpf.service"
SYSTEM_CONFIG_LOGICAL="/etc/rigsignal/rigsignal.toml"
EBPF_BIN=$(stage_path "$EBPF_BIN_LOGICAL")
EBPF_LIB=$(stage_path "$EBPF_LIB_LOGICAL")
EBPF_SVC=$(stage_path "$EBPF_SVC_LOGICAL")
SYSTEM_CONFIG=$(stage_path "$SYSTEM_CONFIG_LOGICAL")

has_system_files=0
[ -f "$EBPF_BIN" ] && has_system_files=1
[ -d "$EBPF_LIB" ] && has_system_files=1
[ -f "$EBPF_SVC" ] && has_system_files=1
[ "$PURGE" = "1" ] && [ -f "$SYSTEM_CONFIG" ] && has_system_files=1

if [ "$USER_ONLY" = "1" ]; then
    if [ "$has_system_files" = "1" ]; then
        printf '\n  System-wide eBPF files left in place (--user-only).\n'
    fi
elif [ "$has_system_files" = "1" ]; then
    if [ -z "$DESTDIR" ] && ! command -v sudo >/dev/null 2>&1; then
        printf '\n  sudo not available — system-wide eBPF files remain.\n'
    else
        printf '\n  Removing system-wide eBPF files...\n'
        steamos_ro=0
        if [ -z "$DESTDIR" ] && [ "$IS_STEAMOS" = "1" ] && command -v steamos-readonly >/dev/null 2>&1; then
            sudo steamos-readonly disable 2>/dev/null && steamos_ro=1
        fi

        if [ -z "$DESTDIR" ] && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl stop rigsignal-ebpf 2>/dev/null || true
            sudo systemctl disable rigsignal-ebpf 2>/dev/null || true
            sudo systemctl daemon-reload 2>/dev/null || true
        fi

        remove_system_file "$EBPF_BIN" "$EBPF_BIN_LOGICAL"
        remove_system_file "$EBPF_SVC" "$EBPF_SVC_LOGICAL"
        if [ -d "$EBPF_LIB" ]; then
            if [ -n "$DESTDIR" ]; then
                rm -rf "$EBPF_LIB"
            else
                sudo rm -rf "$EBPF_LIB"
            fi
            removed "$EBPF_LIB_LOGICAL/"
        fi
        if [ "$PURGE" = "1" ]; then
            remove_system_file "$SYSTEM_CONFIG" "$SYSTEM_CONFIG_LOGICAL"
        else
            info "Keeping configuration $SYSTEM_CONFIG_LOGICAL (use --purge to remove it)."
        fi

        [ "$steamos_ro" = "1" ] && sudo steamos-readonly enable 2>/dev/null || true
    fi
fi

printf '\n  RigSignal uninstalled. Remaining: configuration unless --purge; Elasticsearch data is never touched.\n\n'
