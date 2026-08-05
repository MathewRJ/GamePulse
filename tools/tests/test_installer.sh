#!/usr/bin/env bash
# Root-free, network-free installer hardening tests. Run with:
#   bash tools/tests/test_installer.sh

set -u

# Config discovery honors SUDO_USER before HOME; keep each fixture rooted in
# its test-specific HOME even when this harness is launched from sudo.
export SUDO_USER=""

REPO_ROOT=${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
LAUNCHER="$REPO_ROOT/packaging/rigsignal-launcher.sh"
INSTALLER="$REPO_ROOT/packaging/install.sh"
UNINSTALLER="$REPO_ROOT/packaging/uninstall.sh"
TEST_TMP=$(mktemp -d)
MOCK_PID=""
MOCK_REQUESTS=""
MOCK_TARGET_PORT=""
PASS_COUNT=0
FAIL_COUNT=0

cleanup() {
    if [[ -n "$MOCK_PID" ]]; then
        kill "$MOCK_PID" 2>/dev/null || true
        wait "$MOCK_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_TMP"
}
trap cleanup EXIT

pass() {
    printf 'TEST PASS %s\n' "$1"
    PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
    printf 'TEST FAIL %s\n' "$1" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

run_test() {
    local name="$1"
    shift
    if "$@"; then
        pass "$name"
    else
        fail "$name"
    fi
}

start_mock_es() {
    local mode="$1"
    local port_file="$TEST_TMP/mock-port-${mode}"
    local request_file="$TEST_TMP/mock-requests-${mode}"
    rm -f "$port_file"
    : >"$request_file"
    python3 -c '
import http.server, json, ssl, sys, threading
mode, port_file, request_file, cert_file, key_file = sys.argv[1:]
class Target(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def do_GET(self):
        with open(request_file, "a", encoding="utf-8") as handle:
            handle.write("target-auth:" + self.headers.get("Authorization", "") + "\\n")
        self.send_response(200); self.end_headers()
target = None
if mode == "redirect":
    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def send_json(self, status, body):
        with open(request_file, "a", encoding="utf-8") as handle: handle.write(self.path + "\\n")
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def do_GET(self):
        if mode == "redirect" and self.path == "/_security/_authenticate":
            with open(request_file, "a", encoding="utf-8") as handle: handle.write(self.path + "\\n")
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:%d/redirect-target" % target.server_address[1])
            self.end_headers()
        elif self.path == "/_security/_authenticate":
            self.send_json(401 if mode == "unauthorized" else 200, {"username": "rigsignal"})
        elif self.path == "/":
            self.send_json(200, {"version": {"number": "9.4.3"}})
        else:
            self.send_json(404, {"error": "not found"})
    def do_POST(self):
        if self.path == "/_security/user/_has_privileges":
            self.send_json(200, {"has_all_requested": mode != "missing_privilege"})
        else:
            self.send_json(404, {"error": "not found"})
server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
if mode != "plain_happy":
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
with open(port_file, "w", encoding="utf-8") as handle:
    handle.write(str(server.server_address[1]) + "\n" + (str(target.server_address[1]) if target else ""))
server.serve_forever()
' "$mode" "$port_file" "$request_file" "$TLS_SERVER_CERT" "$TLS_SERVER_KEY" &
    MOCK_PID=$!

    local attempts=0
    while [[ ! -s "$port_file" && "$attempts" -lt 50 ]]; do
        kill -0 "$MOCK_PID" 2>/dev/null || return 1
        sleep 0.1
        attempts=$((attempts + 1))
    done
    [[ -s "$port_file" ]] || return 1
    MOCK_PORT=$(sed -n '1p' "$port_file")
    MOCK_TARGET_PORT=$(sed -n '2p' "$port_file")
    MOCK_REQUESTS=$request_file
}

stop_mock_es() {
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
}

run_setup_against_mock() {
    local mode="$1"
    local home="$TEST_TMP/home-${mode}"
    local output="$TEST_TMP/setup-${mode}.out"
    local setup_status
    mkdir -p "$home"
    start_mock_es "$mode" || return 1
    if printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$output" 2>&1; then
        setup_status=0
    else
        setup_status=$?
    fi
    stop_mock_es
    printf '%s' "$setup_status"
}

make_tls_certificates() {
    TLS_DIR="$TEST_TMP/tls"
    TLS_CA="$TLS_DIR/ca.pem"
    TLS_CA_TWO="$TLS_DIR/ca-two.pem"
    TLS_CA_THREE="$TLS_DIR/ca-three.pem"
    TLS_BUNDLE="$TLS_DIR/ca-bundle.pem"
    TLS_BUNDLE_THREE="$TLS_DIR/ca-bundle-three.pem"
    TLS_SERVER_KEY="$TLS_DIR/server.key"
    TLS_SERVER_CERT="$TLS_DIR/server.pem"
    mkdir -p "$TLS_DIR" || return 1
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -sha256 \
        -subj '/CN=RigSignal test CA' \
        -addext 'basicConstraints=critical,CA:TRUE' \
        -addext 'keyUsage=critical,keyCertSign,cRLSign' \
        -keyout "$TLS_DIR/ca.key" -out "$TLS_CA" >/dev/null 2>&1 || return 1
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -sha256 \
        -subj '/CN=RigSignal second test CA' \
        -addext 'basicConstraints=critical,CA:TRUE' \
        -addext 'keyUsage=critical,keyCertSign,cRLSign' \
        -keyout "$TLS_DIR/ca-two.key" -out "$TLS_CA_TWO" >/dev/null 2>&1 || return 1
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -sha256 \
        -subj '/CN=RigSignal third test CA' \
        -addext 'basicConstraints=critical,CA:TRUE' \
        -addext 'keyUsage=critical,keyCertSign,cRLSign' \
        -keyout "$TLS_DIR/ca-three.key" -out "$TLS_CA_THREE" >/dev/null 2>&1 || return 1
    openssl req -newkey rsa:2048 -nodes -subj '/CN=127.0.0.1' \
        -keyout "$TLS_SERVER_KEY" -out "$TLS_DIR/server.csr" >/dev/null 2>&1 || return 1
    printf '%s\n' \
        'basicConstraints=critical,CA:FALSE' \
        'keyUsage=critical,digitalSignature,keyEncipherment' \
        'extendedKeyUsage=serverAuth' \
        'subjectAltName=IP:127.0.0.1' >"$TLS_DIR/server.ext"
    openssl x509 -req -days 1 -sha256 -in "$TLS_DIR/server.csr" -CA "$TLS_CA" -CAkey "$TLS_DIR/ca.key" \
        -CAcreateserial -extfile "$TLS_DIR/server.ext" -out "$TLS_SERVER_CERT" >/dev/null 2>&1 || return 1
    cat "$TLS_CA" "$TLS_CA_TWO" >"$TLS_BUNDLE"
    cat "$TLS_CA" "$TLS_CA_TWO" "$TLS_CA_THREE" >"$TLS_BUNDLE_THREE"
}

test_setup_fails_on_401() {
    [[ "$(run_setup_against_mock unauthorized)" != "0" ]]
}

test_setup_fails_without_create_doc() {
    [[ "$(run_setup_against_mock missing_privilege)" != "0" ]]
}

test_setup_succeeds_with_required_privileges() {
    local home="$TEST_TMP/home-happy"
    [[ "$(run_setup_against_mock happy)" == "0" ]] \
        && grep -q '^ca_cert = ' "$home/.config/rigsignal/rigsignal.toml" \
        && cmp -s "$TLS_BUNDLE" "$home/.config/rigsignal/certs/elasticsearch-ca.pem"
}

test_setup_rejects_bad_pin_before_network() {
    local home="$TEST_TMP/home-bad-pin"
    local output="$TEST_TMP/bad-pin.out"
    mkdir -p "$home"
    start_mock_es happy || return 1
    if HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" --ca-sha256 "$(printf '%064d' 0)" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    [[ ! -s "$MOCK_REQUESTS" ]] && grep -qi 'SHA-256' "$output"
}

test_setup_rejects_trailing_ca_garbage_before_network() {
    local home="$TEST_TMP/home-trailing-ca-garbage"
    local malformed="$TEST_TMP/tls/ca-trailing-garbage.pem"
    local output="$TEST_TMP/trailing-ca-garbage.out"
    local stage="$TEST_TMP/trailing-ca-stage"
    local shim="$TEST_TMP/trailing-ca-shim"
    local sudo_log="$TEST_TMP/trailing-ca-sudo.log"
    cat "$TLS_BUNDLE" >"$malformed"
    printf '\nnot a PEM certificate\n' >>"$malformed"
    mkdir -p "$home" "$stage/usr/bin" "$shim"
    : >"$sudo_log"
    printf '#!/bin/sh\nexit 0\n' >"$shim/rigsignal-ebpf"
    printf '#!/bin/sh\nprintf "%%s\\n" "$*" >>"$RIGSIGNAL_TEST_SUDO_LOG"\nexit 0\n' >"$shim/sudo"
    chmod +x "$shim/rigsignal-ebpf" "$shim/sudo"
    start_mock_es happy || return 1
    if HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$shim:$PATH" \
        RIGSIGNAL_TEST_ETC_ROOT="$stage" RIGSIGNAL_TEST_SUDO_LOG="$sudo_log" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$malformed" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    [[ ! -s "$MOCK_REQUESTS" ]] \
        && [[ ! -e "$home/.config/rigsignal/rigsignal.toml" ]] \
        && [[ ! -e "$home/.config/rigsignal/certs/elasticsearch-ca.pem" ]] \
        && [[ ! -e "$stage/etc/rigsignal/rigsignal.toml" ]] \
        && [[ ! -e "$stage/etc/rigsignal/certs/elasticsearch-ca.pem" ]] \
        && [[ ! -s "$sudo_log" ]] \
        && grep -qi 'PEM certificate bundle' "$output"
}

test_setup_rejects_commented_ca_before_network() {
    local home="$TEST_TMP/home-commented-ca"
    local malformed="$TEST_TMP/tls/ca-commented.pem"
    local output="$TEST_TMP/commented-ca.out"
    printf '# operator comment\n' >"$malformed"
    cat "$TLS_BUNDLE" >>"$malformed"
    mkdir -p "$home"
    start_mock_es happy || return 1
    if HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$malformed" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    [[ ! -s "$MOCK_REQUESTS" ]] && grep -qi 'PEM certificate bundle' "$output"
}

test_setup_rejects_pin_only_before_network() {
    local home="$TEST_TMP/home-pin-only"
    if HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-sha256 "$(printf '%064d' 0)" >"$TEST_TMP/pin-only.out" 2>&1; then
        return 1
    fi
    grep -qi 'requires --ca-file' "$TEST_TMP/pin-only.out"
}

test_setup_rejects_untrusted_ca() {
    local home="$TEST_TMP/home-untrusted"
    start_mock_es happy || return 1
    if printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_CA_TWO" >"$TEST_TMP/untrusted.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    [[ ! -e "$home/.config/rigsignal/rigsignal.toml" ]] \
        && grep -qi 'certificate' "$TEST_TMP/untrusted.out"
}

test_setup_refuses_remote_http() {
    local home="$TEST_TMP/home-remote-http"
    local output="$TEST_TMP/remote-http.out"
    if printf 'http://example.invalid:9200\ntest-api-key\n' | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$output" 2>&1; then
        return 1
    fi
    grep -qi 'clear-text HTTP.*loopback' "$output"
}

test_setup_refuses_http_userinfo_authority_bypass() {
    local output="$TEST_TMP/http-userinfo.out"
    if printf 'http://localhost:9200@attacker.invalid\ntest-api-key\n' | \
        HOME="$TEST_TMP/home-http-userinfo" XDG_CONFIG_HOME="$TEST_TMP/home-http-userinfo/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$output" 2>&1; then
        return 1
    fi
    grep -qi 'clear-text HTTP.*loopback' "$output"
}

test_setup_refuses_redirect_without_leaking_authorization() {
    local home="$TEST_TMP/home-redirect"
    local output="$TEST_TMP/redirect.out"
    start_mock_es redirect || return 1
    if printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    grep -qi 'refusing Elasticsearch redirect' "$output" \
        && ! grep -q 'target-auth:ApiKey' "$MOCK_REQUESTS"
}

test_curl_fallback_uses_pinned_ca_and_refuses_missing_ca() {
    local home="$TEST_TMP/home-curl-fallback"
    local shim="$TEST_TMP/curl-shim"
    local log="$TEST_TMP/curl-argv.log"
    local cacert_bytes="$TEST_TMP/curl-cacert-bytes.pem"
    local output="$TEST_TMP/curl-fallback.out"
    mkdir -p "$shim"
    # Keep the launcher runnable with only the commands it needs, but omit
    # python3 entirely.  /bin itself exposes python3 on this test platform.
    local command
    for command in awk cat chmod cp date dirname grep head install mkdir mktemp mv rm rmdir sed sha256sum sleep sync tail tr wc; do
        ln -s "/bin/$command" "$shim/$command"
    done
    printf '%s\n' \
        '#!/bin/bash' \
        'printf "%s\\n" "$*" >>"$RIGSIGNAL_TEST_CURL_LOG"' \
        'for ((i = 1; i <= $#; i++)); do' \
        '    if [[ ${!i} == --cacert ]]; then' \
        '        next=$((i + 1)); cp "${!next}" "$RIGSIGNAL_TEST_CACERT_BYTES"' \
        '    fi' \
        'done' \
        'exec /usr/bin/curl "$@"' >"$shim/curl"
    printf '#!/bin/sh\nexec /usr/bin/openssl "$@"\n' >"$shim/openssl"
    printf '#!/bin/sh\nexit 1\n' >"$shim/systemctl"
    chmod +x "$shim/curl" "$shim/openssl" "$shim/systemctl"
    PATH="$shim" command -v python3 >/dev/null 2>&1 && return 1
    start_mock_es happy || return 1
    if ! printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$shim" \
        RIGSIGNAL_TEST_CURL_LOG="$log" RIGSIGNAL_TEST_CACERT_BYTES="$cacert_bytes" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    # curl must disable its rc-file (-q first) and pin a CA (--cacert, never -k).
    # Python is genuinely absent here; requests use curl and durability uses
    # sync. Capture --cacert while curl
    # has the snapshot open; its pathname is intentionally ephemeral afterward.
    grep -q '^-q ' "$log" || return 1
    grep -q -- '--cacert ' "$log" || return 1
    cmp -s "$cacert_bytes" "$TLS_BUNDLE" || return 1
    cmp -s "$home/.config/rigsignal/certs/elasticsearch-ca.pem" "$TLS_BUNDLE" || return 1

    start_mock_es happy || return 1
    if printf 'https://127.0.0.1:%s\ntest-api-key\n\n' "$MOCK_PORT" | \
        HOME="$TEST_TMP/home-curl-no-ca" XDG_CONFIG_HOME="$TEST_TMP/home-curl-no-ca/.config" PATH="$shim" \
        RIGSIGNAL_TEST_CURL_LOG="$log" RIGSIGNAL_TEST_CACERT_BYTES="$cacert_bytes" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$TEST_TMP/curl-no-ca.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    grep -qi 'certificate\|SSL' "$TEST_TMP/curl-no-ca.out"
}

test_awk_only_ca_validation_accepts_bundle() {
    local home="$TEST_TMP/home-awk-only"
    local shim="$TEST_TMP/awk-only-shim"
    local output="$TEST_TMP/awk-only.out"
    mkdir -p "$shim"
    local command
    for command in awk cat chmod cp date dirname grep head install mkdir mktemp mv rm rmdir sed sha256sum sleep sync tail tr wc; do
        ln -s "/bin/$command" "$shim/$command"
    done
    printf '%s\n' \
        '#!/bin/bash' \
        'exec /usr/bin/curl "$@"' >"$shim/curl"
    printf '#!/bin/sh\nexit 1\n' >"$shim/systemctl"
    chmod +x "$shim/curl" "$shim/systemctl"
    PATH="$shim" command -v python3 >/dev/null 2>&1 && return 1
    PATH="$shim" command -v openssl >/dev/null 2>&1 && return 1
    start_mock_es happy || return 1
    if ! printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$shim" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    cmp -s "$home/.config/rigsignal/certs/elasticsearch-ca.pem" "$TLS_BUNDLE" \
        && grep -q '^ca_cert = ' "$home/.config/rigsignal/rigsignal.toml"
}

test_setup_accepts_three_certificate_bundle() {
    local home="$TEST_TMP/home-three-cert-bundle"
    local output="$TEST_TMP/three-cert-bundle.out"
    mkdir -p "$home"
    start_mock_es happy || return 1
    if ! printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE_THREE" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    cmp -s "$home/.config/rigsignal/certs/elasticsearch-ca.pem" "$TLS_BUNDLE_THREE"
}

test_setup_accepts_crlf_certificate_bundle() {
    local home="$TEST_TMP/home-crlf-cert-bundle"
    local bundle="$TEST_TMP/tls/ca-bundle-crlf.pem"
    local output="$TEST_TMP/crlf-cert-bundle.out"
    # Real CR+LF bytes (sed with an ANSI-C literal carriage return), NOT the
    # two-character text "\r\n" — the launcher scanner correctly rejects the
    # latter as non-PEM garbage.
    sed $'s/$/\r/' "$TLS_BUNDLE" >"$bundle"
    mkdir -p "$home"
    start_mock_es happy || return 1
    if ! printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$bundle" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    cmp -s "$home/.config/rigsignal/certs/elasticsearch-ca.pem" "$bundle" \
        && grep -q '^ca_cert = ' "$home/.config/rigsignal/rigsignal.toml"
}

test_setup_accepts_certificate_bundle_with_trailing_whitespace() {
    local home="$TEST_TMP/home-trailing-ca-whitespace"
    local bundle="$TEST_TMP/tls/ca-trailing-whitespace.pem"
    local output="$TEST_TMP/trailing-ca-whitespace.out"
    cat "$TLS_BUNDLE" >"$bundle"
    printf ' \t\r\n\n\t \n' >>"$bundle"
    mkdir -p "$home"
    start_mock_es happy || return 1
    if ! printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$bundle" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    cmp -s "$home/.config/rigsignal/certs/elasticsearch-ca.pem" "$bundle" \
        && grep -q '^ca_cert = ' "$home/.config/rigsignal/rigsignal.toml"
}

test_structurally_framed_garbage_ca_aborts_before_side_effects() {
    local home="$TEST_TMP/home-framed-garbage-ca"
    local malformed="$TEST_TMP/tls/ca-framed-garbage.pem"
    local output="$TEST_TMP/framed-garbage-ca.out"
    local stage="$TEST_TMP/framed-garbage-stage"
    local shim="$TEST_TMP/framed-garbage-shim"
    local sudo_log="$TEST_TMP/framed-garbage-sudo.log"
    printf '%s\n' '-----BEGIN CERTIFICATE-----' 'not-valid-base64' '-----END CERTIFICATE-----' >"$malformed"
    mkdir -p "$home" "$stage/usr/bin" "$shim"
    : >"$sudo_log"
    local command
    for command in awk cat chmod cp date dirname grep head install mkdir mktemp mv rm rmdir sed sha256sum sleep sync tail tr wc; do
        ln -s "/bin/$command" "$shim/$command"
    done
    printf '#!/bin/sh\nexit 0\n' >"$shim/rigsignal-ebpf"
    printf '#!/bin/sh\nprintf "%%s\\n" "$*" >>"$RIGSIGNAL_TEST_SUDO_LOG"\nexit 0\n' >"$shim/sudo"
    printf '%s\n' \
        '#!/bin/bash' \
        'exec /usr/bin/curl "$@"' >"$shim/curl"
    printf '#!/bin/sh\nexit 1\n' >"$shim/systemctl"
    chmod +x "$shim/rigsignal-ebpf" "$shim/sudo" "$shim/curl" "$shim/systemctl"
    start_mock_es happy || return 1
    if printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$shim" \
        RIGSIGNAL_TEST_ETC_ROOT="$stage" RIGSIGNAL_TEST_SUDO_LOG="$sudo_log" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$malformed" >"$output" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    [[ ! -e "$home/.config/rigsignal/rigsignal.toml" ]] \
        && [[ ! -e "$home/.config/rigsignal/certs/elasticsearch-ca.pem" ]] \
        && [[ ! -e "$stage/etc/rigsignal/rigsignal.toml" ]] \
        && [[ ! -e "$stage/etc/rigsignal/certs/elasticsearch-ca.pem" ]] \
        && [[ ! -s "$sudo_log" ]] \
        && grep -qi 'certificate\|SSL' "$output"
}

test_explicit_ca_rerun_snapshots_once() {
    local home="$TEST_TMP/home-explicit-ca-rerun"
    local shim="$TEST_TMP/explicit-ca-rerun-shim"
    local cp_log="$TEST_TMP/explicit-ca-rerun-cp.log"
    mkdir -p "$home" "$shim"
    start_mock_es happy || return 1
    if ! printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$TEST_TMP/explicit-ca-initial.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    : >"$cp_log"
    printf '%s\n' \
        '#!/bin/sh' \
        'printf "%s\\n" "$*" >>"$RIGSIGNAL_TEST_CP_LOG"' \
        'exec /bin/cp "$@"' >"$shim/cp"
    chmod +x "$shim/cp"
    if ! HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$shim:$PATH" \
        RIGSIGNAL_TEST_CP_LOG="$cp_log" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$TEST_TMP/explicit-ca-rerun.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    awk -v source="$TLS_BUNDLE" '$1 == source { snapshots++ } END { exit snapshots == 1 ? 0 : 1 }' "$cp_log" \
        && cmp -s "$home/.config/rigsignal/certs/elasticsearch-ca.pem" "$TLS_BUNDLE" \
        && [[ ! -e "$home/.config/rigsignal/rigsignal.toml.bak" ]] \
        && [[ ! -e "$home/.config/rigsignal/certs/elasticsearch-ca.pem.bak" ]]
}

test_setup_fsync_failure_rolls_back_before_replacement() {
    local home="$TEST_TMP/home-user-fsync-failure"
    local shim="$TEST_TMP/user-fsync-shim"
    mkdir -p "$home" "$shim"
    printf '%s\n' \
        '#!/bin/sh' \
        '# fsync_file_and_dir passes two target paths; CA validation does not.' \
        '[ "$#" -eq 3 ] && exit 1' \
        'exec /usr/bin/python3 "$@"' >"$shim/python3"
    chmod +x "$shim/python3"
    start_mock_es happy || return 1
    if printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$shim:$PATH" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$TEST_TMP/user-fsync-failure.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    [[ ! -e "$home/.config/rigsignal/rigsignal.toml" ]] \
        && [[ ! -e "$home/.config/rigsignal/certs/elasticsearch-ca.pem" ]] \
        && grep -qi 'fsync' "$TEST_TMP/user-fsync-failure.out"
}

test_launcher_contains_no_tls_bypass() {
    ! grep -Eq 'CERT_NONE|check_hostname=False|(^|[[:space:]])-k([[:space:]]|$)|--insecure' "$LAUNCHER"
}

fixture_arch() {
    case "$(uname -m)" in
        x86_64|amd64) printf 'x86_64' ;;
        *) return 1 ;;
    esac
}

make_release_fixture() {
    local release_dir="$1"
    local version="$2"
    local checksum_mode="$3"
    local arch tarball package_dir
    arch=$(fixture_arch) || return 1
    tarball="rigsignal-${version}-linux-${arch}.tar.gz"
    package_dir="$release_dir/rigsignal-${version}-linux-${arch}"
    mkdir -p "$package_dir"
    printf '#!/bin/sh\nexit 0\n' >"$package_dir/rigsignal-agent"
    cp "$LAUNCHER" "$package_dir/rigsignal"
    cp "$UNINSTALLER" "$package_dir/rigsignal-uninstall"
    printf '[Unit]\nDescription=fixture\n' >"$package_dir/rigsignal-agent.service"
    printf '#!/bin/sh\nexit 0\n' >"$package_dir/rigsignal-spool-retention"
    printf '[Unit]\nDescription=fixture\n' >"$package_dir/rigsignal-spool-retention.service"
    printf '[Timer]\nOnCalendar=hourly\n' >"$package_dir/rigsignal-spool-retention.timer"
    mkdir -p "$package_dir/engine"
    printf '#!/usr/bin/env python3\n' >"$package_dir/engine/install_assets.py"
    printf '# fixture adapter\n' >"$package_dir/engine/asset_adapters.py"
    printf 'ENGINE_VERSION = "%s"\nSOURCE_COMMIT = "fixture"\n' "$version" >"$package_dir/engine/_version.py"
    chmod +x "$package_dir/rigsignal-agent" "$package_dir/rigsignal" "$package_dir/rigsignal-uninstall" "$package_dir/rigsignal-spool-retention"
    (cd "$release_dir" && tar -czf "$tarball" "$(basename "$package_dir")")
    if [[ "$checksum_mode" == "valid" ]]; then
        (cd "$release_dir" && sha256sum "$tarball" >"${tarball}.sha256")
    else
        printf '%064d  %s\n' 0 "$tarball" >"$release_dir/${tarball}.sha256"
    fi
}

test_checksum_mismatch_aborts_before_unpack() {
    local release="$TEST_TMP/release-bad"
    local home="$TEST_TMP/home-bad"
    local stage="$TEST_TMP/stage-bad"
    make_release_fixture "$release" 1.2.3 invalid || return 1
    if HOME="$home" DESTDIR="$stage" RIGSIGNAL_INSTALL_LOCAL_DIR="$release" \
        "$INSTALLER" --version 1.2.3 >"$TEST_TMP/checksum.out" 2>&1; then
        return 1
    fi
    # must fail BECAUSE of the checksum, not for an unrelated reason (e.g. exec bit)
    grep -qi 'checksum' "$TEST_TMP/checksum.out" || return 1
    [[ ! -e "$stage$home/.local/bin/rigsignal" ]]
}

test_architecture_refusal_precedes_release_requests() {
    local shim="$TEST_TMP/unsupported-arch-bin"
    local requests="$TEST_TMP/unsupported-arch-requests"
    mkdir -p "$shim"
    printf '%s\n' \
        '#!/bin/sh' \
        '[ "$1" = "-m" ] && { printf "%s\\n" aarch64; exit 0; }' \
        'exec /usr/bin/uname "$@"' >"$shim/uname"
    printf '#!/bin/sh\nprintf curl >>"$RIGSIGNAL_TEST_REQUESTS"\nexit 1\n' >"$shim/curl"
    printf '#!/bin/sh\nprintf wget >>"$RIGSIGNAL_TEST_REQUESTS"\nexit 1\n' >"$shim/wget"
    chmod +x "$shim/uname" "$shim/curl" "$shim/wget"
    : >"$requests"
    if HOME="$TEST_TMP/home-unsupported-arch" PATH="$shim:$PATH" \
        RIGSIGNAL_TEST_REQUESTS="$requests" "$INSTALLER" --version 1.2.3 \
        >"$TEST_TMP/unsupported-arch.out" 2>&1; then
        return 1
    fi
    grep -Fq 'Unsupported architecture: aarch64. RigSignal release builds support Linux x86_64 only.' \
        "$TEST_TMP/unsupported-arch.out" \
        && [[ ! -s "$requests" ]]
}

test_x86_64_architecture_installs_fixture() {
    local release="$TEST_TMP/release-x86_64"
    local home="$TEST_TMP/home-x86_64"
    local stage="$TEST_TMP/stage-x86_64"
    local shim="$TEST_TMP/x86_64-arch-bin"
    local tarball digest
    mkdir -p "$shim"
    printf '%s\n' \
        '#!/bin/sh' \
        '[ "$1" = "-m" ] && { printf "%s\\n" x86_64; exit 0; }' \
        'exec /usr/bin/uname "$@"' >"$shim/uname"
    chmod +x "$shim/uname"
    make_release_fixture "$release" 1.2.5 valid || return 1
    tarball='rigsignal-1.2.5-linux-x86_64.tar.gz'
    digest=$(sha256sum "$release/$tarball") || return 1
    digest=${digest%% *}
    printf '%s *%s\n' "$digest" "$tarball" >"$release/$tarball.sha256"
    HOME="$home" DESTDIR="$stage" RIGSIGNAL_INSTALL_LOCAL_DIR="$release" PATH="$shim:$PATH" \
        "$INSTALLER" --version 1.2.5 >"$TEST_TMP/x86_64.out" 2>&1 \
        && [[ -x "$stage$home/.local/bin/rigsignal" ]] \
        && [[ -x "$stage$home/.local/lib/rigsignal/engine/install_assets.py" ]] \
        && [[ -f "$stage$home/.local/lib/rigsignal/engine/asset_adapters.py" ]] \
        && [[ -f "$stage$home/.local/lib/rigsignal/engine/_version.py" ]] \
        && grep -qx 'rigsignal-release' "$stage$home/.local/lib/rigsignal/engine/channel"
}

test_python_preflight_rejects_missing_or_broken_python() {
    local mode shim output
    for mode in missing broken; do
        shim="$TEST_TMP/python-${mode}-bin"
        output="$TEST_TMP/python-${mode}.out"
        mkdir -p "$shim"
        printf '%s\n' \
            '#!/bin/sh' \
            '[ "$1" = "-m" ] && { printf "%s\\n" x86_64; exit 0; }' \
            'exec /usr/bin/uname "$@"' >"$shim/uname"
        if [[ "$mode" == broken ]]; then
            printf '#!/bin/sh\nexit 1\n' >"$shim/python3"
            chmod +x "$shim/python3"
        fi
        chmod +x "$shim/uname"
        if HOME="$TEST_TMP/home-python-${mode}" PATH="$shim" "$INSTALLER" --version 1.2.3 >"$output" 2>&1; then
            return 1
        fi
        grep -qi 'Python 3' "$output" || return 1
    done
}

test_sidecar_rejects_noncanonical_records() {
    local release="$TEST_TMP/release-malformed-sidecars"
    local home="$TEST_TMP/home-malformed-sidecars"
    local shim="$TEST_TMP/malformed-sidecar-bin"
    local tarball digest mode stage
    make_release_fixture "$release" 1.2.6 valid || return 1
    tarball='rigsignal-1.2.6-linux-x86_64.tar.gz'
    digest=$(sha256sum "$release/$tarball") || return 1
    digest=${digest%% *}
    # These files make the wrong-target records valid to the legacy
    # sha256sum -c verifier.  The cp shim puts them in the installer's fresh
    # temporary directory as well, so these controls exercise parser
    # strictness rather than a missing-file failure.
    cp "$release/$tarball" "$release/wrong.tar.gz" || return 1
    cp "$release/$tarball" "$release/another.tar.gz" || return 1
    cp "$release/$tarball" "$release/rigsignal-1.2.6-linux-*.tar.gz" || return 1
    mkdir -p "$shim"
    printf '%s\n' \
        '#!/bin/sh' \
        '/bin/cp "$@" || exit $?' \
        'case "$1" in' \
        '  *rigsignal-1.2.6-linux-x86_64.tar.gz)' \
        '    target_dir=${2%/*}' \
        '    /bin/cp "$2" "$target_dir/wrong.tar.gz"' \
        '    /bin/cp "$2" "$target_dir/another.tar.gz"' \
        '    /bin/cp "$2" "$target_dir/rigsignal-1.2.6-linux-*.tar.gz"' \
        '    ;;' \
        'esac' >"$shim/cp"
    chmod +x "$shim/cp"
    for mode in wrong-basename extra-record missing-newline uppercase short glob-lookalike; do
        case "$mode" in
            wrong-basename)
                printf '%s  wrong.tar.gz\n' "$digest" >"$release/$tarball.sha256" ;;
            extra-record)
                printf '%s  %s\n%s  another.tar.gz\n' "$digest" "$tarball" "$digest" >"$release/$tarball.sha256" ;;
            missing-newline)
                printf '%s  %s' "$digest" "$tarball" >"$release/$tarball.sha256" ;;
            uppercase)
                printf '%s  %s\n' "${digest^^}" "$tarball" >"$release/$tarball.sha256" ;;
            short)
                printf '%s  %s\n' "${digest:1}" "$tarball" >"$release/$tarball.sha256" ;;
            glob-lookalike)
                printf '%s  rigsignal-1.2.6-linux-*.tar.gz\n' "$digest" >"$release/$tarball.sha256" ;;
        esac
        case "$mode" in
            wrong-basename|extra-record|glob-lookalike)
                (cd "$release" && sha256sum -c "$tarball.sha256" >/dev/null) || return 1 ;;
        esac
        stage="$TEST_TMP/stage-malformed-sidecar-$mode"
        if HOME="$home" DESTDIR="$stage" RIGSIGNAL_INSTALL_LOCAL_DIR="$release" \
            PATH="$shim:$PATH" "$INSTALLER" --version 1.2.6 >"$TEST_TMP/malformed-sidecar-$mode.out" 2>&1; then
            return 1
        fi
        grep -qi 'checksum' "$TEST_TMP/malformed-sidecar-$mode.out" || return 1
        [[ ! -e "$stage$home/.local/bin/rigsignal" ]] || return 1
    done

    # Execute the same corpus through install.sh's embedded verifier.  Valid
    # grammar proceeds to the intentionally wrong digest; malformed records
    # stop at the sidecar verifier.  This is a behavioral fence, not a source
    # text/third-regex comparison.
    local corpus="$REPO_ROOT/packaging/tests/sidecar-verifier-corpus.tsv"
    local name encoded expected output
    while IFS=$'\t' read -r name encoded expected; do
        [[ -z "$name" || "$name" == \#* ]] && continue
        make_release_fixture "$release" 1.2.6 valid || return 1
        python3 -c 'import base64, pathlib, sys; data = base64.b64decode(sys.argv[1]); pathlib.Path(sys.argv[2]).write_bytes(data.replace(b"bundle.tar.gz", sys.argv[3].encode("ascii")))' \
            "$encoded" "$release/$tarball.sha256" "$tarball" || return 1
        stage="$TEST_TMP/stage-sidecar-corpus-$name"
        output="$TEST_TMP/sidecar-corpus-$name.out"
        if HOME="$home" DESTDIR="$stage" RIGSIGNAL_INSTALL_LOCAL_DIR="$release" PATH="$shim:$PATH" "$INSTALLER" --version 1.2.6 >"$output" 2>&1; then
            return 1
        fi
        if [[ "$expected" = 1 ]]; then
            grep -q 'Checksum verification failed' "$output" || return 1
        else
            grep -q 'Checksum sidecar must contain exactly' "$output" || return 1
        fi
    done <"$corpus"
}

test_package_dependencies_declare_python3() {
    grep -qx 'depends = "$auto, python3"' "$REPO_ROOT/src/Cargo.toml" || return 1
    grep -qxF 'requires = { python3 = "*" }' "$REPO_ROOT/src/Cargo.toml" || return 1
    grep -qx "depends=('python3')" "$REPO_ROOT/packaging/PKGBUILD" || return 1
    grep -qx "depends=('python3')" "$REPO_ROOT/packaging/aur/PKGBUILD" || return 1
    grep -qx "depends=('python3')" "$REPO_ROOT/.github/packaging/PKGBUILD"
}

test_package_paths_write_channel_markers() {
    # Source each real PKGBUILD and invoke package() against a small staged
    # payload. This verifies the produced package path, rather than merely
    # checking PKGBUILD text, has the marker required by assets resolution.
    local fixture="$TEST_TMP/channel-package-fixture"
    local spec path marker stage
    mkdir -p "$fixture/target/release" "$fixture/ebpf/target/release" \
        "$fixture/ebpf/target/bpfel-unknown-none/release" "$fixture/dist/engine" \
        "$fixture/packaging/systemd" "$fixture/packaging/config" "$fixture/profiles" "$fixture/packaging"
    for file in target/release/rigsignal-agent ebpf/target/release/rigsignal-ebpf ebpf/target/bpfel-unknown-none/release/rigsignal-ebpf-probes; do
        install -Dm755 /bin/true "$fixture/$file" || return 1
    done
    for file in install_assets.py asset_adapters.py _version.py; do
        printf 'fixture\n' >"$fixture/dist/engine/$file" || return 1
    done
    install -Dm755 "$REPO_ROOT/packaging/rigsignal-launcher.sh" "$fixture/packaging/rigsignal-launcher.sh" || return 1
    install -Dm755 "$REPO_ROOT/packaging/rigsignal-spool-retention.py" "$fixture/packaging/rigsignal-spool-retention.py" || return 1
    for file in rigsignal-agent.service rigsignal-ebpf.service rigsignal-spool-retention.service rigsignal-spool-retention.timer; do
        install -Dm644 "$REPO_ROOT/packaging/systemd/$file" "$fixture/packaging/systemd/$file" || return 1
    done
    install -Dm644 "$REPO_ROOT/packaging/config/rigsignal.toml.example" "$fixture/packaging/config/rigsignal.toml.example" || return 1
    printf 'fixture\n' >"$fixture/profiles/fixture.toml"
    printf 'fixture license\n' >"$fixture/LICENSE"
    for spec in "packaging/PKGBUILD:rigsignal-release" "packaging/aur/PKGBUILD:rigsignal-git" ".github/packaging/PKGBUILD:rigsignal-release"; do
        path=${spec%%:*}; marker=${spec#*:}; stage="$TEST_TMP/channel-stage-$(basename "$(dirname "$path")")"
        mkdir -p "$stage"
        if [[ "$path" == .github/* ]]; then
            ( pkgdir="$stage"; REPOROOT="$fixture"; source "$REPO_ROOT/$path"; package ) || return 1
        elif [[ "$path" == packaging/aur/* ]]; then
            mkdir -p "$TEST_TMP/channel-aur-src"
            ln -sfn "$fixture" "$TEST_TMP/channel-aur-src/rigsignal-git"
            ( pkgdir="$stage"; srcdir="$TEST_TMP/channel-aur-src"; source "$REPO_ROOT/$path"; package ) || return 1
        else
            ( pkgdir="$stage"; startdir="$fixture/packaging"; source "$REPO_ROOT/$path"; _repodir="$fixture"; package ) || return 1
        fi
        grep -qx "$marker" "$stage/usr/lib/rigsignal/engine/channel" || return 1
    done
    # cargo-deb and cargo-generate-rpm consume these parsed asset descriptors;
    # assert both real package paths carry the same concrete release marker.
    python3 - "$REPO_ROOT/src/Cargo.toml" "$REPO_ROOT/packaging/engine-channel-release" <<'PY'
import pathlib, sys, tomllib
metadata = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())['package']['metadata']
deb = metadata['deb']['assets']
rpm = metadata['generate-rpm']['assets']
if not any(item[0] == '../packaging/engine-channel-release' and item[1] == 'usr/lib/rigsignal/engine/channel' for item in deb):
    raise SystemExit(1)
if not any(item['source'] == '../packaging/engine-channel-release' and item['dest'] == '/usr/lib/rigsignal/engine/channel' for item in rpm):
    raise SystemExit(1)
if pathlib.Path(sys.argv[2]).read_bytes() != b'rigsignal-release\n':
    raise SystemExit(1)
PY
}

test_uninstall_removes_staged_install() {
    local release="$TEST_TMP/release-good"
    local home="$TEST_TMP/home-good"
    local stage="$TEST_TMP/stage-good"
    local installed_uninstaller="$stage$home/.local/bin/rigsignal-uninstall"
    make_release_fixture "$release" 1.2.4 valid || return 1
    HOME="$home" DESTDIR="$stage" RIGSIGNAL_INSTALL_LOCAL_DIR="$release" \
        "$INSTALLER" --version 1.2.4 >"$TEST_TMP/install.out" 2>&1 || return 1
    [[ -x "$installed_uninstaller" ]] || return 1
    HOME="$home" DESTDIR="$stage" "$installed_uninstaller" >"$TEST_TMP/uninstall.out" 2>&1 || return 1
    [[ ! -e "$stage$home/.local/bin/rigsignal-agent" ]] \
        && [[ ! -e "$stage$home/.local/bin/rigsignal" ]] \
        && [[ ! -e "$stage$home/.local/bin/rigsignal-uninstall" ]] \
        && [[ ! -e "$stage$home/.local/bin/rigsignal-spool-retention" ]] \
        && [[ ! -e "$stage$home/.config/systemd/user/rigsignal-agent.service" ]] \
        && [[ ! -e "$stage$home/.config/systemd/user/rigsignal-spool-retention.service" ]] \
        && [[ ! -e "$stage$home/.config/systemd/user/rigsignal-spool-retention.timer" ]] \
        && [[ ! -e "$stage$home/.local/lib/rigsignal/engine/install_assets.py" ]] \
        && [[ ! -e "$stage$home/.local/lib/rigsignal/engine/asset_adapters.py" ]] \
        && [[ ! -e "$stage$home/.local/lib/rigsignal/engine/_version.py" ]] || return 1
    HOME="$home" DESTDIR="$stage" RIGSIGNAL_INSTALL_LOCAL_DIR="$release" \
        "$INSTALLER" --version 1.2.4 >"$TEST_TMP/reinstall.out" 2>&1 || return 1
    [[ -e "$stage$home/.local/bin/rigsignal-agent" ]] \
        && [[ -e "$stage$home/.local/bin/rigsignal" ]] \
        && [[ -e "$stage$home/.local/bin/rigsignal-uninstall" ]] \
        && [[ -e "$stage$home/.local/bin/rigsignal-spool-retention" ]] \
        && [[ -e "$stage$home/.config/systemd/user/rigsignal-agent.service" ]] \
        && [[ -e "$stage$home/.config/systemd/user/rigsignal-spool-retention.service" ]] \
        && [[ -e "$stage$home/.config/systemd/user/rigsignal-spool-retention.timer" ]] \
        && [[ -e "$stage$home/.local/lib/rigsignal/engine/install_assets.py" ]]
}

test_setup_preserves_collection_on_reauth() {
    # Regression (reviewer 2026-07-22): credential re-run must not clobber the
    # user's [collection]/[session] settings (e.g. ebpf = true silently reset).
    local home="$TEST_TMP/home-reauth"
    local cfg="$home/.config/rigsignal"
    mkdir -p "$cfg"
    printf '[elasticsearch]\nendpoint = "http://127.0.0.1:1"\napi_key = "stale-key"\n\n[collection]\nebpf = true\ncpu = false\n\n[session]\nlabel = "custom"\n' >"$cfg/rigsignal.toml"
    start_mock_es happy || return 1
    printf 'https://127.0.0.1:%s\ntest-api-key\n%s\n' "$MOCK_PORT" "$TLS_BUNDLE" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$TEST_TMP/setup-reauth.out" 2>&1 || { stop_mock_es; return 1; }
    stop_mock_es
    grep -q '^ebpf = true' "$cfg/rigsignal.toml" \
        && grep -q '^cpu = false' "$cfg/rigsignal.toml" \
        && grep -q 'label = "custom"' "$cfg/rigsignal.toml" \
        && grep -q 'api_key = "test-api-key"' "$cfg/rigsignal.toml"
}

test_setup_reauth_uses_persisted_ca_without_prompting() {
    local home="$TEST_TMP/home-persisted-ca"
    local cfg="$home/.config/rigsignal"
    mkdir -p "$cfg/certs"
    cp "$TLS_BUNDLE" "$cfg/certs/elasticsearch-ca.pem"
    printf '[other]\nendpoint = "https://wrong.invalid"\napi_key = "wrong"\n\n[elasticsearch]\nendpoint = "https://127.0.0.1:1" # stale endpoint\napi_key = "stale-key" # stale key\nca_cert = "%s" # retain me\n\n[collection]\nebpf = true\n' "$cfg/certs/elasticsearch-ca.pem" >"$cfg/rigsignal.toml"
    start_mock_es happy || return 1
    # Supplying only endpoint and key proves the persisted CA wins over a new
    # prompt during reauthentication; comments also remain on managed fields.
    printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$TEST_TMP/persisted-ca.out" 2>&1 || { stop_mock_es; return 1; }
    stop_mock_es
    grep -q 'endpoint = "https://127.0.0.1:.*" # stale endpoint' "$cfg/rigsignal.toml" \
        && grep -q 'api_key = "test-api-key" # stale key' "$cfg/rigsignal.toml" \
        && grep -q 'ca_cert = ".*" # retain me' "$cfg/rigsignal.toml"
}

test_package_user_unit_uses_user_config_and_detects_packaged_ebpf() {
    local stage="$TEST_TMP/package-stage"
    local home="$TEST_TMP/package-home"
    local unit="$stage/usr/lib/systemd/user/rigsignal-agent.service"
    local example="$stage/usr/share/rigsignal/examples/rigsignal.toml.example"
    local sudo_log="$TEST_TMP/package-sudo.log"

    # Build the relevant package tree under a staged root, rather than relying
    # on the tarball's user-local unit path.
    install -Dm644 "$REPO_ROOT/packaging/systemd/rigsignal-agent.service" "$unit" || return 1
    install -Dm644 "$REPO_ROOT/packaging/config/rigsignal.toml.example" "$example" || return 1
    install -Dm755 /bin/true "$stage/usr/bin/rigsignal-ebpf" || return 1
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'printf "%s\\n" "$*" >> "$RIGSIGNAL_TEST_SUDO_LOG"' \
        'root=${RIGSIGNAL_TEST_ETC_ROOT:?}' \
        'map_path() { [[ $1 == /etc/* ]] && printf "%s%s" "$root" "$1" || printf "%s" "$1"; }' \
        'case "$1" in' \
        '  mkdir|chmod) command=$1; shift; args=(); for arg in "$@"; do args+=("$(map_path "$arg")"); done; /bin/$command "${args[@]}" ;;' \
        '  test) shift; args=(); for arg in "$@"; do args+=("$(map_path "$arg")"); done; /usr/bin/test "${args[@]}" ;;' \
        '  install) [[ ${RIGSIGNAL_TEST_FAIL_SUDO:-} == install ]] && exit 1; /usr/bin/install "$2" "$3" "$4" "$(map_path "$5")" ;;' \
        '  mv) [[ ${RIGSIGNAL_TEST_FAIL_SUDO:-} == mv && $2 != *.bak && $3 != *.bak ]] && exit 1; /bin/mv "$(map_path "$2")" "$(map_path "$3")" ;;' \
        '  rm) shift; args=(); for arg in "$@"; do args+=("$(map_path "$arg")"); done; /bin/rm "${args[@]}" ;;' \
        '  mktemp) case "$2" in /etc/rigsignal/certs/*) printf "/etc/rigsignal/certs/.test-ca\n" ;; *) printf "/etc/rigsignal/.test-config\n" ;; esac ;;' \
        '  systemctl) [[ ${RIGSIGNAL_TEST_FAIL_SUDO:-} == restart && $2 == start ]] && exit 1; exit 0 ;;' \
        '  python3) [[ ${RIGSIGNAL_TEST_FAIL_SUDO:-} == fsync ]] && exit 1; exit 0 ;;' \
        '  steamos-readonly) exit 0 ;;' \
        '  *) exit 0 ;;' \
        'esac' >"$stage/usr/bin/sudo"
    chmod +x "$stage/usr/bin/sudo"

    grep -qx 'ExecStart=/usr/bin/rigsignal-agent' "$unit" || return 1
    ! grep -q -- '--config /etc/rigsignal/rigsignal.toml' "$unit" || return 1
    ! grep -q '^Environment=HOME=' "$unit" || return 1
    # Package and tarball units are intentionally identical except for the
    # installed agent binary path.
    sed '/^ExecStart=/d' "$unit" >"$TEST_TMP/packaged-unit-without-exec"
    sed '/^ExecStart=/d' "$REPO_ROOT/packaging/systemd/rigsignal-agent.user-install.service" >"$TEST_TMP/tarball-unit-without-exec"
    cmp -s "$TEST_TMP/packaged-unit-without-exec" "$TEST_TMP/tarball-unit-without-exec" || return 1
    [[ -f "$example" && ! -e "$stage/etc/rigsignal/rigsignal.toml" ]] || return 1
    local pkgbuild
    for pkgbuild in "$REPO_ROOT/packaging/PKGBUILD" "$REPO_ROOT/packaging/aur/PKGBUILD" "$REPO_ROOT/.github/packaging/PKGBUILD"; do
        grep -qx 'install=rigsignal.install' "$pkgbuild" || return 1
        grep -q 'usr/share/rigsignal/examples/rigsignal.toml.example' "$pkgbuild" || return 1
        ! grep -q '^backup=' "$pkgbuild" || return 1
    done
    grep -qx "depends=('python3')" "$REPO_ROOT/.github/packaging/PKGBUILD" || return 1
    ! grep -q 'rigsignal-ebpf.service' "$REPO_ROOT/.github/packaging/PKGBUILD" || return 1
    grep -q 'packaging/systemd/rigsignal-agent.service' "$REPO_ROOT/.github/packaging/PKGBUILD" || return 1
    grep -q 'usr/lib/systemd/user/rigsignal-agent.service' "$REPO_ROOT/.github/packaging/PKGBUILD" || return 1
    cmp -s "$REPO_ROOT/packaging/rigsignal.install" "$REPO_ROOT/packaging/aur/rigsignal.install" || return 1
    cmp -s "$REPO_ROOT/packaging/rigsignal.install" "$REPO_ROOT/.github/packaging/rigsignal.install" || return 1
    for pkgbuild in "$REPO_ROOT/packaging/PKGBUILD" "$REPO_ROOT/packaging/aur/PKGBUILD"; do
        grep -q -- '--engine-output' "$pkgbuild" || return 1
        grep -q 'usr/lib/rigsignal/engine/install_assets.py' "$pkgbuild" || return 1
        grep -q 'usr/lib/rigsignal/engine/asset_adapters.py' "$pkgbuild" || return 1
        grep -q 'usr/lib/rigsignal/engine/_version.py' "$pkgbuild" || return 1
    done
    grep -Fq "printf 'rigsignal-release\\n'" "$REPO_ROOT/packaging/PKGBUILD" || return 1
    grep -Fq "printf 'rigsignal-git\\n'" "$REPO_ROOT/packaging/aur/PKGBUILD" || return 1

    # Exercise the Arch upgrade hook under a staged /etc: it removes known
    # pristine examples but leaves an operator-modified pacsave untouched.
    local hook_stage="$TEST_TMP/package-hook-stage"
    local hook_config="$hook_stage/etc/rigsignal/rigsignal.toml"
    sed "s#/etc/rigsignal/rigsignal.toml#$hook_config#g" \
        "$REPO_ROOT/packaging/rigsignal.install" >"$TEST_TMP/staged-rigsignal.install"
    mkdir -p "$(dirname "$hook_config")"
    source "$TEST_TMP/staged-rigsignal.install"
    git -C "$REPO_ROOT" show 50e07d4:packaging/config/rigsignal.toml.example >"$hook_config" || return 1
    post_upgrade >/dev/null
    [[ ! -e "$hook_config" ]] || return 1
    git -C "$REPO_ROOT" show c9557dd:packaging/config/rigsignal.toml.example >"$hook_config" || return 1
    post_upgrade >/dev/null
    [[ ! -e "$hook_config" ]] || return 1
    printf 'operator eBPF config\n' >"${hook_config}.pacsave"
    post_upgrade >"$TEST_TMP/package-hook-pacsave.out"
    [[ ! -e "$hook_config" && -f "${hook_config}.pacsave" ]] || return 1
    grep -qx 'operator eBPF config' "${hook_config}.pacsave" || return 1
    grep -q 'Preserved modified .*\.pacsave' "$TEST_TMP/package-hook-pacsave.out" || return 1

    # setup writes the user config. A staged /usr/bin eBPF binary must be
    # discovered through PATH and cause the existing privileged sync path.
    mkdir -p "$home"
    start_mock_es happy || return 1
    if ! printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$stage/usr/bin:$PATH" \
        RIGSIGNAL_TEST_SUDO_LOG="$sudo_log" RIGSIGNAL_TEST_ETC_ROOT="$stage" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$TEST_TMP/package-setup.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es

    [[ -f "$home/.config/rigsignal/rigsignal.toml" ]] \
        && grep -q '^install -m 600 ' "$sudo_log" \
        && cmp -s "$TLS_BUNDLE" "$stage/etc/rigsignal/certs/elasticsearch-ca.pem" \
        && grep -q '^ca_cert = "/etc/rigsignal/certs/elasticsearch-ca.pem"' "$stage/etc/rigsignal/rigsignal.toml"
}

test_elevated_failures_roll_back_and_restore_readonly() {
    local mode="$1"
    local stage="$TEST_TMP/failure-stage-$mode"
    local home="$TEST_TMP/failure-home-$mode"
    local sudo_log="$TEST_TMP/failure-sudo-$mode.log"
    local old_cfg='[elasticsearch]\nendpoint = "http://127.0.0.1:1"\napi_key = "old-key"\n'
    mkdir -p "$stage/usr/bin" "$stage/etc/rigsignal/certs" "$home/.config/rigsignal"
    printf '%s' "$old_cfg" >"$home/.config/rigsignal/rigsignal.toml"
    printf 'old root config\n' >"$stage/etc/rigsignal/rigsignal.toml"
    printf 'old root CA\n' >"$stage/etc/rigsignal/certs/elasticsearch-ca.pem"
    printf '#!/bin/sh\nexit 0\n' >"$stage/usr/bin/rigsignal-ebpf"
    printf '#!/bin/sh\nexit 0\n' >"$stage/usr/bin/steamos-readonly"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'printf "%s\n" "$*" >> "$RIGSIGNAL_TEST_SUDO_LOG"' \
        'root=${RIGSIGNAL_TEST_ETC_ROOT:?}' \
        'map() { [[ $1 == /etc/* ]] && printf "%s%s" "$root" "$1" || printf "%s" "$1"; }' \
        'case "$1" in' \
        'mkdir|chmod|rm|test) cmd=$1; shift; a=(); for x in "$@"; do a+=("$(map "$x")"); done; /bin/$cmd "${a[@]}" ;;' \
        'install) [[ $RIGSIGNAL_TEST_FAIL_SUDO == install ]] && exit 1; /usr/bin/install "$2" "$3" "$4" "$(map "$5")" ;;' \
        'mv) [[ $RIGSIGNAL_TEST_FAIL_SUDO == backup-mv && $3 == *.bak ]] && exit 1; [[ $RIGSIGNAL_TEST_FAIL_SUDO == mv && $2 != *.bak && $3 != *.bak ]] && exit 1; /bin/mv "$(map "$2")" "$(map "$3")" ;;' \
        'mktemp) case "$2" in /etc/rigsignal/certs/*) printf "/etc/rigsignal/certs/.temp-ca\n";; *) printf "/etc/rigsignal/.temp-config\n";; esac ;;' \
        'systemctl) [[ $RIGSIGNAL_TEST_FAIL_SUDO == restart && $2 == start ]] && exit 1; exit 0 ;;' \
        'python3) [[ $RIGSIGNAL_TEST_FAIL_SUDO == fsync ]] && exit 1; exit 0 ;;' \
        'steamos-readonly) [[ $RIGSIGNAL_TEST_FAIL_SUDO == enable && $2 == enable ]] && exit 1; exit 0 ;;' \
        '*) exit 0 ;; esac' >"$stage/usr/bin/sudo"
    chmod +x "$stage/usr/bin/rigsignal-ebpf" "$stage/usr/bin/steamos-readonly" "$stage/usr/bin/sudo"
    start_mock_es happy || return 1
    if printf 'https://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$stage/usr/bin:$PATH" \
        RIGSIGNAL_TEST_ETC_ROOT="$stage" RIGSIGNAL_TEST_SUDO_LOG="$sudo_log" RIGSIGNAL_TEST_FAIL_SUDO="$mode" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup --ca-file "$TLS_BUNDLE" >"$TEST_TMP/failure-$mode.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    cmp -s <(printf '%s' "$old_cfg") "$home/.config/rigsignal/rigsignal.toml" \
        && grep -qx 'old root config' "$stage/etc/rigsignal/rigsignal.toml" \
        && grep -qx 'old root CA' "$stage/etc/rigsignal/certs/elasticsearch-ca.pem" \
        && grep -q 'steamos-readonly enable' "$sudo_log"
}

test_elevated_install_failure_rolls_back_and_restores_readonly() {
    test_elevated_failures_roll_back_and_restore_readonly install
}

test_elevated_restart_failure_rolls_back_and_restores_readonly() {
    test_elevated_failures_roll_back_and_restore_readonly restart
}

test_elevated_mv_failure_rolls_back_and_restores_readonly() {
    test_elevated_failures_roll_back_and_restore_readonly mv
}

test_elevated_fsync_failure_rolls_back_and_restores_readonly() {
    test_elevated_failures_roll_back_and_restore_readonly fsync
}

test_elevated_backup_mv_failure_keeps_originals() {
    test_elevated_failures_roll_back_and_restore_readonly backup-mv
}

test_elevated_enable_failure_retries_and_reports_writable() {
    test_elevated_failures_roll_back_and_restore_readonly enable \
        && grep -qi 'SteamOS left writable — run steamos-readonly enable' "$TEST_TMP/failure-enable.out" \
        && [[ $(grep -c '^steamos-readonly enable$' "$TEST_TMP/failure-sudo-enable.log") -ge 3 ]]
}

test_existing_config_early_sync_failure_preserves_both_scopes() {
    local mode=fsync
    local stage="$TEST_TMP/early-sync-stage"
    local home="$TEST_TMP/early-sync-home"
    local sudo_log="$TEST_TMP/early-sync-sudo.log"
    local old_cfg='[elasticsearch]\nendpoint = "http://127.0.0.1:PLACEHOLDER"\napi_key = "old-key"\n'
    mkdir -p "$stage/usr/bin" "$stage/etc/rigsignal/certs" "$home/.config/rigsignal"
    printf 'old root config\n' >"$stage/etc/rigsignal/rigsignal.toml"
    printf 'old root CA\n' >"$stage/etc/rigsignal/certs/elasticsearch-ca.pem"
    printf '#!/bin/sh\nexit 0\n' >"$stage/usr/bin/rigsignal-ebpf"
    printf '#!/bin/sh\nexit 0\n' >"$stage/usr/bin/steamos-readonly"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'printf "%s\\n" "$*" >> "$RIGSIGNAL_TEST_SUDO_LOG"' \
        'root=${RIGSIGNAL_TEST_ETC_ROOT:?}' \
        'map() { [[ $1 == /etc/* ]] && printf "%s%s" "$root" "$1" || printf "%s" "$1"; }' \
        'case "$1" in' \
        'mkdir|chmod|rm|test) cmd=$1; shift; a=(); for x in "$@"; do a+=("$(map "$x")"); done; /bin/$cmd "${a[@]}" ;;' \
        'install) /usr/bin/install "$2" "$3" "$4" "$(map "$5")" ;;' \
        'mv) /bin/mv "$(map "$2")" "$(map "$3")" ;;' \
        'mktemp) case "$2" in /etc/rigsignal/certs/*) printf "/etc/rigsignal/certs/.temp-ca\\n";; *) printf "/etc/rigsignal/.temp-config\\n";; esac ;;' \
        'systemctl) exit 0 ;;' \
        'python3) exit 1 ;;' \
        'steamos-readonly) exit 0 ;;' \
        '*) exit 0 ;; esac' >"$stage/usr/bin/sudo"
    chmod +x "$stage/usr/bin/rigsignal-ebpf" "$stage/usr/bin/steamos-readonly" "$stage/usr/bin/sudo"
    start_mock_es plain_happy || return 1
    printf "$old_cfg" | sed "s/PLACEHOLDER/$MOCK_PORT/" >"$home/.config/rigsignal/rigsignal.toml"
    if HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$stage/usr/bin:$PATH" \
        RIGSIGNAL_TEST_ETC_ROOT="$stage" RIGSIGNAL_TEST_SUDO_LOG="$sudo_log" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$TEST_TMP/early-sync.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es
    grep -q "endpoint = \"http://127.0.0.1:$MOCK_PORT\"" "$home/.config/rigsignal/rigsignal.toml" \
        && grep -qx 'old root config' "$stage/etc/rigsignal/rigsignal.toml" \
        && grep -qx 'old root CA' "$stage/etc/rigsignal/certs/elasticsearch-ca.pem" \
        && grep -q 'steamos-readonly enable' "$sudo_log"
}

test_deb_obsolete_conffile_transition_is_wired() {
    local manifest="$REPO_ROOT/src/Cargo.toml"
    local script

    grep -qx 'maintainer-scripts = "debian"' "$manifest" || return 1
    for script in preinst postinst postrm; do
        [[ -f "$REPO_ROOT/src/debian/$script" ]] || return 1
        grep -qx 'dpkg-maintscript-helper rm_conffile /etc/rigsignal/rigsignal.toml 0.3.0-1 -- "$@"' \
            "$REPO_ROOT/src/debian/$script" || return 1
    done
}

make_tls_certificates || { printf 'TEST FAIL TLS fixture generation\n' >&2; exit 1; }

run_test setup_fails_on_401 test_setup_fails_on_401
run_test setup_preserves_collection_on_reauth test_setup_preserves_collection_on_reauth
run_test setup_fails_without_create_doc test_setup_fails_without_create_doc
run_test setup_succeeds_with_required_privileges test_setup_succeeds_with_required_privileges
run_test setup_rejects_bad_pin_before_network test_setup_rejects_bad_pin_before_network
run_test setup_rejects_trailing_ca_garbage_before_network test_setup_rejects_trailing_ca_garbage_before_network
run_test setup_rejects_commented_ca_before_network test_setup_rejects_commented_ca_before_network
run_test setup_rejects_pin_only_before_network test_setup_rejects_pin_only_before_network
run_test setup_rejects_untrusted_ca test_setup_rejects_untrusted_ca
run_test setup_refuses_remote_http test_setup_refuses_remote_http
run_test setup_refuses_http_userinfo_authority_bypass test_setup_refuses_http_userinfo_authority_bypass
run_test setup_refuses_redirect_without_leaking_authorization test_setup_refuses_redirect_without_leaking_authorization
run_test curl_fallback_uses_pinned_ca_and_refuses_missing_ca test_curl_fallback_uses_pinned_ca_and_refuses_missing_ca
run_test awk_only_ca_validation_accepts_bundle test_awk_only_ca_validation_accepts_bundle
run_test setup_accepts_three_certificate_bundle test_setup_accepts_three_certificate_bundle
run_test setup_accepts_crlf_certificate_bundle test_setup_accepts_crlf_certificate_bundle
run_test setup_accepts_certificate_bundle_with_trailing_whitespace test_setup_accepts_certificate_bundle_with_trailing_whitespace
run_test structurally_framed_garbage_ca_aborts_before_side_effects test_structurally_framed_garbage_ca_aborts_before_side_effects
run_test explicit_ca_rerun_snapshots_once test_explicit_ca_rerun_snapshots_once
run_test setup_fsync_failure_rolls_back_before_replacement test_setup_fsync_failure_rolls_back_before_replacement
run_test launcher_contains_no_tls_bypass test_launcher_contains_no_tls_bypass
run_test setup_reauth_uses_persisted_ca_without_prompting test_setup_reauth_uses_persisted_ca_without_prompting
run_test checksum_mismatch_aborts_before_unpack test_checksum_mismatch_aborts_before_unpack
run_test architecture_refusal_precedes_release_requests test_architecture_refusal_precedes_release_requests
run_test x86_64_architecture_installs_fixture test_x86_64_architecture_installs_fixture
run_test python_preflight_rejects_missing_or_broken_python test_python_preflight_rejects_missing_or_broken_python
run_test sidecar_rejects_noncanonical_records test_sidecar_rejects_noncanonical_records
run_test package_dependencies_declare_python3 test_package_dependencies_declare_python3
run_test package_paths_write_channel_markers test_package_paths_write_channel_markers
run_test uninstall_removes_staged_install test_uninstall_removes_staged_install
run_test package_user_unit_uses_user_config_and_detects_packaged_ebpf test_package_user_unit_uses_user_config_and_detects_packaged_ebpf
run_test elevated_install_failure_rolls_back_and_restores_readonly test_elevated_install_failure_rolls_back_and_restores_readonly
run_test elevated_mv_failure_rolls_back_and_restores_readonly test_elevated_mv_failure_rolls_back_and_restores_readonly
run_test elevated_restart_failure_rolls_back_and_restores_readonly test_elevated_restart_failure_rolls_back_and_restores_readonly
run_test elevated_fsync_failure_rolls_back_and_restores_readonly test_elevated_fsync_failure_rolls_back_and_restores_readonly
run_test elevated_backup_mv_failure_keeps_originals test_elevated_backup_mv_failure_keeps_originals
run_test elevated_enable_failure_retries_and_reports_writable test_elevated_enable_failure_retries_and_reports_writable
run_test existing_config_early_sync_failure_preserves_both_scopes test_existing_config_early_sync_failure_preserves_both_scopes
run_test deb_obsolete_conffile_transition_is_wired test_deb_obsolete_conffile_transition_is_wired

printf 'TEST RESULT %d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
[[ "$FAIL_COUNT" -eq 0 ]]
