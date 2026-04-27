#!/usr/bin/env python3
"""
gpx — GamePulse Agent eXchange.

Cross-platform orchestrator for the multi-agent pipeline across Claude Code,
OpenAI Codex, and Gemini CLI. Runs identically on Linux, macOS (Git Bash),
and Windows (cmd / PowerShell). Stdlib-only — no install step.

All three CLIs use subscription auth (Claude Pro, ChatGPT Plus, Google free
tier). No API keys are required or used. CI is deterministic-only — LLM
gates run locally via this CLI or the optional pre-push hook.

Usage:
  gpx plan                       # ask the planner for the next task
  gpx architect <topic>          # invoke architect agent on a topic
  gpx implement <task-id>        # codex worktree implementer for a task
  gpx review                     # claude code reviewer on current diff
  gpx test                       # run the validation suite
  gpx dashboard <action> [args]  # dashboard-designer workflow
  gpx audit <kind>               # security or integration auditor
  gpx ci                         # full local pre-merge gate
  gpx doctor                     # verify the environment is ready

Environment (all optional — none are API keys):
  ES_URL, ES_API_KEY   used by `gpx dashboard export` if you export from Kibana
  KIBANA_URL           used by `gpx dashboard export`
  GPX_FORCE=1          skip safety gates (logged; emergency override only)

Work packages (for `gpx implement <task-id>`):
  Loaded from `tasks/<task-id>.yaml` if present. Recognised keys:
    goal:                 one sentence — outcome, not steps
    files_in_scope:       [list of relative paths]
    acceptance_criteria:  [list of testable statements]
  Fields are substituted into prompts/codex/implementer.md before the
  prompt is piped to `codex exec` over stdin.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GPX_VERSION = "0.2.0"
PY_MIN = (3, 10)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if platform.system() == "Windows":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"[gpx] {msg}", file=sys.stderr)


def fail(msg: str, code: int = 1):
    print(f"[gpx][error] {msg}", file=sys.stderr)
    sys.exit(code)


def require(tool: str) -> str:
    found = shutil.which(tool)
    if not found:
        fail(f"required tool not found in PATH: {tool}")
    return found


# ---------------------------------------------------------------------------
# Repo and logging setup
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


REPO = repo_root()
LOG_DIR = REPO / ".gpx" / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log_path(prefix: str) -> Path:
    return LOG_DIR / f"{prefix}-{int(time.time())}.log"


# ---------------------------------------------------------------------------
# Subprocess wrappers
# ---------------------------------------------------------------------------

def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    log_to: Path | None = None,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> int:
    """Run a command, stream output, optionally tee to log_to. Returns exit code."""
    log(f"$ {' '.join(argv)}")
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
        finally:
            proc.stdin.close()
    fh = log_to.open("w", encoding="utf-8") if log_to else None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if fh:
                fh.write(line)
    finally:
        if fh:
            fh.close()
    proc.wait()
    if check and proc.returncode != 0:
        fail(f"command failed (exit {proc.returncode}): {' '.join(argv)}")
    return proc.returncode


def capture(argv: list[str], *, cwd: Path | None = None) -> str:
    """Run and return stdout as text. Empty string on failure."""
    try:
        return subprocess.check_output(
            argv, cwd=str(cwd) if cwd else None, text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# Helpers: agent invocation, work-package loader, diff piping
# ---------------------------------------------------------------------------

def call_claude_agent(agent: str, prompt: str, *, cwd: Path | None = None,
                      log_prefix: str = "claude") -> int:
    """Invoke a Claude Code subagent in print mode. Prompt is the positional arg."""
    require("claude")
    return run(
        ["claude", "--agent", agent, "-p", prompt],
        cwd=cwd,
        log_to=log_path(log_prefix),
        check=False,
    )


def current_diff() -> str:
    """Return the diff against origin/main, falling back to staged + unstaged."""
    diff = capture(["git", "diff", "origin/main...HEAD"], cwd=REPO)
    if diff.strip():
        return diff
    return capture(["git", "diff", "HEAD"], cwd=REPO)


def load_work_package(task_id: str) -> dict[str, object]:
    """Load tasks/<task-id>.yaml if present. Tiny YAML subset (top-level scalars
    and bullet lists) — no external dep. Returns empty dict if file missing."""
    path = REPO / "tasks" / f"{task_id}.yaml"
    if not path.is_file():
        return {}
    out: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_list is not None:
            current_list.append(line[4:].strip().strip('"').strip("'"))
            continue
        if line.startswith("- ") and current_list is not None:
            current_list.append(line[2:].strip().strip('"').strip("'"))
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                current_list = []
                out[key] = current_list
                current_key = key
            else:
                out[key] = val.strip('"').strip("'")
                current_list = None
                current_key = key
    return out


def substitute_template(template: str, vars: dict[str, str]) -> str:
    out = template
    for k, v in vars.items():
        out = out.replace("${" + k + "}", v)
    return out


# ---------------------------------------------------------------------------
# Subcommand: plan
# ---------------------------------------------------------------------------

def cmd_plan(_args: argparse.Namespace) -> int:
    log("invoking planner agent")
    return call_claude_agent(
        "planner",
        "Read CLAUDE.md, docs/STATUS.md, and docs/ROADMAP.md. Produce the next task per your output format.",
        log_prefix="plan",
    )


# ---------------------------------------------------------------------------
# Subcommand: architect
# ---------------------------------------------------------------------------

def cmd_architect(args: argparse.Namespace) -> int:
    topic = " ".join(args.topic).strip()
    if not topic:
        fail("usage: gpx architect <topic>")
    log(f"invoking architect on: {topic}")
    return call_claude_agent(
        "architect",
        f"Architect change request: {topic}",
        log_prefix="architect",
    )


# ---------------------------------------------------------------------------
# Subcommand: implement (Codex in worktree)
# ---------------------------------------------------------------------------

def cmd_implement(args: argparse.Namespace) -> int:
    require("codex")
    require("git")

    task_id = args.task_id
    template_path = REPO / "prompts" / "codex" / "implementer.md"
    if not template_path.is_file():
        fail(f"missing prompt template: {template_path}")
    template = template_path.read_text(encoding="utf-8")

    pkg = load_work_package(task_id)
    if not pkg:
        log(f"no work package at tasks/{task_id}.yaml — using minimal substitution")
    goal = str(pkg.get("goal", "(goal not provided — refer to claude.ai planning notes)"))
    files = pkg.get("files_in_scope") or []
    ac = pkg.get("acceptance_criteria") or []
    files_block = "\n".join(f"- {p}" for p in files) if isinstance(files, list) and files else "(none specified)"
    ac_block = "\n".join(f"- {c}" for c in ac) if isinstance(ac, list) and ac else "(none specified)"

    prompt = substitute_template(template, {
        "TASK_ID": task_id,
        "TASK_GOAL": goal,
        "TASK_FILES": files_block,
        "TASK_AC": ac_block,
    })

    worktree = REPO / "worktrees" / f"codex-{task_id}"
    branch = f"codex/{task_id}"
    if not worktree.is_dir():
        log(f"creating worktree {worktree} on branch {branch}")
        run(["git", "worktree", "add", str(worktree), "-b", branch], cwd=REPO)
    else:
        log(f"reusing existing worktree {worktree}")

    log(f"running codex against {worktree} (prompt piped via stdin)")
    rc = run(
        ["codex", "exec", "-"],
        cwd=worktree,
        log_to=log_path(f"codex-{task_id}"),
        check=False,
        stdin_text=prompt,
    )
    log("diff (cumulative on the worktree branch):")
    run(["git", "diff", "--stat"], cwd=worktree, check=False)
    log("next: gpx review")
    return rc


# ---------------------------------------------------------------------------
# Subcommand: review (Claude Code reviewer with diff piped in prompt)
# ---------------------------------------------------------------------------

def cmd_review(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve() if args.target else REPO
    diff = current_diff()
    if not diff.strip():
        log("no diff vs origin/main — nothing to review")
        return 0
    prompt = (
        "Review the diff below per your reviewer agent contract. "
        "Reply with APPROVE / APPROVE WITH NOTES / REJECT.\n\n"
        "<diff>\n" + diff + "\n</diff>"
    )
    log(f"invoking reviewer on {target} (diff size: {len(diff)} bytes)")
    return call_claude_agent("reviewer", prompt, cwd=target, log_prefix="review")


# ---------------------------------------------------------------------------
# Subcommand: test
# ---------------------------------------------------------------------------

VALIDATION_CHECKS: list[list[str]] = [
    ["cargo", "check"],
    ["cargo", "clippy", "--", "-D", "warnings"],
    ["cargo", "test"],
    ["elastic-package", "check"],
    ["elastic-package", "test", "static"],
    ["elastic-package", "test", "asset"],
    ["elastic-package", "test", "pipeline"],
]


def cmd_test(_args: argparse.Namespace) -> int:
    force = os.environ.get("GPX_FORCE") == "1"
    failed: list[str] = []
    for check in VALIDATION_CHECKS:
        if shutil.which(check[0]) is None:
            log(f"skip (not installed): {' '.join(check)}")
            continue
        log(f"running: {' '.join(check)}")
        rc = run(check, check=False)
        if rc != 0:
            failed.append(" ".join(check))
            if not force:
                fail(f"check failed: {' '.join(check)}")
            log("FAILED but GPX_FORCE=1, continuing")
    if failed:
        log(f"completed with failures: {failed}")
        return 1
    log("all checks passed")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: dashboard
# ---------------------------------------------------------------------------

def cmd_dashboard(args: argparse.Namespace) -> int:
    action = args.action

    if action == "export":
        if not args.name:
            fail("usage: gpx dashboard export <dashboard-id>")
        kibana_url = os.environ.get("KIBANA_URL")
        es_api_key = os.environ.get("ES_API_KEY")
        if not kibana_url:
            fail("KIBANA_URL not set")
        if not es_api_key:
            fail("ES_API_KEY not set")

        url = f"{kibana_url.rstrip('/')}/api/saved_objects/_export"
        body = json.dumps({
            "type": "dashboard",
            "objects": [{"id": args.name}],
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"ApiKey {es_api_key}",
                "kbn-xsrf": "true",
                "Content-Type": "application/json",
            },
        )
        out = REPO / "kibana" / "dashboard" / f"{args.name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        log(f"exporting {args.name} from {kibana_url}")
        with urllib.request.urlopen(req) as resp:
            out.write_bytes(resp.read())

        log("stripping instance tokens")
        strip = REPO / "tools" / "strip_dashboard_tokens.py"
        if strip.is_file():
            run([sys.executable, str(strip), str(out)])
        else:
            log(f"warning: {strip} not present; skipping token strip")
        log(f"exported and cleaned: {out}")
        return 0

    if action == "validate":
        return call_claude_agent(
            "dashboard-designer",
            "Validate every NDJSON in kibana/dashboard/ for submission readiness.",
            log_prefix="dashboard-validate",
        )

    if action == "new":
        descr = " ".join(args.rest or []).strip()
        if not descr:
            fail("usage: gpx dashboard new <description>")
        return call_claude_agent(
            "dashboard-designer",
            f"Propose a new panel: {descr}",
            log_prefix="dashboard-new",
        )

    fail(f"unknown dashboard action: {action}")


# ---------------------------------------------------------------------------
# Subcommand: audit
# ---------------------------------------------------------------------------

def cmd_audit(args: argparse.Namespace) -> int:
    if args.kind == "security":
        diff = current_diff()
        prompt = "Audit this diff for credential leakage, PII, and supply-chain risk per your contract. ultrathink."
        if diff.strip():
            prompt += "\n\n<diff>\n" + diff + "\n</diff>"
        log("invoking security-auditor (Opus, ultrathink)")
        return call_claude_agent("security-auditor", prompt, log_prefix="audit-security")
    if args.kind == "integration":
        log("invoking integration-auditor (Opus, ultrathink) — pre-PR gate")
        return call_claude_agent(
            "integration-auditor",
            "Pre-submission audit for elastic/integrations PR. ultrathink.",
            log_prefix="audit-integration",
        )
    fail(f"unknown audit kind: {args.kind}")


# ---------------------------------------------------------------------------
# Subcommand: ci  (review → test → audit security)
# ---------------------------------------------------------------------------

def cmd_ci(_args: argparse.Namespace) -> int:
    log("running full local CI gate: review → test → audit")
    rc = cmd_review(argparse.Namespace(target=None))
    if rc != 0:
        log("reviewer step failed")
        return rc
    rc = cmd_test(argparse.Namespace())
    if rc != 0:
        return rc
    rc = cmd_audit(argparse.Namespace(kind="security"))
    if rc != 0:
        return rc
    log("local CI gate passed")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: doctor
# ---------------------------------------------------------------------------

REQUIRED_TOOLS = ["claude", "codex", "gemini", "cargo", "elastic-package",
                  "gh", "git", "jq"]
OPTIONAL_ENV = ["ES_URL", "ES_API_KEY", "KIBANA_URL"]
AGENTS = [
    "planner", "implementer", "reviewer", "tester", "progress-auditor",
    "architect", "dashboard-designer", "devops", "security-auditor",
    "integration-auditor", "docs-writer",
]


def check_login(tool: str) -> str:
    """Return a short status string for a CLI's auth state."""
    if tool == "claude":
        out = capture(["claude", "--help"])
        return "binary present (auth via Pro subscription / OAuth keychain)" if out else "no response"
    if tool == "codex":
        # `codex login status` writes its result to stderr — merge streams.
        try:
            res = subprocess.run(
                ["codex", "login", "status"],
                capture_output=True, text=True, timeout=10,
            )
            out = (res.stdout + res.stderr).strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            out = ""
        first = out.splitlines()[0] if out else ""
        if "logged in" in first.lower() or "signed in" in first.lower():
            return first
        return first or "status unknown — run `codex login`"
    if tool == "gemini":
        out = capture(["gemini", "--help"])
        return "binary present (auth via Google account)" if out else "no response"
    return "n/a"


