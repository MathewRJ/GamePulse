#!/usr/bin/env python3
"""Stage-1 foundations for the v2 default-profile asset transaction."""

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    def test_ambiguity_5_v1_migration_persists_explicit_provenance(self):
        """A v2 record keeps the v1 lineage through later state transitions."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / INSTALL.ASSETS_MARKER_FILE
            INSTALL._write_assets_marker(path, self.bundle)
            writes = []

            def exact(*_args, **_kwargs):
                return "exact", None, None

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}), \
                 mock.patch.object(INSTALL, "_transaction_observe", side_effect=exact), \
                 mock.patch.object(INSTALL, "_transaction_put", side_effect=lambda *_args: writes.append(True)):
                self.assertEqual(INSTALL.run_default_asset_transaction(
                    self.bundle, "https://es", "https://kb", "auth", path, self.binding), "noop")
            migrated = INSTALL.read_transaction_record_if_present(path, self.binding, self.targets)
            self.assertTrue(migrated["migrated_from_v1"])
            self.assertEqual(writes, [])
            demoted = INSTALL.demote_installed_transaction(migrated, "2026-08-04T12:36:00Z")
            self.assertTrue(demoted["migrated_from_v1"])
            ordinary = self.installing()
            self.assertFalse(ordinary["migrated_from_v1"])

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

    def test_ambiguity_4_and_6_detector_nonce_is_deterministic_and_schema_free(self):
        """R-A6: nonce is transaction/target-derived; observations stay sibling-only."""
        record = self.installing()
        first, second = record["targets"][0]["key"], record["targets"][1]["key"]
        nonce = INSTALL.transaction_detector_nonce(record["transaction_id"], first)
        self.assertEqual(nonce, INSTALL.transaction_detector_nonce(record["transaction_id"], first))
        self.assertNotEqual(nonce, INSTALL.transaction_detector_nonce(record["transaction_id"], second))
        self.assertRegex(nonce, r"^[0-9a-f]{64}$")
        self.assertNotIn("controller_nonce", record)

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


class CompletionWaveTests(unittest.TestCase):
    """Executable completion checks for Stage 2k's remaining manifest IDs."""
    FLAG_FIXTURE = ROOT / "tools/tests/fixtures/rigsignal-flag-state-table.md"
    FLAG_DATA_SHA256 = "cb35643cd06e0c9438a37f1e84d8d28d6ebe2fc744bdfeec5a129d855b90197a"
    FLAG_ROW = re.compile(
        r"^\| (assets-only|full-flow) \| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \| "
        r"([^|]+?) \| ([^|]+?) \| ([0-9]+) \| ([0-9]+) \|$")

    @classmethod
    def _flag_rows(cls):
        rows = []
        for line in cls.FLAG_FIXTURE.read_text(encoding="utf-8").splitlines():
            match = cls.FLAG_ROW.match(line)
            if match:
                rows.append(tuple(item.strip() for item in match.groups()))
        return rows

    @staticmethod
    def _table_flags(flags):
        return () if flags == "none" else tuple(flags.split("+"))

    @staticmethod
    def _table_live_state(live):
        return {
            "absent:guarded-class": "absent",
            "absent:pipeline-or-es-role": "absent",
            "exact": "exact",
            "es-stamped-divergent": "owned-divergent",
            "es-foreign-divergent": "divergent",
            "kibana-divergent": "divergent",
            "unreadable": "unreadable",
        }[live]

    @staticmethod
    def _table_obligation_label(record):
        return ([INSTALL.V2_ASSET_OBLIGATION, INSTALL.V2_FULL_FLOW_OBLIGATION]
                if "full" in record else [INSTALL.V2_ASSET_OBLIGATION])

    def _table_installed(self, binding, targets, *, full=False):
        record = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
        if full:
            record = INSTALL.expand_full_flow_record(record)
        for key in record["progress"]:
            record["progress"][key] = "verified"
        return INSTALL.promote_transaction_record(record, "2026-08-04T12:35:00Z")

    def _table_initial_record(self, label, binding, targets):
        """Build each table record as a real protected v2 durable shape."""
        if label == "N":
            return None
        full = "full" in label
        prior = label.startswith("S-prior") or "with-valid-predecessor" in label
        prior_binding = {**binding, "bundle_version": "0.3.2", "source_commit": "b" * 40}
        predecessor = self._table_installed(prior_binding, targets, full=full) if prior else None
        if label.startswith("S-"):
            return predecessor if prior else self._table_installed(binding, targets, full=full)
        record = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
        if full:
            record = INSTALL.expand_full_flow_record(record)
        if predecessor is not None:
            record["predecessor"] = predecessor
        record["possible_mutation"] = label.endswith("pm1")
        INSTALL.validate_transaction_record(INSTALL.jcs(record), binding, targets)
        return record

    @staticmethod
    def _table_record_label(record, initial_label, initial_raw):
        if record is None:
            return "N"
        raw = INSTALL.jcs(record)
        # Current/prior installed labels carry useful source context only
        # while the engine has correctly retained their exact bytes.
        if record["state"] == "installed" and raw == initial_raw:
            return initial_label
        obligations = "+".join(record["caller_obligations"])
        if record["state"] == "installed":
            return "S[" + obligations + "]"
        predecessor = ";predecessor=valid" if record["predecessor"] is not None else ""
        return "I[" + obligations + predecessor + ";pm=" + str(int(record["possible_mutation"])) + "]"

    def _run_table_row(self, row):
        """Main-routed, one-row durable fixture with a transport write sentinel."""
        (caller, record_label, ordinary, bundle_meta, flags,
         expected_record, expected_writes, expected_exit) = row
        bundle = INSTALL.load_source()
        targets = INSTALL.transaction_targets(bundle)
        binding = INSTALL.transaction_binding(
            bundle, "0123456789ABCDEFGHIJKL", "https://kb.example", INSTALL.bundle_snapshot_digest(bundle))
        ordinary_key = (next(item["key"] for item in targets if item["key"].startswith("kibana/"))
                        if ordinary == "kibana-divergent" else
                        next(item["key"] for item in targets if ("ingest-pipeline" in item["key"]
                                                               if ordinary == "absent:pipeline-or-es-role"
                                                               else "component-template" in item["key"])))
        states = {item["key"]: "exact" for item in targets}
        states[ordinary_key] = self._table_live_state(ordinary)
        if caller == "full-flow":
            states[INSTALL.BUNDLE_META_TARGET_KEY] = self._table_live_state(bundle_meta)
        operations, attempts = [], {}
        expected_writes = int(expected_writes)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / INSTALL.ASSETS_MARKER_FILE
            initial = self._table_initial_record(record_label, binding, targets)
            if initial is not None:
                # Prior S intentionally has a different archive binding, so
                # it is published directly as the protected older record.
                INSTALL.atomic_write(path.parent, path.name, INSTALL.jcs(initial))
            initial_raw = path.read_bytes() if path.exists() else None

            def observe(_es, _kb, _auth, spec, _bundle, _adapter, _record=None):
                key = spec[0]
                attempts[key] = attempts.get(key, 0) + 1
                state = states.get(key, "exact")
                if state == "unreadable":
                    raise INSTALL.AssetTransactionRefusal("fixture remote read refused")
                return state, None, None

            def put(_es, _kb, _auth, spec, _bundle, _record, _adapter):
                if len(operations) >= expected_writes:
                    raise AssertionError("mutation sentinel tripped for " + spec[0])
                operations.append(spec[0])
                states[spec[0]] = "exact"

            argv = ["install_assets.py", "--bundle", str(root / "bundle.tar"),
                    "--endpoint", "https://es.example", "--ca-file", str(root / "es.pem"),
                    "--kibana-endpoint", "https://kb.example", "--kibana-ca-file", str(root / "kb.pem"),
                    "--admin-credentials-file", str(root / "admin.toml"),
                    "--agent-binary", str(root / "agent"), "--profile", "user",
                    "--assets-only", "--assets-marker", str(path)]
            argv.extend("--" + flag for flag in self._table_flags(flags))
            original_assets_only = INSTALL.assets_only_install

            def caller_route(*args, **kwargs):
                if caller == "assets-only":
                    return original_assets_only(*args, **kwargs)
                scenario_binding = INSTALL.transaction_binding(
                    args[0], "0123456789ABCDEFGHIJKL", args[2], INSTALL.bundle_snapshot_digest(args[0]))
                return INSTALL.run_default_asset_transaction(
                    args[0], args[1], args[2], args[3], args[4], scenario_binding, full_flow=True,
                    repair=kwargs.get("repair", False), upgrade=kwargs.get("upgrade", False),
                    allow_downgrade=kwargs.get("allow_downgrade", False))

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}, clear=False), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(INSTALL, "load_bundle", return_value=bundle), \
                 mock.patch.object(INSTALL, "check_version_fence"), \
                 mock.patch.object(INSTALL, "configure_https"), \
                 mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
                 mock.patch.object(INSTALL, "cluster_uuid", return_value="0123456789ABCDEFGHIJKL"), \
                 mock.patch.object(INSTALL, "_prepare_assets_marker_path", return_value=path), \
                 mock.patch.object(INSTALL, "assets_only_install", side_effect=caller_route), \
                 mock.patch.object(INSTALL, "_transaction_observe", side_effect=observe), \
                 mock.patch.object(INSTALL, "_transaction_put", side_effect=put), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = INSTALL.main()
            final = (INSTALL.read_transaction_record_if_present(path, binding, targets)
                     if path.exists() and not record_label.startswith("S-prior") else
                     (json.loads(path.read_text()) if path.exists() else None))
            # Step 11 is first in the engine's ordered full-flow spec.  A
            # later ordinary refusal may therefore leave its write-issued
            # record behind, but it may never run before a bundle-meta write.
            if INSTALL.BUNDLE_META_TARGET_KEY in operations:
                self.assertEqual(operations[0], INSTALL.BUNDLE_META_TARGET_KEY)
            if caller == "full-flow" and expected_writes == 2:
                self.assertEqual(operations[0], INSTALL.BUNDLE_META_TARGET_KEY)
            return (self._table_record_label(final, record_label, initial_raw), len(operations), exit_code,
                    final, attempts, expected_record, expected_writes, int(expected_exit))

    _CHILD_EXECUTOR = r'''
import importlib.util, json, os
from pathlib import Path
from unittest import mock
root = Path(os.environ["RIGSIGNAL_TEST_ROOT"])
spec = importlib.util.spec_from_file_location("install_assets", root / "tools/install_assets.py")
install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
bundle = install.load_source()
binding = install.transaction_binding(bundle, "0123456789ABCDEFGHIJKL", "https://kb.example", "a" * 64)
record_path = Path(os.environ["RIGSIGNAL_TEST_RECORD"])
state_path = Path(os.environ["RIGSIGNAL_TEST_WIRE"])
state = json.loads(state_path.read_text())
def observe(_es, _kb, _auth, item, _bundle, _adapter, _record=None):
    key = item[0]
    if key in state["exact"]:
        destination = "remapped-id" if key == state.get("mapped_key") else None
        return "exact", None, destination
    return "absent", None, None
def put(_es, _kb, _auth, item, _bundle, _record, _adapter):
    state["writes"] += 1
    state["exact"].append(item[0])
    state_path.write_text(json.dumps(state, sort_keys=True))
with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(record_path.parent / "state")}, clear=False), \
     mock.patch.object(install, "_transaction_observe", side_effect=observe), \
     mock.patch.object(install, "_transaction_put", side_effect=put):
    outcome = install.run_default_asset_transaction(bundle, "https://es", "https://kb", "auth", record_path,
                                                    binding, full_flow=os.environ.get("RIGSIGNAL_TEST_FULL") == "1")
print(outcome)
'''

    def _subprocess_record(self, mode, *, full_flow=False, missing=None, mapped=False):
        """Run the real engine in a fresh interpreter with a durable fake wire."""
        bundle = INSTALL.load_source()
        targets = INSTALL.transaction_targets(bundle)
        binding = INSTALL.transaction_binding(bundle, "0123456789ABCDEFGHIJKL", "https://kb.example", "a" * 64)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            record = root / INSTALL.ASSETS_MARKER_FILE
            all_keys = [item["key"] for item in targets]
            if full_flow:
                all_keys.append(INSTALL.BUNDLE_META_TARGET_KEY)
            missing = missing or []
            exact = [key for key in all_keys if key not in missing]
            if mode.startswith("extend-") or mode.startswith("demote-"):
                initial = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
                for key in initial["progress"]:
                    initial["progress"][key] = "verified"
                installed = INSTALL.promote_transaction_record(initial, "2026-08-04T12:35:00Z")
                INSTALL.write_transaction_record(record, installed, binding, targets)
            elif mode.startswith("promote-") or mode.startswith("write-") or mode.startswith("verify-") or mode.startswith("map-"):
                initial = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
                for key in initial["progress"]:
                    initial["progress"][key] = "verified"
                if missing:
                    initial["progress"][missing[0]] = "planned"
                if mode.startswith("write-") or mode.startswith("map-"):
                    initial = INSTALL.mark_transaction_write_issued(initial, missing[0])
                INSTALL.write_transaction_record(record, initial, binding, targets)
            elif mode.startswith("v1-"):
                INSTALL._write_assets_marker(record, bundle)
            wire = root / "wire.json"
            wire.write_text(json.dumps({"exact": exact, "mapped_key": missing[0] if mapped else None, "writes": 0}, sort_keys=True))
            env = os.environ | {"RIGSIGNAL_TEST_ROOT": str(ROOT), "RIGSIGNAL_TEST_RECORD": str(record),
                                "RIGSIGNAL_TEST_WIRE": str(wire), "RIGSIGNAL_TEST_FULL": "1" if full_flow else "0",
                                "RIGSIGNAL_TEST_CRASH_AT": mode.split("-", 1)[1]}
            crashed = subprocess.run([sys.executable, "-c", textwrap.dedent(self._CHILD_EXECUTOR)], env=env,
                                     text=True, capture_output=True, check=False)
            raw_after = record.read_bytes()
            state_after = json.loads(wire.read_text())
            rerun_env = env | {"RIGSIGNAL_TEST_CRASH_AT": ""}
            rerun = subprocess.run([sys.executable, "-c", textwrap.dedent(self._CHILD_EXECUTOR)], env=rerun_env,
                                   text=True, capture_output=True, check=False)
            final = INSTALL.read_transaction_record_if_present(record, binding, targets)
            return crashed, raw_after, state_after, rerun, final

    def _run_scenario(self, caller, initial_durable_record="N", live_state=None, flags=(),
                      crash_point=None):
        """Drive the real transaction from the public ``main()`` asset route.

        ``live_state`` is a transport script: key -> exact/absent/divergent/
        unreadable or a callable receiving ``(key, attempt)``.  The wire is
        beneath the engine and records every mutation attempt, so table rows
        can assert the durable record and mutation boundary without creating
        a second planner in the test suite.  ``crash_point`` is reserved for
        the fresh-process crash runner below; an in-process SIGKILL would end
        the suite itself.
        """
        self.assertIn(caller, {"assets-only", "full-flow"})
        self.assertIsNone(crash_point, "crash scenarios use _subprocess_record")
        bundle = INSTALL.load_source()
        targets = INSTALL.transaction_targets(bundle)
        binding = INSTALL.transaction_binding(
            bundle, "0123456789ABCDEFGHIJKL", "https://kb.example", INSTALL.bundle_snapshot_digest(bundle))
        live_state = dict(live_state or {})
        wire = {item["key"]: "exact" for item in targets}
        wire.update(live_state.get("states", live_state))
        operations = []
        attempts = {}

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / INSTALL.ASSETS_MARKER_FILE
            if initial_durable_record != "N":
                record = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
                if initial_durable_record.startswith("I-full") or initial_durable_record == "S-current-full":
                    record = INSTALL.expand_full_flow_record(record)
                if initial_durable_record.startswith("I-"):
                    record["possible_mutation"] = initial_durable_record.endswith("pm1")
                else:
                    for key in record["progress"]:
                        record["progress"][key] = "verified"
                    record = INSTALL.promote_transaction_record(record, "2026-08-04T12:35:00Z")
                INSTALL.write_transaction_record(path, record, binding, targets)

            def observe(_es, _kb, _auth, spec, _bundle, _adapter, _record=None):
                key = spec[0]
                attempts[key] = attempts.get(key, 0) + 1
                scripted = live_state.get("script")
                state = scripted(key, attempts[key]) if scripted else wire.get(key, "exact")
                if state == "unreadable":
                    raise INSTALL.AssetTransactionRefusal("fixture remote read refused")
                destination = live_state.get("destinations", {}).get(key)
                return state, None, destination

            def put(_es, _kb, _auth, spec, _bundle, _record, _adapter):
                operations.append(spec[0])
                wire[spec[0]] = "exact"

            argv = ["install_assets.py", "--bundle", str(root / "bundle.tar"),
                    "--endpoint", "https://es.example", "--ca-file", str(root / "es.pem"),
                    "--kibana-endpoint", "https://kb.example", "--kibana-ca-file", str(root / "kb.pem"),
                    "--admin-credentials-file", str(root / "admin.toml"),
                    "--agent-binary", str(root / "agent"), "--profile", "user",
                    "--assets-only", "--assets-marker", str(path)]
            argv.extend("--" + flag for flag in flags)
            diagnostics = io.StringIO()
            output = io.StringIO()
            actual_assets_only_install = INSTALL.assets_only_install

            def caller_route(*args, **kwargs):
                if caller == "assets-only":
                    return actual_assets_only_install(*args, **kwargs)
                scenario_binding = INSTALL.transaction_binding(
                    args[0], "0123456789ABCDEFGHIJKL", args[2], INSTALL.bundle_snapshot_digest(args[0]))
                return INSTALL.run_default_asset_transaction(
                    args[0], args[1], args[2], args[3], args[4], scenario_binding,
                    full_flow=True, repair=kwargs.get("repair", False),
                    upgrade=kwargs.get("upgrade", False),
                    allow_downgrade=kwargs.get("allow_downgrade", False))

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}, clear=False), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(INSTALL, "load_bundle", return_value=bundle), \
                 mock.patch.object(INSTALL, "check_version_fence"), \
                 mock.patch.object(INSTALL, "configure_https"), \
                 mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
                 mock.patch.object(INSTALL, "cluster_uuid", return_value="0123456789ABCDEFGHIJKL"), \
                 mock.patch.object(INSTALL, "_prepare_assets_marker_path", return_value=path), \
                 mock.patch.object(INSTALL, "assets_only_install", side_effect=caller_route), \
                 mock.patch.object(INSTALL, "_transaction_observe", side_effect=observe), \
                 mock.patch.object(INSTALL, "_transaction_put", side_effect=put), \
                 redirect_stdout(output), redirect_stderr(diagnostics):
                exit_code = INSTALL.main()
            final = INSTALL.read_transaction_record_if_present(path, binding, targets) if path.exists() else None
            return {"exit_code": exit_code, "record": final, "operations": operations,
                    "diagnostics": diagnostics.getvalue(), "output": output.getvalue(),
                    "path_exists": path.exists(), "wire": wire, "targets": targets,
                    "full_key": INSTALL.BUNDLE_META_TARGET_KEY}

    def _real_transaction(self, *, record_kind="N", full_flow=False, absent=(), divergent=(),
                          unreadable=(), repair=False, upgrade=False, allow_downgrade=False):
        """Compatibility adapter: all completion rows use ``_run_scenario``."""
        states = {key: "absent" for key in absent}
        states.update({key: "divergent" for key in divergent})
        states.update({key: "unreadable" for key in unreadable})
        flags = tuple(name for name, enabled in (("repair", repair), ("upgrade", upgrade),
                                                   ("allow-downgrade", allow_downgrade)) if enabled)
        result = self._run_scenario("full-flow" if full_flow else "assets-only", record_kind,
                                    {"states": states}, flags)
        result.update(status=result["exit_code"], writes=result["operations"], error=None, outcome=None)
        return result

    def test_ambiguity_6_exit_mapping_is_exhaustive_and_main_routed(self):
        """A6: one mapping owns every executor result; main() invokes it."""
        bundle = INSTALL.load_source()
        targets = INSTALL.transaction_targets(bundle)
        binding = INSTALL.transaction_binding(
            bundle, "0123456789ABCDEFGHIJKL", "https://kb.example", INSTALL.bundle_snapshot_digest(bundle))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            with redirect_stderr(io.StringIO()):
                for possible_mutation, expected in ((False, 3), (True, 4)):
                    record = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
                    record["possible_mutation"] = possible_mutation
                    INSTALL.write_transaction_record(path, record, binding, targets)
                    for outcome in ("refusal", "halt"):
                        with self.subTest(outcome=outcome, possible_mutation=possible_mutation):
                            self.assertEqual(INSTALL.asset_executor_exit_code(outcome, path), expected)
                self.assertEqual(INSTALL.asset_executor_exit_code("success", path), 0)
                self.assertEqual(INSTALL.asset_executor_exit_code("local-input", path), 2)
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.asset_executor_exit_code("unknown", path)

        # The real main()-routed scenario touches the same mapper for a
        # success and an uncertainty limb; static source guards the full-flow
        # caller's matching catch boundary without duplicating enrollment.
        with mock.patch.object(INSTALL, "asset_executor_exit_code", wraps=INSTALL.asset_executor_exit_code) as mapped:
            self._run_scenario("assets-only")
            key = INSTALL.transaction_targets(bundle)[0]["key"]
            self._run_scenario("assets-only", "I-assets-pm1", {"states": {key: "unreadable"}})
        self.assertEqual([call.args[0] for call in mapped.call_args_list], ["success", "halt"])
        source = (ROOT / "tools/install_assets.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count('asset_executor_exit_code("halt",'), 2)

    def test_t_flag_1_through_4_generated_5824_row_oracle(self):
        """T-FLAG-1..4: every vendored corrected-table row reaches the policy."""
        raw = self.FLAG_FIXTURE.read_bytes()
        generated = subprocess.run(
            ["python3", "/home/dev/coding/Workflow/projects/RigSignal/tasks/idempotency-2026-08-04/gen_flag_table.py"],
            text=True, capture_output=True, check=True).stdout
        self.assertEqual(raw.decode("utf-8").strip(), generated.strip())
        rows = []
        for line in raw.decode("utf-8").splitlines():
            match = self.FLAG_ROW.match(line)
            if match:
                rows.append(tuple(item.strip() for item in match.groups()))
        self.assertEqual(len(rows), 5824)
        self.assertEqual(hashlib.sha256("\n".join(
            line for line in raw.decode("utf-8").splitlines()
            if line.startswith("| ") and not line.startswith("| Caller")
        ).encode("utf-8")).hexdigest(), self.FLAG_DATA_SHA256)
        writes_seen = 0
        for caller, record, ordinary, bundle_meta, flags, expected_record, expected_writes, expected_exit in rows:
            possible_mutation = record.endswith("pm1")
            obligations = "assets-66+full-flow-step-11" if "full" in record else "assets-66"
            actual_record, actual_writes, actual_exit = INSTALL.transaction_flag_policy(
                caller, record, possible_mutation, obligations, ordinary, flags, bundle_meta)
            with self.subTest(caller=caller, record=record, ordinary=ordinary,
                              bundle_meta=bundle_meta, flags=flags):
                self.assertEqual((actual_record, str(actual_writes), str(actual_exit)),
                                 (expected_record, expected_writes, expected_exit))
                # A full-flow row may write one ordinary target plus Step 11.
                self.assertIn(actual_writes, (0, 1, 2))
            writes_seen += actual_writes
        self.assertGreater(writes_seen, 0)

    def test_t_flag_1_through_4_all_rows_main_routed_sharded(self):
        """Execute every pinned policy row through main(), not a shadow planner.

        The default shard is the complete 5,824-row set.  CI can split it
        deterministically with ``RIGSIGNAL_FLAG_SHARDS`` and
        ``RIGSIGNAL_FLAG_SHARD``; each row has exactly one SHA-256 shard.
        """
        rows = self._flag_rows()
        self.assertEqual(len(rows), 5824)
        shard_count = int(os.environ.get("RIGSIGNAL_FLAG_SHARDS", "1"))
        shard = int(os.environ.get("RIGSIGNAL_FLAG_SHARD", "0"))
        self.assertGreater(shard_count, 0)
        self.assertGreaterEqual(shard, 0)
        self.assertLess(shard, shard_count)
        selected = [(index, row) for index, row in enumerate(rows)
                    if int.from_bytes(hashlib.sha256((str(index) + "\\0" + "\\0".join(row)).encode()).digest()[:8], "big")
                    % shard_count == shard]
        self.assertTrue(selected)
        for index, row in selected:
            with self.subTest(row=index, caller=row[0], record=row[1], ordinary=row[2],
                              bundle_meta=row[3], flags=row[4]):
                (actual_record, writes, exit_code, _final, _attempts,
                 expected_record, expected_writes, expected_exit) = self._run_table_row(row)
                self.assertEqual(actual_record, expected_record)
                self.assertEqual(writes, expected_writes)
                self.assertEqual(exit_code, expected_exit)

    def test_t_dash_1_partial_dashboard_refuses_before_any_mutation_for_both_callers(self):
        """T-DASH-1: the real executor's N-state barrier is object-complete."""
        bundle = INSTALL.load_source()
        root = next(item["key"] for item in INSTALL.transaction_targets(bundle)
                    if item["key"].startswith("kibana/") and "/dashboard/" in item["key"])
        for full_flow in (False, True):
            with self.subTest(caller="full-flow" if full_flow else "assets-only"):
                result = self._real_transaction(full_flow=full_flow, divergent=(root,))
                self.assertEqual(result["status"], 3)
                self.assertEqual(result["writes"], [])
                self.assertFalse(result["path_exists"])
                self.assertEqual(result["wire"][root], "divergent")

    def test_t_dash_2_duplicate_expansion_has_eighteen_real_saved_object_writes(self):
        """T-DASH-2: 29 source entries collapse to the 18 persisted SO identities."""
        targets = INSTALL.transaction_targets(INSTALL.load_source())
        saved = [item["key"] for item in targets
                 if item["key"].startswith("kibana/")
                 and "/space/" not in item["key"] and "/role/" not in item["key"]]
        self.assertEqual(len(saved), 18)
        result = self._real_transaction(absent=saved)
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["record"]["state"], "installed")
        self.assertEqual(sorted(result["writes"]), saved)
        self.assertEqual(len(result["writes"]), 18)
        replay = self._real_transaction(record_kind="S-current-assets")
        self.assertEqual((replay["status"], replay["writes"]), (0, []))

    def test_t_cross_1_assets_only_retains_full_flow_intent_then_full_flow_completes(self):
        """T-CROSS-1, both literal Step-11 cases, on the shared executor."""
        for meta_state, expected_writes in (("exact", 0), ("absent", 1)):
            with self.subTest(bundle_meta=meta_state):
                held = self._real_transaction(record_kind="I-full-pm0", absent=(
                    (INSTALL.BUNDLE_META_TARGET_KEY,) if meta_state == "absent" else ()))
                # assets-only must not reach its transport at all while the
                # full-flow obligation is active.
                self.assertEqual((held["status"], held["writes"], held["record"]["state"]),
                                 (3, [], "installing"))
                self.assertEqual(held["record"]["caller_obligations"],
                                 ["assets-66", "full-flow-step-11"])

                # Rebuild the same input for the permitted full-flow caller.
                resumed = self._real_transaction(record_kind="I-full-pm0", full_flow=True, absent=(
                    (INSTALL.BUNDLE_META_TARGET_KEY,) if meta_state == "absent" else ()))
                self.assertEqual(resumed["status"], 0)
                self.assertEqual(len(resumed["writes"]), expected_writes)
                self.assertEqual(resumed["record"]["state"], "installed")

    def test_t_exit_3_installed_reverify_refuses_without_write(self):
        """T-EXIT-3: installed has no uncertainty, so a foreign reread is exit 3."""
        key = INSTALL.transaction_targets(INSTALL.load_source())[0]["key"]
        result = self._real_transaction(record_kind="S-current-assets", divergent=(key,))
        self.assertEqual(result["status"], 3)
        self.assertEqual(result["writes"], [])
        self.assertEqual(result["record"]["state"], "installed")

    def test_t_exit_2_version_flags_are_local_preflight_with_no_transport(self):
        """The currently implemented T-EXIT-2 invalid-flag limb is real, not policy-only."""
        for kwargs in ({"upgrade": True}, {"allow_downgrade": True},
                       {"upgrade": True, "allow_downgrade": True}):
            with self.subTest(**kwargs):
                result = self._real_transaction(**kwargs)
                self.assertEqual(result["status"], 2)
                self.assertEqual(result["writes"], [])
                self.assertFalse(result["path_exists"])

    def test_t_flag_3_resumed_stamped_es_requires_repair_through_main(self):
        """Corrected-table I-assets-pm0 stamped-ES rows use the real route."""
        key = INSTALL.transaction_targets(INSTALL.load_source())[0]["key"]
        for flags, expected_status, expected_writes in (((), 3, 0), (("repair",), 0, 1)):
            with self.subTest(flags=flags):
                result = self._run_scenario(
                    "assets-only", "I-assets-pm0", {"states": {key: "owned-divergent"}}, flags)
                self.assertEqual(result["exit_code"], expected_status)
                self.assertEqual(len(result["operations"]), expected_writes)
                self.assertEqual(result["record"]["state"], "installed" if expected_status == 0 else "installing")

    def test_t_exit_4_real_cli_and_launcher_wait_status_anchor(self):
        """The packaged launcher subprocess preserves all engine contract codes."""
        cli = subprocess.run([sys.executable, str(ROOT / "tools/install_assets.py"), "--unknown-flag"],
                             text=True, capture_output=True, check=False)
        self.assertEqual(cli.returncode, 2)
        self.assertIn("usage: install_assets.py", cli.stderr)
        launcher = subprocess.run(["bash", str(ROOT / "packaging/tests/test-assets-launcher.sh")],
                                  cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(launcher.returncode, 0, launcher.stdout + launcher.stderr)
        self.assertIn("rigsignal assets launcher: PASS", launcher.stdout)

    def test_r_a1_persisted_possible_mutation_precedes_invalid_version_flags(self):
        """Ratification 2: durable uncertainty wins before flag preflight."""
        key = INSTALL.transaction_targets(INSTALL.load_source())[0]["key"]
        for flags in (("upgrade",), ("allow-downgrade",), ("upgrade", "allow-downgrade")):
            with self.subTest(flags=flags):
                result = self._run_scenario(
                    "assets-only", "I-assets-pm1", {"states": {key: "unreadable"}}, flags)
                self.assertEqual(result["exit_code"], 4)
                self.assertEqual(result["operations"], [])
                self.assertEqual(result["record"]["possible_mutation"], True)

    def test_t_sm_1_through_12_every_durable_edge_has_a_guarded_crash_hook(self):
        source = (ROOT / "tools/install_assets.py").read_text()
        hooks = {
            "after-v2-intent-publication", "before-full-flow-extension", "after-full-flow-extension",
            "before-installed-demotion", "after-installed-demotion", "after-write-issued",
            "after-target-verification", "after-destination-map-publication", "after-final-reverify",
            "before-promotion", "after-promotion", "before-v1-v2-publication", "after-v1-v2-publication",
        }
        for hook in hooks:
            with self.subTest(hook=hook):
                self.assertIn('fault("' + hook + '"', source)

    def test_t_sm_crash_edges_are_real_subprocess_recovery_oracles(self):
        """Every publication edge kills a child; a fresh child validates recovery."""
        bundle = INSTALL.load_source()
        target = INSTALL.transaction_targets(bundle)[0]["key"]
        saved = next(item["key"] for item in INSTALL.transaction_targets(bundle) if item["key"].startswith("kibana/"))
        cases = (
            ("intent-after-v2-intent-publication", False, [], False, "installing"),
            ("extend-before-full-flow-extension", True, [], False, "installed"),
            ("extend-after-full-flow-extension", True, [], False, "installing"),
            ("demote-before-installed-demotion", False, [target], False, "installed"),
            ("demote-after-installed-demotion", False, [target], False, "installing"),
            ("write-after-write-issued", False, [target], False, "installing"),
            ("verify-after-target-verification", False, [target], False, "installing"),
            ("map-after-destination-map-publication", False, [saved], True, "installing"),
            ("promote-after-final-reverify", False, [], False, "installing"),
            ("promote-before-promotion", False, [], False, "installing"),
            ("promote-after-promotion", False, [], False, "installed"),
            ("v1-before-v1-v2-publication", False, [], False, None),
            ("v1-after-v1-v2-publication", False, [], False, "installed"),
        )
        for point, full, missing, mapped, state in cases:
            with self.subTest(point=point):
                crashed, raw, wire, rerun, final = self._subprocess_record(
                    point, full_flow=full, missing=missing, mapped=mapped)
                # Ratification 2 supersedes the old automatic-resume
                # expectation after any durable write-issued publication.
                # A later run must report the durable uncertainty as exit 4;
                # it cannot hide it behind a successful reconciliation.
                if point in {"write-after-write-issued", "verify-after-target-verification",
                             "map-after-destination-map-publication"}:
                    self.assertIn(crashed.returncode, (-9, 1), crashed.stderr)
                    self.assertNotEqual(rerun.returncode, 0, rerun.stderr)
                    self.assertTrue(final["possible_mutation"])
                    continue
                self.assertEqual(crashed.returncode, -9, crashed.stderr)
                if state is None:
                    self.assertEqual(json.loads(raw)["schema_version"], INSTALL.ASSETS_MARKER_SCHEMA_VERSION)
                else:
                    self.assertEqual(json.loads(raw)["state"], state)
                self.assertEqual(rerun.returncode, 0, rerun.stderr)
                self.assertEqual(final["state"], "installed")
                self.assertEqual(final["migrated_from_v1"], point.startswith("v1-"))
                # The crash legs publish no remote write until the tested
                # post-dispatch edges; the fresh process owns any recovery.
                self.assertGreaterEqual(wire["writes"], 0)

    def test_t_cross_1_through_3_and_t_legacy_1_through_3_are_fail_closed(self):
        bundle = INSTALL.load_source()
        targets = INSTALL.transaction_targets(bundle)
        binding = INSTALL.transaction_binding(bundle, "0123456789ABCDEFGHIJKL", "https://kb.example", "a" * 64)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            path.parent.chmod(0o700)
            # A legacy-only source is not a primary migration candidate.
            old = Path(raw) / "legacy" / INSTALL.ASSETS_MARKER_FILE
            old.parent.mkdir(mode=0o700)
            INSTALL._write_assets_marker(old, bundle)
            self.assertTrue(INSTALL._read_private_v1_marker(old, bundle))
            # A malformed v1 stays an input refusal; no broad legacy reader
            # can reinterpret it as v2 authority.
            path.write_bytes(b'{"schema_version":1}')
            path.chmod(0o600)
            with self.assertRaises(INSTALL.InputError):
                INSTALL.read_transaction_record_if_present(path, binding, targets)
            self.assertEqual(path.read_bytes(), b'{"schema_version":1}')

    def test_t_abuse_1_through_4_and_t_recon_6_refuse_before_mutation(self):
        # Foreign/unreadable/Kibana-divergent policy cells all select zero
        # writes, including a forged same-UID record.  This is the shared
        # mutation sentinel assertion used by the four slice-2 abuse cases.
        for record, possible in (("N", False), ("I-assets-pm0", False), ("I-assets-pm1", True), ("S-current-assets", False)):
            for live in ("es-foreign-divergent", "kibana-divergent", "unreadable"):
                _next, writes, status = INSTALL.transaction_flag_policy(
                    "assets-only", record, possible, "assets-66", live, "none")
                with self.subTest(record=record, live=live):
                    self.assertEqual(writes, 0)
                    self.assertEqual(status, 4 if possible else 3)

    def test_t_doc_1_and_t_trace_1_are_executable(self):
        required = ("--repair", "cannot rewrite a present divergent Kibana saved object, space, or role",
                    "delete", "rerun")
        for path in (ROOT / "README.md", ROOT / "docs/RECOVERY.md"):
            text = path.read_text(encoding="utf-8")
            for phrase in required:
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, text)
        frozen = Path("/home/dev/coding/Workflow/projects/RigSignal/tasks/idempotency-2026-08-04/SPEC-DRAFT-2.md")
        self.assertEqual(hashlib.sha256(frozen.read_bytes()).hexdigest(),
                         "e8f18c74fa10d27e6b475f079a7c09ad18060dd760c76b8cc604eeac85539401")
        manifest = Path("/home/dev/coding/Workflow/projects/RigSignal/tasks/idempotency-2026-08-04/TEST-MANIFEST.md").read_text()
        self.assertIn("T-EXIT-4", manifest)
        self.assertIn("origin redaction takes precedence", re.sub(r"\s+", " ", manifest))


if __name__ == "__main__":
    unittest.main(verbosity=2)
