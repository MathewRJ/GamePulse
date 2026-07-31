#!/bin/sh
# rigsignal — unified launcher CLI for the RigSignal telemetry agent.
#
# Usage:
#   rigsignal setup [--ca-file <path> [--ca-sha256 <hex>]]
#                                  First-run: configure ES endpoint + API key
#   rigsignal start              Start agent (+ eBPF if sudo available)
#   rigsignal stop               Stop both services gracefully
#   rigsignal status             Show service state + last session label
#   rigsignal run %command%      Steam launch option: start → game → stop
#
# Steam integration:
#   In game Properties → Launch Options:  rigsignal run %command%

AGENT_UNIT="rigsignal-agent"
EBPF_UNIT="rigsignal-ebpf"

# Elasticsearch compatibility policy. The tested range is intentionally kept in
# one place so setup messaging and the version gate cannot drift.
ES_TESTED_MIN_VERSION="9.4.3"
ES_TESTED_MAX_VERSION="9.4.4"
ES_MIN_VERSION="8.13.0"

# User config path — mirrors what the Rust agent's Config::load() searches first.
# The agent also reads /etc/rigsignal/rigsignal.toml (system-wide), but setup
# writes the user config so credentials stay per-user and never world-readable.
case "${XDG_CONFIG_HOME:-}" in
    /*) CONFIG_HOME=$XDG_CONFIG_HOME ;;
    *) CONFIG_HOME=$HOME/.config ;;
esac
CONFIG_DIR="$CONFIG_HOME/rigsignal"
CONFIG_FILE="$CONFIG_DIR/rigsignal.toml"
CERT_DIR="$CONFIG_DIR/certs"
CERT_FILE="$CERT_DIR/elasticsearch-ca.pem"

# ── Launcher debug log ─────────────────────────────────────────────────────────
# _llog always writes timestamped entries to a persistent file so Gaming Mode
# decisions (invisible at launch time) are readable afterwards in Desktop Mode:
#   cat ~/.local/share/rigsignal/launcher.log
#
# Set RIGSIGNAL_DEBUG=1 in Steam launch options to also capture the full shell
# trace (set -x) in the same file — every branch, every command:
#   RIGSIGNAL_DEBUG=1 rigsignal run %command%

_LOG_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/rigsignal"
_LOG_FILE="$_LOG_DIR/launcher.log"
_llog() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$_LOG_FILE" 2>/dev/null || true; }

mkdir -p "$_LOG_DIR" 2>/dev/null || true
# Rotate log at 1 MB so it doesn't grow unbounded.
if [ -f "$_LOG_FILE" ]; then
    _lsz=$(wc -c < "$_LOG_FILE" 2>/dev/null || echo 0)
    [ "${_lsz:-0}" -gt 1048576 ] && mv "$_LOG_FILE" "${_LOG_FILE}.old" 2>/dev/null || true
fi

if [ "${RIGSIGNAL_DEBUG:-0}" = "1" ]; then
    # Redirect stderr to the log file before set -x so the shell trace lands there.
    # The game process will inherit fd 2 → its stderr also goes to the log in this mode.
    exec 2>>"$_LOG_FILE"
    set -x
fi

# ── Output helpers ─────────────────────────────────────────────────────────────
# Use colors only when stdout is a terminal and not running under systemd.

if [ -t 1 ] && [ -z "$JOURNAL_STREAM" ]; then
    _GRN='\033[0;32m'
    _YLW='\033[0;33m'
    _RED='\033[0;31m'
    _NC='\033[0m'
else
    _GRN='' _YLW='' _RED='' _NC=''
fi

_ok()   { printf "${_GRN}[OK]${_NC}   %s\n" "$*"; }
_warn() { printf "${_YLW}[WARN]${_NC} %s\n" "$*" >&2; }
_err()  { printf "${_RED}[ERR]${_NC}  %s\n" "$*" >&2; }
_info() { printf "       %s\n" "$*"; }
_die()  { _err "$*"; exit 1; }

# ── TOML helpers ───────────────────────────────────────────────────────────────

# Extract value from a  key = "value"  TOML line. Strips surrounding quotes.
# Works for the simple flat values rigsignal.toml uses (no multiline, no escapes).
toml_get() {
    # $1 = key name, $2 = file path
    awk -v key="$1" '
        /^\[elasticsearch\][ \t]*(#.*)?$/ { in_es=1; next }
        /^\[[^]]+\]/ { in_es=0 }
        in_es && $0 ~ "^[ \t]*" key "[ \t]*=" { print; exit }
    ' "$2" 2>/dev/null \
        | sed 's/^[^=]*=[[:space:]]*//' \
        | sed 's/[[:space:]]*#.*$//' \
        | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
        | tr -d '"' \
        | tr -d "'"
}

