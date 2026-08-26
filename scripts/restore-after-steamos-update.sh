#!/usr/bin/env bash
# Hardened, manual post-SteamOS-OTA restore.  It deliberately never enables the unit.
#
# Invariant map:
#  1 assemble_config/validate_connection validate the final ES URL, CA and key before mutation.
#  2 assert_self_trusted verifies this executable and every parent is root-owned and not writable.
#  3 field_from/operator-first assembly treats /etc/previous only as fallback input.
#  4 quiesce_unit/acceptance start disabled and print (but never run) the later enable command.
#  5 atomic_install/assert_secure_path implement .new, hash, fsync, rename, probe-first ordering.
#  6 readonly_disable_and_prove/readonly_enable_and_prove prove both RW and RO transitions.
#  7 validate_effective_unit checks the surviving effective unit/drop-ins; it is never replaced.
#  8 apply_sysctl uses only the RigSignal sysctl file and verifies its one required value.
#  9 acceptance checks service state, bytes/inode, journal, restarts, ownership/modes, sysctl and RO.
# 10 strict mode, cleanup trap, flock, stale-temp cleanup and idempotent install provide scaffolding.

set -euo pipefail

# Test-only fault knobs must never alter a shipped restore invoked from ambient root env.
for fault_var in "${!RIGSIGNAL_RESTORE_FAULT_@}"; do unset "$fault_var"; done

DAEMON_SHA=6e70b514ad44754e0899d650eef68b119ad56b4a3df06e2ff6b548747d00c44e
DAEMON_SIZE=8991984
PROBES_SHA=ddf8199e9fe6935b43ca546ac4e1db35c7cee9334c068894817b12ae83ac0bdb
PROBES_SIZE=18888
UNIT=rigsignal-ebpf.service
ROOT=${RIGSIGNAL_RESTORE_ROOT:-}
PROC_ROOT=${RIGSIGNAL_RESTORE_PROC_ROOT:-/proc}
if [[ -n ${RIGSIGNAL_RESTORE_BIN:-} ]]; then PATH=${RIGSIGNAL_RESTORE_BIN}:$PATH; fi
export PATH

DAEMON_SOURCE= PROBES_SOURCE= CONFIG_SOURCE= CA_SOURCE= SYSCTL_SOURCE=
PREVIOUS_CONFIG= EXPECTED_API_KEY_ID= EXPECTED_CA_FINGERPRINT=
SUCCESS=0 CLEANING=0 LOCK_FD=

usage() {
  printf '%s\n' "usage: $0 --daemon PATH --probes PATH --config PROTECTED_PATH --ca PATH --sysctl PATH --expected-api-key-id ID --ca-fingerprint SHA256[:HEX] [--previous-config PATH]" >&2
}
die() { printf 'restore: %s\n' "$1" >&2; exit 1; }
target() { printf '%s%s' "$ROOT" "$1"; }

# The unit survives on the durable /etc partition; a missing binary is the
# expected post-OTA state for a legacy installation and must be restored.
if [[ ! -f "$(target /etc/systemd/system/rigsignal-ebpf.service)" ]]; then
  printf '%s\n' 'eBPF restore unsupported while eBPF is shelved (see EBPF-REENABLE-DESIGN-STUB)'
  exit 0
fi

cleanup() {
  local status=$?
  [[ $CLEANING == 1 ]] && exit "$status"
  CLEANING=1
  if [[ $SUCCESS != 1 ]]; then
    # Do not expose command output (which can contain unit environment values).
    systemctl stop "$UNIT" >/dev/null 2>&1 || true
    systemctl disable "$UNIT" >/dev/null 2>&1 || true
    steamos-readonly enable >/dev/null 2>&1 || true
    printf '%s\n' 'restore: aborted; unit stopped/disabled and filesystem read-only requested.' >&2
  fi
  exit "$status"
}
trap cleanup ERR INT TERM EXIT

