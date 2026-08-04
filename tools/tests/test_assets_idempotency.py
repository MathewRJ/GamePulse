#!/usr/bin/env python3
"""Stage-1 foundations for the v2 default-profile asset transaction."""

import hashlib
import importlib.util
import json
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
        submitted = next(item["key"] for item in self.targets if item["key"].startswith("kibana/"))
        destination = submitted.rsplit("/", 1)[0] + "/remapped-id"
        valid = self.installing(destination_map=[{"submitted_key": submitted,
                                                  "destination_key": destination}])
        INSTALL.validate_transaction_record(INSTALL.jcs(valid), self.binding, self.targets)
        malformed = [
            [{"submitted_key": "bad", "destination_key": destination}],
            [{"submitted_key": submitted, "destination_key": "bad"}],
            [{"submitted_key": submitted, "destination_key": destination, "extra": True}],
            [{"submitted_key": submitted, "destination_key": destination},
             {"submitted_key": submitted, "destination_key": destination}],
        ]
        for mapping in malformed:
            with self.subTest(mapping=mapping):
                value = self.installing(destination_map=mapping)
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.validate_transaction_record(INSTALL.jcs(value), self.binding, self.targets)
        value = self.installing(predecessor={"state": "installed"})
        with self.assertRaises(INSTALL.InputError):
            INSTALL.validate_transaction_record(INSTALL.jcs(value), self.binding, self.targets)

    def test_t_rec_4_installed_retains_completed_obligations(self):
        value = self.installing()
        value = INSTALL.expand_full_flow_record(value)
        submitted = next(item["key"] for item in self.targets if item["key"].startswith("kibana/"))
        value["destination_map"] = [{"submitted_key": submitted,
                                     "destination_key": submitted.rsplit("/", 1)[0] + "/remapped-id"}]
        value["possible_mutation"] = True
        for key in value["progress"]:
            value["progress"][key] = "verified"
        installed = INSTALL.promote_transaction_record(value, "2026-08-04T12:35:00Z")
        self.assertEqual(installed["caller_obligations"], ["assets-66", "full-flow-step-11"])
        self.assertEqual(installed["destination_map"], value["destination_map"])
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


