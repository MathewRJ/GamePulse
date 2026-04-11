#!/usr/bin/env bash
# pre-edit-check.sh
# Blocks edits to GamePulse protected files.
# Receives JSON via stdin with tool_input.file_path

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Normalise to relative path for matching
REL_PATH="${FILE_PATH#./}"

# Protected file patterns
PROTECTED_PATTERNS=(
  "manifest.yml"
  "deploy_pipelines.py"
  "wire_pipelines.py"
  "docs/GamePulse-Scope-v3_2.md"
)

# Protected directory prefixes
PROTECTED_DIRS=(
  "_dev/"
  "packaging/"
)

# Protected file patterns (glob-style substring match)
PROTECTED_GLOBS=(
  "ingest_pipeline"   # matches elasticsearch/ingest_pipeline/ files; _dev/test/pipeline/ is NOT protected
  "index-template"
  "ilm-policy"
)

# Check exact file matches
for pattern in "${PROTECTED_PATTERNS[@]}"; do
  if [[ "$REL_PATH" == *"$pattern"* ]]; then
    echo "BLOCKED: '$REL_PATH' is a protected file in GamePulse." >&2
    echo "Protected files require an explicit planner-assigned task targeting them." >&2
    echo "If this edit is intentional, ask the user to confirm in the chat before proceeding." >&2
    exit 2
  fi
done

# Check directory prefixes
for dir in "${PROTECTED_DIRS[@]}"; do
  if [[ "$REL_PATH" == "$dir"* ]]; then
    echo "BLOCKED: '$REL_PATH' is inside a protected directory ($dir)." >&2
    echo "Edits here require an explicit planner-assigned task." >&2
    exit 2
  fi
done

# Check glob patterns (JSON/NDJSON pipeline/template files)
if [[ "$REL_PATH" == *.json || "$REL_PATH" == *.ndjson ]]; then
  for glob in "${PROTECTED_GLOBS[@]}"; do
    if [[ "$REL_PATH" == *"$glob"* ]]; then
      echo "BLOCKED: '$REL_PATH' matches protected file pattern ('$glob')." >&2
      echo "Pipeline, index template, and ILM policy JSON files are protected." >&2
      echo "If this edit is intentional, ask the user to confirm in the chat." >&2
      exit 2
    fi
  done
fi

exit 0