# Elasticsearch API keys must never be sent to an arbitrary clear-text host.
# Plain HTTP is retained only for loopback development endpoints.
validate_endpoint() {
    _endpoint="$1"
    case "$_endpoint" in
        https://?* ) return 0 ;;
        http://* )
            _http_authority=${_endpoint#http://}
            _http_authority=${_http_authority%%/*}
            _http_authority=${_http_authority%%\?*}
            _http_authority=${_http_authority%%\#*}
            case "$_http_authority" in
                ''|*@*)
                    _err "Refusing clear-text HTTP Elasticsearch endpoint outside loopback; use HTTPS (HTTP is allowed only for localhost development)."
                    return 1 ;;
                localhost|localhost:[0-9]*|[[]::1[]]|[[]::1[]]:[0-9]*) return 0 ;;
                127.*) ;;
                *)
                    _err "Refusing clear-text HTTP Elasticsearch endpoint outside loopback; use HTTPS (HTTP is allowed only for localhost development)."
                    return 1 ;;
            esac
            _http_host=${_http_authority%%:*}
            if printf '%s\n' "$_http_host" | awk -F. 'NF == 4 && $1 == 127 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ && $2 <= 255 && $3 <= 255 && $4 <= 255 { exit 0 } { exit 1 }'; then
                return 0
            fi
            _err "Refusing clear-text HTTP Elasticsearch endpoint outside loopback; use HTTPS (HTTP is allowed only for localhost development)."
            return 1 ;;
        * )
            _err "Elasticsearch endpoint must start with https:// (or http:// for a loopback development endpoint)."
            return 1 ;;
    esac
}

# ── Connectivity test ──────────────────────────────────────────────────────────

# Sets ES_STATUS and ES_BODY. Requests deliberately keep credentials out of
# output; callers report only an HTTP status and a safe remediation hint.
es_request() {
    endpoint="$1"
    api_key="$2"
    method="$3"
    path="$4"
    payload="$5"
    ca_file="$6"
    request_url="${endpoint%/}${path}"
    curl_failed=0

    if [ "${RIGSIGNAL_TEST_DISABLE_REQUEST_PYTHON:-0}" != "1" ] && command -v python3 >/dev/null 2>&1; then
        response=$(python3 - "$request_url" "$api_key" "$method" "$payload" "$ca_file" <<'PYEOF'
import ssl
import sys
import urllib.error
import urllib.request

url, key, method, payload, ca_file = sys.argv[1:]
body = payload.encode() if payload else None
headers = {"Authorization": f"ApiKey {key}"}
if body is not None:
    headers["Content-Type"] = "application/json"
request = urllib.request.Request(url, data=body, headers=headers, method=method)
context = ssl.create_default_context(cafile=ca_file or None)
class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("refusing Elasticsearch redirect")
opener = urllib.request.build_opener(RefuseRedirects(), urllib.request.HTTPSHandler(context=context))
try:
    with opener.open(request, timeout=8) as result:
        print(result.status)
        print(result.read().decode("utf-8", "replace").replace("\n", " "))
except urllib.error.HTTPError as error:
    print(error.code)
    print(error.read().decode("utf-8", "replace").replace("\n", " "))
except Exception as error:
    print(0)
    print(f"{type(error).__name__}: {error}")
PYEOF
)
    elif command -v curl >/dev/null 2>&1; then
        if [ -n "$payload" ]; then
            if [ -n "$ca_file" ]; then
                response=$(curl -q -sS --cacert "$ca_file" --max-time 8 -X "$method" \
                    -H "Authorization: ApiKey $api_key" -H "Content-Type: application/json" \
                    --data "$payload" -w '\n%{http_code}' "$request_url" 2>&1) || curl_failed=1
            else
                response=$(curl -q -sS --max-time 8 -X "$method" \
                    -H "Authorization: ApiKey $api_key" -H "Content-Type: application/json" \
                    --data "$payload" -w '\n%{http_code}' "$request_url" 2>&1) || curl_failed=1
            fi
            if [ "${curl_failed:-0}" = "1" ]; then
                ES_STATUS=0
                ES_BODY="$response"
                return 0
            fi
        else
            if [ -n "$ca_file" ]; then
                response=$(curl -q -sS --cacert "$ca_file" --max-time 8 -X "$method" \
                    -H "Authorization: ApiKey $api_key" -w '\n%{http_code}' "$request_url" 2>&1) || curl_failed=1
            else
                response=$(curl -q -sS --max-time 8 -X "$method" \
                    -H "Authorization: ApiKey $api_key" -w '\n%{http_code}' "$request_url" 2>&1) || curl_failed=1
            fi
            if [ "${curl_failed:-0}" = "1" ]; then
                ES_STATUS=0
                ES_BODY="$response"
                return 0
            fi
        fi
        ES_STATUS=$(printf '%s\n' "$response" | tail -n 1)
        ES_BODY=$(printf '%s\n' "$response" | sed '$d')
        return 0
    else
        ES_STATUS=0
        ES_BODY="Neither Python 3 nor curl is available for a verified Elasticsearch request."
        return 0
    fi

    ES_STATUS=$(printf '%s\n' "$response" | sed -n '1p')
    ES_BODY=$(printf '%s\n' "$response" | sed -n '2p')
}

is_2xx() {
    case "$1" in
        2??) return 0 ;;
        *) return 1 ;;
    esac
}

json_has_all_requested() {
    if command -v python3 >/dev/null 2>&1; then
        printf '%s' "$1" | python3 -c '
import json, sys
try:
    print("true" if json.load(sys.stdin).get("has_all_requested") is True else "false")
except (json.JSONDecodeError, AttributeError):
    print("false")
'
    else
        # No python3: emit true/false on stdout (matching the python branch's
        # contract). grep -q alone only sets exit status, which the caller —
        # which reads stdout via command substitution — would see as empty.
        if printf '%s\n' "$1" | grep -Eq '"has_all_requested"[[:space:]]*:[[:space:]]*true'; then
            printf 'true\n'
        else
            printf 'false\n'
        fi
    fi
}

json_version_number() {
    if command -v python3 >/dev/null 2>&1; then
        printf '%s' "$1" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)["version"]["number"])
except (json.JSONDecodeError, KeyError, TypeError):
    pass
'
    else
        printf '%s\n' "$1" | sed -n 's/.*"number"[[:space:]]*:[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' | head -1
    fi
}

version_at_least() {
    actual="$1"
    required="$2"
    awk -v actual="$actual" -v required="$required" 'BEGIN {
        split(actual, a, "."); split(required, r, ".");
        for (i = 1; i <= 3; i++) {
            av = (a[i] == "" ? 0 : a[i]) + 0;
            rv = (r[i] == "" ? 0 : r[i]) + 0;
            if (av > rv) exit 0;
            if (av < rv) exit 1;
        }
        exit 0;
    }'
}

validate_elasticsearch() {
    endpoint="$1"
    api_key="$2"
    ca_file="$3"
    privileges='{"index":[{"names":["metrics-rigsignal.*","logs-rigsignal.*"],"privileges":["create_doc"]}]}'

    es_request "$endpoint" "$api_key" GET "/_security/_authenticate" "" "$ca_file"
    if ! is_2xx "$ES_STATUS"; then
        _err "Elasticsearch authentication failed (HTTP status: ${ES_STATUS:-0})."
        [ -n "${ES_BODY:-}" ] && _info "$ES_BODY"
        [ "${ES_STATUS:-0}" = "0" ] && _info "For an HTTPS IP endpoint, issue a server certificate with an IP subjectAltName or use its DNS name."
        _info "Check the endpoint and API key; the key must be active and authorized."
        return 1
    fi

    es_request "$endpoint" "$api_key" POST "/_security/user/_has_privileges" "$privileges" "$ca_file"
    if ! is_2xx "$ES_STATUS"; then
        _err "Elasticsearch privilege check failed (HTTP status: ${ES_STATUS:-0})."
        _info "Check that the API key may check privileges; it needs create_doc on RigSignal data streams."
        return 1
    fi
    if [ "$(json_has_all_requested "$ES_BODY")" != "true" ]; then
        _err "API key is missing create_doc privilege for metrics-rigsignal.* and/or logs-rigsignal.*."
        _info "Create or update the key with create_doc on both RigSignal data streams."
        return 1
    fi

    es_request "$endpoint" "$api_key" GET "/" "" "$ca_file"
    if ! is_2xx "$ES_STATUS"; then
        _err "Elasticsearch version check failed (HTTP status: ${ES_STATUS:-0})."
        _info "Check the endpoint and API key; setup needs a successful Elasticsearch response."
        return 1
    fi
    es_version=$(json_version_number "$ES_BODY")
    if [ -z "$es_version" ]; then
        _warn "Could not determine Elasticsearch version; see docs/install.md for the tested range ${ES_TESTED_MIN_VERSION}–${ES_TESTED_MAX_VERSION}."
        return 0
    fi
    if ! version_at_least "$es_version" "$ES_MIN_VERSION"; then
        _err "Elasticsearch ${es_version} is unsupported; RigSignal requires ${ES_MIN_VERSION}+ (TSDS-era APIs)."
        return 1
    fi
    if ! version_at_least "$es_version" "$ES_TESTED_MIN_VERSION" || ! version_at_least "$ES_TESTED_MAX_VERSION" "$es_version"; then
        _warn "Elasticsearch ${es_version} is outside the tested ${ES_TESTED_MIN_VERSION}–${ES_TESTED_MAX_VERSION} range; see docs/install.md."
    fi
}

# ── Service control ────────────────────────────────────────────────────────────

# Wait up to 10 seconds for a user service to become active.
wait_agent_active() {
    i=0
    while [ "$i" -lt 10 ]; do
        if systemctl --user is-active "$AGENT_UNIT" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# Attempt to start/stop the eBPF system service via sudo.
# Non-fatal: eBPF is optional — agent-only mode still ships all metric streams.
ebpf_start() {
    if sudo -n systemctl start "$EBPF_UNIT" >/dev/null 2>&1; then
        _ok "eBPF daemon started ($EBPF_UNIT)"
        return 0
    elif sudo systemctl start "$EBPF_UNIT" >/dev/null 2>&1; then
        _ok "eBPF daemon started ($EBPF_UNIT)"
        return 0
    else
        _warn "eBPF daemon not started (no sudo/polkit or CAP_BPF unavailable)."
        _info "Scheduler and kernel metrics will be absent. Agent-only mode active."
        return 1
    fi
}

ebpf_stop() {
    sudo -n systemctl stop "$EBPF_UNIT" >/dev/null 2>&1 \
        || sudo systemctl stop "$EBPF_UNIT" >/dev/null 2>&1 \
        || true   # not fatal — may already be stopped or no sudo
}

# ── Agent binary resolution ────────────────────────────────────────────────────

# Look next to this script first — the user-mode installer puts rigsignal and
# rigsignal-agent in the same directory (~/.local/bin/). Gamescope / Gaming Mode
# may not have ~/.local/bin on PATH, so we can't rely on PATH alone.
resolve_agent_bin() {
    _script_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
    if [ -x "$_script_dir/rigsignal-agent" ]; then
        echo "$_script_dir/rigsignal-agent"
    elif command -v rigsignal-agent >/dev/null 2>&1; then
        command -v rigsignal-agent
    else
        echo ""
    fi
}

# ── Subcommand: setup ──────────────────────────────────────────────────────────

toml_quote() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

fsync_file_and_dir() {
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$1" "$2" >/dev/null 2>&1 <<'PYEOF'
import os
import sys
for path in sys.argv[1:]:
    fd = os.open(path, os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if os.path.isdir(path) else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
PYEOF
        return $?
    fi
    # Setup can use curl when Python is unavailable.  A whole-filesystem sync
    # is a conservative durability fallback for this one-shot operation.
    command -v sync >/dev/null 2>&1 && sync
}

validate_ca_snapshot() {
    _ca_path="$1"
    # Setup validates only the strict PEM envelope: complete certificate blocks
    # with whitespace only outside them.  Elasticsearch's TLS handshake, then
    # the agent preflight, remain the authority for X.509 validity and trust.
    awk '
        BEGIN {
            # A sentinel record separator makes the entire file one record,
            # including CRLF and a missing final newline.
            RS = "\034"
            begin = "-----BEGIN CERTIFICATE-----"
            end = "-----END CERTIFICATE-----"
        }
        {
            remainder = $0
            gsub(/^[ \t\r\n]+/, "", remainder)
            gsub(/[ \t\r\n]+$/, "", remainder)
            if (remainder == "") exit 1

            while (remainder != "") {
                if (index(remainder, begin) != 1) exit 1
                certificate = substr(remainder, length(begin) + 1)
                end_at = index(certificate, end)
                if (end_at == 0) exit 1
                remainder = substr(certificate, end_at + length(end))
                gsub(/^[ \t\r\n]+/, "", remainder)
                gsub(/[ \t\r\n]+$/, "", remainder)
            }
            valid = 1
            exit
        }
        END { exit valid ? 0 : 1 }
    ' "$_ca_path"
}

verify_ca_pin() {
    _ca_path="$1"
    _expected_sha="$2"
    [ -n "$_expected_sha" ] || return 0
    command -v sha256sum >/dev/null 2>&1 || {
        _err "sha256sum is required to verify --ca-sha256."
        return 1
    }
    _actual_sha=$(sha256sum "$_ca_path" | awk '{print $1}')
    _expected_sha=$(printf '%s' "$_expected_sha" | tr 'A-F' 'a-f')
    if [ "$_actual_sha" != "$_expected_sha" ]; then
        _err "CA SHA-256 does not match --ca-sha256; refusing before contacting Elasticsearch."
        return 1
    fi
}

snapshot_ca() {
    _ca_source="$1"
    _expected_sha="$2"
    [ -n "$_ca_source" ] || return 0
    # Reuse an early explicit snapshot or persisted CA during reauthentication.
    # Otherwise copy once before hashing, structural validation, or TLS so later
    # consumers never reopen a caller-controlled or durable path.
    if [ -n "${CA_SNAPSHOT:-}" ] && [ "${CA_SNAPSHOT_SOURCE:-}" = "$_ca_source" ]; then
        return 0
    fi
    [ -z "${CA_SNAPSHOT:-}" ] || rm -f "$CA_SNAPSHOT"
    CA_SNAPSHOT=""
    CA_SNAPSHOT_SOURCE=""
    [ -f "$_ca_source" ] && [ -r "$_ca_source" ] || {
        _err "CA file is not readable: $_ca_source"
        return 1
    }
    mkdir -p "$CERT_DIR" || return 1
    chmod 700 "$CONFIG_DIR" "$CERT_DIR" || return 1
    CA_SNAPSHOT=$(mktemp "$CERT_DIR/.elasticsearch-ca.pem.XXXXXX") || return 1
    chmod 600 "$CA_SNAPSHOT" || return 1
    cp "$_ca_source" "$CA_SNAPSHOT" || return 1
    verify_ca_pin "$CA_SNAPSHOT" "$_expected_sha" || return 1
    if ! validate_ca_snapshot "$CA_SNAPSHOT"; then
        _err "CA file must be a non-empty PEM certificate bundle: $_ca_source"
        return 1
    fi
    CA_SNAPSHOT_SOURCE=$_ca_source
}

# Rewrite only the managed [elasticsearch] fields; all other bytes remain intact
# where TOML's line-oriented representation permits it.
mutate_elasticsearch_config() {
    _input="$1"
    _output="$2"
    _endpoint_q=$(toml_quote "$3")
    _api_key_q=$(toml_quote "$4")
    _ca_cert_q=$(toml_quote "$5")
    if [ ! -f "$_input" ]; then
        cat > "$_output" <<TOML
# RigSignal configuration — written by 'rigsignal setup'
# SECURITY: this file contains your API key. Permissions are set to 600.

[elasticsearch]
endpoint = "$_endpoint_q"
api_key = "$_api_key_q"
${_ca_cert_q:+ca_cert = "$_ca_cert_q"}

[collection]
# All collectors enabled by default.
ebpf = false

[session]
# Optional: set a fixed label for all sessions.
# label = ""
TOML
        return
    fi
    awk -v endpoint="$_endpoint_q" -v api_key="$_api_key_q" -v ca_cert="$_ca_cert_q" '
        function flush_missing() {
            if (in_es) {
                if (!seen_endpoint) print "endpoint = \"" endpoint "\""
                if (!seen_api_key) print "api_key = \"" api_key "\""
                if (!seen_ca_cert && ca_cert != "") print "ca_cert = \"" ca_cert "\""
            }
        }
        /^\[elasticsearch\][ \t]*(#.*)?$/ {
            flush_missing(); in_es=1; found=1
            seen_endpoint=seen_api_key=seen_ca_cert=0
            print; next
        }
        /^\[[^]]+\]/ {
            flush_missing(); in_es=0
        }
        {
            if (in_es && $0 ~ /^[ \t]*(endpoint|api_key|ca_cert)[ \t]*=/) {
                key=$0; sub(/^[ \t]*/, "", key); sub(/[ \t]*=.*/, "", key)
                match($0, /^[ \t]*/); indent=substr($0, RSTART, RLENGTH)
                comment=""; if (match($0, /[ \t]+#/)) comment=substr($0, RSTART)
                if (key == "endpoint") { print indent "endpoint = \"" endpoint "\"" comment; seen_endpoint=1; next }
                if (key == "api_key") { print indent "api_key = \"" api_key "\"" comment; seen_api_key=1; next }
                if (key == "ca_cert") { if (ca_cert != "") print indent "ca_cert = \"" ca_cert "\"" comment; seen_ca_cert=1; next }
            }
            print
        }
        END {
            flush_missing()
            if (!found) {
                print ""
                print "[elasticsearch]"
                print "endpoint = \"" endpoint "\""
                print "api_key = \"" api_key "\""
                if (ca_cert != "") print "ca_cert = \"" ca_cert "\""
            }
        }
    ' "$_input" > "$_output"
}

# The system copy is derived from the finished user TOML.  Its only textual
# difference is the root-readable CA location; endpoint, credentials, comments,
# and every other field remain byte-for-byte intact.
rewrite_system_ca_cert() {
    _input="$1"
    _output="$2"
    _ca_cert_q=$(toml_quote "$3")
    awk -v ca_cert="$_ca_cert_q" '
        function flush_missing() {
            if (in_es && !seen_ca_cert && ca_cert != "") print "ca_cert = \"" ca_cert "\""
        }
        /^\[elasticsearch\][ \t]*(#.*)?$/ {
            flush_missing(); in_es=1; found=1; seen_ca_cert=0
            print; next
        }
        /^\[[^]]+\]/ { flush_missing(); in_es=0 }
        {
            if (in_es && $0 ~ /^[ \t]*ca_cert[ \t]*=/) {
                match($0, /^[ \t]*/); indent=substr($0, RSTART, RLENGTH)
                comment=""; if (match($0, /[ \t]+#/)) comment=substr($0, RSTART)
                if (ca_cert != "") print indent "ca_cert = \"" ca_cert "\"" comment
                seen_ca_cert=1
                next
            }
            print
        }
        END {
            flush_missing()
            if (!found && ca_cert != "") {
                print ""
                print "[elasticsearch]"
                print "ca_cert = \"" ca_cert "\""
            }
        }
    ' "$_input" > "$_output"
}

find_ebpf_bin() {
    _ebpf_bin=$(command -v rigsignal-ebpf 2>/dev/null || true)
    if [ -z "$_ebpf_bin" ]; then
        for _ebpf_candidate in /usr/local/bin/rigsignal-ebpf /usr/bin/rigsignal-ebpf "$HOME/.local/bin/rigsignal-ebpf"; do
            if [ -x "$_ebpf_candidate" ]; then
                _ebpf_bin=$_ebpf_candidate
                break
            fi
        done
    fi
    printf '%s\n' "$_ebpf_bin"
}

rollback_system_transaction() {
    _rollback_ok=1
    # Do not remove an old target until its staged backup was proven to exist.
    # This keeps a failed backup move from destroying the original.
    if [ "$_system_ca_backup_ready" = "1" ]; then
        sudo rm -f "$_system_ca" >/dev/null 2>&1 || _rollback_ok=0
        if sudo mv "${_system_ca}.bak" "$_system_ca" >/dev/null 2>&1; then
            _system_ca_backup_ready=0
        else
            _rollback_ok=0
        fi
    elif [ "$_system_ca_had" = "0" ] && [ "$_system_ca_replaced" = "1" ]; then
        sudo rm -f "$_system_ca" >/dev/null 2>&1 || _rollback_ok=0
    fi
    if [ "$_system_cfg_backup_ready" = "1" ]; then
        sudo rm -f "$_system_cfg" >/dev/null 2>&1 || _rollback_ok=0
        if sudo mv "${_system_cfg}.bak" "$_system_cfg" >/dev/null 2>&1; then
            _system_cfg_backup_ready=0
        else
            _rollback_ok=0
        fi
    elif [ "$_system_cfg_had" = "0" ] && [ "$_system_cfg_replaced" = "1" ]; then
        sudo rm -f "$_system_cfg" >/dev/null 2>&1 || _rollback_ok=0
    fi
    [ "$_rollback_ok" = "1" ]
}

# This is the only setup path that owns /etc/rigsignal/rigsignal.toml and its
# CA copy.  It is deliberately reusable by S5 without adding another dispatcher.
synchronize_ebpf_system_config() {
    _sync_ca="$1"
    command -v sudo >/dev/null 2>&1 || return 1
    SYSTEM_SCOPE_RESTORED=0
    _sync_tmp=$(mktemp "$CONFIG_DIR/.rigsignal-system.XXXXXX") || return 1
    chmod 600 "$_sync_tmp" || return 1
    rewrite_system_ca_cert "$CONFIG_FILE" "$_sync_tmp" \
        "${_sync_ca:+/etc/rigsignal/certs/elasticsearch-ca.pem}" || return 1

    _ebpf_was_active=0
    sudo systemctl is-active --quiet "$EBPF_UNIT" >/dev/null 2>&1 && _ebpf_was_active=1
    SETUP_EBPF_WAS_ACTIVE=$_ebpf_was_active
    if [ "$_ebpf_was_active" = "1" ] && ! sudo systemctl stop "$EBPF_UNIT"; then
        rm -f "$_sync_tmp"
        return 1
    fi

    # The EXIT trap restores this if setup is interrupted during elevation.
    _sync_ok=1
    if command -v steamos-readonly >/dev/null 2>&1; then
        sudo steamos-readonly disable >/dev/null 2>&1 || _sync_ok=0
        [ "$_sync_ok" = "1" ] && STEAMOS_READONLY_DISABLED=1
    fi
    if [ "$_sync_ok" = "1" ]; then
        sudo mkdir -p /etc/rigsignal/certs && sudo chmod 700 /etc/rigsignal /etc/rigsignal/certs || _sync_ok=0
    fi

    _system_ca=/etc/rigsignal/certs/elasticsearch-ca.pem
    _system_cfg=/etc/rigsignal/rigsignal.toml
    _system_ca_had=0
    _system_cfg_had=0
    _system_ca_backup_ready=0
    _system_cfg_backup_ready=0
    _system_ca_replaced=0
    _system_cfg_replaced=0
    [ "$_sync_ok" = "1" ] && sudo test -e "$_system_ca" && _system_ca_had=1
    [ "$_sync_ok" = "1" ] && sudo test -e "$_system_cfg" && _system_cfg_had=1
    if [ "$_sync_ok" = "1" ]; then
        sudo rm -f "${_system_ca}.bak" "${_system_cfg}.bak" >/dev/null 2>&1 || _sync_ok=0
        if [ "$_sync_ok" = "1" ] && [ "$_system_ca_had" = "1" ]; then
            sudo mv "$_system_ca" "${_system_ca}.bak" && sudo test -e "${_system_ca}.bak" \
                && _system_ca_backup_ready=1 || _sync_ok=0
        fi
        if [ "$_sync_ok" = "1" ] && [ "$_system_cfg_had" = "1" ]; then
            sudo mv "$_system_cfg" "${_system_cfg}.bak" && sudo test -e "${_system_cfg}.bak" \
                && _system_cfg_backup_ready=1 || _sync_ok=0
        fi
    fi
    if [ "$_sync_ok" = "1" ] && [ -n "$_sync_ca" ]; then
        _system_ca_tmp=$(sudo mktemp /etc/rigsignal/certs/.elasticsearch-ca.pem.XXXXXX) || _sync_ok=0
        [ "$_sync_ok" = "1" ] && sudo install -m 600 "$_sync_ca" "$_system_ca_tmp" \
            && sudo mv "$_system_ca_tmp" "$_system_ca" && _system_ca_replaced=1 || _sync_ok=0
    fi
    if [ "$_sync_ok" = "1" ]; then
        _system_cfg_tmp=$(sudo mktemp /etc/rigsignal/.rigsignal.toml.XXXXXX) || _sync_ok=0
        [ "$_sync_ok" = "1" ] && sudo install -m 600 "$_sync_tmp" "$_system_cfg_tmp" \
            && sudo mv "$_system_cfg_tmp" "$_system_cfg" && _system_cfg_replaced=1 || _sync_ok=0
    fi
    if [ "$_sync_ok" = "1" ]; then
        if [ -n "$_sync_ca" ]; then
            sudo python3 - "$_system_cfg" /etc/rigsignal "$_system_ca" /etc/rigsignal/certs <<'PYEOF' >/dev/null 2>&1 || _sync_ok=0
import os
import sys
for path in sys.argv[1:]:
    fd = os.open(path, os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if os.path.isdir(path) else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
PYEOF
        else
            sudo python3 - "$_system_cfg" /etc/rigsignal <<'PYEOF' >/dev/null 2>&1 || _sync_ok=0
import os
import sys
for path in sys.argv[1:]:
    fd = os.open(path, os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if os.path.isdir(path) else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
PYEOF
        fi
    fi
    if [ "$_sync_ok" = "1" ] && [ "$_ebpf_was_active" = "1" ]; then
        sudo systemctl start "$EBPF_UNIT" && sudo systemctl is-active --quiet "$EBPF_UNIT" || _sync_ok=0
    fi
    # Restore root scope while SteamOS is still writable.  If this fails, leave
    # the caller's matching user transaction intact rather than making it mixed.
    if [ "$_sync_ok" != "1" ]; then
        rollback_system_transaction || _sync_ok=0
        [ "$_system_ca_backup_ready" = "0" ] && [ "$_system_cfg_backup_ready" = "0" ] \
            && SYSTEM_SCOPE_RESTORED=1
    fi
    if [ "${STEAMOS_READONLY_DISABLED:-0}" = "1" ]; then
        # A read-only restore failure is also a transaction failure: restore
        # /etc before returning so the caller can safely roll back user scope.
        if sudo steamos-readonly enable >/dev/null 2>&1; then
            STEAMOS_READONLY_DISABLED=0
        else
            _sync_ok=0
        fi
    fi
    # A failed re-enable leaves SteamOS writable, so rollback can still occur.
    # This makes the root rollback and the caller's user rollback one failure
    # path, rather than returning a new root config beside an old user config.
    if [ "$_sync_ok" != "1" ]; then
        rollback_system_transaction || _sync_ok=0
        [ "$_system_ca_backup_ready" = "0" ] && [ "$_system_cfg_backup_ready" = "0" ] \
            && SYSTEM_SCOPE_RESTORED=1
    fi
    rm -f "$_sync_tmp"
    if [ "$_sync_ok" = "1" ]; then
        sudo rm -f "${_system_ca}.bak" "${_system_cfg}.bak" >/dev/null 2>&1 || true
    fi
    [ "$_sync_ok" = "1" ]
}

cleanup_setup() {
    _cleanup_status=$?
    _cleanup_failed=0
    if [ "${STEAMOS_READONLY_DISABLED:-0}" = "1" ]; then
        if sudo steamos-readonly enable >/dev/null 2>&1; then
            STEAMOS_READONLY_DISABLED=0
        else
            # One immediate retry covers transient SteamOS service failures.
            if sudo steamos-readonly enable >/dev/null 2>&1; then
                STEAMOS_READONLY_DISABLED=0
            else
                _err "SteamOS left writable — run steamos-readonly enable"
                _cleanup_failed=1
            fi
        fi
    fi
    rm -f "${CA_SNAPSHOT:-}" "${tmp_cfg:-}" 2>/dev/null || true
    rmdir "$CONFIG_DIR/.setup.lock" 2>/dev/null || true
    [ "$_cleanup_failed" = "0" ] || return 1
    return "$_cleanup_status"
}

rollback_user_transaction() {
    if [ "${USER_CONFIG_HAD:-0}" = "0" ]; then
        rm -f "$CONFIG_FILE" 2>/dev/null || true
    elif [ "${USER_CONFIG_BACKUP_READY:-0}" = "1" ]; then
        rm -f "$CONFIG_FILE" 2>/dev/null || true
        mv "${CONFIG_FILE}.bak" "$CONFIG_FILE" 2>/dev/null || true
    fi
    if [ "${USER_CA_CHANGED:-0}" = "1" ]; then
        if [ "${USER_CA_HAD:-0}" = "0" ]; then
            rm -f "$CERT_FILE" 2>/dev/null || true
        elif [ "${USER_CA_BACKUP_READY:-0}" = "1" ]; then
            rm -f "$CERT_FILE" 2>/dev/null || true
            mv "${CERT_FILE}.bak" "$CERT_FILE" 2>/dev/null || true
        fi
    fi
}

setup_interrupted() {
    trap - HUP INT TERM
    exit 128
}

recheck_elasticsearch_delivery() {
    es_request "$SETUP_ENDPOINT" "$SETUP_API_KEY" GET "/" "" "${CA_SNAPSHOT:-}"
    if ! is_2xx "$ES_STATUS"; then
        _err "Post-restart authenticated Elasticsearch recheck failed (HTTP status: ${ES_STATUS:-0})."
        _info "Verify the endpoint, API key, and CA, then rerun rigsignal setup before relying on telemetry delivery."
        return 1
    fi
    return 0
}

parse_setup_args() {
    SETUP_CA_FILE=""
    SETUP_CA_SHA256=""
    SETUP_ARG_ERROR="usage"
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --ca-file)
                [ -z "$SETUP_CA_FILE" ] && [ "$#" -ge 2 ] && [ -n "$2" ] || return 1
                SETUP_CA_FILE=$2; shift 2 ;;
            --ca-sha256)
                [ -z "$SETUP_CA_SHA256" ] && [ "$#" -ge 2 ] && [ -n "$2" ] || return 1
                SETUP_CA_SHA256=$2; shift 2 ;;
            *) return 1 ;;
        esac
    done
    if [ -n "$SETUP_CA_SHA256" ]; then
        case "$SETUP_CA_SHA256" in *[!0123456789abcdefABCDEF]*) return 1 ;; esac
        [ "${#SETUP_CA_SHA256}" -eq 64 ] || return 1
    fi
    [ -z "$SETUP_CA_SHA256" ] || [ -n "$SETUP_CA_FILE" ] || {
        SETUP_ARG_ERROR="pin"
        _err "--ca-sha256 requires --ca-file; a pin alone cannot establish trust."
        return 1
    }
    return 0
}

