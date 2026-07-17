#!/usr/bin/env bash
# Capture a bounded legacy gpu_scheduler reference trace. Run as root.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <seconds> <output-file>" >&2
    exit 1
fi

seconds=$1
output_file=$2
tracefs=/sys/kernel/tracing
events_dir=$tracefs/events/gpu_scheduler
queue_enable=$events_dir/drm_sched_job/enable
run_enable=$events_dir/drm_run_job/enable

if ! [[ $seconds =~ ^[1-9][0-9]*$ ]]; then
    echo "seconds must be a positive integer" >&2
    exit 1
fi
for path in "$tracefs/trace_pipe" "$queue_enable" "$run_enable"; do
    if [[ ! -e $path ]]; then
        echo "required tracefs path is unavailable: $path" >&2
        exit 1
    fi
done

queue_was_enabled=$(<"$queue_enable")
run_was_enabled=$(<"$run_enable")
capture_pid=

cleanup() {
    if [[ -n ${capture_pid} ]] && kill -0 "$capture_pid" 2>/dev/null; then
        kill "$capture_pid" 2>/dev/null || true
        wait "$capture_pid" 2>/dev/null || true
    fi
    printf '%s\n' "$queue_was_enabled" >"$queue_enable"
    printf '%s\n' "$run_was_enabled" >"$run_enable"
}
trap cleanup EXIT INT TERM

: >"$output_file"
printf '1\n' >"$queue_enable"
printf '1\n' >"$run_enable"
cat "$tracefs/trace_pipe" >"$output_file" &
capture_pid=$!

sleep "$seconds"
kill "$capture_pid"
wait "$capture_pid" 2>/dev/null || true
capture_pid=

echo "captured legacy gpu_scheduler reference trace to $output_file" >&2
