#!/usr/bin/env python3
"""
session-summary.py — Stop hook.
Reads the session log and emits a systemMessage summarising which agent
touched which files this turn. Silent if nothing was logged.
"""
import json
import os
import sys
from pathlib import Path


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "unknown")[:8]
    log_path = Path(f"/tmp/gp-session-{session_id}.log")

    if not log_path.exists():
        sys.exit(0)

    lines = log_path.read_text().splitlines()
    if not lines:
        sys.exit(0)

    # Only show lines written since the last summary marker
    marker = "--- turn ---"
    try:
        last_marker = max(i for i, l in enumerate(lines) if l == marker)
        turn_lines = lines[last_marker + 1:]
    except ValueError:
        turn_lines = lines

    # Append turn marker for next time
    with open(log_path, "a") as f:
        f.write(f"{marker}\n")

    if not turn_lines:
        sys.exit(0)

    # Group by agent
    by_agent: dict[str, list[str]] = {}
    for line in turn_lines:
        parts = line.split(" | ")
        if len(parts) < 5:
            continue
        ts, agent, action, path, size = parts[0], parts[1], parts[2], parts[3], parts[4]
        label = f"{action}({size}) {path}"
        by_agent.setdefault(agent, []).append(label)

    if not by_agent:
        sys.exit(0)

    parts = []
    for agent, entries in by_agent.items():
        parts.append(f"{agent.upper()}: {', '.join(entries)}")

    msg = "AGENT LOG — " + " | ".join(parts)
    print(json.dumps({"systemMessage": msg}))
    sys.exit(0)


if __name__ == "__main__":
    main()
