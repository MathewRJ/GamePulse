#!/usr/bin/env python3
"""
fiscal-check.py — PreToolUse advisory hook (Write|Edit).
Never blocks. Injects an additionalContext message when an edit looks
mechanical enough that Codex would be a better (cheaper) choice.
"""
import json
import sys


CODEX_HINT = (
    "FISCAL: This edit looks mechanical — consider routing to Codex instead "
    "(worktree isolation + GPT-5.5, cheaper for bulk/repetitive work). "
    "Reason: {reason}. Proceed here only if judgment/context is needed."
)


def assess_write(inp: dict) -> str | None:
    content = inp.get("content", "")
    path = inp.get("file_path", "")
    lines = content.count("\n") + 1

    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("json", "yaml", "yml", "toml") and lines > 25:
        return f"new config/data file ({lines} lines, .{ext})"
    if ext in ("sh", "bash") and lines > 50:
        return f"new shell script ({lines} lines)"
    if ext in ("py",) and lines > 80:
        return f"new Python file ({lines} lines)"
    if lines > 100:
        return f"large new file ({lines} lines)"
    return None


def assess_edit(inp: dict) -> str | None:
    old = inp.get("old_string", "")
    new = inp.get("new_string", "")
    replace_all = inp.get("replace_all", False)

    if replace_all:
        return "replace_all=True (bulk rename/substitution across file)"

    old_lines = old.count("\n") + 1
    new_lines = new.count("\n") + 1

    # Small anchor, large insertion → templating / scaffolding
    if old_lines <= 2 and new_lines > 15:
        return f"large insertion ({new_lines} lines) at a {old_lines}-line anchor"

    # Large block replacement with repetitive structure
    if new_lines > 30 and old_lines > 10:
        return f"large block replacement ({old_lines}→{new_lines} lines)"

    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {})

    reason = None
    if tool == "Write":
        reason = assess_write(inp)
    elif tool == "Edit":
        reason = assess_edit(inp)

    if reason:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": CODEX_HINT.format(reason=reason),
            }
        }
        print(json.dumps(out))

    sys.exit(0)


if __name__ == "__main__":
    main()
