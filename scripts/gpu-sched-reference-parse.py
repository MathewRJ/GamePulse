#!/usr/bin/env python3
"""Pair legacy gpu_scheduler ftrace events and print daemon-compatible latency buckets."""

import argparse
import re
import sys
from pathlib import Path


BOUNDARIES_US = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)
EVENT_RE = re.compile(
    r"\s(?P<timestamp>\d+\.\d+):\s+(?P<event>drm_sched_job|drm_run_job):.*?\bid=(?P<id>\d+)\b"
)


def parse_latencies(trace: str) -> list[int]:
    """Return paired queue-to-run latencies in nanoseconds.

    This intentionally mirrors the BPF map's last-write-wins behavior for duplicate
    IDs, which is the statistical collision behavior of the daemon's legacy probe.
    """
    queued: dict[int, int] = {}
    latencies: list[int] = []
    for line in trace.splitlines():
        match = EVENT_RE.search(line)
        if match is None:
            continue
        timestamp_ns = int(float(match.group("timestamp")) * 1_000_000_000)
        job_id = int(match.group("id"))
        if match.group("event") == "drm_sched_job":
            queued[job_id] = timestamp_ns
        elif (queue_ns := queued.pop(job_id, None)) is not None and timestamp_ns >= queue_ns:
            latencies.append(timestamp_ns - queue_ns)
    return latencies


def histogram(latencies_ns: list[int]) -> list[int]:
    counts = [0] * len(BOUNDARIES_US)
    for latency_ns in latencies_ns:
        latency_us = latency_ns // 1_000
        for index, boundary in enumerate(BOUNDARIES_US):
            if latency_us < boundary:
                counts[index] += 1
                break
        else:
            counts[-1] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="trace_pipe capture from gpu-sched-ftrace-reference.sh")
    args = parser.parse_args()

    try:
        latencies = parse_latencies(args.trace.read_text())
    except OSError as error:
        print(f"cannot read {args.trace}: {error}", file=sys.stderr)
        return 2

    print(f"count={len(latencies)}")
    if latencies:
        latency_us = [value / 1_000 for value in latencies]
        print(
            "latency_us "
            f"min={min(latency_us):.3f} mean={sum(latency_us) / len(latency_us):.3f} "
            f"max={max(latency_us):.3f}"
        )
    else:
        print("latency_us min=0.000 mean=0.000 max=0.000")

    counts = histogram(latencies)
    print("latency_histogram_values_us=" + ",".join(map(str, BOUNDARIES_US)))
    print("latency_histogram_counts=" + ",".join(map(str, counts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
