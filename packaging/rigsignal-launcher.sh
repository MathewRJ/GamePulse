#!/bin/sh
# rigsignal — unified launcher CLI for the RigSignal telemetry agent.
#
# Usage:
#   rigsignal setup              First-run: configure ES endpoint + API key
#   rigsignal start              Start agent (+ eBPF if sudo available)
#   rigsignal stop               Stop both services gracefully
#   rigsignal status             Show service state + last session label
#   rigsignal run %command%      Steam launch option: start → game → stop
#
# Steam integration:
#   In game Properties → Launch Options:  rigsignal run %command%

AGENT_UNIT="rigsignal-agent"
EBPF_UNIT="rigsignal-ebpf"

# User config path — mirrors what the Rust agent's Config::load() searches first.
# The agent also reads /etc/rigsignal/rigsignal.toml (system-wide), but setup
# writes the user config so credentials stay per-user and never world-readable.
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rigsignal"
CONFIG_FILE="$CONFIG_DIR/rigsignal.toml"

# ── Launcher debug log ─────────────────────────────────────────────────────────
# _llog always writes timestamped entries to a persistent file so Gaming Mode
# decisions (invisible at launch time) are readable afterwards in Desktop Mode:
#   cat ~/.local/share/rigsignal/launcher.log
#
# Set RIGSIGNAL_DEBUG=1 in Steam launch options to also capture the full shell
# trace (set -x) in the same file — every branch, every command:
#   RIGSIGNAL_DEBUG=1 rigsignal run %command%

_LOG_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/rigsignal"
_LOG_FILE="$_LOG_DIR/launcher.log"
_llog() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$_LOG_FILE" 2>/dev/null || true; }

mkdir -p "$_LOG_DIR" 2>/dev/null || true
# Rotate log at 1 MB so it doesn't grow unbounded.
if [ -f "$_LOG_FILE" ]; then
    _lsz=$(wc -c < "$_LOG_FILE" 2>/dev/null || echo 0)
    [ "${_lsz:-0}" -gt 1048576 ] && mv "$_LOG_FILE" "${_LOG_FILE}.old" 2>/dev/null || true
fi

if [ "${RIGSIGNAL_DEBUG:-0}" = "1" ]; then
    # Redirect stderr to the log file before set -x so the shell trace lands there.
    # The game process will inherit fd 2 → its stderr also goes to the log in this mode.
    exec 2>>"$_LOG_FILE"
    set -x
fi

# ── Output helpers ─────────────────────────────────────────────────────────────
# Use colors only when stdout is a terminal and not running under systemd.

if [ -t 1 ] && [ -z "$JOURNAL_STREAM" ]; then
    _GRN='\033[0;32m'
    _YLW='\033[0;33m'
    _RED='\033[0;31m'
    _NC='\033[0m'
else
    _GRN='' _YLW='' _RED='' _NC=''
fi

_ok()   { printf "${_GRN}[OK]${_NC}   %s\n" "$*"; }
_warn() { printf "${_YLW}[WARN]${_NC} %s\n" "$*" >&2; }
_err()  { printf "${_RED}[ERR]${_NC}  %s\n" "$*" >&2; }
_info() { printf "       %s\n" "$*"; }
_die()  { _err "$*"; exit 1; }

# ── TOML helpers ───────────────────────────────────────────────────────────────

# Extract value from a  key = "value"  TOML line. Strips surrounding quotes.
# Works for the simple flat values rigsignal.toml uses (no multiline, no escapes).
toml_get() {
    # $1 = key name, $2 = file path
    grep "^$1[[:space:]]*=" "$2" 2>/dev/null \
        | head -1 \
        | sed 's/^[^=]*=[[:space:]]*//' \
        | tr -d '"' \
        | tr -d "'"
}

# ── Connectivity test ──────────────────────────────────────────────────────────

