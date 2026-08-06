import importlib.util
import io
import json
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = b"delivery proof test\n"
SPEC = importlib.util.spec_from_file_location(
    "rigsignal_spool_retention", ROOT / "packaging/rigsignal-spool-retention.py"
)
RETENTION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RETENTION)


class SpoolRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spool = self.root / "spool"
        self.spool.mkdir()
        self.registry = self.root / "log.json"
        self.agent = self.root / "elastic-agent"
        self.agent.write_text("#!/bin/sh\nprintf 'status: (HEALTHY)\\n'\n")
        self.agent.chmod(0o755)
        self.patches = [
            patch.object(RETENTION, "configured_spool", return_value=self.spool),
            patch.object(RETENTION, "REGISTRY_GLOB", str(self.registry)),
            patch.dict(os.environ, {"RIGSIGNAL_ELASTIC_AGENT": str(self.agent)}),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp.cleanup()

    def final(self, name, age_seconds=49 * 3600):
        path = self.spool / name
        path.write_bytes(PAYLOAD)
        old = time.time() - age_seconds
        os.utime(path, (old, old))
        return path

    def write_registry(self, entries):
        self.registry.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    def cursor_entry(self, path, offset=None):
        if offset is None:
            offset = path.stat().st_size
        return {
            "k": f"state::{path.name}",
            "v": {"meta": {"source": str(path)}, "cursor": {"offset": offset}},
        }

    def run_helper(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(RETENTION.main(), 0)
        return output.getvalue()

    def test_env_discovered_healthy_agent_deletes_harvested_final(self):
        final = self.final("rigsignal-harvested.ndjson")
        self.write_registry([self.cursor_entry(final)])

        output = self.run_helper()

        self.assertFalse(final.exists())
        self.assertIn(f"deleted=1 bytes={len(PAYLOAD)} skipped=0", output)
        self.assertIn(f"registry={self.registry}", output)

    def test_stranded_files_without_a_registry_entry_survive(self):
        harvested = self.final("rigsignal-harvested.ndjson")
        finals = [self.final(f"rigsignal-stranded-{index}.ndjson") for index in range(12)]
        self.write_registry([self.cursor_entry(harvested)])

        output = self.run_helper()
        counts = re.search(r"deleted=(\d+) bytes=(\d+) skipped=(\d+)", output)

        self.assertIsNotNone(counts)
        self.assertEqual(counts.group(1), "1")
        self.assertEqual(counts.group(3), "12")
        self.assertFalse(harvested.exists())
        self.assertTrue(all(path.exists() for path in finals))

    def test_non_rigsignal_ndjson_survives_even_when_harvested(self):
        final = self.final("unrelated-harvested.ndjson")
        self.write_registry([self.cursor_entry(final)])

        self.run_helper()

        self.assertTrue(final.exists())

    def test_behind_cursor_final_survives(self):
        # This glob-matching sentinel meets every deletion condition except a
        # complete harvested cursor; removing that guard deletes it.
        final = self.final("rigsignal-behind-cursor-sentinel.ndjson")
        self.write_registry([self.cursor_entry(final, final.stat().st_size - 1)])

        self.run_helper()

        self.assertTrue(final.exists())

    def test_missing_registry_is_a_successful_skip(self):
        # This old, glob-matching deletion sentinel must survive a missing
        # registry; a fail-open implementation would remove it.
        final = self.final("rigsignal-missing-registry-sentinel.ndjson")

        output = self.run_helper()

        self.assertTrue(final.exists())
        self.assertEqual(output, "skip: no filestream registry\n")

    def test_malformed_registry_causes_zero_deletions(self):
        # This old, glob-matching deletion sentinel must survive malformed
        # registry input; a fail-open implementation would remove it.
        final = self.final("rigsignal-malformed-registry-sentinel.ndjson")
        self.registry.write_text("not json\n")

        output = self.run_helper()

        self.assertTrue(final.exists())
        self.assertIn("skip: retention inputs unavailable:", output)

    def test_blank_registry_lines_do_not_block_harvested_final_deletion(self):
        final = self.final("rigsignal-blank-lines.ndjson")
        self.registry.write_text(
            "\n  \t\n" + json.dumps(self.cursor_entry(final)) + "\n\n"
        )

        self.run_helper()

        self.assertFalse(final.exists())

    def test_unreadable_registry_causes_zero_deletions(self):
        # This glob-matching sentinel has a complete cursor and would be
        # deleted if unreadable registry input were not fail-closed.
        final = self.final("rigsignal-unreadable-registry-sentinel.ndjson")
        self.write_registry([self.cursor_entry(final)])

        with patch.object(RETENTION, "registry_sources", side_effect=PermissionError("denied")):
            output = self.run_helper()

        self.assertTrue(final.exists())
        self.assertIn("skip: retention inputs unavailable: denied", output)

    def test_unhealthy_agent_causes_zero_deletions(self):
        # This glob-matching sentinel has a complete cursor and would be
        # deleted if the HEALTHY agent requirement were bypassed.
        final = self.final("rigsignal-unhealthy-agent-sentinel.ndjson")
        self.write_registry([self.cursor_entry(final)])
        self.agent.write_text("#!/bin/sh\nprintf 'status: (FAILED)\\n'\nexit 1\n")

        output = self.run_helper()

        self.assertTrue(final.exists())
        self.assertEqual(output, "skip: elastic-agent is not healthy\n")

    def test_failed_agent_status_causes_zero_deletions(self):
        # This glob-matching sentinel has a complete cursor and would be
        # deleted if an agent-status execution failure were ignored.
        final = self.final("rigsignal-failed-agent-sentinel.ndjson")
        self.write_registry([self.cursor_entry(final)])

        with patch.object(RETENTION, "discover_agent", return_value=self.root / "missing-elastic-agent"):
            output = self.run_helper()

        self.assertTrue(final.exists())
        self.assertIn("skip: elastic-agent status failed:", output)

    def test_final_at_or_under_48h_survives(self):
        # This glob-matching sentinel has a complete cursor and would be
        # deleted if the strict age-over-48-hours guard were removed.
        final = self.final("rigsignal-age-boundary-sentinel.ndjson", 48 * 3600 - 1)
        self.write_registry([self.cursor_entry(final)])

        self.run_helper()

        self.assertTrue(final.exists())


if __name__ == "__main__":
    unittest.main()
