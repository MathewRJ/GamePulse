#!/usr/bin/env python3
"""Regression tests for the release-version consistency guard."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/check-release-version"
FIXTURE = ROOT / "tools/tests/fixtures/release-version-skew"


class ReleaseVersionGuardTests(unittest.TestCase):
    def fixture_copy(self, temporary: str) -> Path:
        repo = Path(temporary) / "release-version-skew"
        shutil.copytree(FIXTURE, repo)
        return repo

    def run_guard(self, repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), "--repo-root", str(repo), *arguments],
            text=True,
            capture_output=True,
        )

    def test_skewed_lockfile_fails_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.fixture_copy(temporary)
            lockfile = repo / "Cargo.lock"
            before = lockfile.read_bytes()
            result = self.run_guard(repo)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Cargo.lock local package rigsignal is 0.3.0, expected 0.3.1", result.stderr)
            self.assertEqual(lockfile.read_bytes(), before)

    def test_unexpected_workspace_member_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.fixture_copy(temporary)
            lockfile = repo / "Cargo.lock"
            lockfile.write_text(lockfile.read_text().replace('version = "0.3.0"', 'version = "0.3.1"'))
            (repo / "Cargo.toml").write_text('[workspace]\nmembers = ["src", "extra"]\nresolver = "2"\n')
            extra = repo / "extra"
            (extra / "src").mkdir(parents=True)
            (extra / "Cargo.toml").write_text(
                '[package]\nname = "unexpected"\nversion = "0.3.1"\nedition = "2021"\n'
            )
            (extra / "src/lib.rs").write_text("// Unexpected member.\n")
            result = self.run_guard(repo)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("unexpected root workspace member: extra/Cargo.toml (unexpected)", result.stderr)

    def test_release_tag_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.fixture_copy(temporary)
            lockfile = repo / "Cargo.lock"
            lockfile.write_text(lockfile.read_text().replace('version = "0.3.0"', 'version = "0.3.1"'))
            result = self.run_guard(repo, "--tag", "v0.3.2")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("tag v0.3.2 does not match crate version 0.3.1", result.stderr)

    def test_malformed_release_tags_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.fixture_copy(temporary)
            lockfile = repo / "Cargo.lock"
            lockfile.write_text(lockfile.read_text().replace('version = "0.3.0"', 'version = "0.3.1"'))
            for tag in ("", "vnot-a-version", "v0.3"):
                with self.subTest(tag=tag):
                    result = self.run_guard(repo, "--tag", tag)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("tag must be a nonempty v<semver> string", result.stderr)

    def test_matching_release_tag_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self.fixture_copy(temporary)
            lockfile = repo / "Cargo.lock"
            lockfile.write_text(lockfile.read_text().replace('version = "0.3.0"', 'version = "0.3.1"'))
            result = self.run_guard(repo, "--tag", "v0.3.1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
