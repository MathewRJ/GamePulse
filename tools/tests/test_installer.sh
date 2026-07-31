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
    rm -f "$port_file"
    python3 -c '
import http.server, json, sys
mode, port_file = sys.argv[1:]
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def send_json(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def do_GET(self):
        if self.path == "/_security/_authenticate":
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
with open(port_file, "w", encoding="utf-8") as handle:
    handle.write(str(server.server_address[1]))
server.serve_forever()
' "$mode" "$port_file" &
    MOCK_PID=$!

    local attempts=0
    while [[ ! -s "$port_file" && "$attempts" -lt 50 ]]; do
        kill -0 "$MOCK_PID" 2>/dev/null || return 1
        sleep 0.1
        attempts=$((attempts + 1))
    done
    [[ -s "$port_file" ]] || return 1
    MOCK_PORT=$(<"$port_file")
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
    if printf 'http://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$output" 2>&1; then
        setup_status=0
    else
        setup_status=$?
    fi
    stop_mock_es
    printf '%s' "$setup_status"
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
        && [[ -f "$home/.config/rigsignal/rigsignal.toml" ]]
}

fixture_arch() {
    case "$(uname -m)" in
        x86_64|amd64) printf 'x86_64' ;;
        aarch64|arm64) printf 'aarch64' ;;
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
    chmod +x "$package_dir/rigsignal-agent" "$package_dir/rigsignal" "$package_dir/rigsignal-uninstall"
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
        && [[ ! -e "$stage$home/.config/systemd/user/rigsignal-agent.service" ]] || return 1
    HOME="$home" DESTDIR="$stage" RIGSIGNAL_INSTALL_LOCAL_DIR="$release" \
        "$INSTALLER" --version 1.2.4 >"$TEST_TMP/reinstall.out" 2>&1 || return 1
    [[ -e "$stage$home/.local/bin/rigsignal-agent" ]] \
        && [[ -e "$stage$home/.local/bin/rigsignal" ]] \
        && [[ -e "$stage$home/.local/bin/rigsignal-uninstall" ]] \
        && [[ -e "$stage$home/.config/systemd/user/rigsignal-agent.service" ]]
}

test_setup_preserves_collection_on_reauth() {
    # Regression (reviewer 2026-07-22): credential re-run must not clobber the
    # user's [collection]/[session] settings (e.g. ebpf = true silently reset).
    local home="$TEST_TMP/home-reauth"
    local cfg="$home/.config/rigsignal"
    mkdir -p "$cfg"
    printf '[elasticsearch]\nendpoint = "http://127.0.0.1:1"\napi_key = "stale-key"\n\n[collection]\nebpf = true\ncpu = false\n\n[session]\nlabel = "custom"\n' >"$cfg/rigsignal.toml"
    start_mock_es happy || return 1
    printf 'http://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$TEST_TMP/setup-reauth.out" 2>&1 || { stop_mock_es; return 1; }
    stop_mock_es
    grep -q '^ebpf = true' "$cfg/rigsignal.toml" \
        && grep -q '^cpu = false' "$cfg/rigsignal.toml" \
        && grep -q 'label = "custom"' "$cfg/rigsignal.toml" \
        && grep -q 'api_key = "test-api-key"' "$cfg/rigsignal.toml"
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
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "$RIGSIGNAL_TEST_SUDO_LOG"\n' >"$stage/usr/bin/sudo"
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
    for pkgbuild in "$REPO_ROOT/packaging/PKGBUILD" "$REPO_ROOT/packaging/aur/PKGBUILD"; do
        grep -qx 'install=rigsignal.install' "$pkgbuild" || return 1
        grep -q 'usr/share/rigsignal/examples/rigsignal.toml.example' "$pkgbuild" || return 1
        ! grep -q '^backup=' "$pkgbuild" || return 1
    done
    cmp -s "$REPO_ROOT/packaging/rigsignal.install" "$REPO_ROOT/packaging/aur/rigsignal.install" || return 1

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
    if ! printf 'http://127.0.0.1:%s\ntest-api-key\n' "$MOCK_PORT" | \
        HOME="$home" XDG_CONFIG_HOME="$home/.config" PATH="$stage/usr/bin:$PATH" \
        RIGSIGNAL_TEST_SUDO_LOG="$sudo_log" RIGSIGNAL_DEBUG=0 \
        "$LAUNCHER" setup >"$TEST_TMP/package-setup.out" 2>&1; then
        stop_mock_es
        return 1
    fi
    stop_mock_es

    [[ -f "$home/.config/rigsignal/rigsignal.toml" ]] \
        && grep -q '^install -m 600 ' "$sudo_log"
}

run_test setup_fails_on_401 test_setup_fails_on_401
run_test setup_preserves_collection_on_reauth test_setup_preserves_collection_on_reauth
run_test setup_fails_without_create_doc test_setup_fails_without_create_doc
run_test setup_succeeds_with_required_privileges test_setup_succeeds_with_required_privileges
run_test checksum_mismatch_aborts_before_unpack test_checksum_mismatch_aborts_before_unpack
run_test uninstall_removes_staged_install test_uninstall_removes_staged_install
run_test package_user_unit_uses_user_config_and_detects_packaged_ebpf test_package_user_unit_uses_user_config_and_detects_packaged_ebpf

printf 'TEST RESULT %d passed, %d failed\n' "$PASS_COUNT" "$FAIL_COUNT"
[[ "$FAIL_COUNT" -eq 0 ]]
