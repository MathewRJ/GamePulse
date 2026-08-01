#!/usr/bin/env bash
# Offline proof harness for tools/verify-release-window.  It builds disposable,
# deterministic fixtures so no recorded fixture can ever address a provider.
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
tool="$root/tools/verify-release-window"
work="$(mktemp -d "${TMPDIR:-/tmp}/rigsignal-window-test.XXXXXX")"
trap 'jobs -pr | xargs -r kill 2>/dev/null || true; rm -rf "$work"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; return 1; }

fixture="$work/fixture"
installed="$work/installed"
mkdir -p "$fixture/assets" "$fixture/bin" "$installed/bin" "$installed/lib/rigsignal/engine" "$work/real-bin"

commit=0123456789abcdef0123456789abcdef01234567
tag=v0.3.1

cat >"$work/agent.c" <<EOF
#include <stdio.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc == 2 && strcmp(argv[1], "--build-info-json") == 0) {
    puts("{\"name\":\"rigsignal-agent\",\"version\":\"0.3.1\",\"commit\":\"$commit\"}"); return 0;
  }
  if (argc == 2 && strcmp(argv[1], "--hold") == 0) { sleep(30); return 0; }
  return 0;
}
EOF
cc -O2 -o "$installed/bin/rigsignal-agent" "$work/agent.c"
cp "$installed/bin/rigsignal-agent" "$work/swapped-agent"
printf 'different native executable\n' >>"$work/swapped-agent"
chmod +x "$work/swapped-agent"
printf '#!/usr/bin/env bash\nexit 0\n' >"$installed/bin/rigsignal"
chmod +x "$installed/bin/rigsignal"
printf 'fixture engine %s\n' "$commit" >"$installed/lib/rigsignal/engine/install_assets.py"
printf 'fixture adapter %s\n' "$commit" >"$installed/lib/rigsignal/engine/asset_adapters.py"
printf 'ENGINE_VERSION = "0.3.1"\nSOURCE_COMMIT = "%s"\n' "$commit" >"$installed/lib/rigsignal/engine/_version.py"
printf 'rigsignal-release\n' >"$installed/lib/rigsignal/engine/channel"

stage="$work/stage"
linux_stage="$stage/rigsignal-0.3.1-linux-x86_64"
mkdir -p "$linux_stage/engine"
cp "$installed/bin/rigsignal" "$linux_stage/rigsignal"
cp "$installed/bin/rigsignal-agent" "$linux_stage/rigsignal-agent"
cp "$installed/lib/rigsignal/engine/install_assets.py" "$linux_stage/engine/install_assets.py"
cp "$installed/lib/rigsignal/engine/asset_adapters.py" "$linux_stage/engine/asset_adapters.py"
cp "$installed/lib/rigsignal/engine/_version.py" "$linux_stage/engine/_version.py"
tar -C "$stage" -czf "$fixture/assets/rigsignal-0.3.1-linux-x86_64.tar.gz" rigsignal-0.3.1-linux-x86_64
printf '{"bundle_version":"0.3.1"}\n' >"$stage/manifest.json"
tar -C "$stage" -czf "$fixture/assets/rigsignal-assets-0.3.1.tar.gz" manifest.json

# The real release uses a native package for the channel marker.  Build the
# smallest valid .deb-like ar archive needed by Oracle 6 without using dpkg.
python3 - "$installed" "$fixture/assets/rigsignal_0.3.1-1_amd64.deb" <<'PY'
import io, os, sys, tarfile
installed, output = sys.argv[1:]
paths = {
    'usr/bin/rigsignal': 'bin/rigsignal',
    'usr/bin/rigsignal-agent': 'bin/rigsignal-agent',
    'usr/lib/rigsignal/engine/install_assets.py': 'lib/rigsignal/engine/install_assets.py',
    'usr/lib/rigsignal/engine/asset_adapters.py': 'lib/rigsignal/engine/asset_adapters.py',
    'usr/lib/rigsignal/engine/_version.py': 'lib/rigsignal/engine/_version.py',
    'usr/lib/rigsignal/engine/channel': 'lib/rigsignal/engine/channel',
}
payload = io.BytesIO()
with tarfile.open(fileobj=payload, mode='w:gz') as archive:
    for destination, source in paths.items():
        archive.add(os.path.join(installed, source), arcname=destination, recursive=False)
