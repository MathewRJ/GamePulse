#!/usr/bin/env bash
# Emergency save hook — triggered when user message contains "code red".
# Stages all changes, commits, and pushes to origin.
# Also injects context telling Claude to update memory before continuing.

REPO=/home/cachyos/claude/GamePulse

MSG=$(jq -r '.message // ""' 2>/dev/null)
if echo "$MSG" | grep -qi "code red"; then
    cd "$REPO" || exit 0
    git add -A 2>/dev/null
    git commit -m "emergency save [code red] $(date +%Y-%m-%dT%H:%M:%S)" --allow-empty 2>/dev/null
    git push 2>/dev/null
    cat <<'EOF'
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "CODE RED signal received. Git emergency save has been executed (git add -A + commit + push). Before doing anything else, immediately update your memory files (/home/cachyos/.claude/projects/-home-cachyos-claude-GamePulse/memory/project_state.md) with the full current session state, then acknowledge to the user that the save is complete."}}
EOF
fi