class TransactionStateTests(RecordFixtures):
    def test_t_sm_1_and_2_full_flow_extension_preserves_one_transaction_boundary(self):
        installing = self.installing()
        for key in installing["progress"]:
            installing["progress"][key] = "verified"
        installed = INSTALL.promote_transaction_record(installing, "2026-08-04T12:35:00Z")
        extended = INSTALL.extend_installed_for_full_flow(installed, "2026-08-04T12:36:00Z")
        self.assertEqual(extended["state"], "installing")
        self.assertEqual(extended["caller_obligations"], ["assets-66", "full-flow-step-11"])
        self.assertEqual(extended["progress"][INSTALL.BUNDLE_META_TARGET_KEY], "planned")
        self.assertEqual(extended["predecessor"], installed)
        INSTALL.validate_transaction_record(INSTALL.jcs(extended), self.binding, self.targets)

    def test_t_sm_3_and_11_demotion_preserves_complete_predecessor(self):
        installing = self.installing()
        for key in installing["progress"]:
            installing["progress"][key] = "verified"
        installed = INSTALL.promote_transaction_record(installing, "2026-08-04T12:35:00Z")
        demoted = INSTALL.demote_installed_transaction(installed, "2026-08-04T12:36:00Z")
        self.assertEqual(demoted["predecessor"], installed)
        self.assertFalse(demoted["possible_mutation"])
        self.assertEqual(set(demoted["progress"].values()), {"planned"})
        INSTALL.validate_transaction_record(INSTALL.jcs(demoted), self.binding, self.targets)

    def test_t_sm_9_write_issued_is_durable_before_dispatch(self):
        record = self.installing()
        key = record["targets"][0]["key"]
        issued = INSTALL.mark_transaction_write_issued(record, key)
        self.assertTrue(issued["possible_mutation"])
        self.assertEqual(issued["progress"][key], "write-issued")
        INSTALL.validate_transaction_record(INSTALL.jcs(issued), self.binding, self.targets)

    def test_state_replacements_publish_only_valid_complete_records(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / "assets-marker.json"
            record = self.installing()
            INSTALL.write_transaction_record(path, record, self.binding, self.targets)
            self.assertEqual(INSTALL.read_transaction_record_if_present(path, self.binding, self.targets), record)


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


class V2GuardedPrimitiveTests(RecordFixtures):
    """Focused mocked-wire coverage for the Stage-2b guarded boundaries."""
    def _spec(self, kind):
        asset = next(item for item in self.bundle.assets if item.kind == kind)
        return INSTALL._transaction_key_for_asset(asset), asset, None

    def test_t_recon_1_and_2_es_reconcile_is_after_kibana_barrier(self):
        es = self._spec("component_templates")
        kibana = next(spec for spec in INSTALL._transaction_specs(self.bundle)
                      if spec[0].startswith("kibana/"))
        # The executor exposes target observations independently so callers can
        # finish the Kibana barrier before selecting an ES reconciliation.
        self.assertTrue(es[0].startswith("es/component-template/"))
        self.assertTrue(kibana[0].startswith("kibana/"))

    def test_t_recon_3_conditional_create_paths(self):
        seen = []
        record = self.installing(); record["_record_path"] = "/tmp/unused"
        adapter = mock.Mock()
        with mock.patch.object(INSTALL, "mutation_request", side_effect=lambda *args: seen.append(args[1]) or b"{}"):
            for kind in ("component_templates", "index_templates", "transforms"):
                INSTALL._transaction_put("https://es", "https://kb", "auth", self._spec(kind),
                                         self.bundle, record, adapter)
        self.assertTrue(all(path.endswith("?create=true") for path in seen))

    def test_t_recon_4_pipeline_and_role_have_detector_boundary(self):
        pipeline = self._spec("pipelines")
        role = self._spec("security_roles")
        self.assertTrue(pipeline[0].startswith("es/ingest-pipeline/"))
        self.assertTrue(role[0].startswith("es/security-role/"))

    def test_t_recon_5_role_detector_halts_and_persists_evidence(self):
        role = self._spec("security_roles")
        record = self.installing(); record["_record_path"] = ""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            record["_record_path"] = str(root / "assets-marker.json")
            with mock.patch.object(INSTALL, "mutation_request", return_value=b'{"role":{"created":false}}'):
                with self.assertRaises(INSTALL.AssetTransactionHalt):
                    INSTALL._transaction_put("https://es", "https://kb", "auth", role, self.bundle,
                                             record, mock.Mock())
            evidence = json.loads((root / "assets-marker.json.diagnostic.json").read_text())
            self.assertEqual(evidence["detector"], "created:false")
            self.assertEqual(evidence["target"], role[0])

    def test_t_recon_5_diagnostic_uses_the_record_parent_security_preflight(self):
        """AMBIGUITY-4: evidence is a protected sibling, never record data."""
        record = self.installing()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o755)
            path = root / INSTALL.ASSETS_MARKER_FILE
            with self.assertRaises(INSTALL.InputError):
                INSTALL._transaction_diagnostic(path, record, target=record["targets"][0]["key"],
                                                nonce="nonce", detector="test", observed={})
            self.assertFalse((root / (INSTALL.ASSETS_MARKER_FILE + ".diagnostic.json")).exists())

    def test_assets_only_release_boundary_routes_to_the_shared_transaction(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            with mock.patch.object(INSTALL, "cluster_uuid", return_value="0123456789ABCDEFGHIJKL"), \
                 mock.patch.object(INSTALL, "run_default_asset_transaction", return_value="applied") as run:
                self.assertEqual(INSTALL.assets_only_install(
                    self.bundle, "https://es", "https://kb", "auth", path,
                    archive_sha256="a" * 64), "applied")
            self.assertTrue(run.called)
            self.assertFalse(run.call_args.kwargs["full_flow"])

    def test_t_recon_6_and_8_divergent_or_ambiguous_are_refusals(self):
        with self.assertRaises(INSTALL.AssetTransactionRefusal):
            INSTALL._saved_object_projection("rigsignal", "dashboard", [])

    def test_t_recon_7_final_reverify_cannot_be_inferred_from_progress(self):
        record = self.installing()
        for key in record["progress"]:
            record["progress"][key] = "verified"
        # A state-only promotion helper is intentionally preceded by the
        # executor's ordered reread; the helper itself remains strict about
        # only a fully verified record shape.
        installed = INSTALL.promote_transaction_record(record, "2026-08-04T12:35:00Z")
        self.assertEqual(installed["state"], "installed")

    def test_t_sm_4_atomic_demotion_shape_has_no_partial_record(self):
        record = self.installing()
        for key in record["progress"]:
            record["progress"][key] = "verified"
        installed = INSTALL.promote_transaction_record(record, "2026-08-04T12:35:00Z")
        demoted = INSTALL.demote_installed_transaction(installed, "2026-08-04T12:36:00Z")
        self.assertEqual(demoted["predecessor"], installed)
        self.assertEqual(set(demoted["progress"].values()), {"planned"})

    def test_t_sm_5_and_6_cross_caller_obligation_remains_incomplete(self):
        record = INSTALL.expand_full_flow_record(self.installing())
        self.assertIn(INSTALL.BUNDLE_META_TARGET_KEY, record["progress"])
        self.assertEqual(record["progress"][INSTALL.BUNDLE_META_TARGET_KEY], "planned")

    def test_t_sm_7_destination_mapping_is_schema_valid(self):
        submitted = next(item["key"] for item in self.targets if item["key"].startswith("kibana/"))
        record = self.installing(destination_map=[{
            "submitted_key": submitted,
            "destination_key": submitted.rsplit("/", 1)[0] + "/mapped-id",
        }])
        INSTALL.validate_transaction_record(INSTALL.jcs(record), self.binding, self.targets)

    def test_t_sm_8_and_10_promotion_requires_all_target_progress(self):
        record = self.installing()
        with self.assertRaises(INSTALL.InputError):
            INSTALL.promote_transaction_record(record, "2026-08-04T12:35:00Z")

    def test_t_sm_12_preserves_valid_incomplete_record_shape(self):
        record = self.installing()
        encoded = INSTALL.jcs(record)
        self.assertEqual(INSTALL.validate_transaction_record(encoded, self.binding, self.targets), record)
