#!/usr/bin/env python3
# pre-edit-check.py
# Blocks edits to GamePulse protected files.
# Receives JSON via stdin with tool_input.file_path and cwd.
#
# Run with --test to execute the self-test harness.

import json
import sys


def normalize_path(file_path, cwd):
    """Return a repo-relative forward-slash path.

    Order: normalize backslashes → strip cwd prefix if present → strip leading ./
    If file_path is outside cwd, return it as-is after backslash normalization
    so the downstream checks still run without crashing.
    """
    norm = file_path.replace('\\', '/')
    norm_cwd = cwd.replace('\\', '/') if cwd else ''

    if norm_cwd:
        rest = norm[len(norm_cwd):]
        # Guard: only strip when the next char is '/' (or string ends),
        # to avoid matching a sibling directory that shares a prefix.
        if norm.startswith(norm_cwd) and (not rest or rest.startswith('/')):
            norm = rest.lstrip('/')

    if norm.startswith('./'):
        norm = norm[2:]

    return norm


def check_protected(rel_path):
    """Return (blocked: bool, message: str | None)."""

    # Root manifest.yml is protected; data_stream/*/manifest.yml are NOT
    if rel_path == 'manifest.yml':
        return True, (
            "BLOCKED: 'manifest.yml' is the protected root package manifest.\n"
            "Protected files require an explicit planner-assigned task targeting them.\n"
            "If this edit is intentional, ask the user to confirm in the chat before proceeding."
        )

    # Protected file patterns (substring match)
    protected_patterns = [
        'deploy_pipelines.py',
        'wire_pipelines.py',
        'docs/SCOPE.md',
    ]
    for pattern in protected_patterns:
        if pattern in rel_path:
            return True, (
                f"BLOCKED: '{rel_path}' is a protected file in GamePulse.\n"
                "Protected files require an explicit planner-assigned task targeting them.\n"
                "If this edit is intentional, ask the user to confirm in the chat before proceeding."
            )

    # Protected directory prefixes
    protected_dirs = [
        '_dev/',
        'packaging/',
    ]
    for dir_prefix in protected_dirs:
        if rel_path.startswith(dir_prefix):
            return True, (
                f"BLOCKED: '{rel_path}' is inside a protected directory ({dir_prefix}).\n"
                "Edits here require an explicit planner-assigned task."
            )

    # Protected glob patterns (substring match, only for .json/.ndjson files)
    protected_globs = [
        'ingest_pipeline',   # matches elasticsearch/ingest_pipeline/ files
        'index-template',
        'ilm-policy',
    ]
    if rel_path.endswith('.json') or rel_path.endswith('.ndjson'):
        for glob in protected_globs:
            if glob in rel_path:
                return True, (
                    f"BLOCKED: '{rel_path}' matches protected file pattern ('{glob}').\n"
                    "Pipeline, index template, and ILM policy JSON files are protected.\n"
                    "If this edit is intentional, ask the user to confirm in the chat."
                )

    return False, None


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    file_path = data.get('tool_input', {}).get('file_path', '')
    if not file_path:
        sys.exit(0)

    cwd = data.get('cwd', '')
    rel_path = normalize_path(file_path, cwd)

    blocked, message = check_protected(rel_path)
    if blocked:
        print(message, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


def run_tests():
    FAKE_CWD = 'C:\\Users\\matma\\Documents\\coding\\Gamepulse'

    cases = [
        # (description, file_path, cwd, expected_blocked)
        (
            'Absolute Windows path to manifest.yml',
            'C:\\Users\\matma\\Documents\\coding\\Gamepulse\\manifest.yml',
            FAKE_CWD, True,
        ),
        (
            'Absolute Windows path to docs/SCOPE.md',
            'C:\\Users\\matma\\Documents\\coding\\Gamepulse\\docs\\SCOPE.md',
            FAKE_CWD, True,
        ),
        (
            'Absolute Windows path to _dev/foo.yml',
            'C:\\Users\\matma\\Documents\\coding\\Gamepulse\\_dev\\foo.yml',
            FAKE_CWD, True,
        ),
        (
            'Absolute Windows path to packaging/foo.sh',
            'C:\\Users\\matma\\Documents\\coding\\Gamepulse\\packaging\\foo.sh',
            FAKE_CWD, True,
        ),
        (
            'Absolute Windows path to src/main.rs',
            'C:\\Users\\matma\\Documents\\coding\\Gamepulse\\src\\main.rs',
            FAKE_CWD, False,
        ),
        (
            'Relative path "docs/SCOPE.md" (Linux-style, no cwd)',
            'docs/SCOPE.md',
            '', True,
        ),
        (
            'Relative path "manifest.yml" (Linux-style, no cwd)',
            'manifest.yml',
            '', True,
        ),
        (
            'Path with backslashes "docs\\SCOPE.md" (no cwd)',
            'docs\\SCOPE.md',
            '', True,
        ),
        (
            'File outside cwd (unusual case — no crash, checks run on raw path)',
            'C:\\Other\\Project\\file.py',
            FAKE_CWD, False,
        ),
        (
            'Sibling directory sharing cwd prefix (must not strip)',
            'C:\\Users\\matma\\Documents\\coding\\Gamepulse-other\\manifest.yml',
            FAKE_CWD, False,
        ),
    ]

    passed = 0
    failed = 0
    for desc, file_path, cwd, expected_blocked in cases:
        rel_path = normalize_path(file_path, cwd)
        blocked, _ = check_protected(rel_path)
        ok = blocked == expected_blocked
        tag = 'PASS' if ok else 'FAIL'
        passed += ok
        failed += not ok
        got = 'blocked' if blocked else 'allowed'
        exp = 'blocked' if expected_blocked else 'allowed'
        print(f"[{tag}] {desc}")
        if not ok:
            print(f"       expected={exp!r}  got={got!r}  rel_path={rel_path!r}")

    print(f"\n{passed}/{passed + failed} passed", end='')
    print('' if failed == 0 else '  *** FAILURES ***')
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        run_tests()
    else:
        main()
