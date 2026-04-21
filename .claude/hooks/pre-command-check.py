#!/usr/bin/env python3
# pre-command-check.py
# Only allows approved GamePulse validation and inspection commands.
# Receives JSON via stdin with tool_input.command

import json
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    command = data.get('tool_input', {}).get('command', '')
    if not command:
        sys.exit(0)

    # Strip leading whitespace for matching
    trimmed = command.lstrip()

    # Allowed command prefixes — these are safe to auto-approve.
    # NOTE: This list has no blocking effect. Non-blocked commands fall through
    # to exit 0 regardless, so this allowlist is present-but-dead. Preserved
    # from bash version for parity; do not enforce without deliberate review.
    allowed_prefixes = [
        'cargo check',
        'cargo clippy',
        'cargo test',
        'cargo build',
        'elastic-package check',
        'elastic-package test static',
        'git diff',
        'git status',
        'git log',
        'git show',
        'grep',
        'find',
        'cat ',
        'ls ',
        'echo ',
        'pwd',
        'wc ',
        'head ',
        'tail ',
        'python3 -c',   # used by hooks themselves
    ]

    # Explicitly blocked commands — always deny
    blocked_patterns = [
        'rm -rf',
        'rm -f',
        # 'elastic-package test system'  # unblocked — user confirmed Docker stack availability
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
        'docker run',   # docker inspect/ps is fine; docker run is not
    ]

    # Check blocked patterns first
    for blocked in blocked_patterns:
        if blocked in trimmed:
            print(f"BLOCKED: Command contains disallowed pattern: '{blocked}'", file=sys.stderr)
            print(f"Command was: {trimmed}", file=sys.stderr)
            sys.exit(2)

    # Check allowed prefixes
    for prefix in allowed_prefixes:
        if trimmed.startswith(prefix):
            sys.exit(0)

    # Not explicitly blocked — allow it.
    # Hook exit code contract: 0 = allow, 2 = block, anything else = hook error.
    sys.exit(0)


if __name__ == '__main__':
    main()
