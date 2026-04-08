#!/usr/bin/env bash
# Emergency save hook — triggered when user message contains "code red".
# Stages all changes, commits, and pushes to origin.
# Also injects context telling Claude to update HANDOFF.md and memory before continuing.

REPO=/home/cachyos/claude/GamePulse

MSG=$(jq -r '.message // ""' 2>/dev/null)
if echo "$MSG" | grep -qi "code red"; then
    cd "$REPO" || exit 0
    git add -A 2>/dev/null
    git commit -m "emergency save [code red] $(date +%Y-%m-%dT%H:%M:%S)" --allow-empty 2>/dev/null
    git push 2>/dev/null
    cat <<'EOF'
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "CODE RED signal received. Git emergency save has been executed (git add -A + commit + push). Before doing anything else you MUST do ALL THREE of the following steps in order:\n1. Update docs/HANDOFF.md — prepend a new session entry with: what was worked on this session, all decisions made and why, dead ends tried, exact commits, current state, and clear next steps. Be detailed — this is the primary continuity doc for future sessions.\n2. Update memory file /home/cachyos/.claude/projects/-home-cachyos-claude-GamePulse/memory/project_state.md with the compressed current state.\n3. Run: cd /home/cachyos/claude/GamePulse && git add docs/HANDOFF.md /home/cachyos/.claude/projects/-home-cachyos-claude-GamePulse/memory/project_state.md && git commit -m 'docs: update HANDOFF.md and memory [code red]' && git push\nOnly after completing all three steps, acknowledge to the user that the save is complete."}}
EOF
fi