# Returns 0 if the endpoint is reachable (HTTP 2xx or 4xx — anything but timeout/refuse).
# 401 Unauthorized means the endpoint is alive but the key is wrong.
# Uses python3 urllib (always available) with a fallback to curl.
test_connection() {
    endpoint="$1"
    api_key="$2"
    health_url="${endpoint%/}/_cluster/health"

    if command -v python3 >/dev/null 2>&1; then
        code=$(python3 - "$health_url" "$api_key" <<'PYEOF'
import sys, urllib.request, ssl, urllib.error
url, key = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(url, headers={"Authorization": f"ApiKey {key}"})
try:
    with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
PYEOF
)
    elif command -v curl >/dev/null 2>&1; then
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
            -H "Authorization: ApiKey $api_key" \
            -k \
            "$health_url" 2>/dev/null)
    else
        _warn "Neither python3 nor curl found — cannot verify connectivity."
        return 0  # assume reachable, let the agent report errors
    fi

    # Any HTTP response means the endpoint is reachable.
    # 200 = OK, 401 = wrong key (endpoint alive), 410 = API not supported on Serverless (alive).
    # Only connection errors (timeout, DNS failure) → code 0 or empty → unreachable.
    case "$code" in
        ""|0)  return 1 ;;   # no response — timeout or connection refused
        *)     return 0 ;;   # any HTTP status = endpoint is up
    esac
}

# ── Service control ────────────────────────────────────────────────────────────

