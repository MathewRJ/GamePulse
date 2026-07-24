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

    def test_rollback_order_deletes_absent_preimage_and_only_journaled_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            marker = journal.write_intent("component_templates", "rigsignal-bundle-meta", "create",
                                          INSTALL.asset_adapters.dashboard_absent_hash(), "marker-after", b"{}")
            asset = journal.write_intent("pipelines", "journaled-only", "create",
                                         INSTALL.asset_adapters.dashboard_absent_hash(), "asset-after", b"{}")
            journal.write_verified(marker, "marker-after")
            journal.write_verified(asset, "asset-after")
            calls = []
            def request(base, path, method, authorization, data=None, headers=None):
                calls.append((method, path)); return b"{}"
            with mock.patch.object(INSTALL, "request", side_effect=request), \
                 mock.patch.object(INSTALL, "invalidate"), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", side_effect=lambda *_args: INSTALL.asset_adapters.dashboard_absent_hash()):
                order = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertEqual(order[:2], ["marker", "fence"])
            self.assertEqual(calls[0], ("DELETE", "/_component_template/rigsignal-bundle-meta"))
            self.assertIn(("DELETE", "/_ingest/pipeline/journaled-only"), calls)
            self.assertFalse(any("not-journaled" in path for _method, path in calls))

    def test_transform_rollback_restores_preimage_without_pivot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            preimage = {"id": "rigsignal-game-timeline", "description": "before",
                        "pivot": {"group_by": {}}}
            intent = journal.write_intent("transforms", "rigsignal-game-timeline", "update",
                                          "before", "after", b'{"description":"after"}',
                                          preimage_body=INSTALL.jcs(preimage))
            journal.write_verified(intent, "after")
            calls = []
            def request(_base, path, method, _authorization, data=None, _headers=None):
                calls.append((method, path, data))
                return b"{}"
            with mock.patch.object(INSTALL, "request", side_effect=request), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", return_value="before"):
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertIn(("POST", "/_transform/rigsignal-game-timeline/_update",
                           INSTALL.jcs({"description": "before"})), calls)
            self.assertIn("asset:transforms/rigsignal-game-timeline", operations)
            self.assertNotIn("degradations", INSTALL.TransactionJournal(root, "fleet-coexist").value)

    def test_transform_rollback_rejection_degrades_to_journaled_verify_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            preimage = {"id": "rigsignal-game-timeline", "description": "before",
                        "pivot": {"group_by": {}}}
            intent = journal.write_intent("transforms", "rigsignal-game-timeline", "update",
                                          "before", "after", b'{"description":"after"}',
                                          preimage_body=INSTALL.jcs(preimage))
            journal.write_verified(intent, "after")
            def request(_base, path, method, _authorization, data=None, _headers=None):
                if path.endswith("/_update"):
                    raise INSTALL.RequestFailure(400, "_meta cannot be removed")
                if path.endswith("/_stats"):
                    return b'{"transforms":[{"state":"started"}]}'
                if method == "GET":
                    return (b'{"transforms":[{"id":"rigsignal-game-timeline",'
                            b'"description":"before","pivot":{"group_by":{}},'
                            b'"_meta":{"managed_by":"rigsignal-asset-bundle"}}]}')
                return b"{}"
            with mock.patch.object(INSTALL, "request", side_effect=request), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", return_value="before"):
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertIn("verify-only:transforms/rigsignal-game-timeline", operations)
            self.assertEqual(INSTALL.TransactionJournal(root, "fleet-coexist").value["degradations"], [{
                "kind": "transforms", "name": "rigsignal-game-timeline",
                "reason": "meta_absent_restore_rejected_verify_only",
            }])

    def test_proof_deletion_refuses_completed_transaction_without_explicit_reverse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.apply_ok()
            with self.assertRaisesRegex(INSTALL.ProvisionError, "transaction_proof_delete_not_authorized"):
                INSTALL.rollback_transaction_proofs("https://es", "auth", journal)
            journal.value["proofs"] = [{"event_id": None, "created_index": None}]
            journal.value["apply_ok"] = False
            with self.assertRaisesRegex(INSTALL.ProvisionError, "transaction_proof_ambiguous"):
                INSTALL.rollback_transaction_proofs("https://es", "auth", journal)

    def test_m1_anchor_oracle_passes_and_stops_for_break_glass(self):
        pins = {ident: "p-" + str(index) for index, ident in enumerate(INSTALL.M1_ANCHOR_IDS)}
        with mock.patch.object(INSTALL, "m1_anchor_pins", return_value=pins):
            INSTALL.verify_m1_anchors("https://es", "auth", pins)
        changed = dict(pins); changed[INSTALL.M1_ANCHOR_IDS[0]] = "changed"
        with mock.patch.object(INSTALL, "m1_anchor_pins", return_value=changed):
            with self.assertRaisesRegex(INSTALL.ProvisionError, "m1_anchor_mismatch_break_glass"):
                INSTALL.verify_m1_anchors("https://es", "auth", pins)