def member(name, data):
    encoded = name.encode().ljust(16, b' ') + b'0'.ljust(12, b' ') + b'0'.ljust(6, b' ') + b'0'.ljust(6, b' ') + b'100644'.ljust(8, b' ') + str(len(data)).encode().ljust(10, b' ') + b'`\n'
    return encoded + data + (b'\n' if len(data) & 1 else b'')
open(output, 'wb').write(b'!<arch>\n' + member('debian-binary/', b'2.0\n') + member('data.tar.gz/', payload.getvalue()))
PY

payloads=(
  rigsignal_0.3.1-1_amd64.deb
  rigsignal-0.3.1-1.x86_64.rpm
  rigsignal-0.3.1-1-x86_64.pkg.tar.zst
  rigsignal-0.3.1-x86_64.msi
  rigsignal-0.3.1-linux-x86_64.tar.gz
  install.sh
  rigsignal-assets-0.3.1.tar.gz
)
for payload in "${payloads[@]}"; do
    if [ ! -e "$fixture/assets/$payload" ]; then printf 'synthetic payload %s\n' "$payload" >"$fixture/assets/$payload"; fi
    digest="$(sha256sum "$fixture/assets/$payload" | awk '{print $1}')"
    printf '%s  %s\n' "$digest" "$payload" >"$fixture/assets/$payload.sha256"
done

python3 - "$fixture/assets" "$tag" "$commit" <<'PY'
import hashlib, json, os, sys
d, tag, commit = sys.argv[1:]
names=sorted(x for x in os.listdir(d) if x != 'release-assets.json')
assets=[]
for name in names:
    p=os.path.join(d,name); b=open(p,'rb').read()
    assets.append({'name':name,'size':len(b),'sha256':hashlib.sha256(b).hexdigest()})
open(os.path.join(d,'release-assets.json'),'w').write(json.dumps({'schema':1,'tag':tag,'source_commit':commit,'assets':assets},separators=(',',':'))+'\n')
release=[]
for number, name in enumerate(sorted(os.listdir(d)), 100):
    p=os.path.join(d,name); b=open(p,'rb').read()
    release.append({'id':number,'name':name,'size':len(b),'digest':'sha256:'+hashlib.sha256(b).hexdigest(),'state':'uploaded'})
open(os.path.join(os.path.dirname(d),'release-assets.json'),'w').write(json.dumps(release,separators=(',',':'))+'\n')
PY
printf '%s\n' "$commit" >"$fixture/tag-commit"
printf '{"tagName":"v0.3.1","isDraft":true,"databaseId":99,"isImmutable":false}\n' >"$fixture/release-view.json"
python3 - "$fixture/assets" "$fixture/attestation-status.json" <<'PY'
import hashlib, json, os, sys
assets, output = sys.argv[1:]
subjects = {name: hashlib.sha256(open(os.path.join(assets, name), 'rb').read()).hexdigest()
            for name in os.listdir(assets)}
json.dump({'oracle2_real_crypto':'s8c-live-validated-deferred',
           'reason':'No genuine GitHub Sigstore bundle and trusted root are available in the build sandbox; fixture mode never substitutes a mock verifier.',
           'subjects':subjects}, open(output, 'w'), sort_keys=True)
PY
printf '{"items":[{"id":"multi-cert","status":"fixed-tested"},{"id":"f6","status":"fixed-tested"},{"id":"chain-driver-count","status":"historical"}]}\n' >"$fixture/a4-evidence.json"
a4_sha="$(sha256sum "$fixture/a4-evidence.json" | awk '{print $1}')"
printf '{"owner_bound":true,"status":"success","repo":"acme/rigsignal","tag":"v0.3.1","target_identity":"fixture-cluster-never-networked"}\n' >"$fixture/owner-snapshot.json"
snapshot_sha="$(sha256sum "$fixture/owner-snapshot.json" | awk '{print $1}')"