while (($#)); do
  case $1 in
    --daemon) DAEMON_SOURCE=${2-}; shift 2 ;;
    --probes) PROBES_SOURCE=${2-}; shift 2 ;;
    --config) CONFIG_SOURCE=${2-}; shift 2 ;;
    --ca) CA_SOURCE=${2-}; shift 2 ;;
    --sysctl) SYSCTL_SOURCE=${2-}; shift 2 ;;
    --previous-config) PREVIOUS_CONFIG=${2-}; shift 2 ;;
    --expected-api-key-id) EXPECTED_API_KEY_ID=${2-}; shift 2 ;;
    --ca-fingerprint) EXPECTED_CA_FINGERPRINT=${2-}; shift 2 ;;
    -h|--help) usage; SUCCESS=1; exit 0 ;;
    *) usage; die 'unknown argument' ;;
  esac
done
[[ -n $DAEMON_SOURCE && -n $PROBES_SOURCE && -n $CONFIG_SOURCE && -n $CA_SOURCE && -n $SYSCTL_SOURCE ]] || { usage; die 'all staged protected paths are required'; }
[[ -n $EXPECTED_API_KEY_ID && -n $EXPECTED_CA_FINGERPRINT ]] || die 'all expected connection pins are required'
[[ -r $DAEMON_SOURCE && -r $PROBES_SOURCE && -r $CONFIG_SOURCE && -r $CA_SOURCE && -r $SYSCTL_SOURCE ]] || die 'a staged input is unreadable'
[[ -n $PREVIOUS_CONFIG ]] || PREVIOUS_CONFIG=$(target /etc/previous/rigsignal/rigsignal.toml)

