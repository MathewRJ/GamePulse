#!/usr/bin/env python3
"""Self-tests for the reusable v2 urllib-boundary scripted transport."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.tests.transaction_transport import ScriptedTransactionTransport, TargetScript


SPEC = importlib.util.spec_from_file_location("install_assets", ROOT / "tools/install_assets.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class ScriptedTransactionTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = INSTALL.load_source()
        cls.targets = INSTALL.transaction_targets(cls.bundle)
        cls.binding = INSTALL.transaction_binding(
            cls.bundle, "0123456789ABCDEFGHIJKL", "https://kb", "a" * 64)

    def _record(self):
        return INSTALL.new_installing_record(self.binding, self.targets, "2026-08-04T12:34:56Z")

    def test_guarded_create_uses_real_layer_and_conditional_url(self):
        asset = next(item for item in self.bundle.assets if item.kind == "component_templates")
        key = INSTALL._transaction_key_for_asset(asset)
        # This is the same five-column shape used by frozen flag-table rows.
        fake = ScriptedTransactionTransport.from_table_row(
            INSTALL, self.bundle,
            ("assets-only", "N", "absent:guarded-class", "not-applicable", "none"),
            target_key=key)
        spec = (key, asset, None)
        record = self._record()
        with mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen):
            state, _live, _destination = INSTALL._transaction_observe(
                "https://es", "https://kb", "auth", spec, self.bundle, mock.Mock(), record)
            self.assertEqual(state, "absent")
            INSTALL._transaction_put("https://es", "https://kb", "auth", spec, self.bundle,
                                     record, mock.Mock(), state=state)
        put = fake.calls[-1]
        self.assertEqual(put.method, "PUT")
        self.assertEqual(put.path, INSTALL.es_path(asset))
        self.assertEqual(put.query, {"create": ("true",)})
        self.assertEqual(fake.row.caller, "assets-only")
        self.assertEqual(fake.row.flags, ())

    def test_pre_race_role_script_reaches_real_detector_halt(self):
        asset = next(item for item in self.bundle.assets if item.kind == "security_roles")
        key = INSTALL._transaction_key_for_asset(asset)
        # GET-absent then PUT-created:false is T-RECON-5's deterministic
        # foreign-create race.  No engine helper is mocked.
        fake = ScriptedTransactionTransport(
            INSTALL, self.bundle, {key: TargetScript(live_state="absent", role_created=False)})
        spec = (key, asset, None)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / INSTALL.ASSETS_MARKER_FILE
            record = self._record()
            INSTALL.write_transaction_record(path, record, self.binding, self.targets)
            runtime = dict(record, _record_path=str(path))
            with mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen):
                state, live, _destination = INSTALL._transaction_observe(
                    "https://es", "https://kb", "auth", spec, self.bundle, mock.Mock(), runtime)
                self.assertEqual(state, "absent")
                with self.assertRaisesRegex(INSTALL.AssetTransactionHalt, "partial-remote-possible"):
                    INSTALL._transaction_put("https://es", "https://kb", "auth", spec, self.bundle,
                                             runtime, mock.Mock(), live=live, state=state)
            self.assertEqual([(call.method, call.path) for call in fake.calls],
                             [("GET", INSTALL.es_path(asset)), ("PUT", INSTALL.es_path(asset))])
            evidence = json.loads((root / (INSTALL.ASSETS_MARKER_FILE + ".diagnostic.json")).read_text())
            self.assertEqual(evidence["detector"], "created:false")
            self.assertEqual(evidence["target"], key)

    def test_pre_race_pipeline_script_reaches_real_detector_halt(self):
        asset = next(item for item in self.bundle.assets if item.kind == "pipelines")
        key = INSTALL._transaction_key_for_asset(asset)
        fake = ScriptedTransactionTransport(INSTALL, self.bundle, {
            key: TargetScript(live_state="absent", pipeline_created_millis=100,
                              pipeline_modified_millis=101)})
        spec = (key, asset, None)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / INSTALL.ASSETS_MARKER_FILE
            record = self._record()
            INSTALL.write_transaction_record(path, record, self.binding, self.targets)
            runtime = dict(record, _record_path=str(path))
            with mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen):
                state, live, _destination = INSTALL._transaction_observe(
                    "https://es", "https://kb", "auth", spec, self.bundle, mock.Mock(), runtime)
                self.assertEqual(state, "absent")
                with self.assertRaisesRegex(INSTALL.AssetTransactionHalt, "partial-remote-possible"):
                    INSTALL._transaction_put("https://es", "https://kb", "auth", spec, self.bundle,
                                             runtime, mock.Mock(), live=live, state=state)
            evidence = json.loads((root / (INSTALL.ASSETS_MARKER_FILE + ".diagnostic.json")).read_text())
            self.assertEqual(evidence["detector"], "created<modified")
            self.assertEqual(evidence["observed_created_millis"], 100)
            self.assertEqual(evidence["observed_modified_millis"], 101)

    def test_owned_updates_ignore_create_detectors_and_keep_pipeline_version_guard(self):
        for kind, response in (("security_roles", {"role_created": False}),
                               ("pipelines", {"pipeline_created_millis": 100,
                                              "pipeline_modified_millis": 101, "version": 7})):
            with self.subTest(kind=kind):
                asset = next(item for item in self.bundle.assets if item.kind == kind)
                key = INSTALL._transaction_key_for_asset(asset)
                fake = ScriptedTransactionTransport(
                    INSTALL, self.bundle, {key: TargetScript(live_state="owned-divergent", **response)})
                spec = (key, asset, None)
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw); root.chmod(0o700)
                    path = root / INSTALL.ASSETS_MARKER_FILE
                    record = self._record()
                    INSTALL.write_transaction_record(path, record, self.binding, self.targets)
                    runtime = dict(record, _record_path=str(path))
                    with mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen):
                        state, live, _destination = INSTALL._transaction_observe(
                            "https://es", "https://kb", "auth", spec, self.bundle, mock.Mock(), runtime)
                        self.assertEqual(state, "owned-divergent")
                        INSTALL._transaction_put("https://es", "https://kb", "auth", spec, self.bundle,
                                                 runtime, mock.Mock(), live=live, state=state)
                    self.assertFalse((root / (INSTALL.ASSETS_MARKER_FILE + ".diagnostic.json")).exists())
                put = next(call for call in fake.calls if call.method == "PUT")
                if kind == "pipelines":
                    self.assertEqual(put.query, {"if_version": ("7",)})
                else:
                    self.assertEqual(put.query, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