cat >"$fixture/bin/rigsignal" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
if [ "${1:-}" != assets ] || [ "${2:-}" != install ]; then
  printf '%s\n' 'Usage: rigsignal assets install [options]' >&2
  exit 2
fi
shift 2
admin_credentials_file=''
noninteractive=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --admin-credentials-file)
      [ "$#" -ge 2 ] && [ -n "$2" ] || { printf '%s\n' 'assets install: missing value for --admin-credentials-file' >&2; exit 2; }
      [ -z "$admin_credentials_file" ] || { printf '%s\n' 'assets install: duplicate --admin-credentials-file' >&2; exit 2; }
      admin_credentials_file=$2
      shift 2
      ;;
    --bundle|--endpoint|--ca-file|--ca-sha256|--kibana-endpoint)
      [ "$#" -ge 2 ] && [ -n "$2" ] || { printf 'assets install: missing value for %s\n' "$1" >&2; exit 2; }
      shift 2
      ;;
    --non-interactive|--noninteractive) noninteractive=1; shift ;;
    *) printf '%s\n' 'Usage: rigsignal assets install [options]' >&2; exit 2 ;;
  esac
done
if [ "$noninteractive" = 1 ] && [ -z "$admin_credentials_file" ]; then
  printf '%s\n' 'assets install: noninteractive input missing (endpoint, CA, Kibana endpoint, or administrator credentials)' >&2
  exit 2
fi
printf '%s\n' dispatched >"${RIGSIGNAL_FIXTURE_INSTALLER_TOUCHED:?}"
printf '%s\n' "$admin_credentials_file" >"${RIGSIGNAL_FIXTURE_INSTALLER_ADMIN_PATH:?}"
case "${RIGSIGNAL_FIXTURE_INSTALLER_RC:?}" in
  0) exit 0 ;;
  2) printf 'install refused: fixture-local\n' >&2; exit 2 ;;
  3) printf 'install refused: fixture-remote\n' >&2; exit 3 ;;
  4) printf 'install failed: fixture-post-mutation\nRIGSIGNAL_FAILURE_SITE=fixture\n' >&2; exit 4 ;;
  *) exit "${RIGSIGNAL_FIXTURE_INSTALLER_RC}" ;;
esac
EOF
chmod +x "$fixture/bin/rigsignal"
mock_admin_credentials="$work/mock-admin-credentials.toml"
printf '[elasticsearch]\nusername = "fixture-admin"\npassword = "fixture-password"\n' >"$mock_admin_credentials"
for forbidden in gh curl wget git ssh; do
    cat >"$fixture/bin/$forbidden" <<EOF
#!/usr/bin/env bash
printf 'NETWORK DISPATCH: $forbidden\\n' >&2
exit 97
EOF
    chmod +x "$fixture/bin/$forbidden"
done

run_window() {
    local evidence=$1
    shift
    PATH="$fixture/bin:$PATH" GH_TOKEN='' RIGSIGNAL_FIXTURE_INSTALLER_TOUCHED="$evidence/installer-touched" RIGSIGNAL_FIXTURE_INSTALLER_ADMIN_PATH="$evidence/installer-admin-path" \
        bash "$tool" --mode fixture --repo acme/rigsignal --tag "$tag" --evidence-dir "$evidence" --fixture-dir "$fixture" \
        --a4-evidence "$fixture/a4-evidence.json" --a4-evidence-sha256 "$a4_sha" \
        --owner-snapshot "$fixture/owner-snapshot.json" --owner-snapshot-sha256 "$snapshot_sha" \
        --launcher-path "$installed/bin/rigsignal" --agent-path "$installed/bin/rigsignal-agent" \
        --engine-path "$installed/lib/rigsignal/engine/install_assets.py" --admin-credentials-file "$mock_admin_credentials" "$@"
}