assert_self_trusted() {
  local p part
  p=$(readlink -f "$0") || die 'cannot resolve script path'
  [[ -f $p ]] || die 'script path is not a regular file'
  while :; do
    part=$(stat -c '%u:%a' "$p") || die 'cannot stat script trust path'
    [[ ${part%%:*} == 0 ]] || die 'script trust path is not root-owned'
    # Group/other write bits are the final two octal digits.
    (( (8#${part#*:} & 0022) == 0 )) || die 'script trust path is group/other-writable'
    [[ $p == / ]] && break
    p=$(dirname "$p")
  done
}

field_from() {
  # Canonical, quoted TOML values only.  This keeps the secret in shell memory, never argv/logs.
  local key=$1 file=$2
  [[ -r $file ]] || return 0
  awk -v k="$key" '
    /^\[elasticsearch\][[:space:]]*$/ { yes=1; next }
    /^\[/ { yes=0 }
    yes && $0 ~ "^[[:space:]]*" k "[[:space:]]*=" {
      sub(/^[^=]*=[[:space:]]*/, "");
      if ($0 ~ /^"[^"]*"[[:space:]]*$/) { sub(/^"/, ""); sub(/"[[:space:]]*$/, ""); print; exit }
    }' "$file"
}

toml_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
sha_of() { sha256sum "$1" | awk '{print $1}'; }
size_of() { stat -c '%s' "$1"; }
assert_pinned() {
  local file=$1 wanted_sha=$2 wanted_size=$3 label=$4
  [[ $(size_of "$file") == "$wanted_size" ]] || die "$label size does not match attested pin"
  [[ $(sha_of "$file") == "$wanted_sha" ]] || die "$label hash does not match attested pin"
}

assert_self_trusted

# Operator file fields win one at a time.  An /etc/previous file is consulted only for omitted fields.
ES_URL=$(field_from endpoint "$CONFIG_SOURCE")
ES_API_KEY=$(field_from api_key "$CONFIG_SOURCE")
ES_CA_CERT=$(field_from ca_cert "$CONFIG_SOURCE")
[[ -n $ES_URL ]] || ES_URL=$(field_from endpoint "$PREVIOUS_CONFIG")
[[ -n $ES_API_KEY ]] || ES_API_KEY=$(field_from api_key "$PREVIOUS_CONFIG")
[[ -n $ES_CA_CERT ]] || ES_CA_CERT=$(field_from ca_cert "$PREVIOUS_CONFIG")
[[ -n $ES_URL && -n $ES_API_KEY ]] || die 'assembled configuration lacks endpoint or API key'
# The source CA is authoritative input; final TOML uses the fixed protected install location.
FINAL_CONFIG=$(printf '[elasticsearch]\nendpoint = "%s"\napi_key = "%s"\nca_cert = "/etc/rigsignal/ca.crt"\n\n[ebpf]\nprobe_path = "/usr/local/lib/rigsignal/rigsignal-ebpf-probes"\n' "$(toml_escape "$ES_URL")" "$(toml_escape "$ES_API_KEY")")

validate_connection() {
  local auth privileges got_id got_fingerprint expected_fingerprint
  got_fingerprint=$(openssl x509 -in "$CA_SOURCE" -noout -fingerprint -sha256 | sed 's/.*=//; s/://g') || die 'cannot fingerprint staged CA'
  expected_fingerprint=${EXPECTED_CA_FINGERPRINT//:/}
  [[ ${got_fingerprint^^} == ${expected_fingerprint^^} ]] || die 'staged CA fingerprint does not match pin'
  # Header material is passed through an anonymous fd, not an argv value or a persistent temp file.
  auth=$(curl --fail --silent --show-error -H 'Accept: application/json' --cacert "$CA_SOURCE" --config <(printf 'header = "Authorization: ApiKey %s"\n' "$ES_API_KEY") "${ES_URL%/}/_security/_authenticate?pretty=false&filter_path=enabled,authentication_type,api_key.id") || die 'ES authentication request failed'
  auth=$(printf '%s' "$auth" | tr -d '[:space:]')
  [[ $auth == *'"enabled":true'* && $auth == *'"authentication_type":"api_key"'* ]] || die 'ES authentication response is not API-key authentication'
  got_id=$(printf '%s' "$auth" | sed -n 's/.*"api_key"[[:space:]]*:[[:space:]]*{[^}]*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [[ $got_id == "$EXPECTED_API_KEY_ID" ]] || die 'ES API-key id does not match expected id'
  privileges=$(curl --fail --silent --show-error -H 'Accept: application/json' --cacert "$CA_SOURCE" --config <(printf 'header = "Authorization: ApiKey %s"\nheader = "Content-Type: application/json"\n' "$ES_API_KEY") --data '{"index":[{"names":["metrics-rigsignal.ebpf-default","metrics-rigsignal.ebpf_thread-default"],"privileges":["create_doc"]}]}' "${ES_URL%/}/_security/user/_has_privileges?pretty=false&filter_path=has_all_requested") || die 'ES privilege request failed'
  privileges=$(printf '%s' "$privileges" | tr -d '[:space:]')
  [[ $privileges == *'"has_all_requested":true'* ]] || die 'ES key lacks create_doc on one or more required streams'
  # Cluster identity is pinned by the CA fingerprint + api_key.id + create_doc privilege on the named streams; a create_doc-scoped ingest key cannot read cluster_uuid (needs cluster:monitor), so it is intentionally not checked.
}

# This is intentionally before lock creation, readonly toggling, service changes, or destination writes.
validate_connection
assert_pinned "$DAEMON_SOURCE" "$DAEMON_SHA" "$DAEMON_SIZE" daemon
assert_pinned "$PROBES_SOURCE" "$PROBES_SHA" "$PROBES_SIZE" probes

validate_effective_unit() {
  local unit_text
  unit_text=$(systemctl cat "$UNIT") || die 'cannot read effective surviving unit'
  [[ $unit_text =~ ExecStart=.*/usr/local/bin/rigsignal-ebpf ]] || die 'effective unit ExecStart is unexpected'
  [[ $unit_text == *'AmbientCapabilities='*CAP_BPF*CAP_PERFMON*CAP_SYS_ADMIN*CAP_DAC_READ_SEARCH* ]] || die 'effective unit lacks required ambient capabilities'
  [[ $unit_text == *'CapabilityBoundingSet='*CAP_BPF*CAP_PERFMON*CAP_SYS_ADMIN*CAP_DAC_READ_SEARCH* ]] || die 'effective unit lacks required capability bound'
  [[ $unit_text == *'Restart=on-failure'* ]] || die 'effective unit restart policy is unexpected'
  if [[ $unit_text =~ Environment(File)?=.*ES_(URL|API_KEY|CA_CERT) ]]; then die 'effective unit overrides ES configuration'; fi
}
validate_effective_unit

assert_secure_path() {
  local p=$1 s
  while [[ $p != "$ROOT" && $p != / ]]; do
    [[ ! -L $p ]] || die "symlink in target path: $p"
    s=$(stat -c '%u' "$p") || die "cannot stat target component: $p"
    [[ $s == 0 ]] || die "target component is not root-owned: $p"
    p=$(dirname "$p")
  done
}
LOCK_PATH=$(target /run/rigsignal-restore.lock)
assert_secure_path "$(dirname "$LOCK_PATH")"
mkdir -p "$(dirname "$LOCK_PATH")"
exec {LOCK_FD}>"$LOCK_PATH"
flock -n "$LOCK_FD" || die 'another restore is already running'

prepare_dir() {
  assert_secure_path "$(dirname "$1")"
  [[ ! -L $1 ]] || die "symlink in target path: $1"
  mkdir -p "$1"
  chown root:root "$1"; chmod 0755 "$1"
  assert_secure_path "$1"
}
readonly_disable_and_prove() {
  local proof
  steamos-readonly disable
  proof="$(target /usr/local/.rigsignal-restore-rw.$$)"
  : >"$proof" || die '/usr/local did not become writable'
  chown root:root "$proof"; rm -f "$proof"
}
readonly_enable_and_prove() {
  local proof
  steamos-readonly enable
  proof="$(target /usr/local/.rigsignal-restore-ro.$$)"
  if { : >"$proof"; } 2>/dev/null; then rm -f "$proof" || true; die '/usr/local remains writable after readonly enable'; fi
}
fsync_path() { sync -f "$1"; }
atomic_install() {
  local src=$1 dst=$2 mode=$3 sha=$4 size=$5 label=$6 tmp
  assert_secure_path "$(dirname "$dst")"
  tmp="$dst.new"
  rm -f "$tmp"                         # resumable cleanup of an interrupted prior attempt
  cp -- "$src" "$tmp"
  chown root:root "$tmp"; chmod "$mode" "$tmp"
  assert_pinned "$tmp" "$sha" "$size" "$label temporary copy"
  fsync_path "$tmp"; fsync_path "$(dirname "$tmp")"
  mv -f -- "$tmp" "$dst"               # same-directory rename() is atomic
  fsync_path "$(dirname "$dst")"
}
atomic_text() {
  local text=$1 dst=$2 mode=$3 tmp
  assert_secure_path "$(dirname "$dst")"; tmp="$dst.new"; rm -f "$tmp"
  (umask 077; printf '%s' "$text" >"$tmp")
  chown root:root "$tmp"; chmod "$mode" "$tmp"; fsync_path "$tmp"; fsync_path "$(dirname "$tmp")"
  mv -f -- "$tmp" "$dst"; fsync_path "$(dirname "$dst")"
}
assert_mode_owner() { [[ $(stat -c '%u:%a' "$1") == "0:$2" ]] || die "unexpected ownership/mode: $1"; }

quiesce_unit() { systemctl stop "$UNIT"; systemctl disable "$UNIT"; }
readonly_disable_and_prove
prepare_dir "$(target /usr/local/bin)"
prepare_dir "$(target /usr/local/lib/rigsignal)"
prepare_dir "$(target /etc/rigsignal)"
prepare_dir "$(target /etc/sysctl.d)"
quiesce_unit

# Probes are committed first; the daemon is the deliberate commit marker.
atomic_install "$PROBES_SOURCE" "$(target /usr/local/lib/rigsignal/rigsignal-ebpf-probes)" 0755 "$PROBES_SHA" "$PROBES_SIZE" probes
atomic_install "$DAEMON_SOURCE" "$(target /usr/local/bin/rigsignal-ebpf)" 0755 "$DAEMON_SHA" "$DAEMON_SIZE" daemon
atomic_text "$FINAL_CONFIG" "$(target /etc/rigsignal/rigsignal.toml)" 0600
atomic_install "$CA_SOURCE" "$(target /etc/rigsignal/ca.crt)" 0644 "$(sha_of "$CA_SOURCE")" "$(size_of "$CA_SOURCE")" CA
atomic_install "$SYSCTL_SOURCE" "$(target /etc/sysctl.d/20-rigsignal-perf.conf)" 0644 "$(sha_of "$SYSCTL_SOURCE")" "$(size_of "$SYSCTL_SOURCE")" sysctl

apply_sysctl() {
  sysctl -p "$(target /etc/sysctl.d/20-rigsignal-perf.conf)" >/dev/null
  [[ $(sysctl -n kernel.perf_event_paranoid) == 1 ]] || die 'perf_event_paranoid is not 1'
}
acceptance() {
  local pid before after journal installed_inode running_inode attempt start_cursor
  systemctl daemon-reload
  start_cursor=$(journalctl -u "$UNIT" --no-pager -n1 --show-cursor 2>/dev/null | sed -n 's/^-- cursor: //p')
  systemctl start "$UNIT"               # remains disabled by quiesce_unit
  [[ $(systemctl is-active "$UNIT") == active ]] || die 'unit did not become active'
  assert_pinned "$(target /usr/local/bin/rigsignal-ebpf)" "$DAEMON_SHA" "$DAEMON_SIZE" daemon
  assert_pinned "$(target /usr/local/lib/rigsignal/rigsignal-ebpf-probes)" "$PROBES_SHA" "$PROBES_SIZE" probes
  pid=$(systemctl show -p MainPID --value "$UNIT"); [[ $pid =~ ^[1-9][0-9]*$ ]] || die 'unit has no MainPID'
  [[ -e $PROC_ROOT/$pid/exe ]] || die 'MainPID executable is unavailable'
  installed_inode=$(stat -Lc '%i' "$(target /usr/local/bin/rigsignal-ebpf)")
  running_inode=$(stat -Lc '%i' "$PROC_ROOT/$pid/exe")
  [[ $installed_inode == "$running_inode" && $(sha_of "$PROC_ROOT/$pid/exe") == "$DAEMON_SHA" ]] || die 'running daemon bytes do not match installed daemon'
  # A cold eBPF attach can take time; the self-test's journalctl mock is instantaneous.
  for attempt in {1..10}; do
    if [[ -n $start_cursor ]]; then
      journal=$(journalctl -u "$UNIT" --after-cursor "$start_cursor" --no-pager) || die 'cannot read daemon journal'
    else
      journal=$(journalctl -u "$UNIT" -b --no-pager) || die 'cannot read daemon journal'
    fi
    [[ $journal == *'probes: 9/9 loaded'* ]] && break
    (( attempt < 10 )) && sleep 1
  done
  [[ $journal == *'probes: 9/9 loaded'* ]] || die 'journal does not prove all probe candidates loaded'
  if grep -Eqi 'skipping probe|failed to attach|collect.*error|loss[ -]?counters.*(increased|error)' <<<"$journal"; then die 'journal contains probe/attach/collect/loss errors'; fi
  before=$(systemctl show -p NRestarts --value "$UNIT"); sleep "${RIGSIGNAL_RESTORE_DWELL:-10}"; after=$(systemctl show -p NRestarts --value "$UNIT")
  [[ $(systemctl is-active "$UNIT") == active ]] || die 'unit did not remain active during dwell'
  [[ $before == "$after" ]] || die 'unit restarted during dwell'
  assert_mode_owner "$(target /usr/local/bin/rigsignal-ebpf)" 755
  assert_mode_owner "$(target /usr/local/lib/rigsignal/rigsignal-ebpf-probes)" 755
  assert_mode_owner "$(target /etc/rigsignal/rigsignal.toml)" 600
  assert_mode_owner "$(target /etc/rigsignal/ca.crt)" 644
  assert_mode_owner "$(target /etc/sysctl.d/20-rigsignal-perf.conf)" 644
  [[ $(sysctl -n kernel.perf_event_paranoid) == 1 ]] || die 'perf_event_paranoid changed after start'
}
apply_sysctl
readonly_enable_and_prove
acceptance
SUCCESS=1
printf '%s\n' 'restore: infrastructure restored; end-to-end shipping remains session-gated/unverified.'
printf '%s\n' 'After the external headless-FC6 shipping gate passes, run: systemctl enable rigsignal-ebpf.service'
