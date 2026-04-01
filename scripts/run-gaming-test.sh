#!/usr/bin/env bash
# GamePulse gaming session test runner
#
# Usage:
#   ES_URL=https://... ES_API_KEY=... bash scripts/run-gaming-test.sh
#
# What this does:
#   1. Checks ES credentials are set
#   2. Deploys the collector to the gaming PC
#   3. Starts the collector in the background on gamingpc
#   4. Prompts you to launch a Steam game
#   5. Waits for you to stop (Ctrl+C or Enter)
#   6. Stops the collector and queries ES for doc counts per stream

set -euo pipefail

REMOTE=gamingpc
REMOTE_VENV=/tmp/gp-venv
REMOTE_COLLECTOR=/tmp/gp-collector
REMOTE_PID_FILE=/tmp/gamepulse-collector.pid
REMOTE_LOG=/tmp/gamepulse-collector.log

# ── 1. Check credentials ──────────────────────────────────────────────────────
if [[ -z "${ES_URL:-}" ]]; then
    echo "ERROR: ES_URL is not set."
    echo "  export ES_URL=https://your-deployment.es.region.cloud.es.io"
    exit 1
fi
if [[ -z "${ES_API_KEY:-}" ]]; then
    echo "ERROR: ES_API_KEY is not set."
    echo "  export ES_API_KEY=your-base64-api-key"
    exit 1
fi

echo "==> GamePulse gaming session test"
echo ""

# ── 2. Deploy collector to gaming PC ─────────────────────────────────────────
echo "[1/4] Deploying collector to ${REMOTE}..."
ssh "${REMOTE}" "mkdir -p ${REMOTE_COLLECTOR}"
rsync -a --delete collector/ "${REMOTE}:${REMOTE_COLLECTOR}/collector/" 2>/dev/null \
    || scp -r collector "${REMOTE}:${REMOTE_COLLECTOR}/" > /dev/null

# Ensure venv and httpx are ready
ssh "${REMOTE}" "bash -c '
    if [ ! -f ${REMOTE_VENV}/bin/python3 ]; then
        python3 -m venv ${REMOTE_VENV}
    fi
    ${REMOTE_VENV}/bin/pip install httpx -q 2>/dev/null
'"
echo "    Collector deployed."

# ── 3. Start collector on gaming PC ──────────────────────────────────────────
echo "[2/4] Starting collector on ${REMOTE}..."
ssh "${REMOTE}" "bash -c '
    export ES_URL=\"${ES_URL}\"
    export ES_API_KEY=\"${ES_API_KEY}\"
    export PYTHONPATH=${REMOTE_COLLECTOR}/collector
    nohup ${REMOTE_VENV}/bin/python3 -m gamepulse.cli \
        --debug \
        > ${REMOTE_LOG} 2>&1 &
    echo \$! > ${REMOTE_PID_FILE}
    echo \"    Collector PID: \$(cat ${REMOTE_PID_FILE})\"
'"

echo ""
echo "    Collector is running. Log: ssh ${REMOTE} tail -f ${REMOTE_LOG}"
echo ""

# ── 4. Prompt to start game ───────────────────────────────────────────────────
echo "[3/4] Launch a Steam game now on the gaming PC."
echo "      Let it run for at least 60 seconds for meaningful data."
echo ""
echo "      Press Enter when you want to stop collecting..."
read -r

# ── 5. Stop collector ─────────────────────────────────────────────────────────
echo ""
echo "[4/4] Stopping collector..."
ssh "${REMOTE}" "bash -c '
    if [ -f ${REMOTE_PID_FILE} ]; then
        PID=\$(cat ${REMOTE_PID_FILE})
        kill \"\$PID\" 2>/dev/null && echo \"    Stopped PID \$PID\" || echo \"    Process already gone\"
        rm -f ${REMOTE_PID_FILE}
    else
        echo \"    No PID file found\"
    fi
'"

# ── 6. Query ES for doc counts ────────────────────────────────────────────────
echo ""
echo "==> Data in Elasticsearch (doc counts per stream):"
echo ""

STREAMS=(
    "metrics-gamepulse.gpu-default"
    "metrics-gamepulse.cpu-default"
    "metrics-gamepulse.memory-default"
    "metrics-gamepulse.storage-default"
    "metrics-gamepulse.frame-default"
    "metrics-gamepulse.network-default"
    "metrics-gamepulse.power-default"
    "metrics-gamepulse.audio-default"
    "metrics-gamepulse.session-default"
)

for stream in "${STREAMS[@]}"; do
    count=$(curl -s -X GET "${ES_URL}/${stream}/_count" \
        -H "Authorization: ApiKey ${ES_API_KEY}" \
        -H "Content-Type: application/json" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('count','ERR'))" 2>/dev/null || echo "ERR")
    printf "    %-45s %s docs\n" "${stream}" "${count}"
done

echo ""
echo "==> Done. Open Kibana to explore the session data."
echo "    Log on gaming PC: ssh ${REMOTE} cat ${REMOTE_LOG}"