run_window_without_admin_credentials() {
    local evidence=$1
    shift
    PATH="$fixture/bin:$PATH" GH_TOKEN='' RIGSIGNAL_FIXTURE_INSTALLER_TOUCHED="$evidence/installer-touched" RIGSIGNAL_FIXTURE_INSTALLER_ADMIN_PATH="$evidence/installer-admin-path" \
        bash "$tool" --mode fixture --repo acme/rigsignal --tag "$tag" --evidence-dir "$evidence" --fixture-dir "$fixture" \
        --a4-evidence "$fixture/a4-evidence.json" --a4-evidence-sha256 "$a4_sha" \
        --owner-snapshot "$fixture/owner-snapshot.json" --owner-snapshot-sha256 "$snapshot_sha" \
        --launcher-path "$installed/bin/rigsignal" --agent-path "$installed/bin/rigsignal-agent" \
        --engine-path "$installed/lib/rigsignal/engine/install_assets.py" "$@"
}

assert_success() {
    if ! "$@"; then fail "expected success: $*"; return 1; fi
}
assert_failure() {
    if "$@"; then fail "expected failure: $*"; return 1; fi
}
assert_file_contains() {
    local file=$1 pattern=$2
    if ! grep -F -- "$pattern" "$file" >/dev/null; then fail "missing $pattern in $file"; return 1; fi
}
assert_no_file() {
    if [ -e "$1" ]; then fail "unexpected file: $1"; return 1; fi
}
assert_ledger_transition() {
    local evidence=$1 oracle=$2 state=$3
    assert_file_contains "$evidence/check-ledger.tsv" "$oracle"$'\t'"started"
    assert_file_contains "$evidence/check-ledger.tsv" "$oracle"$'\t'"$state"
    assert_file_contains "$evidence/check-ledger.tsv" $'terminal\tfailed'
}

# Baseline: all available fixture oracles pass; Oracle 2 is explicitly deferred.
native="$installed/bin/rigsignal-agent"
"$native" --hold & native_pid=$!
assert_success run_window "$work/pass" --native-agent-pid "$native_pid"
kill "$native_pid" 2>/dev/null || true
assert_file_contains "$work/pass/check-ledger.tsv" $'provenance-deferred\tpassed'
assert_file_contains "$work/pass/installer-touched" dispatched
assert_file_contains "$work/pass/installer-admin-path" "$mock_admin_credentials"

# Fixture mode intentionally does not require a live credential input up
# front, but its non-interactive mock dispatch still fails closed without it.
assert_failure run_window_without_admin_credentials "$work/fixture-no-admin"
assert_file_contains "$work/fixture-no-admin/installer.stderr" 'assets install: noninteractive input missing (endpoint, CA, Kibana endpoint, or administrator credentials)'
assert_no_file "$work/fixture-no-admin/installer-touched"
assert_ledger_transition "$work/fixture-no-admin" install failed

# The mock mirrors the launcher's non-interactive administrator-credential
# contract, so a missing pass-through cannot hide behind fixture success.
if RIGSIGNAL_FIXTURE_INSTALLER_TOUCHED="$work/mock-missing-admin-touched" RIGSIGNAL_FIXTURE_INSTALLER_ADMIN_PATH="$work/mock-missing-admin-path" RIGSIGNAL_FIXTURE_INSTALLER_RC=0 "$fixture/bin/rigsignal" assets install --bundle "$fixture/assets/rigsignal-assets-0.3.1.tar.gz" --non-interactive >"$work/mock-missing-admin.stdout" 2>"$work/mock-missing-admin.stderr"; then fail 'fixture mock accepted non-interactive install without admin credentials'; return 1; fi
assert_file_contains "$work/mock-missing-admin.stderr" 'assets install: noninteractive input missing (endpoint, CA, Kibana endpoint, or administrator credentials)'
assert_no_file "$work/mock-missing-admin-touched"

