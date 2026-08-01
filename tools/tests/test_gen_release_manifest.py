#!/usr/bin/env python3
"""Fail-closed contract tests for tools/gen-release-manifest."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools" / "gen-release-manifest"
TAG = "v0.3.1"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def load_generator():
    loader = importlib.machinery.SourceFileLoader("gen_release_manifest", str(GENERATOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


GEN = load_generator()


class ReleaseManifestTest(unittest.TestCase):
    def make_dist(self, directory: Path, *, self_sidecar: bool = False) -> list[str]:
        names = GEN.payload_names(TAG[1:])
        for index, name in enumerate(names):
            path = directory / name
            path.write_bytes(f"payload {index}\n".encode())
            (directory / f"{name}.sha256").write_text(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n", encoding="utf-8"
            )
        (directory / "release-assets.json").write_text("old root is excluded\n", encoding="utf-8")
        if self_sidecar:
            (directory / "release-assets.json.sha256").write_text("self sidecar is excluded\n", encoding="utf-8")
        return names

    def run_generator(
        self, directory: Path, *, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GENERATOR), "--dist", str(directory), "--tag", TAG, "--source-commit", COMMIT],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def test_exact_contract_root_exclusion_and_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            payloads = self.make_dist(dist, self_sidecar=True)
            first = self.run_generator(dist)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_bytes = (dist / "release-assets.json").read_bytes()
            second = self.run_generator(dist)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((dist / "release-assets.json").read_bytes(), first_bytes)

            manifest = json.loads(first_bytes)
            expected = sorted(payloads + [f"{name}.sha256" for name in payloads])
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(manifest["tag"], TAG)
            self.assertEqual(manifest["source_commit"], COMMIT)
            self.assertEqual([asset["name"] for asset in manifest["assets"]], expected)
            self.assertNotIn("release-assets.json", [asset["name"] for asset in manifest["assets"]])
            self.assertNotIn("release-assets.json.sha256", [asset["name"] for asset in manifest["assets"]])
            for asset in manifest["assets"]:
                data = (dist / asset["name"]).read_bytes()
                self.assertEqual(asset["size"], len(data))
                self.assertEqual(asset["sha256"], hashlib.sha256(data).hexdigest())

    def test_fails_closed_on_missing_and_extra_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            payloads = self.make_dist(dist)
            (dist / payloads[0]).unlink()
            result = self.run_generator(dist)
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing", result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            self.make_dist(dist)
            (dist / "injected.bin").write_bytes(b"unexpected")
            result = self.run_generator(dist)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unexpected", result.stderr)

    def test_fails_closed_on_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            dist.mkdir()
            self.make_dist(dist)
            (root / "sitecustomize.py").write_text(
                """import os
import pathlib

_original_iterdir = pathlib.Path.iterdir
_target = pathlib.Path(os.environ[\"GEN_RELEASE_MANIFEST_DUPLICATE_DIR\"]).resolve()

def _duplicate_iterdir(path):
    entries = list(_original_iterdir(path))
    if path.resolve() == _target:
        return iter(entries + [entries[0]])
    return iter(entries)

pathlib.Path.iterdir = _duplicate_iterdir
""",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "GEN_RELEASE_MANIFEST_DUPLICATE_DIR": str(dist),
                "PYTHONPATH": os.pathsep.join(filter(None, (str(root), os.environ.get("PYTHONPATH")))),
            }
            result = self.run_generator(dist, environment=environment)
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate dist entries", result.stderr)


if __name__ == "__main__":
    unittest.main()
