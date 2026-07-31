#!/usr/bin/env bash
set -euo pipefail

launcher="$(cd "$(dirname "$0")/.." && pwd)/rigsignal-launcher.sh"
generation="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
test_tmp="$(mktemp -d)"

cleanup() {
    rm -rf "$test_tmp"
}
trap cleanup EXIT

plain="$($launcher status 2>&1 || true)"
case "$plain" in *"Agent"*) ;; *) echo "plain status no longer dispatches cmd_status" >&2; exit 1 ;; esac

expect_reserved() {
    local output status
    set +e
    output="$($launcher status "$@" 2>&1)"
    status=$?
    set -e
    [ "$status" -ne 0 ] || { echo "reserved form unexpectedly succeeded: $*" >&2; exit 1; }
    case "$output" in *"not yet available"*) ;; *) echo "valid reserved form was not recognized: $*" >&2; exit 1 ;; esac
}

expect_rejected() {
    local output status
    set +e
    output="$($launcher status "$@" 2>&1)"
    status=$?
    set -e
    [ "$status" -ne 0 ] || { echo "invalid form unexpectedly succeeded: $*" >&2; exit 1; }
    case "$output" in *"Agent"*) echo "invalid form invoked cmd_status: $*" >&2; exit 1 ;; esac
}

expect_reserved handshake recheck
expect_reserved handshake recheck "$generation"
expect_rejected handshake
expect_rejected handshake recheck "${generation^^}"
expect_rejected handshake recheck "${generation:0:63}"
expect_rejected handshake recheck "${generation}a"
expect_rejected handshake recheck "$(printf 'g%.0s' {1..64})"
expect_rejected handshake recheck "$generation" extra
expect_rejected something else

# Positional parameters are never reparsed as shell code, even if a hostile
# launcher environment supplies a surprising IFS value.
marker="$test_tmp/injection-marker"
injection="$(printf '%s%s%s%s' '$' '(touch ' "$marker" ')')"
expect_rejected handshake recheck "$injection"
[ ! -e "$marker" ] || { echo "generation argument was executed by the launcher" >&2; exit 1; }

set +e
ifs_output="$(IFS='/' "$launcher" status handshake recheck "$generation" 2>&1)"
ifs_status=$?
set -e
[ "$ifs_status" -ne 0 ] || { echo "IFS variant unexpectedly succeeded" >&2; exit 1; }
case "$ifs_output" in *"not yet available"*) ;; *) echo "IFS variant was not recognized" >&2; exit 1 ;; esac

test_home="$test_tmp/home"
test_bin="$test_tmp/bin"
mkdir -p "$test_home" "$test_bin"
printf '%s\n' '#!/usr/bin/env bash' \
    '[ "$1" = "-c" ] && exec /usr/bin/python3 "$@"' \
    'case "$4" in' \
    "    GET) printf '%s\n' '200' '{\"version\":{\"number\":\"9.4.3\"}}' ;;" \
    "    POST) printf '%s\n' '200' '{\"has_all_requested\":true}' ;;" \
    "    *) printf '%s\n' '404' '{}' ;;" \
    'esac' >"$test_bin/python3"
chmod +x "$test_bin/python3"
if ! (
    cd "$test_tmp"
    printf 'http://127.0.0.1:9200\ntest-api-key\n' | \
        env SUDO_USER='' HOME="$test_home" XDG_CONFIG_HOME='relative/config' \
        RIGSIGNAL_DEBUG=0 PATH="$test_bin:/usr/bin:/bin" "$launcher" setup
); then
    echo "relative XDG_CONFIG_HOME setup failed" >&2
    exit 1
fi
[ -f "$test_home/.config/rigsignal/rigsignal.toml" ] || {
    echo "relative XDG_CONFIG_HOME did not fall back to HOME/.config" >&2
    exit 1
}
[ ! -e "$test_tmp/relative/config/rigsignal/rigsignal.toml" ] || {
    echo "relative XDG_CONFIG_HOME was used as a config path" >&2
    exit 1
}

echo "rigsignal launcher handshake status guard: PASS"
