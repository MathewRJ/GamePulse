#!/usr/bin/env python3
"""Stage-1 foundations for the v2 default-profile asset transaction."""

import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("install_assets", ROOT / "tools/install_assets.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class RecordFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = INSTALL.load_source()
        cls.targets = INSTALL.transaction_targets(cls.bundle)
        cls.binding = INSTALL.transaction_binding(
            cls.bundle, "0123456789ABCDEFGHIJKL", "https://KIBANA.EXAMPLE:443", "a" * 64)

    def installing(self, **changes):
        value = INSTALL.new_installing_record(self.binding, self.targets, "2026-08-04T12:34:56Z")
        value.update(changes)
        return value

    def test_t_rec_1_rejects_noncanonical_shape(self):
        cases = []
        value = self.installing(); value["extra"] = 1; cases.append(value)
        value = self.installing(); del value["state"]; cases.append(value)
        value = self.installing(); value["created_at"] = "2026-02-30T12:34:56Z"; cases.append(value)
        value = self.installing(); value["targets"] = list(reversed(value["targets"])); cases.append(value)
        value = self.installing(); value["targets"][0]["digest"] = "0" * 64; cases.append(value)
        value = self.installing(); value["targets"] = value["targets"][:-1]; cases.append(value)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.validate_transaction_record(INSTALL.jcs(case), self.binding, self.targets)
        raw = b'{"a":1,"a":2}'
        with self.assertRaises(INSTALL.InputError):
            INSTALL.validate_transaction_record(raw, self.binding, self.targets)

    def test_t_rec_2_validates_obligation_and_progress_vocabulary(self):
        for obligations, progress in (([], None), (["assets-66", "wrong"], None),
                                      (["assets-66"], {"es/bundle-meta/x": "planned"}),
                                      (["assets-66", "full-flow-step-11"], None)):
            value = self.installing(caller_obligations=obligations)
            if progress is not None:
                value["progress"].update(progress)
            if obligations == ["assets-66", "full-flow-step-11"]:
                # Deliberately omit the required bundle-meta progress entry.
                pass
            with self.subTest(obligations=obligations):
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.validate_transaction_record(INSTALL.jcs(value), self.binding, self.targets)
        value = self.installing(); key = value["targets"][0]["key"]; value["progress"][key] = "done"
        with self.assertRaises(INSTALL.InputError):
            INSTALL.validate_transaction_record(INSTALL.jcs(value), self.binding, self.targets)

    def test_t_rec_3_rejects_mapping_and_predecessor_misuse(self):
        value = self.installing(); value["saved_object_mappings"] = [{"submitted_key": "bad", "destination_key": "bad"}]
        with self.assertRaises(INSTALL.InputError):
            INSTALL.validate_transaction_record(INSTALL.jcs(value), self.binding, self.targets)
        value = self.installing(predecessor={"state": "installed"})
        with self.assertRaises(INSTALL.InputError):
            INSTALL.validate_transaction_record(INSTALL.jcs(value), self.binding, self.targets)

    def test_t_rec_4_installed_retains_completed_obligations(self):
        value = self.installing()
        value = INSTALL.expand_full_flow_record(value)
        value["possible_mutation"] = True
        for key in value["progress"]:
            value["progress"][key] = "verified"
        installed = INSTALL.promote_transaction_record(value, "2026-08-04T12:35:00Z")
        self.assertEqual(installed["caller_obligations"], ["assets-66", "full-flow-step-11"])
        self.assertIn("verified_target_set_sha256", installed)
        INSTALL.validate_transaction_record(INSTALL.jcs(installed), self.binding, self.targets)

    def test_t_rec_5_exact_target_accounting(self):
        self.assertEqual(len(self.targets), 66)
        self.assertEqual(self.targets, sorted(self.targets, key=lambda item: item["key"].encode()))
        value = self.installing()
        self.assertEqual(len(value["progress"]), 66)
        expanded = INSTALL.expand_full_flow_record(value)
        self.assertEqual(len(expanded["progress"]), 67)
        self.assertIn(INSTALL.BUNDLE_META_TARGET_KEY, expanded["progress"])

    def test_t_rec_6_independent_bundle_meta_jcs_golden(self):
        # This body is built from the literal fixture returned by no production marker helper.
        literal = [{"digest": item["digest"], "key": item["key"]} for item in self.targets]
        body = {"_meta": {"asset_set": literal, "bundle_version": "0.3.2",
                           "managed_by": "rigsignal-asset-bundle", "ownership_profile": "default",
                           "source_commit": self.bundle.source_commit,
                           "timestamp": "2026-08-04T12:34:56Z"}, "template": {}}
        encoded = INSTALL.jcs(body)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), hashlib.sha256(INSTALL.default_bundle_meta_body(
            literal, "0.3.2", self.bundle.source_commit, "2026-08-04T12:34:56Z")).hexdigest())
        self.assertNotEqual(encoded, INSTALL.jcs({**body, "_meta": {**body["_meta"], "managed_by": "other"}}))


