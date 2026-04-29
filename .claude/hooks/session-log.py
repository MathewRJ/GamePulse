#!/usr/bin/env python3
"""
session-log.py — PostToolUse logger (Write|Edit).
Appends one line per file-write to /tmp/gp-session-<session_id>.log
so the Stop hook can produce an agent-routing summary.
"""
import json
import os
import sys
from datetime import datetime, timezone


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "unknown")[:8]
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {})

    path = inp.get("file_path", inp.get("path", "?"))
    if not path or path == "?":
        sys.exit(0)

    if tool == "Write":
        content = inp.get("content", "")
        size = f"{content.count(chr(10)) + 1}L"
        action = "write"
    elif tool == "Edit":
        replace_all = inp.get("replace_all", False)
        new = inp.get("new_string", "")
        size = f"+{new.count(chr(10)) + 1}L"
        action = "edit-all" if replace_all else "edit"
    else:
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log_path = f"/tmp/gp-session-{session_id}.log"

    with open(log_path, "a") as f:
        f.write(f"{ts} | sonnet | {action} | {path} | {size}\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
