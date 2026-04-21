#!/usr/bin/env python3
# post-edit-check.py
# After any .rs file edit, runs cargo check automatically.
# Receives JSON via stdin.

import json
import subprocess
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    file_path = data.get('tool_input', {}).get('file_path', '')
    if not file_path:
        sys.exit(0)

    # Only fire for Rust source files
    if not file_path.endswith('.rs'):
        sys.exit(0)

    print(f"--- GamePulse post-edit: running cargo check after edit to {file_path} ---")

    # Find repo root via git, same as bash: git rev-parse --show-toplevel
    try:
        git_result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True,
            text=True,
        )
        repo_root = git_result.stdout.strip() if git_result.returncode == 0 else None
    except OSError:
        repo_root = None

    cargo_result = subprocess.run(['cargo', 'check'], cwd=repo_root)

    if cargo_result.returncode != 0:
        print('', file=sys.stderr)
        print(f"cargo check FAILED after editing {file_path}", file=sys.stderr)
        print("Fix compilation errors before proceeding.", file=sys.stderr)
        sys.exit(2)

    print("--- cargo check: OK ---")
    sys.exit(0)


if __name__ == '__main__':
    main()