# Wait up to 10 seconds for a user service to become active.
wait_agent_active() {
    i=0
    while [ "$i" -lt 10 ]; do
        if systemctl --user is-active "$AGENT_UNIT" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# Attempt to start/stop the eBPF system service via sudo.
# Non-fatal: eBPF is optional — agent-only mode still ships all metric streams.
ebpf_start() {
    if sudo -n systemctl start "$EBPF_UNIT" >/dev/null 2>&1; then
        _ok "eBPF daemon started ($EBPF_UNIT)"
        return 0
    elif sudo systemctl start "$EBPF_UNIT" >/dev/null 2>&1; then
        _ok "eBPF daemon started ($EBPF_UNIT)"
        return 0
    else
        _warn "eBPF daemon not started (no sudo/polkit or CAP_BPF unavailable)."
        _info "Scheduler and kernel metrics will be absent. Agent-only mode active."
        return 1
    fi
}

ebpf_stop() {
    sudo -n systemctl stop "$EBPF_UNIT" >/dev/null 2>&1 \
        || sudo systemctl stop "$EBPF_UNIT" >/dev/null 2>&1 \
        || true   # not fatal — may already be stopped or no sudo
}

# ── Agent binary resolution ────────────────────────────────────────────────────

# Look next to this script first — the user-mode installer puts rigsignal and
# rigsignal-agent in the same directory (~/.local/bin/). Gamescope / Gaming Mode
# may not have ~/.local/bin on PATH, so we can't rely on PATH alone.
resolve_agent_bin() {
    _script_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
    if [ -x "$_script_dir/rigsignal-agent" ]; then
        echo "$_script_dir/rigsignal-agent"
    elif command -v rigsignal-agent >/dev/null 2>&1; then
        command -v rigsignal-agent
    else
        echo ""
    fi
}

# ── Subcommand: setup ──────────────────────────────────────────────────────────

cmd_setup() {
    # Check for existing valid config first.
    if [ -f "$CONFIG_FILE" ]; then
        existing_endpoint=$(toml_get "endpoint" "$CONFIG_FILE")
        existing_key=$(toml_get "api_key" "$CONFIG_FILE")

        if [ -n "$existing_endpoint" ] && [ -n "$existing_key" ]; then
            printf "       Config found: %s\n" "$CONFIG_FILE"
            printf "       Endpoint:     %s\n" "$existing_endpoint"
            printf "       Testing connection...\n"
            if test_connection "$existing_endpoint" "$existing_key"; then
                _ok "Already configured. Connection OK."
                return 0
            else
                _warn "Already configured but connection failed."
                printf "       Endpoint: %s\n" "$existing_endpoint"
                printf "       Re-running setup to update credentials.\n\n"
            fi
        fi
    fi

    # Print a brief explanation.
    printf "\n${_GRN}RigSignal Setup${_NC}\n"
    printf "===============\n"
    printf "RigSignal ships gaming metrics to Elasticsearch.\n"
    printf "You need an Elastic Cloud deployment (or self-hosted ES 8.13+).\n\n"
    printf "You will need:\n"
    printf "  - Your Elasticsearch endpoint URL\n"
    printf "  - An API key with write access to metrics-rigsignal.* indices\n\n"

    # Prompt for endpoint.
    printf "Elasticsearch endpoint\n"
    printf "  Example: https://my-deployment.es.us-central1.gcp.elastic.cloud\n"
    printf "  Endpoint: "
    read -r endpoint

    if [ -z "$endpoint" ]; then
        _die "Endpoint cannot be empty."
    fi

    # Strip trailing slash.
    endpoint="${endpoint%/}"

    # Prompt for API key (stty -echo to prevent it appearing in terminal logs).
    printf "API key (input hidden): "
    # Use stty to suppress echo if available (POSIX but may not work in all contexts)
    if stty -echo 2>/dev/null; then
        read -r api_key
        stty echo 2>/dev/null
        printf "\n"
    else
        read -r api_key
    fi

    if [ -z "$api_key" ]; then
        _die "API key cannot be empty."
    fi

    # Test connectivity before writing anything.
    printf "       Testing connection to %s ...\n" "$endpoint"
    if ! test_connection "$endpoint" "$api_key"; then
        _err "Cannot reach Elasticsearch endpoint."
        _info "Check that the URL is correct and the endpoint is reachable."
        _info "If using Elastic Cloud, ensure the deployment is running."
        exit 1
    fi
    _ok "Connection verified."

    # Write config.
    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"   # config dir private — contains credentials

    # Write atomically: write to temp file then move.
    tmp_cfg="${CONFIG_FILE}.tmp.$$"
    cat > "$tmp_cfg" << TOML
# RigSignal configuration — written by 'rigsignal setup'
# Edit this file to change settings. Re-run 'rigsignal setup' to update credentials.
# SECURITY: this file contains your API key. Permissions are set to 600.

[elasticsearch]
endpoint = "$endpoint"
api_key = "$api_key"

[collection]
# All collectors enabled by default.
# Set individual fields to false to disable:
#   cpu = true
#   gpu = true
#   memory = true
#   storage = true
#   network = true
#   frame_timing = true

[session]
# Optional: set a fixed label for all sessions (e.g. "testing-driver-26").
# If unset, labels are auto-generated: "starfield-20260414-143000"
# label = ""
TOML

    chmod 600 "$tmp_cfg"
    mv "$tmp_cfg" "$CONFIG_FILE"

    _ok "Config written to $CONFIG_FILE"

    # If the eBPF daemon is installed, sync credentials to the system config so it
    # can reach Elasticsearch without a separate manual step.
    if [ -f /usr/local/bin/rigsignal-ebpf ] && command -v sudo >/dev/null 2>&1; then
        _steamos_ro_setup=0
        if command -v steamos-readonly >/dev/null 2>&1; then
            sudo steamos-readonly disable 2>/dev/null && _steamos_ro_setup=1
        fi
        sudo mkdir -p /etc/rigsignal
        sudo install -m 600 "$CONFIG_FILE" /etc/rigsignal/rigsignal.toml
        [ "$_steamos_ro_setup" = "1" ] && sudo steamos-readonly enable 2>/dev/null || true
        _ok "eBPF daemon config updated (/etc/rigsignal/rigsignal.toml)"
        # Restart the daemon so it picks up the new credentials immediately.
        if systemctl is-active --quiet rigsignal-ebpf 2>/dev/null; then
            sudo systemctl restart rigsignal-ebpf 2>/dev/null || true
            _ok "eBPF daemon restarted with new credentials"
        fi
    fi

    _info "Run 'rigsignal start' to begin collecting, or add 'rigsignal run %command%' as a Steam launch option."
}

# ── Subcommand: start ──────────────────────────────────────────────────────────

cmd_start() {
    # Start the user agent service.
    if systemctl --user start "$AGENT_UNIT" 2>/dev/null; then
        if wait_agent_active; then
            _ok "Agent started ($AGENT_UNIT)"
        else
            _warn "Agent started but did not become active within 10 s — check journald:"
            _info "  journalctl --user -u $AGENT_UNIT -n 20"
        fi
    else
        _die "Failed to start $AGENT_UNIT. Is it installed? Check: systemctl --user status $AGENT_UNIT"
    fi

    # Try the eBPF system service (optional — degrades gracefully).
    ebpf_start || true
}

# ── Subcommand: stop ───────────────────────────────────────────────────────────

cmd_stop() {
    systemctl --user stop "$AGENT_UNIT" >/dev/null 2>&1 && _ok "Agent stopped" || true
    ebpf_stop && _ok "eBPF daemon stopped" || true
}

# ── Subcommand: status ─────────────────────────────────────────────────────────

cmd_status() {
    # Agent service state — split capture from fallback so the fallback
    # echo doesn't double-up with systemctl's own stdout inside $().
    agent_state=$(systemctl --user is-active "$AGENT_UNIT" 2>/dev/null) || agent_state="inactive"
    ebpf_state=$(systemctl is-active "$EBPF_UNIT" 2>/dev/null) || ebpf_state="inactive"

    printf "\n  Agent  (%s): %s\n" "$AGENT_UNIT" "$agent_state"
    printf "  eBPF   (%s):  %s\n\n" "$EBPF_UNIT" "$ebpf_state"

    # Last session label from journald (if agent was ever run).
    last_label=$(journalctl --user -u "$AGENT_UNIT" -n 50 --no-pager 2>/dev/null \
        | grep -o 'label="[^"]*"' | tail -1 | sed 's/label="//;s/"//')
    last_game=$(journalctl --user -u "$AGENT_UNIT" -n 50 --no-pager 2>/dev/null \
        | grep "Game detected:" | tail -1 \
        | sed 's/.*Game detected: \([^(]*\).*/\1/' | sed 's/[[:space:]]*$//')

    if [ -n "$last_game" ]; then
        printf "  Last game:  %s\n" "$last_game"
    fi
    if [ -n "$last_label" ]; then
        printf "  Last label: %s\n" "$last_label"
    fi

    printf "\n  Logs:   journalctl --user -u %s -f\n" "$AGENT_UNIT"
    printf "  Config: %s\n\n" "$CONFIG_FILE"
}

# ── Subcommand: run ────────────────────────────────────────────────────────────
# Steam launch option form: rigsignal run %command%
# Steam expands %command% to the full game executable + arguments.

cmd_run() {
    if [ $# -eq 0 ]; then
        _die "Usage: rigsignal run <command> [args...]"
    fi

    # Strip leading KEY=VALUE args and export them so users can write:
    #   rigsignal run RIGSIGNAL_LOG=debug %command%
    # rather than needing to place env vars before 'rigsignal' in the launch option.
    while [ $# -gt 0 ]; do
        case "$1" in
            [A-Za-z_]*=*)
                _varname="${1%%=*}"
                case "$_varname" in
                    *[!A-Za-z0-9_]*) break ;;
                    *)
                        _llog "cmd_run: exporting env: $_varname"
                        export "$1"
                        shift
                        continue
                        ;;
                esac
                ;;
        esac
        break
    done

    if [ $# -eq 0 ]; then
        _die "No command remaining after env var extraction."
    fi

    _llog "cmd_run: command=$*"

    # Resolve the agent binary before starting anything.
    AGENT_BIN="$(resolve_agent_bin)"
    _llog "cmd_run: AGENT_BIN='$AGENT_BIN'"
    if [ -z "$AGENT_BIN" ]; then
        _warn "rigsignal-agent not found — launching game without telemetry."
        _info "Re-run the installer or add ~/.local/bin to PATH."
        _llog "cmd_run: agent not found — exec-ing game directly without telemetry"
        exec "$@"
    fi

    # Enable MangoHud frame timing collection via MANGOHUD=1.
    # We export MANGOHUD=1 rather than wrapping with the `mangohud` binary so
    # that the Steam Linux Runtime (pressure-vessel) uses its own bundled MangoHud
    # layer — the host binary cannot inject across the container boundary.
    # autostart_log=1 is written to ~/.config/MangoHud/MangoHud.conf by the
    # installer so it applies unconditionally; no_display=1 hides the overlay.
    # Set RIGSIGNAL_MANGOHUD=0 to opt out entirely.
    # Set RIGSIGNAL_MANGOHUD=display to show the overlay.
    if [ "${RIGSIGNAL_MANGOHUD:-auto}" != "0" ] && [ -z "${MANGOHUD:-}" ]; then
        MANGOHUD=1
        export MANGOHUD
        _llog "cmd_run: MANGOHUD=1 set (SLR bundled MangoHud, autostart_log via conf)"
        if [ "${RIGSIGNAL_MANGOHUD:-auto}" != "display" ]; then
            MANGOHUD_CONFIG="no_display=1"
            export MANGOHUD_CONFIG
            _llog "cmd_run: MANGOHUD_CONFIG=no_display=1 (overlay hidden)"
        else
            _llog "cmd_run: RIGSIGNAL_MANGOHUD=display — overlay visible"
        fi
    fi

    # Start the agent — systemd if reachable, direct binary otherwise.
    # Reset any prior FAILED state so the unit can always be started fresh.
    systemctl --user reset-failed "$AGENT_UNIT" 2>/dev/null || true
    _llog "cmd_run: reset-failed $AGENT_UNIT"

    # Capture the launcher PID before exec replaces this shell with the game.
    # In POSIX sh, $$ in a subshell still refers to the parent shell's PID,
    # so the watcher correctly monitors the game process after exec.
    _gp_pid=$$
    _llog "cmd_run: launcher PID=$_gp_pid (becomes game PID after exec)"

    if systemctl --user start "$AGENT_UNIT" >/dev/null 2>&1; then
        _llog "cmd_run: systemctl --user start $AGENT_UNIT succeeded (service mode)"
        # Double-fork the watcher: the outer subshell exits immediately, making
        # the inner watcher a child of init (PID 1) rather than of the exec'd
        # process. Without this, exec replaces this shell with steam-launch-wrapper
        # which becomes reaper — reaper then waits for the watcher (its child) and
        # the watcher waits for reaper to exit: deadlock, permanent black screen.
        # /proc/<pid> vanishes atomically on process exit — no PID-reuse race.
        (
          (
            while [ -d "/proc/$_gp_pid" ]; do sleep 1; done
            _llog "cmd_run: game PID $_gp_pid exited — stopping $AGENT_UNIT"
            systemctl --user stop "$AGENT_UNIT" --no-block 2>/dev/null || true
          ) &
        ) &
    else
        _llog "cmd_run: systemctl start failed (DBUS absent? unit missing?) — running agent directly"
        "$AGENT_BIN" &
        _agent_pid=$!
        _llog "cmd_run: direct agent started PID=$_agent_pid"
        (
          (
            while [ -d "/proc/$_gp_pid" ]; do sleep 1; done
            _llog "cmd_run: game PID $_gp_pid exited — kill -TERM agent $_agent_pid"
            kill -TERM "$_agent_pid" 2>/dev/null || true
          ) &
        ) &
    fi

    # exec replaces the launcher shell with the game process so Steam/Gamescope
    # tracks the correct PID for cgroup assignment, GPU priority, and display
    # management. Running the game as a subprocess (not exec) puts it in the
    # wrong place in the process tree and breaks Gaming Mode (Gamescope).
    _llog "cmd_run: exec-ing game (this shell becomes the game process): $*"
    exec "$@"
}

# ── Usage ──────────────────────────────────────────────────────────────────────

usage() {
    cat << 'EOF'
RigSignal launcher

Usage:
  rigsignal setup              Configure Elasticsearch endpoint and API key
  rigsignal start              Start agent (+ eBPF if sudo available)
  rigsignal stop               Stop both services gracefully
  rigsignal status             Show service status and last session info
  rigsignal run <cmd> [args]   Start services, run command, stop on exit

Steam integration — set in game Properties → Launch Options:
  rigsignal run %command%

Examples:
  rigsignal setup
  rigsignal start
  rigsignal status
  rigsignal run ./mygame --fullscreen
EOF
}

# ── Dispatch ───────────────────────────────────────────────────────────────────

subcmd="${1:-}"
_llog "launch: $0 $*"
shift 2>/dev/null || true

case "$subcmd" in
    setup)  cmd_setup ;;
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    run)    cmd_run "$@" ;;
    "")     usage; exit 0 ;;
    *)      _err "Unknown subcommand: $subcmd"; echo; usage; exit 1 ;;
esac