cmd_setup() {
    # Debug tracing would expand the API key in command arguments. Disable it
    # for setup so credentials never reach the persistent launcher debug log.
    [ "${RIGSIGNAL_DEBUG:-0}" = "1" ] && set +x

    if ! parse_setup_args "$@"; then
        [ "$SETUP_ARG_ERROR" = "pin" ] || _err "Usage: rigsignal setup [--ca-file <path> [--ca-sha256 <64-hex>]]"
        exit 1
    fi
    CA_SNAPSHOT=""
    CA_SNAPSHOT_SOURCE=""
    STEAMOS_READONLY_DISABLED=0
    trap cleanup_setup EXIT
    trap setup_interrupted HUP INT TERM
    if [ -n "$SETUP_CA_FILE" ]; then
        mkdir -p "$CONFIG_DIR" || _die "Could not create $CONFIG_DIR."
        chmod 700 "$CONFIG_DIR" || _die "Could not secure $CONFIG_DIR."
        snapshot_ca "$SETUP_CA_FILE" "$SETUP_CA_SHA256" || exit 1
    fi
    for _override in RIGSIGNAL_CONFIG ES_URL ES_API_KEY ES_CA_CERT; do
        eval "_override_value=\${$_override:-}"
        [ -z "$_override_value" ] || _die "Refusing setup while $_override is set: unset it so the selected Elasticsearch target is unambiguous."
    done

    mkdir -p "$CONFIG_DIR" || _die "Could not create $CONFIG_DIR."
    chmod 700 "$CONFIG_DIR" || _die "Could not secure $CONFIG_DIR."
    mkdir "$CONFIG_DIR/.setup.lock" 2>/dev/null || _die "Another RigSignal setup is in progress; retry when it finishes."

    # Check for existing valid config first.  An explicit CA is a requested
    # replacement, so it intentionally continues through the durable write path.
    if [ -f "$CONFIG_FILE" ]; then
        existing_endpoint=$(toml_get "endpoint" "$CONFIG_FILE")
        existing_key=$(toml_get "api_key" "$CONFIG_FILE")
        existing_ca=$(toml_get "ca_cert" "$CONFIG_FILE")

        if [ -n "$existing_endpoint" ] && [ -n "$existing_key" ]; then
            validate_endpoint "$existing_endpoint" || exit 1
            SETUP_ENDPOINT=$existing_endpoint
            SETUP_API_KEY=$existing_key
            _ca_source=$SETUP_CA_FILE
            [ -n "$_ca_source" ] || _ca_source=$existing_ca
            if [ -z "$_ca_source" ] && [ "${existing_endpoint#https://}" != "$existing_endpoint" ]; then
                printf "CA certificate file (leave empty to use system trust): "
                read -r _ca_source
            fi
            snapshot_ca "$_ca_source" "$SETUP_CA_SHA256" || exit 1
            printf "       Config found: %s\n" "$CONFIG_FILE"
            printf "       Endpoint:     %s\n" "$existing_endpoint"
            printf "       Verifying authentication and write privileges...\n"
            if validate_elasticsearch "$existing_endpoint" "$existing_key" "$CA_SNAPSHOT"; then
                if [ -z "$SETUP_CA_FILE" ] && [ -z "$CA_SNAPSHOT" ]; then
                    _ok "Already configured. Authentication and write privileges verified."
                    _ebpf_bin=$(find_ebpf_bin)
                    if [ -n "$_ebpf_bin" ]; then
                        synchronize_ebpf_system_config "" || _die "eBPF is installed but its system config could not be synchronized. Restore sudo access, then rerun rigsignal setup."
                        _ok "eBPF daemon config synchronized under /etc/rigsignal"
                        [ "${SETUP_EBPF_WAS_ACTIVE:-0}" = "0" ] || recheck_elasticsearch_delivery || exit 1
                    fi
                    return 0
                fi
                _ok "Authentication and write privileges verified. Updating the CA snapshot."
            else
                _warn "Already configured but connection failed."
                printf "       Endpoint: %s\n" "$existing_endpoint"
                printf "       Re-running setup to update credentials.\n\n"
                SETUP_ENDPOINT=""
                SETUP_API_KEY=""
            fi
        fi
    fi

    if [ -z "${SETUP_ENDPOINT:-}" ]; then
    # Print a brief explanation.
    printf '\n%sRigSignal Setup%s\n' "$_GRN" "$_NC"
    printf "===============\n"
    printf "RigSignal ships gaming metrics to Elasticsearch.\n"
    printf "You need an Elastic Cloud deployment (or self-hosted ES 8.13+).\n\n"
    printf "You will need:\n"
    printf "  - Your Elasticsearch endpoint URL\n"
    printf "  - An API key with write access to metrics-rigsignal.* indices\n\n"

    # Prompt for endpoint.
    printf "Elasticsearch endpoint\n"
    printf "  Example: https://my-deployment.es.us-central1.gcp.elastic.cloud\n"
    printf "  Endpoint: "
    read -r endpoint

    if [ -z "$endpoint" ]; then
        _die "Endpoint cannot be empty."
    fi

    # Strip trailing slash.
    endpoint="${endpoint%/}"
    validate_endpoint "$endpoint" || exit 1

    # Prompt for API key (stty -echo to prevent it appearing in terminal logs).
    printf "API key (input hidden): "
    # Use stty to suppress echo if available (POSIX but may not work in all contexts)
    if stty -echo 2>/dev/null; then
        read -r api_key
        stty echo 2>/dev/null
        printf "\n"
    else
        read -r api_key
    fi

    if [ -z "$api_key" ]; then
        _die "API key cannot be empty."
    fi

    _ca_source=$SETUP_CA_FILE
    [ -n "$_ca_source" ] || _ca_source=${existing_ca:-}
    if [ -z "$_ca_source" ] && [ "${endpoint#https://}" != "$endpoint" ]; then
        printf "CA certificate file (leave empty to use system trust): "
        read -r _ca_source
    fi
    snapshot_ca "$_ca_source" "$SETUP_CA_SHA256" || exit 1

    SETUP_ENDPOINT=$endpoint
    SETUP_API_KEY=$api_key
    fi

    if [ -z "${existing_endpoint:-}" ] || [ "$SETUP_ENDPOINT" != "$existing_endpoint" ] || [ -z "${existing_key:-}" ] || [ "$SETUP_API_KEY" != "$existing_key" ]; then
        printf "       Verifying authentication and write privileges for %s ...\n" "$SETUP_ENDPOINT"
        if ! validate_elasticsearch "$SETUP_ENDPOINT" "$SETUP_API_KEY" "$CA_SNAPSHOT"; then
            exit 1
        fi
        _ok "Authentication and write privileges verified."
    fi

    # Stop only services that were active before replacing either user file.  This
    # is deliberately independent of eBPF discovery: the user agent reads this
    # config even on installations without the optional daemon.
    SETUP_AGENT_WAS_ACTIVE=0
    systemctl --user is-active --quiet "$AGENT_UNIT" 2>/dev/null && SETUP_AGENT_WAS_ACTIVE=1
    if [ "$SETUP_AGENT_WAS_ACTIVE" = "1" ] && ! systemctl --user stop "$AGENT_UNIT"; then
        _die "Could not stop $AGENT_UNIT before replacing its configuration."
    fi

    # Install the exact validated bytes and TOML as one user-scope transaction.
    # Keep the old files as .bak until both replacements and durability checks
    # succeed so a later write failure cannot leave a mixed configuration.
    if [ -n "$CA_SNAPSHOT" ]; then
        SETUP_CA_CERT=$CERT_FILE
    else
        SETUP_CA_CERT=""
    fi
    tmp_cfg=$(mktemp "$CONFIG_DIR/.rigsignal.toml.XXXXXX") || _die "Could not create a config temporary file."
    chmod 600 "$tmp_cfg"
    mutate_elasticsearch_config "$CONFIG_FILE" "$tmp_cfg" "$SETUP_ENDPOINT" "$SETUP_API_KEY" "$SETUP_CA_CERT" \
        || _die "Could not update Elasticsearch settings."
    fsync_file_and_dir "$tmp_cfg" "$CONFIG_DIR" || _die "Could not fsync the new configuration."
    USER_CONFIG_HAD=0 USER_CA_HAD=0 USER_CA_CHANGED=0
    USER_CONFIG_BACKUP_READY=0 USER_CA_BACKUP_READY=0
    [ -e "$CONFIG_FILE" ] && USER_CONFIG_HAD=1
    [ -n "$CA_SNAPSHOT" ] && USER_CA_CHANGED=1
    [ "$USER_CA_CHANGED" = "0" ] || { [ -e "$CERT_FILE" ] && USER_CA_HAD=1; }
    if [ "$USER_CONFIG_HAD" = "1" ]; then
        rm -f "${CONFIG_FILE}.bak" || _die "Could not clear the previous configuration rollback staging file."
        mv "$CONFIG_FILE" "${CONFIG_FILE}.bak" && [ -e "${CONFIG_FILE}.bak" ] \
            && USER_CONFIG_BACKUP_READY=1 || _die "Could not stage the previous configuration for rollback."
    fi
    if [ "$USER_CA_CHANGED" = "1" ]; then
        if [ "$USER_CA_HAD" = "1" ]; then
            rm -f "${CERT_FILE}.bak" || { rollback_user_transaction; _die "Could not clear the previous CA rollback staging file."; }
            mv "$CERT_FILE" "${CERT_FILE}.bak" && [ -e "${CERT_FILE}.bak" ] \
                && USER_CA_BACKUP_READY=1 || { rollback_user_transaction; _die "Could not stage the previous CA for rollback."; }
        fi
        _user_ca_tmp=$(mktemp "$CERT_DIR/.elasticsearch-ca.install.XXXXXX") || { rollback_user_transaction; _die "Could not create a CA temporary file."; }
        chmod 600 "$_user_ca_tmp" && install -m 600 "$CA_SNAPSHOT" "$_user_ca_tmp" && mv "$_user_ca_tmp" "$CERT_FILE" || { rm -f "$_user_ca_tmp"; rollback_user_transaction; _die "Could not install the CA snapshot."; }
    fi
    mv "$tmp_cfg" "$CONFIG_FILE" || { rollback_user_transaction; _die "Could not atomically replace $CONFIG_FILE."; }
    _user_durable=1
    fsync_file_and_dir "$CONFIG_FILE" "$CONFIG_DIR" || _user_durable=0
    if [ "$USER_CA_CHANGED" = "1" ]; then
        fsync_file_and_dir "$CERT_FILE" "$CERT_DIR" || _user_durable=0
    fi
    if [ "$_user_durable" != "1" ]; then
        rollback_user_transaction
        _die "Could not durably install the user configuration."
    fi

    _ok "Config written to $CONFIG_FILE"

    _ebpf_bin=$(find_ebpf_bin)
    if [ -n "$_ebpf_bin" ]; then
        if ! synchronize_ebpf_system_config "$CA_SNAPSHOT"; then
            if [ "${SYSTEM_SCOPE_RESTORED:-0}" = "1" ]; then
                rollback_user_transaction
            else
                _err "The eBPF system configuration could not be restored; leaving the matching user transaction in place to avoid a mixed configuration."
            fi
            [ "$SETUP_AGENT_WAS_ACTIVE" = "0" ] || systemctl --user start "$AGENT_UNIT" >/dev/null 2>&1 || true
            _die "eBPF is installed but its system config could not be synchronized. Restore sudo access, then rerun rigsignal setup --ca-file <path>."
        fi
        _ok "eBPF daemon config and CA updated under /etc/rigsignal"
    fi
    rm -f "${CONFIG_FILE}.bak" "${CERT_FILE}.bak"

    if [ "$SETUP_AGENT_WAS_ACTIVE" = "1" ]; then
        systemctl --user start "$AGENT_UNIT" && wait_agent_active || _die "Could not restart $AGENT_UNIT after updating its configuration."
    fi
    if [ "$SETUP_AGENT_WAS_ACTIVE" = "1" ] || [ "${SETUP_EBPF_WAS_ACTIVE:-0}" = "1" ]; then
        recheck_elasticsearch_delivery || exit 1
    fi

    _info "Run 'rigsignal start' to begin collecting, or add 'rigsignal run %command%' as a Steam launch option."
}

