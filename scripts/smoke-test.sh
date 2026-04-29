#!/usr/bin/env bash
# F.7 — CI smoke test for the GamePulse agent.
# Runs the agent once in dry-run mode and verifies that all required fields
# appear in the output. GPU, audio, power, frame, and eBPF are not checked
# here because they depend on hardware / daemons absent in CI runners.
#
# Usage: bash scripts/smoke-test.sh [binary-path]
# Exit 0 = all required fields present; non-zero = one or more missing.

set -euo pipefail

BINARY="${1:-./target/debug/gamepulse-agent}"

if [[ ! -x "$BINARY" ]]; then
    echo "ERROR: binary not found or not executable: $BINARY" >&2
    exit 1
fi

# Minimal config — no ES credentials needed for --dry-run.
CFG=$(mktemp --suffix=.toml)
trap 'rm -f "$CFG"' EXIT

cat > "$CFG" <<'TOML'
[elasticsearch]
endpoint = "http://localhost:9200"

[collection]
interval_ms = 1000
cpu = true
memory = true
gpu = true
storage = true
network = true
ebpf = false
frame_timing = true
game_detection = true

[privacy]
opt_in_public = false
TOML

echo "Binary : $BINARY"
echo "Config : $CFG (temp)"
echo ""

OUTPUT=$(timeout 30 "$BINARY" --dry-run --log-level debug --config "$CFG" 2>&1)
echo "$OUTPUT"
echo ""

FAIL=0

check() {
    local label="$1"
    local pattern="$2"
    if echo "$OUTPUT" | grep -qF "$pattern"; then
        printf "  PASS  %s\n" "$label"
    else
        printf "  FAIL  %s  (pattern: %s)\n" "$label" "$pattern"
        FAIL=1
    fi
}

echo "=== Field checks ==="

check "cpu.total_utilisation_pct"           '"total_utilisation_pct"'
check "cpu.per_core"                        '"per_core"'
check "cpu.clock_mhz_avg"                  '"clock_mhz_avg"'

check "memory.system_used_mb"              '"system_used_mb"'
check "memory.page_cache_mb"               '"page_cache_mb"'
check "memory.swap_used_mb"                '"swap_used_mb"'

check "storage.read_mbps"                  '"read_mbps"'
check "storage.write_mbps"                 '"write_mbps"'
check "storage.read_iops"                  '"read_iops"'
check "storage.write_iops"                 '"write_iops"'

check "network.bandwidth_utilisation_mbps" '"bandwidth_utilisation_mbps"'
check "network.tx_packets_per_sec"         '"tx_packets_per_sec"'
check "network.rx_packets_per_sec"         '"rx_packets_per_sec"'

check "agent completed"                    "dry-run complete"

echo ""
if [[ $FAIL -eq 0 ]]; then
    echo "=== SMOKE TEST PASS ==="
    exit 0
else
    echo "=== SMOKE TEST FAIL ==="
    exit 1
fi
