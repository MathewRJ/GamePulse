#!/usr/bin/env python3
# pre-command-check.py
# Block-list hook: rejects explicitly dangerous commands; allows everything else.
# Receives JSON via stdin with tool_input.command.
#
# Design: block-list only (no allow-list). Commands not in the block-list are
# allowed implicitly. The hook intentionally does NOT block-list everything and
# allow a subset — that model is too restrictive for iterative development.

import json
import re
import sys


def _scan_target(command: str) -> str:
    """Return the portion of the command that should be scanned for blocked
    patterns. For 'git commit' the message body is excluded so that commit
    messages mentioning tool names (curl, wget, ssh, …) are not false-positive
    blocked. Handles compound commands (e.g. 'git add X && git commit -m ...')."""
    # Split on shell operators so each segment is examined independently
    segments = re.split(r'\s*(?:&&|\|\||;)\s*', command)
    cleaned = []
    for seg in segments:
        trimmed = seg.lstrip()
        if re.match(r'git\s+commit\b', trimmed):
            # Strip -m / --message content and heredoc bodies — scan only git flags
            trimmed = re.split(r'\s+-m\b|\s+--message\b', trimmed)[0]
        cleaned.append(trimmed)
    return ' '.join(cleaned)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    command = data.get('tool_input', {}).get('command', '')
    if not command:
        sys.exit(0)

    scan = _scan_target(command)

    # Explicitly blocked patterns — match with word boundaries to avoid
    # false positives on substrings (e.g. "apt" inside "adapter").
    blocked_patterns = [
        'rm -rf',
        'rm -f',
        'pip install',
        'pip3 install',
        'cargo install',
        'apt',
        'apt-get',
        'sudo',
        'curl',
        'wget',
        'ssh',
        'scp',
        'docker run',   # docker inspect/ps/build are fine; docker run is not
    ]

    for blocked in blocked_patterns:
        if re.search(rf'\b{re.escape(blocked)}\b', scan):
            print(f"BLOCKED: Command contains disallowed pattern: '{blocked}'", file=sys.stderr)
            print(f"Command was: {command.lstrip()}", file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == '__main__':
    main()