# ── Subcommand: start ──────────────────────────────────────────────────────────

cmd_start() {
    # Start the user agent service.
    if systemctl --user start "$AGENT_UNIT" 2>/dev/null; then
        if wait_agent_active; then
            _ok "Agent started ($AGENT_UNIT)"
        else
            _warn "Agent started but did not become active within 10 s — check journald:"
            _info "  journalctl --user -u $AGENT_UNIT -n 20"
        fi
    else
        _die "Failed to start $AGENT_UNIT. Is it installed? Check: systemctl --user status $AGENT_UNIT"
    fi

    # Try the eBPF system service (optional — degrades gracefully).
    ebpf_start || true
}

# ── Subcommand: stop ───────────────────────────────────────────────────────────

cmd_stop() {
    systemctl --user stop "$AGENT_UNIT" >/dev/null 2>&1 && _ok "Agent stopped" || true
    ebpf_stop && _ok "eBPF daemon stopped" || true
}

# ── Subcommand: status ─────────────────────────────────────────────────────────

cmd_status() {
    # Agent service state — split capture from fallback so the fallback
    # echo doesn't double-up with systemctl's own stdout inside $().
    agent_state=$(systemctl --user is-active "$AGENT_UNIT" 2>/dev/null) || agent_state="inactive"
    ebpf_state=$(systemctl is-active "$EBPF_UNIT" 2>/dev/null) || ebpf_state="inactive"

    printf "\n  Agent  (%s): %s\n" "$AGENT_UNIT" "$agent_state"
    printf "  eBPF   (%s):  %s\n\n" "$EBPF_UNIT" "$ebpf_state"

    # Last session label from journald (if agent was ever run).
    last_label=$(journalctl --user -u "$AGENT_UNIT" -n 50 --no-pager 2>/dev/null \
        | grep -o 'label="[^"]*"' | tail -1 | sed 's/label="//;s/"//')
    last_game=$(journalctl --user -u "$AGENT_UNIT" -n 50 --no-pager 2>/dev/null \
        | grep "Game detected:" | tail -1 \
        | sed 's/.*Game detected: \([^(]*\).*/\1/' | sed 's/[[:space:]]*$//')

    if [ -n "$last_game" ]; then
        printf "  Last game:  %s\n" "$last_game"
    fi
    if [ -n "$last_label" ]; then
        printf "  Last label: %s\n" "$last_label"
    fi

    printf "\n  Logs:   journalctl --user -u %s -f\n" "$AGENT_UNIT"
    printf "  Config: %s\n\n" "$CONFIG_FILE"
}