# Fixture dispatch is fail-closed: a missing fixture mock cannot fall through
# to a real `rigsignal` found later on PATH.
printf '#!/usr/bin/env bash\nprintf real-binary-ran >"%s"\nexit 0\n' "$work/real-binary-touched" >"$work/real-bin/rigsignal"
chmod +x "$work/real-bin/rigsignal"
mv "$fixture/bin/rigsignal" "$fixture/bin/rigsignal.absent"
PATH="$work/real-bin:$PATH" assert_failure run_window "$work/no-fixture-mock"
assert_no_file "$work/real-binary-touched"
assert_ledger_transition "$work/no-fixture-mock" install failed
mv "$fixture/bin/rigsignal.absent" "$fixture/bin/rigsignal"

# Artifact-verification fault children: credentials stripped, no installer dispatch.
for fault in hash-mismatch bad-sidecar manifest-mismatch manifest-schema manifest-tag-source missing-api-digest api-size-mismatch duplicate-api duplicate-manifest; do
    evidence="$work/fault-$fault"
    assert_failure run_window "$evidence" --fixture-fault "$fault"
    assert_no_file "$evidence/installer-touched"
    case "$fault" in
        manifest-schema|manifest-tag-source) assert_ledger_transition "$evidence" manifest failed ;;
        *) assert_ledger_transition "$evidence" hash failed ;;
    esac
done

# API asset names are a closed basename contract before download.  Both
# traversal spellings are present in the fixture list, yet no download (and in
# particular no evidence-parent write through ../evil) is attempted.
traversal_evidence="$work/fault-unsafe-api-names"
assert_failure run_window "$traversal_evidence" --fixture-fault unsafe-api-names
assert_ledger_transition "$traversal_evidence" release-shape failed
assert_file_contains "$traversal_evidence/release-assets-pre.json" '../evil'
assert_file_contains "$traversal_evidence/release-assets-pre.json" 'foo/bar'
assert_no_file "$traversal_evidence/evil"
assert_no_file "$traversal_evidence/download"
assert_no_file "$traversal_evidence/installer-touched"

# A corrupted release byte reaches both genuinely independent rejectors.
assert_ledger_transition "$work/fault-hash-mismatch" hash failed
assert_ledger_transition "$work/fault-hash-mismatch" provenance-deferred failed

# A mutable draft is re-read after the one allowed fixture installer dispatch.
assert_failure run_window "$work/post-drift" --fixture-fault post-drift
assert_file_contains "$work/post-drift/installer-touched" dispatched
assert_ledger_transition "$work/post-drift" drift-post failed

# Exact installed-byte binding, then the real /proc native-executable leg.
printf 'substitution\n' >>"$installed/bin/rigsignal"
assert_failure run_window "$work/launch-substitution"
assert_ledger_transition "$work/launch-substitution" launch-surface failed
cp "$linux_stage/rigsignal" "$installed/bin/rigsignal"
"$work/swapped-agent" --hold & swapped_pid=$!
assert_failure run_window "$work/proc-substitution" --native-agent-pid "$swapped_pid"
assert_ledger_transition "$work/proc-substitution" proc-binding failed
kill "$swapped_pid" 2>/dev/null || true

# Fixture-local installer exit taxonomy is a separate, permitted test class.
for status in 2 3 4; do
    evidence="$work/installer-$status"
    assert_failure run_window "$evidence" --fixture-installer-rc "$status"
    assert_file_contains "$evidence/installer-touched" dispatched
    assert_ledger_transition "$evidence" install failed
done
assert_file_contains "$work/installer-4/recovery-status" manual_recovery_required
if [ -e "$work/installer-2/recovery-status" ]; then fail 'exit 2 incorrectly requests manual recovery'; return 1; fi
if [ -e "$work/installer-3/recovery-status" ]; then fail 'exit 3 incorrectly requests manual recovery'; return 1; fi

