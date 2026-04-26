#!/usr/bin/env bash
# pre-command-check.sh
# Only allows approved GamePulse validation and inspection commands.
# Receives JSON via stdin with tool_input.command

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Strip leading whitespace for matching
TRIMMED=$(echo "$COMMAND" | sed 's/^[[:space:]]*//')

# Allowed command prefixes — these are safe to auto-approve
ALLOWED_PREFIXES=(
  "cargo check"
  "cargo clippy"
  "cargo test"
  "cargo build"
  "elastic-package check"
  "elastic-package test static"
  "git diff"
  "git status"
  "git log"
  "git show"
  "grep"
  "find"
  "cat "
  "ls "
  "echo "
  "pwd"
  "wc "
  "head "
  "tail "
  "python3 -c"   # used by hooks themselves
)

# Explicitly blocked commands — always deny
BLOCKED_PATTERNS=(
  "rm -rf"
  "rm -f"
  # "elastic-package test system"  # unblocked — user confirmed Docker stack availability
  "pip install"
  "pip3 install"
  "cargo install"
  "apt"
  "apt-get"
  "sudo"
  "curl"
  "wget"
  "ssh"
  "scp"
  "docker run"   # docker inspect/ps is fine; docker run is not
)

# Check blocked patterns first. Use word-boundary regex so that short tokens
# like "apt" do not false-positive on "adapter"/"capture" inside commit
# messages (mirrors the Python port's \bapt\b fix).
for blocked in "${BLOCKED_PATTERNS[@]}"; do
  if [[ "$TRIMMED" =~ (^|[^[:alnum:]_])${blocked}($|[^[:alnum:]_]) ]]; then
    echo "BLOCKED: Command contains disallowed pattern: '$blocked'" >&2
    echo "Command was: $TRIMMED" >&2
    true
    exit 2
  fi
done

# Check allowed prefixes
for prefix in "${ALLOWED_PREFIXES[@]}"; do
  if [[ "$TRIMMED" == "$prefix"* ]]; then
    exit 0
  fi
done

# Not explicitly blocked — allow it.
# Hook exit code contract: 0 = allow, 2 = block, anything else = hook error.
exit 0