# ── Subcommand: run ────────────────────────────────────────────────────────────
# Steam launch option form: rigsignal run %command%
# Steam expands %command% to the full game executable + arguments.

cmd_run() {
    if [ $# -eq 0 ]; then
        _die "Usage: rigsignal run <command> [args...]"
    fi

    # Strip leading KEY=VALUE args and export them so users can write:
    #   rigsignal run RIGSIGNAL_LOG=debug %command%
    # rather than needing to place env vars before 'rigsignal' in the launch option.
    while [ $# -gt 0 ]; do
        case "$1" in
            [A-Za-z_]*=*)
                _varname="${1%%=*}"
                case "$_varname" in
                    *[!A-Za-z0-9_]*) break ;;
                    *)
                        _llog "cmd_run: exporting env: $_varname"
                        # shellcheck disable=SC2163  # $1 is NAME=VALUE by prior validation
                        export "$1"
                        shift
                        continue
                        ;;
                esac
                ;;
        esac
        break
    done

    if [ $# -eq 0 ]; then
        _die "No command remaining after env var extraction."
    fi

    _llog "cmd_run: command=$*"

    # Resolve the agent binary before starting anything.
    AGENT_BIN="$(resolve_agent_bin)"
    _llog "cmd_run: AGENT_BIN='$AGENT_BIN'"
    if [ -z "$AGENT_BIN" ]; then
        _warn "rigsignal-agent not found — launching game without telemetry."
        _info "Re-run the installer or add ~/.local/bin to PATH."
        _llog "cmd_run: agent not found — exec-ing game directly without telemetry"
        exec "$@"
    fi

    # Enable MangoHud frame timing collection via MANGOHUD=1.
    # We export MANGOHUD=1 rather than wrapping with the `mangohud` binary so
    # that the Steam Linux Runtime (pressure-vessel) uses its own bundled MangoHud
    # layer — the host binary cannot inject across the container boundary.
    # autostart_log=1 is written to ~/.config/MangoHud/MangoHud.conf by the
    # installer so it applies unconditionally; no_display=1 hides the overlay.
    # Set RIGSIGNAL_MANGOHUD=0 to opt out entirely.
    # Set RIGSIGNAL_MANGOHUD=display to show the overlay.
    if [ "${RIGSIGNAL_MANGOHUD:-auto}" != "0" ] && [ -z "${MANGOHUD:-}" ]; then
        MANGOHUD=1
        export MANGOHUD
        _llog "cmd_run: MANGOHUD=1 set (SLR bundled MangoHud, autostart_log via conf)"
        if [ "${RIGSIGNAL_MANGOHUD:-auto}" != "display" ]; then
            MANGOHUD_CONFIG="no_display=1"
            export MANGOHUD_CONFIG
            _llog "cmd_run: MANGOHUD_CONFIG=no_display=1 (overlay hidden)"
        else
            _llog "cmd_run: RIGSIGNAL_MANGOHUD=display — overlay visible"
        fi
    fi

    # Start the agent — systemd if reachable, direct binary otherwise.
    # Reset any prior FAILED state so the unit can always be started fresh.
    systemctl --user reset-failed "$AGENT_UNIT" 2>/dev/null || true
    _llog "cmd_run: reset-failed $AGENT_UNIT"

    # Capture the launcher PID before exec replaces this shell with the game.
    # In POSIX sh, $$ in a subshell still refers to the parent shell's PID,
    # so the watcher correctly monitors the game process after exec.
    _gp_pid=$$
    _llog "cmd_run: launcher PID=$_gp_pid (becomes game PID after exec)"

    if systemctl --user start "$AGENT_UNIT" >/dev/null 2>&1; then
        _llog "cmd_run: systemctl --user start $AGENT_UNIT succeeded (service mode)"
        # Double-fork the watcher: the outer subshell exits immediately, making
        # the inner watcher a child of init (PID 1) rather than of the exec'd
        # process. Without this, exec replaces this shell with steam-launch-wrapper
        # which becomes reaper — reaper then waits for the watcher (its child) and
        # the watcher waits for reaper to exit: deadlock, permanent black screen.
        # /proc/<pid> vanishes atomically on process exit — no PID-reuse race.
        (
          (
            while [ -d "/proc/$_gp_pid" ]; do sleep 1; done
            _llog "cmd_run: game PID $_gp_pid exited — stopping $AGENT_UNIT"
            systemctl --user stop "$AGENT_UNIT" --no-block 2>/dev/null || true
          ) &
        ) &
    else
        _llog "cmd_run: systemctl start failed (DBUS absent? unit missing?) — running agent directly"
        "$AGENT_BIN" &
        _agent_pid=$!
        _llog "cmd_run: direct agent started PID=$_agent_pid"
        (
          (
            while [ -d "/proc/$_gp_pid" ]; do sleep 1; done
            _llog "cmd_run: game PID $_gp_pid exited — kill -TERM agent $_agent_pid"
            kill -TERM "$_agent_pid" 2>/dev/null || true
          ) &
        ) &
    fi

    # exec replaces the launcher shell with the game process so Steam/Gamescope
    # tracks the correct PID for cgroup assignment, GPU priority, and display
    # management. Running the game as a subprocess (not exec) puts it in the
    # wrong place in the process tree and breaks Gaming Mode (Gamescope).
    _llog "cmd_run: exec-ing game (this shell becomes the game process): $*"
    exec "$@"
}

