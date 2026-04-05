#!/usr/bin/env bash
# post-edit-check.sh
# After any .rs file edit, runs cargo check automatically.
# Receives JSON via stdin.

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Only fire for Rust source files
if [[ "$FILE_PATH" != *.rs ]]; then
  exit 0
fi

echo "--- GamePulse post-edit: running cargo check after edit to $FILE_PATH ---"
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cargo check 2>&1
STATUS=$?

if [ $STATUS -ne 0 ]; then
  echo "" >&2
  echo "cargo check FAILED after editing $FILE_PATH" >&2
  echo "Fix compilation errors before proceeding." >&2
  exit 2
fi

echo "--- cargo check: OK ---"
exit 0