# Owner-bound snapshot and carried-A4 evidence are both hash-bound, fail-closed inputs.
printf '{"items":[{"id":"multi-cert","status":"unknown"},{"id":"f6","status":"fixed-tested"}]}\n' >"$work/bad-a4.json"
bad_a4_sha="$(sha256sum "$work/bad-a4.json" | awk '{print $1}')"
assert_failure run_window "$work/bad-a4" --a4-evidence "$work/bad-a4.json" --a4-evidence-sha256 "$bad_a4_sha"
assert_ledger_transition "$work/bad-a4" a4 failed
printf '{"owner_bound":false,"status":"success","repo":"acme/rigsignal","tag":"v0.3.1","target_identity":"bad"}\n' >"$work/bad-snapshot.json"
bad_snapshot_sha="$(sha256sum "$work/bad-snapshot.json" | awk '{print $1}')"
assert_failure run_window "$work/bad-snapshot" --owner-snapshot "$work/bad-snapshot.json" --owner-snapshot-sha256 "$bad_snapshot_sha"
assert_ledger_transition "$work/bad-snapshot" owner-snapshot failed

# The static ledger owns exit, including an attempted early success exit.
for ledger_case in missing started failed duplicate unknown malformed early-exit; do
    assert_failure run_window "$work/ledger-$ledger_case" --fixture-ledger-case "$ledger_case"
done

# Mode gate: omission and fixture GO fail before any provider/installer dispatch.
if PATH="$fixture/bin:$PATH" bash "$tool" --repo acme/rigsignal --tag "$tag" --evidence-dir "$work/no-mode" 2>/dev/null; then fail 'omitted mode unexpectedly passed'; return 1; fi
assert_failure run_window "$work/fixture-go" --i-have-owner-go
if GH_TOKEN=token PATH="$fixture/bin:$PATH" bash "$tool" --mode live --repo acme/rigsignal --tag "$tag" --evidence-dir "$work/live-no-go" --a4-evidence "$fixture/a4-evidence.json" --a4-evidence-sha256 "$a4_sha" --owner-snapshot "$fixture/owner-snapshot.json" --owner-snapshot-sha256 "$snapshot_sha" 2>/dev/null; then fail 'live without GO unexpectedly passed'; return 1; fi
assert_no_file "$work/live-no-go/installer-touched"

# Live mode fails before provider or installer dispatch when its required
# administrator-credential path is omitted.
if GH_TOKEN=token PATH="$fixture/bin:$PATH" bash "$tool" --mode live --i-have-owner-go --repo acme/rigsignal --tag "$tag" --evidence-dir "$work/live-no-admin" --a4-evidence "$fixture/a4-evidence.json" --a4-evidence-sha256 "$a4_sha" --owner-snapshot "$fixture/owner-snapshot.json" --owner-snapshot-sha256 "$snapshot_sha" --launcher-path "$installed/bin/rigsignal" --agent-path "$installed/bin/rigsignal-agent" --engine-path "$installed/lib/rigsignal/engine/install_assets.py" >"$work/live-no-admin.stdout" 2>"$work/live-no-admin.stderr"; then fail 'live without admin credentials unexpectedly passed'; return 1; fi
assert_file_contains "$work/live-no-admin.stderr" '--admin-credentials-file is required in live mode'
assert_no_file "$work/live-no-admin/installer-touched"

# Source-level safety proof: no publish/mutation command and exact status capture.
if grep -E 'gh[[:space:]]+release[[:space:]]+edit|--draft=false|gh[[:space:]]+api[[:space:]].*(POST|PATCH|DELETE)' "$tool" >/dev/null; then fail 'publish or release mutation found in source'; return 1; fi
assert_file_contains "$tool" 'if "$launcher_path" assets install --bundle'
assert_file_contains "$tool" 'if "$fixture_launcher" assets install --bundle'
assert_file_contains "$tool" '--admin-credentials-file "$admin_credentials_file"'
assert_file_contains "$tool" 'then rc=0; else rc=$?; fi'
if grep -F 'tee' "$tool" >/dev/null; then fail 'installer output is piped through tee'; return 1; fi

printf '%s\n' 'verify-release-window fixture tests: PASS (Oracle 2 real crypto is explicitly S8c-deferred)'
