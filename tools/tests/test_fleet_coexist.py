import importlib.util
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("install_assets", ROOT / "tools/install_assets.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class FleetCoexistenceTests(unittest.TestCase):
    def test_compiled_identity_table_covers_the_current_manifest(self):
        bundle = INSTALL.load_source()
        ownership = INSTALL.ownership_for_assets(bundle, "fleet-coexist")
        self.assertEqual(len(ownership), 55)
        self.assertEqual(list(ownership.values()).count("bundle-owned"), 16)
        self.assertEqual(list(ownership.values()).count("external"), 39)

    def test_unclassified_asset_refuses_before_network_work(self):
        bundle = INSTALL.load_source()
        bundle.assets.append(INSTALL.Asset("pipelines", "unclassified", "", b"{}"))
        with self.assertRaises(INSTALL.InputError):
            INSTALL.ownership_for_assets(bundle, "fleet-coexist")

    def test_marker_requires_complete_disjoint_accounting(self):
        bundle = INSTALL.load_source()
        with self.assertRaises(INSTALL.InputError):
            INSTALL.marker_body(bundle, "fleet-coexist", [], [])

    def test_journal_intent_pins_body_and_verified_state_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "state")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            record = journal.write_intent("pipelines", "x", "update", "before", "after", b'{"x":1}')
            loaded = INSTALL.parse_json((root / INSTALL.JOURNAL_FILE).read_bytes(), "journal")
            self.assertEqual(loaded["intents"][0]["intended_after_sha256"], "after")
            body = root / record["request_body"]["path"]
            self.assertEqual(hashlib.sha256(body.read_bytes()).hexdigest(), record["request_body"]["sha256"])
            self.assertNotIn("write_verified", loaded["intents"][0])
            journal.write_verified(record, "after")
            self.assertTrue(INSTALL.parse_json((root / INSTALL.JOURNAL_FILE).read_bytes(), "journal")["intents"][0]["write_verified"])

    def test_ambiguous_crash_uses_only_persisted_three_way_pins(self):
        intent = {"preimage_sha256": "before", "intended_after_sha256": "after"}
        self.assertEqual(INSTALL.ambiguous_crash_outcome(intent, "after"), "restore")
        self.assertEqual(INSTALL.ambiguous_crash_outcome(intent, "before"), "untouched")
        with self.assertRaises(INSTALL.ProvisionError):
            INSTALL.ambiguous_crash_outcome(intent, "concurrent")

    def test_exact_proof_recovery_is_exact_id_and_zero_or_one_hit(self):
        calls = []
        def response(*args):
            calls.append(args)
            return {"hits": {"hits": [{"_id": "provision-x", "_index": ".ds-x"}]}}
        with mock.patch.object(INSTALL, "es_json", side_effect=response):
            hit = INSTALL.exact_proof_recovery_hit("https://es", "auth", "provision-x")
        self.assertEqual(hit["_index"], ".ds-x")
        self.assertEqual(calls[0][4]["query"], {"ids": {"values": ["provision-x"]}})
        with mock.patch.object(INSTALL, "es_json", return_value={"hits": {"hits": [{}, {}]}}):
            with self.assertRaises(INSTALL.ProvisionError):
                INSTALL.exact_proof_recovery_hit("https://es", "auth", "provision-x")

    def test_profile_mismatch_is_fenced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "state")
            INSTALL.atomic_write(root, INSTALL.OWNERSHIP_PROFILE_FILE,
                                 INSTALL.jcs({"profile": "fleet-coexist", "table_version": INSTALL.OWNERSHIP_TABLE_VERSION}) + b"\n")
            with self.assertRaises(INSTALL.ProvisionError):
                INSTALL.bind_ownership_profile(root, "default")
