#!/usr/bin/env bash
set -euo pipefail

launcher="$(cd "$(dirname "$0")/.." && pwd)/rigsignal-launcher.sh"
generation="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

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
marker="$(mktemp)"
rm -f "$marker"
trap 'rm -f "$marker"' EXIT
injection="$(printf '%s%s%s%s' '$' '(touch ' "$marker" ')')"
expect_rejected handshake recheck "$injection"
[ ! -e "$marker" ] || { echo "generation argument was executed by the launcher" >&2; exit 1; }

set +e
ifs_output="$(IFS='/' "$launcher" status handshake recheck "$generation" 2>&1)"
ifs_status=$?
set -e
[ "$ifs_status" -ne 0 ] || { echo "IFS variant unexpectedly succeeded" >&2; exit 1; }
case "$ifs_output" in *"not yet available"*) ;; *) echo "IFS variant was not recognized" >&2; exit 1 ;; esac

echo "rigsignal launcher handshake status guard: PASS"
