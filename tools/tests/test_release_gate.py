#!/usr/bin/env python3
"""Fail-closed structural checks for the release workflow quality gate."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release.yml"
MANDATORY_JOBS = (
    "packaging-tests",
    "ebpf-workspace",
    "enrollment-tail-root",
    "python-discovery",
    "check",
    "fmt",
    "smoke-test",
    "dashboard-hygiene",
)
REQUIRED_JOB_SNIPPETS = {
    "packaging-tests": (
        "tools/check-release-version --tag",
        "tools/tests/test_gen_release_manifest.py",
        "tools/tests/test_installer.sh",
        "packaging/tests/test-rigsignal-launcher.sh",
        "working-directory: ebpf",
        "cargo test --workspace --locked",
    ),
    "ebpf-workspace": ("working-directory: ebpf", "cargo test --locked"),
    "enrollment-tail-root": ("sudo python3 tools/tests/test_enrollment_tail.py",),
    "python-discovery": (
        "python3 -m unittest discover -s tools/tests",
        "python3 tools/tests/run_shuffled.py --seed 20260826",
        "python3 tools/tests/test_asset_tools.py",
    ),
    "check": (
        "toolchain: 1.98.0",
        "components: clippy",
        "cargo check --manifest-path src/Cargo.toml --locked",
        "cargo clippy --manifest-path src/Cargo.toml --locked --all-targets -- -D warnings",
        "cargo test --manifest-path src/Cargo.toml --locked",
        "cargo check --manifest-path src/Cargo.toml --locked --features ebpf",
        "cargo clippy --manifest-path src/Cargo.toml --locked --all-targets --features ebpf -- -D warnings",
    ),
    "fmt": (
        "toolchain: 1.98.0",
        "components: rustfmt",
        "cargo fmt --manifest-path src/Cargo.toml -- --check",
    ),
    "smoke-test": (
        "toolchain: 1.98.0",
        "cargo build --manifest-path src/Cargo.toml --locked",
        "bash scripts/smoke-test.sh ./target/debug/rigsignal-agent",
    ),
    "dashboard-hygiene": (
        "for dir in kibana/dashboard dashboards dashboards/archive",
        "contains instance token",
    ),
}
JOB_PATTERN = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$")
FIELD_PATTERN = re.compile(r"^    ([A-Za-z0-9_-]+):(?:\s*(.*?))?\s*$")
LIST_ITEM_PATTERN = re.compile(r"^      -\s+([A-Za-z0-9_-]+)\s*(?:#.*)?$")


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_needs(value, following_lines, job_name):
    value = value.strip()
    if value:
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip() for item in value[1:-1].split(",")]
            if not items or any(not re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in items):
                raise AssertionError(f"{job_name}: cannot safely parse inline needs: {value!r}")
            return items
        if re.fullmatch(r"[A-Za-z0-9_-]+", value):
            return [value]
        raise AssertionError(f"{job_name}: cannot safely parse needs: {value!r}")

    items = []
    for line in following_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = LIST_ITEM_PATTERN.match(line)
        if match:
            items.append(match.group(1))
            continue
        if len(line) - len(line.lstrip(" ")) <= 4:
            break
        raise AssertionError(f"{job_name}: cannot safely parse block needs line: {line!r}")
    if not items:
        raise AssertionError(f"{job_name}: empty needs list")
    return items


def parse_jobs():
    text = WORKFLOW.read_text(encoding="utf-8")
    if "\t" in text:
        raise AssertionError("release.yml contains tabs; refusing ambiguous indentation")
    lines = text.splitlines()
    jobs_markers = [index for index, line in enumerate(lines) if line == "jobs:"]
    if len(jobs_markers) != 1:
        raise AssertionError(f"expected exactly one top-level jobs: mapping, found {len(jobs_markers)}")

    starts = []
    for index in range(jobs_markers[0] + 1, len(lines)):
        match = JOB_PATTERN.match(lines[index])
        if match:
            starts.append((index, match.group(1)))
        elif lines[index] and not lines[index].startswith(" ") and not lines[index].startswith("#"):
            raise AssertionError(f"unexpected top-level content after jobs: {lines[index]!r}")
    if not starts:
        raise AssertionError("release.yml has no jobs; refusing a vacuous gate check")

    jobs = {}
    for position, (start, name) in enumerate(starts):
        if name in jobs:
            raise AssertionError(f"duplicate job name: {name}")
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = lines[start + 1 : end]
        fields = {}
        for offset, line in enumerate(block):
            match = FIELD_PATTERN.match(line)
            if not match:
                continue
            key, value = match.group(1), (match.group(2) or "").strip()
            if key in fields:
                raise AssertionError(f"{name}: duplicate top-level field {key}")
            if key == "needs":
                fields[key] = _parse_needs(value, block[offset + 1 :], name)
            elif key in {"if", "continue-on-error"}:
                if not value:
                    raise AssertionError(f"{name}: empty {key} value")
                fields[key] = _unquote(value)
        fields["_block"] = "\n".join(block)
        jobs[name] = fields
    return jobs


def transitive_needs(jobs, job_name):
    closure = set()
    visiting = set()

    def visit(name):
        if name not in jobs:
            raise AssertionError(f"{job_name}: needs references missing job {name!r}")
        if name in visiting:
            raise AssertionError(f"dependency cycle while traversing {job_name}: {name}")
        if name in closure:
            return
        visiting.add(name)
        for dependency in jobs[name].get("needs", []):
            visit(dependency)
        visiting.remove(name)
        closure.add(name)

    for dependency in jobs[job_name].get("needs", []):
        visit(dependency)
    return closure


class ReleaseGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = parse_jobs()

    def test_mandatory_job_list_is_nonempty_and_jobs_exist(self):
        self.assertGreater(
            len(MANDATORY_JOBS),
            0,
            "mandatory-job list is empty; refusing a vacuous release gate check",
        )
        for name in MANDATORY_JOBS:
            self.assertIn(name, self.jobs, f"mandatory job {name!r} is missing")

    def test_mandatory_jobs_retain_their_defining_checks_and_pinned_actions(self):
        self.assertEqual(
            set(REQUIRED_JOB_SNIPPETS),
            set(MANDATORY_JOBS),
            "required-snippet coverage must exactly match the mandatory-job list",
        )
        for name in MANDATORY_JOBS:
            block = self.jobs[name]["_block"]
            snippets = REQUIRED_JOB_SNIPPETS[name]
            self.assertTrue(snippets, f"{name}: no defining snippets; refusing a vacuous job-content check")
            for snippet in snippets:
                self.assertIn(snippet, block, f"{name}: missing defining workflow content {snippet!r}")

            uses = re.findall(r"^\s+-?\s*uses:\s*([^\s#]+)", block, flags=re.MULTILINE)
            self.assertTrue(uses, f"{name}: no actions found; refusing a vacuous pin check")
            for action in uses:
                self.assertRegex(
                    action,
                    r"^[^@]+@[0-9a-f]{40}$",
                    f"{name}: action is not pinned to a full commit SHA: {action}",
                )

    def test_mandatory_jobs_cannot_be_optional_or_skip_tags(self):
        for name in MANDATORY_JOBS:
            job = self.jobs[name]
            value = job.get("continue-on-error", "false").lower()
            self.assertIn(value, {"true", "false"}, f"mandatory job {name!r} has an invalid continue-on-error value")
            self.assertNotEqual(
                value,
                "true",
                f"mandatory job {name!r} has continue-on-error: true",
            )
            self.assertNotIn(
                "if",
                job,
                f"mandatory job {name!r} has a job-level if condition; refusing to assume it includes tag pushes",
            )

    def test_every_publish_side_effect_transitively_needs_every_mandatory_job(self):
        package_jobs = sorted(name for name in self.jobs if name.startswith("package-"))
        self.assertTrue(package_jobs, "no package-* jobs found; refusing a vacuous publication check")
        self.assertIn("release", self.jobs, "publishing job 'release' is missing")
        publishing_jobs = ["release", *package_jobs]
        required = set(MANDATORY_JOBS)
        for name in publishing_jobs:
            closure = transitive_needs(self.jobs, name)
            missing = sorted(required - closure)
            self.assertFalse(
                missing,
                f"publishing job {name!r} can run without mandatory jobs: {', '.join(missing)}",
            )

    def test_publishing_jobs_have_no_failure_bypass(self):
        publishing_jobs = ["release", *(name for name in self.jobs if name.startswith("package-"))]
        for name in publishing_jobs:
            condition = self.jobs[name].get("if", "")
            normalized = re.sub(r"\s+", "", condition.lower())
            bypass_tokens = ("always()", "failure()", "cancelled()", "||")
            self.assertFalse(
                any(token in normalized for token in bypass_tokens),
                f"publishing job {name!r} has a possible failure-bypass condition: {condition!r}",
            )


if __name__ == "__main__":
    unittest.main()
