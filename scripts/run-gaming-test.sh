#!/usr/bin/env bash
# GamePulse gaming session test runner
#
# Usage:
#   ES_URL=https://... ES_API_KEY=... bash scripts/run-gaming-test.sh
#
# Flow:
#   1. Check credentials
#   2. Deploy collector to gamingpc
#   3. Prompt: launch a game, then press Enter
#   4. Run collector interactively over SSH (live output, Ctrl+C to stop)
#   5. Query ES for doc counts per stream

REMOTE=gamingpc
REMOTE_VENV=/tmp/gp-venv
REMOTE_COLLECTOR=/tmp/gp-collector

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

# ── 2. Deploy collector ───────────────────────────────────────────────────────
echo "[1/3] Deploying collector to ${REMOTE}..."
ssh "${REMOTE}" "mkdir -p ${REMOTE_COLLECTOR}"
rsync -a --delete collector/ "${REMOTE}:${REMOTE_COLLECTOR}/collector/" 2>/dev/null \
    || scp -rq collector "${REMOTE}:${REMOTE_COLLECTOR}/"

ssh "${REMOTE}" "bash -c '
    if [ ! -f ${REMOTE_VENV}/bin/python3 ]; then
        python3 -m venv ${REMOTE_VENV}
    fi
    ${REMOTE_VENV}/bin/pip install httpx -q 2>/dev/null
'"
echo "    Done."
echo ""

# ── 3. Prompt to launch game ──────────────────────────────────────────────────
echo "[2/3] Launch a game on the gaming PC, then press Enter to start collecting."
echo "      (Press Ctrl+C when you're done playing to stop and see the summary.)"
echo ""
read -r

# ── 4. Run collector interactively ───────────────────────────────────────────
echo "[3/3] Collecting — Ctrl+C to stop..."
echo ""

# Trap Ctrl+C so we can still run the ES query after SSH exits
set +e
ssh -t "${REMOTE}" "bash -c '
    export ES_URL=\"${ES_URL}\"
    export ES_API_KEY=\"${ES_API_KEY}\"
    export PYTHONPATH=${REMOTE_COLLECTOR}/collector
    exec ${REMOTE_VENV}/bin/python3 -m gamepulse.cli --debug
'"
set -e

# ── 5. Query ES for doc counts ────────────────────────────────────────────────
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
    count=$(curl -s "${ES_URL}/${stream}/_count" \
        -H "Authorization: ApiKey ${ES_API_KEY}" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('count','ERR'))" 2>/dev/null || echo "ERR")
    printf "    %-45s %s docs\n" "${stream}" "${count}"
done

echo ""
echo "==> Done. Open Kibana to explore the session."