class SnapshotTests(unittest.TestCase):
    def test_t_hash_1_through_3_snapshot_binds_open_descriptor_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "bundle.tgz"
            # Snapshot itself preserves all bytes even when parsing is deferred.
            source.write_bytes(b"abc\x00sentinel")
            snap = INSTALL.snapshot_bundle(source, root, parse=False)
            self.assertEqual(snap.sha256, hashlib.sha256(b"abc\x00sentinel").hexdigest())
            source.write_bytes(b"replacement")
            self.assertEqual(snap.path.read_bytes(), b"abc\x00sentinel")
            snap.close()

    def test_t_hash_4_residue_cleanup_is_strict(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); residue = root / ".rigsignal-test"
            residue.write_bytes(b"x"); residue.chmod(0o600)
            INSTALL.cleanup_snapshot_residue(root, residue.name)
            self.assertFalse(residue.exists())
            bad = root / ".rigsignal-bad"; bad.symlink_to("elsewhere")
            with self.assertRaises(INSTALL.InputError):
                INSTALL.cleanup_snapshot_residue(root, bad.name)

    def test_t_hash_5_temp_is_not_a_record(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); temp = root / ".rigsignal-leftover"; temp.write_bytes(b"partial"); temp.chmod(0o600)
            self.assertIsNone(INSTALL.read_transaction_record_if_present(root / "assets-marker.json", None, None))
            self.assertTrue(temp.exists())


class OriginAndLockTests(unittest.TestCase):
    def test_canonical_origin(self):
        self.assertEqual(INSTALL.canonical_https_origin("HTTPS://BÜCHER.example:443/", "--kibana"),
                         "https://xn--bcher-kva.example")
        self.assertEqual(INSTALL.canonical_https_origin("https://[2001:0DB8::1]:9443", "--kibana"),
                         "https://[2001:db8::1]:9443")
        for value in ("https://user@example.test", "https://example.test/path", "http://example.test",
                      "https://[fe80::1%25eth0]", "https://example.test:0"):
            with self.subTest(value=value):
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.canonical_https_origin(value, "--kibana")

    def test_t_lock_1_and_2_domains_and_nonblocking_acquisition(self):
        with tempfile.TemporaryDirectory() as raw:
            one, two = Path(raw) / "one", Path(raw) / "two"
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(one)}):
                first = INSTALL.AssetTransactionLock.acquire()
                try:
                    self.assertEqual(INSTALL.AssetTransactionLock.lock_path(), one / "rigsignal/assets/assets-install.lock")
                    with self.assertRaises(INSTALL.AssetLockHeld):
                        INSTALL.AssetTransactionLock.acquire()
                finally:
                    first.close()
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(two)}):
                second = INSTALL.AssetTransactionLock.acquire()
                second.close()

    def test_t_lock_3_and_4_lock_read_boundary_is_explicit(self):
        self.assertEqual(INSTALL.AUTHORITATIVE_RECORD_READ_BOUNDARY, "after-assets-lock")