# ── Usage ──────────────────────────────────────────────────────────────────────

usage() {
    cat << 'EOF'
RigSignal launcher

Usage:
  rigsignal setup [--ca-file <path> [--ca-sha256 <hex>]]
                              Configure Elasticsearch endpoint, API key, and CA
  rigsignal start              Start agent (+ eBPF if sudo available)
  rigsignal stop               Stop both services gracefully
  rigsignal status             Show service status and last session info
  rigsignal run <cmd> [args]   Start services, run command, stop on exit

Steam integration — set in game Properties → Launch Options:
  rigsignal run %command%

Examples:
  rigsignal setup --ca-file ./http_ca.crt
  rigsignal start
  rigsignal status
  rigsignal run ./mygame --fullscreen
EOF
}

# ── Dispatch ───────────────────────────────────────────────────────────────────

subcmd="${1:-}"
_llog "launch: $0 $*"
shift 2>/dev/null || true

case "$subcmd" in
    setup)  cmd_setup "$@" ;;
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status)
        # Reserve the durable handshake recheck surface without allowing malformed
        # status arguments to silently fall through into ordinary status output.
        case "$#" in
            0) cmd_status ;;
            2)
                if [ "$1" = "handshake" ] && [ "$2" = "recheck" ]; then
                    _err "handshake recheck is not yet available"
                else
                    _err "Invalid status arguments"
                fi
                exit 1
                ;;
            3)
                if [ "$1" = "handshake" ] && [ "$2" = "recheck" ] \
                    && [ "${#3}" -eq 64 ] && [ -n "$3" ] \
                    && case "$3" in *[!0123456789abcdef]*) false ;; *) true ;; esac
                then
                    _err "handshake recheck is not yet available"
                else
                    _err "Invalid status arguments"
                fi
                exit 1
                ;;
            *) _err "Invalid status arguments"; exit 1 ;;
        esac
        ;;
    run)    cmd_run "$@" ;;
    "")     usage; exit 0 ;;
    *)      _err "Unknown subcommand: $subcmd"; echo; usage; exit 1 ;;
esac
