#!/usr/bin/env bash
# Offline PATH-shimmed verification for restore-after-steamos-update.sh.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
RESTORE=$HERE/restore-after-steamos-update.sh
TMP=$(mktemp -d)
trap 'chmod -R u+w "$TMP" 2>/dev/null || true; rm -rf "$TMP"' EXIT
PASS=0

fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }
ok() { PASS=$((PASS + 1)); printf 'ok - %s\n' "$1"; }
make_shims() {
  local d=$1
  mkdir -p "$d"
  for c in systemctl sysctl curl steamos-readonly journalctl openssl stat sha256sum chown mv; do : >"$d/$c"; chmod +x "$d/$c"; done
  cat >"$d/systemctl" <<'EOF'
#!/usr/bin/env bash
set -eu; log=${MOCK_LOG:?}; cmd=$1; shift || :; printf 'systemctl %s %s\n' "$cmd" "$*" >>"$log"
case $cmd in
 cat) if [[ ${MOCK_UNIT_OVERRIDE:-0} == 1 ]]; then printf '[Service]\nExecStart=/usr/local/bin/rigsignal-ebpf\nEnvironment=ES_API_KEY=bad\nAmbientCapabilities=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH\nCapabilityBoundingSet=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH\nRestart=on-failure\n'; else printf '[Service]\nExecStart=/usr/local/bin/rigsignal-ebpf --config /etc/rigsignal/rigsignal.toml\nAmbientCapabilities=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH\nCapabilityBoundingSet=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH\nRestart=on-failure\n'; fi ;;
 is-active)
   active_calls=$(grep -c '^systemctl is-active ' "$log" || :)
   if [[ -n ${MOCK_ACTIVE_AFTER_DWELL:-} && $active_calls -ge 2 ]]; then printf '%s\n' "$MOCK_ACTIVE_AFTER_DWELL"; else printf '%s\n' "${MOCK_ACTIVE:-active}"; fi ;;
 show) [[ $* == *MainPID* ]] && printf '4242\n' || printf '0\n' ;;
 *) : ;;
esac
EOF
  cat >"$d/chown" <<'EOF'
#!/usr/bin/env bash
# Ownership is asserted through the stat shim; the unprivileged harness cannot chown root.
exit 0
EOF
  cat >"$d/sysctl" <<'EOF'
#!/usr/bin/env bash
set -eu; [[ ${1:-} == -n ]] && { printf '1\n'; exit 0; }; exit 0
EOF
  cat >"$d/curl" <<'EOF'
#!/usr/bin/env bash
set -eu
args="$*"
case $args in
 *_authenticate*) case ${MOCK_AUTH:-ok} in ok) if [[ ${MOCK_PRETTY_JSON:-0} == 1 ]]; then printf '{\n  "api_key" : {\n    "id" : "expected-id"\n  },\n  "authentication_type" : "api_key",\n  "enabled" : true\n}\n'; else printf '{"enabled":true,"authentication_type":"api_key","api_key":{"id":"expected-id"}}'; fi ;; wrong-id) printf '{"enabled":true,"authentication_type":"api_key","api_key":{"id":"other-id"}}' ;; *) printf '{"enabled":false}' ;; esac ;;
 *_has_privileges*) [[ ${MOCK_PRIV:-ok} == ok ]] && { [[ ${MOCK_PRETTY_JSON:-0} == 1 ]] && printf '{\n  "has_all_requested" : true\n}\n' || printf '{"has_all_requested":true}'; } || printf '{"has_all_requested":false}' ;;
 *) [[ ${MOCK_PRETTY_JSON:-0} == 1 ]] && printf '{\n  "cluster_uuid" : "cluster-1"\n}\n' || printf '{"cluster_uuid":"cluster-1"}' ;;
esac
EOF
  cat >"$d/mv" <<'EOF'