def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True
    print(f"  python              {sys.version.split()[0]} ({platform.system()} {platform.release()})")
    if sys.version_info < PY_MIN:
        print(f"  python              REQUIRES {PY_MIN[0]}.{PY_MIN[1]}+")
        ok = False

    for tool in REQUIRED_TOOLS:
        path = shutil.which(tool)
        if path:
            print(f"  {tool:<20s}OK ({path})")
        else:
            print(f"  {tool:<20s}MISSING")
            ok = False

    print("  --- LLM CLI auth status ---")
    for tool in ("claude", "codex", "gemini"):
        if shutil.which(tool):
            print(f"  {tool:<20s}{check_login(tool)}")

    print("  --- optional env (only needed for `gpx dashboard export`) ---")
    for var in OPTIONAL_ENV:
        state = "set" if os.environ.get(var) else "unset"
        print(f"  {var:<20s}{state}")

    print("  --- agent files ---")
    agents_dir = REPO / ".claude" / "agents"
    for agent in AGENTS:
        if (agents_dir / f"{agent}.md").is_file():
            print(f"  agent {agent:<22s}OK")
        else:
            print(f"  agent {agent:<22s}MISSING")
            ok = False

    if not ok:
        fail("doctor reports problems")
    log("all good")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpx", description="GamePulse Agent eXchange")
    p.add_argument("-v", "--version", action="version", version=f"gpx {GPX_VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("plan", help="ask the planner for the next task")

    sp_arch = sub.add_parser("architect", help="invoke the architect agent")
    sp_arch.add_argument("topic", nargs="+", help="topic to architect")

    sp_impl = sub.add_parser("implement", help="codex worktree implementer")
    sp_impl.add_argument("task_id", help="short kebab task id, e.g. add-cpu-fields")

    sp_rev = sub.add_parser("review", help="reviewer on current diff vs origin/main")
    sp_rev.add_argument("target", nargs="?", default=None,
                        help="optional path (defaults to repo root)")

    sub.add_parser("test", help="run the validation suite")

    sp_dash = sub.add_parser("dashboard", help="dashboard-designer workflow")
    sp_dash.add_argument("action", choices=["export", "validate", "new"])
    sp_dash.add_argument("name", nargs="?", default=None,
                         help="dashboard id (for export)")
    sp_dash.add_argument("rest", nargs=argparse.REMAINDER,
                         help="description text (for new)")

    sp_audit = sub.add_parser("audit", help="security or integration audit")
    sp_audit.add_argument("kind", choices=["security", "integration"])

    sub.add_parser("ci", help="full local pre-merge gate")
    sub.add_parser("doctor", help="verify environment is ready")
    return p


HANDLERS = {
    "plan": cmd_plan,
    "architect": cmd_architect,
    "implement": cmd_implement,
    "review": cmd_review,
    "test": cmd_test,
    "dashboard": cmd_dashboard,
    "audit": cmd_audit,
    "ci": cmd_ci,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = HANDLERS.get(args.cmd)
    if handler is None:
        fail(f"unknown command: {args.cmd}")
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
