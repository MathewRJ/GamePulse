#!/usr/bin/env python3
"""Keep every workflow Rust toolchain input aligned with the root pin."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLCHAIN_FILE = ROOT / "rust-toolchain.toml"
WORKFLOWS = (
    ROOT / ".github/workflows/ci.yml",
    ROOT / ".github/workflows/release.yml",
)
CHANNEL_PATTERN = re.compile(r'^\s*channel\s*=\s*["\']([^"\']+)["\']\s*(?:#.*)?$')
TOOLCHAIN_PATTERN = re.compile(
    r"^\s*toolchain\s*:\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s#]+))\s*(?:#.*)?$"
)


def pinned_channel() -> str:
    for line in TOOLCHAIN_FILE.read_text(encoding="utf-8").splitlines():
        match = CHANNEL_PATTERN.match(line)
        if match:
            return match.group(1)
    raise AssertionError(f"{TOOLCHAIN_FILE.relative_to(ROOT)}: missing [toolchain] channel")


def workflow_toolchains():
    declarations = []
    for workflow in WORKFLOWS:
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            match = TOOLCHAIN_PATTERN.match(line)
            if match:
                toolchain = next(value for value in match.groups() if value is not None)
                declarations.append((workflow.relative_to(ROOT), line_number, toolchain))
    return declarations


class ToolchainPinTests(unittest.TestCase):
    def test_workflow_toolchains_match_root_pin(self):
        channel = pinned_channel()
        declarations = workflow_toolchains()

        declarations_by_workflow = {workflow.relative_to(ROOT): [] for workflow in WORKFLOWS}
        for path, line, toolchain in declarations:
            declarations_by_workflow[path].append((line, toolchain))

        for path, workflow_declarations in declarations_by_workflow.items():
            self.assertTrue(
                workflow_declarations,
                f"no workflow toolchain declarations found in {path}; refusing a vacuous drift check",
            )

        self.assertTrue(
            declarations,
            "no workflow toolchain declarations found in .github/workflows/ci.yml or "
            ".github/workflows/release.yml; refusing a vacuous drift check",
        )

        mismatches = [
            f"{path}:{line}: toolchain {toolchain!r} does not match rust-toolchain.toml channel {channel!r}"
            for path, line, toolchain in declarations
            if toolchain != channel
        ]
        self.assertFalse(mismatches, "\n".join(mismatches))


if __name__ == "__main__":
    unittest.main()