#!/usr/bin/env bash
set -eu
/bin/mv "$@"
if [[ ${MOCK_PAUSE_AFTER_PROBES_RENAME:-0} == 1 && ${!#} == *rigsignal-ebpf-probes ]]; then
  : >"${MOCK_FAULT_MARKER:?}"
  while [[ ! -e ${MOCK_FAULT_RELEASE:?} ]]; do sleep 0.05; done
fi
EOF
  cat >"$d/steamos-readonly" <<'EOF'
#!/usr/bin/env bash
set -eu; root=${RIGSIGNAL_RESTORE_ROOT:?}
if [[ $1 == disable ]]; then chmod -R u+w "$root/usr" 2>/dev/null || :; else chmod -R u-w "$root/usr" 2>/dev/null || :; fi
printf 'readonly %s\n' "$1" >>"${MOCK_LOG:?}"
EOF
  cat >"$d/journalctl" <<'EOF'
#!/usr/bin/env bash
if [[ ${MOCK_JOURNAL_BAD:-0} == 1 ]]; then printf 'probes: 9/9 loaded\nfailed to attach probe\n'; else printf 'probes: 9/9 loaded\n'; fi
EOF
  cat >"$d/openssl" <<'EOF'
#!/usr/bin/env bash
printf 'sha256 Fingerprint=AA:BB\n'
EOF
  cat >"$d/stat" <<'EOF'
#!/usr/bin/env bash
set -eu
fmt= path=
for x in "$@"; do [[ $x == -c || $x == -Lc ]] && continue; [[ -z $fmt && $x == %* ]] && { fmt=$x; continue; }; path=$x; done
if [[ $fmt == %u || $fmt == %u:%a ]]; then
  if [[ ${MOCK_BAD_SELF:-0} == 1 && $path == *restore-after-steamos-update.sh ]]; then
    [[ $fmt == %u ]] && printf '1000\n' || printf '1000:775\n'
  elif [[ $fmt == %u ]]; then printf '0\n'
  elif [[ $path == *.toml ]]; then printf '0:600\n'
  elif [[ $path == *.crt || $path == *20-rigsignal-perf.conf ]]; then printf '0:644\n'
  else printf '0:755\n'; fi
elif [[ $fmt == %s ]]; then
  case $path in
    *rigsignal-ebpf-probes*) printf '18888\n' ;;
    *rigsignal-ebpf*|*/proc/*/exe) printf '8574648\n' ;;
    *) /usr/bin/stat -c %s "$path" ;;
  esac
elif [[ $fmt == %i ]]; then /usr/bin/stat -Lc %i "$path"
else /usr/bin/stat "$@"; fi
EOF
  cat >"$d/sha256sum" <<'EOF'
#!/usr/bin/env bash
set -eu
case ${1:?} in
  *rigsignal-ebpf-probes*) printf '%s  %s\n' ddf8199e9fe6935b43ca546ac4e1db35c7cee9334c068894817b12ae83ac0bdb "$1" ;;
  *input/rigsignal-ebpf) [[ ${MOCK_BAD_HASH:-0} == 1 ]] && printf '%s  %s\n' bad "$1" || printf '%s  %s\n' 8f4676684ecfe38814af4b6cae362b442200b94a17f7acc261f08634ed9a4e9a "$1" ;;
  *rigsignal-ebpf*|*/proc/*/exe) printf '%s  %s\n' 8f4676684ecfe38814af4b6cae362b442200b94a17f7acc261f08634ed9a4e9a "$1" ;;
  *) /usr/bin/sha256sum "$1" ;;
esac
EOF
}
setup_case() {
  CASE=$TMP/case-$1; ROOT=$CASE/root; BIN=$CASE/bin; LOG=$CASE/log
  mkdir -p "$ROOT"{,/usr/local/bin,/usr/local/lib/rigsignal,/etc/rigsignal,/etc/sysctl.d,/run,/proc/4242} "$CASE/input"
  make_shims "$BIN"; : >"$LOG"
  printf 'daemon bytes\n' >"$CASE/input/rigsignal-ebpf"; printf 'probe bytes\n' >"$CASE/input/rigsignal-ebpf-probes"; printf 'CA\n' >"$CASE/input/ca"; printf 'kernel.perf_event_paranoid = 1\n' >"$CASE/input/sysctl"
  printf '[elasticsearch]\nendpoint = "https://mock"\napi_key = "secret-never-logged"\n' >"$CASE/input/config"
  ln -s "$ROOT/usr/local/bin/rigsignal-ebpf" "$ROOT/proc/4242/exe"
}
restore_args() {
  printf '%s\n' --daemon "$CASE/input/rigsignal-ebpf" --probes "$CASE/input/rigsignal-ebpf-probes" --config "$CASE/input/config" --ca "$CASE/input/ca" --sysctl "$CASE/input/sysctl" --expected-api-key-id expected-id
}
run_restore_with_pin() {
  local pin=$1; shift
  mapfile -t args < <(restore_args)
  env RIGSIGNAL_RESTORE_ROOT="$ROOT" RIGSIGNAL_RESTORE_PROC_ROOT="$ROOT/proc" RIGSIGNAL_RESTORE_BIN="$BIN" MOCK_LOG="$LOG" RIGSIGNAL_RESTORE_DWELL=0 "$@" bash "$RESTORE" "${args[@]}" --ca-fingerprint "$pin"
}
run_restore() { run_restore_with_pin AABB "$@"; }

setup_case happy
run_restore >"$CASE/out" 2>&1 || fail happy
[[ -f $ROOT/usr/local/bin/rigsignal-ebpf && -f $ROOT/usr/local/lib/rigsignal/rigsignal-ebpf-probes ]] || fail 'happy installs payloads'
[[ $(grep -n 'systemctl start' "$LOG" | cut -d: -f1) -gt $(grep -n 'systemctl disable' "$LOG" | cut -d: -f1) ]] || fail 'happy starts while disabled'
[[ $(grep -n 'readonly enable' "$LOG" | cut -d: -f1) -lt $(grep -n 'systemctl start' "$LOG" | cut -d: -f1) ]] || fail 'happy restores RO before start'
[[ $(/usr/bin/stat -c %a "$ROOT/etc/rigsignal/rigsignal.toml") == 600 && $(/usr/bin/stat -c %a "$ROOT/etc/rigsignal/ca.crt") == 644 ]] || fail 'happy installs private config and public CA modes'
ok 'happy path: probes then daemon, disabled start, acceptance, RO'

setup_case pretty-json
run_restore MOCK_PRETTY_JSON=1 >"$CASE/out" 2>&1 || fail 'pretty JSON validation'
ok 'multi-line reordered ES JSON validates'

setup_case mixed-fingerprint
run_restore_with_pin aa:Bb >"$CASE/out" 2>&1 || fail 'mixed-case CA fingerprint pin'
ok 'mixed-case CA fingerprint pin validates'

setup_case hash
if run_restore MOCK_BAD_HASH=1 >"$CASE/out" 2>&1; then fail 'hash mismatch unexpectedly succeeded'; fi
[[ ! -e $ROOT/usr/local/bin/rigsignal-ebpf && ! -e $ROOT/usr/local/lib/rigsignal/rigsignal-ebpf-probes ]] || fail 'hash failure mutated payloads'
ok 'hash mismatch aborts before installs'

setup_case auth
if run_restore MOCK_AUTH=bad >"$CASE/out" 2>&1; then fail 'auth failure unexpectedly succeeded'; fi
[[ ! -e $ROOT/usr/local/bin/rigsignal-ebpf ]] || fail 'auth failure mutated payloads'
ok 'ES auth failure aborts before installs'

setup_case wrong-id
if run_restore MOCK_AUTH=wrong-id >"$CASE/out" 2>&1; then fail 'wrong API-key id unexpectedly succeeded'; fi
[[ ! -e $ROOT/usr/local/bin/rigsignal-ebpf ]] || fail 'wrong API-key id mutated payloads'
ok 'wrong ES API-key id aborts before installs'

setup_case privilege
if run_restore MOCK_PRIV=bad >"$CASE/out" 2>&1; then fail 'privilege failure unexpectedly succeeded'; fi
[[ ! -e $ROOT/usr/local/bin/rigsignal-ebpf ]] || fail 'privilege failure mutated payloads'
ok 'missing stream privilege aborts before installs'

setup_case fault
mapfile -t args < <(restore_args)
FAULT_MARKER=$CASE/fault-marker FAULT_RELEASE=$CASE/fault-release
env RIGSIGNAL_RESTORE_ROOT="$ROOT" RIGSIGNAL_RESTORE_PROC_ROOT="$ROOT/proc" RIGSIGNAL_RESTORE_BIN="$BIN" MOCK_LOG="$LOG" RIGSIGNAL_RESTORE_DWELL=0 MOCK_PAUSE_AFTER_PROBES_RENAME=1 MOCK_FAULT_MARKER="$FAULT_MARKER" MOCK_FAULT_RELEASE="$FAULT_RELEASE" bash "$RESTORE" "${args[@]}" --ca-fingerprint AABB >"$CASE/out" 2>&1 &
restore_pid=$!
for _ in {1..100}; do [[ -e $FAULT_MARKER ]] && break; sleep 0.05; done
[[ -e $FAULT_MARKER ]] || fail 'fault harness did not observe probes rename'
kill -KILL "$restore_pid"
: >"$FAULT_RELEASE"
if wait "$restore_pid" 2>/dev/null; then fail 'external fault injection unexpectedly succeeded'; fi
[[ -f $ROOT/usr/local/lib/rigsignal/rigsignal-ebpf-probes && ! -e $ROOT/usr/local/bin/rigsignal-ebpf ]] || fail 'commit marker invariant'
run_restore >"$CASE/out2" 2>&1 || fail 'resume after fault'
[[ ! -e $ROOT/usr/local/bin/rigsignal-ebpf.new && ! -e $ROOT/usr/local/lib/rigsignal/rigsignal-ebpf-probes.new ]] || fail 'resume left stale temporary'
ok 'fault/resume preserves daemon commit marker and cleans .new'

setup_case unit
if run_restore MOCK_UNIT_OVERRIDE=1 >"$CASE/out" 2>&1; then fail 'unit override unexpectedly succeeded'; fi
[[ ! -e $ROOT/usr/local/bin/rigsignal-ebpf ]] || fail 'unit override mutated payloads'
ok 'ES unit environment override rejected'

setup_case self
if run_restore MOCK_BAD_SELF=1 >"$CASE/out" 2>&1; then fail 'untrusted script unexpectedly succeeded'; fi
ok 'group/user-writable execution path rejected'

setup_case acceptance
if run_restore MOCK_ACTIVE=activating >"$CASE/out" 2>&1; then fail 'activating unit unexpectedly succeeded'; fi
grep -q 'systemctl stop' "$LOG" && grep -q 'systemctl disable' "$LOG" && grep -q 'readonly enable' "$LOG" || fail 'failure cleanup missing'
ok 'acceptance failure leaves stopped, disabled, RO requested'

setup_case dwell-failed
if run_restore MOCK_ACTIVE_AFTER_DWELL=failed >"$CASE/out" 2>&1; then fail 'failed unit after dwell unexpectedly succeeded'; fi
grep -q 'systemctl stop' "$LOG" && grep -q 'systemctl disable' "$LOG" && grep -q 'readonly enable' "$LOG" || fail 'dwell failure cleanup missing'
ok 'stable restarts do not hide failed unit after dwell'
printf 'all %d self-test cases passed\n' "$PASS"
