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

from tools.tests.transaction_transport import HttpReply, ScriptedTransactionTransport, TargetScript


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

    def test_v2_rejects_unknown_fields_in_current_and_nested_shapes(self):
        cases = []
        installing = self.installing(); installing["migrated_from_v1"] = True; cases.append(installing)
        installed_source = self.installing()
        for key in installed_source["progress"]:
            installed_source["progress"][key] = "verified"
        installed = INSTALL.promote_transaction_record(installed_source, "2026-08-04T12:35:00Z")
        installed["migrated_from_v1"] = True; cases.append(installed)
        predecessor_source = INSTALL.new_installing_record(self.binding, self.targets, "2026-08-04T12:34:56Z")
        for key in predecessor_source["progress"]:
            predecessor_source["progress"][key] = "verified"
        nested = self.installing(predecessor=INSTALL.promote_transaction_record(
            predecessor_source, "2026-08-04T12:35:00Z"))
        nested["predecessor"]["unknown"] = True; cases.append(nested)
        for value in cases:
            with self.subTest(state=value["state"]):
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.validate_transaction_record(INSTALL.jcs(value), self.binding, self.targets)

    def test_destination_map_cannot_cross_space_or_type(self):
        submitted = next(item["key"] for item in self.targets if item["key"].startswith("kibana/"))
        _, object_type, object_id = INSTALL._v2_kibana_key_parts(submitted)
        bad_space = "kibana/other/" + INSTALL._v2_quote(object_type) + "/" + INSTALL._v2_quote(object_id)
        value = self.installing(destination_map=[{"submitted_key": submitted, "destination_key": bad_space}])
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
        # Both files are independently checked-in literals.  Constructing and
        # hashing this oracle deliberately calls no production body helper.
        fixture_root = ROOT / "tools/tests/fixtures"
        literal = json.loads((fixture_root / "default-bundle-meta-assets-66.json").read_text())
        golden = (fixture_root / "default-bundle-meta-golden.jcs").read_bytes().rstrip(b"\n")
        body = {"_meta": {"asset_set": literal, "bundle_version": "0.3.2",
                           "managed_by": "rigsignal-asset-bundle", "ownership_profile": "default",
                           "source_commit": "a80eaa01c831e0727bfabbd20155b829a1301792",
                           "timestamp": "2026-08-04T12:34:56Z"}, "template": {}}
        # The fixed fixture vocabulary is ASCII, so this RFC-8785-compatible
        # serialization is independent from the implementation under test.
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.assertEqual(len(literal), 66)
        self.assertEqual(encoded, golden)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         "c9690763ec182e150f4c68968d9fd1e14911704f8b264b9020356be928f5a5c1")
        changed = json.loads(encoded)
        changed["_meta"]["asset_set"][0]["digest"] = "0" * 64
        self.assertNotEqual(json.dumps(changed, sort_keys=True, separators=(",", ":")).encode("utf-8"), golden)


class SnapshotTests(unittest.TestCase):
    _BOUNDARY_CHILD = r'''
import importlib.util, os, sys
from pathlib import Path
root = Path(os.environ["RIGSIGNAL_TEST_ROOT"])
spec = importlib.util.spec_from_file_location("install_assets", root / "tools/install_assets.py")
install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
directory = Path(os.environ["RIGSIGNAL_TEST_DIR"])
if os.environ["RIGSIGNAL_TEST_OP"] == "atomic":
    install.atomic_write(directory, "state.json", b"new")
else:
    install.snapshot_bundle(directory / "bundle.tar", directory, parse=False)
'''

    def _boundary_child(self, directory, operation, crash_at):
        env = os.environ | {"RIGSIGNAL_TEST_ROOT": str(ROOT), "RIGSIGNAL_TEST_DIR": str(directory),
                            "RIGSIGNAL_TEST_OP": operation, "RIGSIGNAL_TEST_CRASH_AT": crash_at}
        return subprocess.run([sys.executable, "-c", textwrap.dedent(self._BOUNDARY_CHILD)], env=env,
                              text=True, capture_output=True, check=False)

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

    def test_f8_atomic_write_crashes_leave_only_complete_old_or_new_bytes(self):
        """F8: each atomic-write crash point is SIGKILL, never torn bytes."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            target = root / "state.json"
            target.write_bytes(b"old"); target.chmod(0o600)
            before_replace = self._boundary_child(root, "atomic", "atomic-write-after-temp-fsync:state.json")
            self.assertEqual(before_replace.returncode, -9, before_replace.stderr)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertTrue(any(path.name.startswith(".rigsignal-") for path in root.iterdir()))
            # A fresh writer recovers from the interrupted publication and
            # overwrites only the selected protected leaf.
            INSTALL.atomic_write(root, "state.json", b"new")
            self.assertEqual(target.read_bytes(), b"new")

            target.write_bytes(b"old")
            after_replace = self._boundary_child(root, "atomic", "atomic-write-after-replace:state.json")
            self.assertEqual(after_replace.returncode, -9, after_replace.stderr)
            self.assertEqual(target.read_bytes(), b"new")

    def test_f8_snapshot_crash_residue_is_owned_cleanup_or_foreign_refusal(self):
        """F8: archive residue is recoverable only when it is private and owned."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            (root / "bundle.tar").write_bytes(b"archive-bytes")
            crashed = self._boundary_child(root, "snapshot", "snapshot-after-copy-fsync")
            self.assertEqual(crashed.returncode, -9, crashed.stderr)
            residues = [path for path in root.iterdir() if path.name.startswith(".rigsignal-archive-")]
            self.assertEqual(len(residues), 1)
            self.assertEqual(residues[0].stat().st_mode & 0o777, 0o600)
            INSTALL.cleanup_snapshot_residues(root)
            self.assertFalse(residues[0].exists())
            fresh = INSTALL.snapshot_bundle(root / "bundle.tar", root, parse=False)
            self.assertEqual(fresh.path.read_bytes(), b"archive-bytes")
            fresh.close()
            foreign = root / ".rigsignal-foreign"
            foreign.symlink_to("bundle.tar")
            with self.assertRaises(INSTALL.InputError):
                INSTALL.cleanup_snapshot_residues(root)


class TransactionStateTests(RecordFixtures):
    def test_ambiguity_5_v1_migration_does_not_extend_the_v2_authority_schema(self):
        """v1 migration does not publish an off-authority required field."""
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
            self.assertNotIn("migrated_from_v1", migrated)
            self.assertEqual(writes, [])
            demoted = INSTALL.demote_installed_transaction(migrated, "2026-08-04T12:36:00Z")
            self.assertNotIn("migrated_from_v1", demoted)
            ordinary = self.installing()
            self.assertNotIn("migrated_from_v1", ordinary)

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
        # Templates use the ?create=true conditional; transforms are inherently
        # create-only via bare PUT and accept no such parameter (live-caught on
        # real 9.4.4 — the parameter is rejected outright).
        self.assertTrue(all(path.endswith("?create=true") for path in seen[:2]))
        self.assertFalse(seen[2].endswith("?create=true"))
        self.assertTrue("/_transform/" in seen[2] and "?" not in seen[2])

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

    def test_t_recon_5_pipeline_diagnostic_retains_only_timestamp_scalars(self):
        record = self.installing()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / INSTALL.ASSETS_MARKER_FILE
            INSTALL._transaction_diagnostic(path, record, target=record["targets"][0]["key"],
                                            nonce="nonce", detector="created<modified",
                                            observed={"created": 10, "modified": 11, "secret": "no"})
            evidence = json.loads((root / (INSTALL.ASSETS_MARKER_FILE + ".diagnostic.json")).read_text())
            self.assertEqual(evidence["observed"], "<redacted>")
            self.assertEqual(evidence["observed_created_millis"], 10)
            self.assertEqual(evidence["observed_modified_millis"], 11)

    def test_http_audit_log_is_private_regular_file(self):
        with tempfile.TemporaryDirectory() as raw:
            audit = Path(raw) / "audit.log"
            response = mock.MagicMock(); response.status = 200; response.read.return_value = b"{}"
            with mock.patch.dict(os.environ, {"RIGSIGNAL_HTTP_AUDIT_LOG": str(audit)}, clear=False), \
                 mock.patch.object(INSTALL.urllib.request, "urlopen", return_value=response):
                INSTALL.request_response("https://es", "/_cluster/health", "GET", "auth")
            self.assertEqual(audit.read_text(), "GET /_cluster/health\n")
            self.assertEqual(audit.stat().st_mode & 0o777, 0o600)

    def test_http_audit_log_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audit = root / "audit.log"
            audit.symlink_to(root / "target.log")
            with mock.patch.dict(os.environ, {"RIGSIGNAL_HTTP_AUDIT_LOG": str(audit)}, clear=False):
                with self.assertRaises(OSError):
                    INSTALL.request_response("https://es", "/_cluster/health", "GET", "auth")

    def test_all_test_environment_controls_are_inert_without_unsafe_cli_flag(self):
        program = """
import importlib.util, os, sys
from pathlib import Path
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('install_assets', root / 'tools/install_assets.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
bundle = module.load_source()
module.load_bundle = lambda _path: bundle
module.check_version_fence = lambda *_args: None
module.configure_https = lambda *_args: None
module.admin_authorization = lambda *_args: 'auth'
module._prepare_assets_marker_path = lambda *_args: Path('/tmp/marker')
module.asset_executor_exit_code = lambda *_args: 0
module.assets_only_install = lambda *_args, **_kwargs: print(
    'hooks=' + repr(sorted(key for key in os.environ if key.startswith('RIGSIGNAL_TEST_')))) or 'applied'
sys.argv = ['install_assets.py', '--bundle', 'fixture.tar', '--endpoint', 'https://localhost',
            '--ca-file', 'fixture.pem', '--kibana-endpoint', 'https://localhost',
            '--kibana-ca-file', 'fixture.pem', '--admin-credentials-file', 'admin.toml',
            '--agent-binary', 'agent', '--profile', 'user', '--assets-only']
sys.exit(module.main())
"""
        hooks = {
            "RIGSIGNAL_TEST_UNRESOLVED_ASSET": "1",
            "RIGSIGNAL_TEST_ILM_DELETE_PHASE": "1",
            "RIGSIGNAL_TEST_CLUSTER_HEALTH": "red",
            "RIGSIGNAL_TEST_TRANSFORM_META_RESTORE_REJECT": "1",
            "RIGSIGNAL_TEST_CRASH_AT": "point",
            "RIGSIGNAL_TEST_ROLLOVER_AT": "point",
            "RIGSIGNAL_TEST_EXTERNAL_WRITE": "1",
            "RIGSIGNAL_TEST_HALT_AT": "point",
            "RIGSIGNAL_TEST_PAUSE_AT": "point",
            "RIGSIGNAL_TEST_PAUSE_SENTINEL": "/tmp/unused",
        }
        result = subprocess.run([sys.executable, "-c", textwrap.dedent(program), str(ROOT)],
                                env=os.environ | hooks, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hooks=[]", result.stdout)
        self.assertNotIn("test hooks active:", result.stderr)

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
                 mock.patch.object(INSTALL, "prerequisites"), \
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
    FLAG_DATA_SHA256 = "6d75f47a167b4f5a6ad7510fd61844b1b78e3cfe249094b1f4b9634140d355f8"
    FLAG_ROW = re.compile(
        r"^\| (assets-only|full-flow) \| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \| "
        r"([^|]+?) \| ([^|]+?) \| ([^|]+?) \| ([0-9]+) \| ([0-9]+) \|$")

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
    def _prerequisite_transport(base, path, _method, _authorization, _data=None, _headers=None):
        """Minimal scripted HTTP boundary for the version/capability gate.

        Scenario-specific target state remains below this boundary for now;
        this keeps the capability gate real instead of bypassing it.
        """
        if path == "/":
            return 200, b'{"cluster_uuid":"0123456789ABCDEFGHIJKL","version":{"number":"9.4.4"}}'
        if path == "/api/status":
            return 200, b'{"version":{"number":"9.4.4"}}'
        return 200, b"{}"

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

    def _table_initial_record(self, label, binding, targets, flags=()):
        """Build each table record as a real protected v2 durable shape."""
        if label == "N":
            return None
        full = "full" in label
        prior = label.startswith("S-prior") or "with-valid-predecessor" in label
        # Direction is an input to the table row, not a shadow-policy
        # decision: provide a genuinely older predecessor for --upgrade and
        # a genuinely newer one for --allow-downgrade.
        prior_version = "0.3.3" if "allow-downgrade" in flags and "upgrade" not in flags else "0.3.1"
        prior_binding = {**binding, "bundle_version": prior_version, "source_commit": "b" * 40}
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
        (caller, record_label, ordinary, bundle_meta, flags, reconciliation,
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
        # The table's pm=true detector-positive/unreachable branch is a
        # transport read halt, not an ordinary absent target that the executor
        # may create.  Keep it on the wire so the main-routed runner exercises
        # the exit-4 boundary rather than consuming the row's write sentinel.
        if reconciliation.startswith("halt:"):
            states[ordinary_key] = "unreadable"
        if caller == "full-flow":
            states[INSTALL.BUNDLE_META_TARGET_KEY] = self._table_live_state(bundle_meta)
        expected_writes = int(expected_writes)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            path = root / INSTALL.ASSETS_MARKER_FILE
            initial = self._table_initial_record(record_label, binding, targets, self._table_flags(flags))
            if initial is not None:
                # Prior S intentionally has a different archive binding, so
                # it is published directly as the protected older record.
                INSTALL.atomic_write(path.parent, path.name, INSTALL.jcs(initial))
            initial_raw = path.read_bytes() if path.exists() else None

            scripts = {key: TargetScript(live_state=state) for key, state in states.items()
                       if state != "exact"}

            def mutation_sentinel(key):
                if len(fake.mutations) > expected_writes:
                    raise AssertionError("mutation sentinel tripped for " + key)

            # The frozen table's policy oracle must exercise the transport
            # implementation, not replace observation/write helpers.  This
            # retains endpoint envelopes, mutation tracking, conditional
            # paths, and post-write rereads under the real executor.
            fake = ScriptedTransactionTransport(
                INSTALL, bundle, scripts, on_mutation=mutation_sentinel,
                bundle_meta_timestamp="2026-08-04T12:34:56Z")

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
                 mock.patch.object(INSTALL, "_transaction_now", return_value="2026-08-04T12:34:56Z"), \
                 mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = INSTALL.main()
            final = (INSTALL.read_transaction_record_if_present(path, binding, targets)
                     if path.exists() and not record_label.startswith("S-prior") else
                     (json.loads(path.read_text()) if path.exists() else None))
            # Step 11 is first in the engine's ordered full-flow spec.  A
            # later ordinary refusal may therefore leave its write-issued
            # record behind, but it may never run before a bundle-meta write.
            if INSTALL.BUNDLE_META_TARGET_KEY in fake.mutations:
                self.assertEqual(fake.mutations[0], INSTALL.BUNDLE_META_TARGET_KEY)
            if caller == "full-flow" and expected_writes == 2:
                self.assertEqual(fake.mutations[0], INSTALL.BUNDLE_META_TARGET_KEY)
            actual_label = self._table_record_label(final, record_label, initial_raw)
            # An unchanged current S is still the successful canonical S
            # outcome in ordinary table rows.  Retain its input label only
            # where the oracle explicitly requires byte-for-byte refusal
            # preservation (for example a full-flow barrier rejection).
            if actual_label == record_label and expected_record.startswith("S["):
                actual_label = expected_record
            return (actual_label, len(fake.mutations), exit_code,
                    final, {}, expected_record, expected_writes, int(expected_exit))

    _CHILD_EXECUTOR = r'''
import importlib.util, json, os, sys
from pathlib import Path
from unittest import mock
root = Path(os.environ["RIGSIGNAL_TEST_ROOT"])
if str(root) not in sys.path: sys.path.insert(0, str(root))
from tools.tests.transaction_transport import ScriptedTransactionTransport, TargetScript
spec = importlib.util.spec_from_file_location("install_assets", root / "tools/install_assets.py")
install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
bundle = install.load_source()
binding = install.transaction_binding(bundle, "0123456789ABCDEFGHIJKL", "https://kb.example", "a" * 64)
record_path = Path(os.environ["RIGSIGNAL_TEST_RECORD"])
state_path = Path(os.environ["RIGSIGNAL_TEST_WIRE"])
state = json.loads(state_path.read_text())
scripts = {}
for key in install.transaction_targets(bundle):
    target = key["key"]
    scripts[target] = TargetScript(
        live_state="exact" if target in state["exact"] else "absent",
        destination_id="remapped-id" if target == state.get("mapped_key") else None)
def persist_mutation(key):
    state["writes"] += 1
    if key not in state["exact"]: state["exact"].append(key)
    state_path.write_text(json.dumps(state, sort_keys=True))
fake = ScriptedTransactionTransport(install, bundle, scripts, on_mutation=persist_mutation,
                                    bundle_meta_timestamp="2026-08-04T12:34:56Z")
with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(record_path.parent / "state")}, clear=False), \
     mock.patch.object(install.urllib.request, "urlopen", side_effect=fake.urlopen), \
     mock.patch.object(install, "_transaction_now", return_value="2026-08-04T12:34:56Z"):
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

            def observe(_es, _kb, _auth, spec, _bundle, _adapter, _record=None, **_kwargs):
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
                 mock.patch.object(INSTALL, "request_response", side_effect=self._prerequisite_transport), \
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

    def test_f4_recovered_remap_persists_before_dependent_reference_create(self):
        """F4: 404/resolve recovery is durable authority for a later reference."""
        bundle = INSTALL.load_source()
        specs = INSTALL._transaction_specs(bundle)
        by_key = {key: (key, asset, saved) for key, asset, saved in specs}
        source = dependent = None
        for key, asset, saved in specs:
            if saved is None:
                continue
            space = INSTALL.dashboard_target_space(asset)
            for ref in saved.get("references", []):
                ref_key = ("kibana/" + INSTALL._v2_quote(space) + "/" + INSTALL._v2_quote(ref["type"]) +
                           "/" + INSTALL._v2_quote(ref["id"]))
                if ref_key in by_key and ref_key.encode() < key.encode():
                    source, dependent = ref_key, key
                    break
            if source is not None:
                break
        self.assertIsNotNone(source, "fixture needs an ordered saved-object dependency")
        targets = INSTALL.transaction_targets(bundle)
        binding = INSTALL.transaction_binding(bundle, "0123456789ABCDEFGHIJKL", "https://kb.example",
                                              INSTALL.bundle_snapshot_digest(bundle))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); root.chmod(0o700)
            record_path = root / INSTALL.ASSETS_MARKER_FILE
            record = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
            INSTALL.write_transaction_record(record_path, record, binding, targets)
            fake = ScriptedTransactionTransport(INSTALL, bundle)
            remapped = "recovered-remap"
            fake._scripts[source] = TargetScript(
                get=[HttpReply(404, {"error": "submitted missing"}),
                     HttpReply(200, fake._desired_body(source))],
                resolve=HttpReply(200, {"destinationId": remapped}), destination_id=remapped)
            # Every later dependent must be recreated with the resolved
            # identity.  Leaving a second dependent "exact" under the
            # submitted ID would correctly fail the final full-set reread.
            for key, asset, saved in specs:
                if saved is None or key.encode() <= source.encode():
                    continue
                space = INSTALL.dashboard_target_space(asset)
                if any(("kibana/" + INSTALL._v2_quote(space) + "/" +
                        INSTALL._v2_quote(ref["type"]) + "/" + INSTALL._v2_quote(ref["id"])) == source
                       for ref in saved.get("references", [])):
                    fake._scripts[key] = TargetScript(live_state="absent")
            with mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen), \
                 mock.patch.object(INSTALL, "_transaction_now", return_value="2026-08-04T12:34:56Z"):
                self.assertEqual(INSTALL.run_default_asset_transaction(
                    bundle, "https://es", "https://kb", "auth", record_path, binding,
                    defer_step_11=True, lock=mock.Mock()), "deferred")
            final = INSTALL.read_transaction_record_if_present(record_path, binding, targets)
            mapped_key = source.rsplit("/", 1)[0] + "/" + INSTALL._v2_quote(remapped)
            self.assertIn({"submitted_key": source, "destination_key": mapped_key}, final["destination_map"])
            self.assertNotIn(source, fake.mutations, "resolve-exact must not create the submitted id")
            request = next(call for call in fake.calls if call.path.endswith("/" + dependent.rsplit("/", 1)[1])
                           and call.method == "POST")
            references = json.loads(request.data)["references"]
            source_type = INSTALL._v2_kibana_key_parts(source)[1]
            self.assertTrue(any(item["type"] == source_type and item["id"] == remapped for item in references))

    def test_f5_create_conflicts_reread_exact_or_halt_per_class(self):
        """F5: only documented create conflicts receive one immediate reread."""
        bundle = INSTALL.load_source()
        choices = {
            "template": next(spec for spec in INSTALL._transaction_specs(bundle)
                             if spec[1] is not None and spec[1].kind == "component_templates"),
            "transform": next(spec for spec in INSTALL._transaction_specs(bundle)
                              if spec[1] is not None and spec[1].kind == "transforms"),
            "space": next(spec for spec in INSTALL._transaction_specs(bundle)
                          if spec[1] is not None and spec[1].kind == "kibana_spaces"),
            "kibana-role": next(spec for spec in INSTALL._transaction_specs(bundle)
                                if spec[1] is not None and spec[1].kind == "kibana_roles"),
        }
        targets = INSTALL.transaction_targets(bundle)
        binding = INSTALL.transaction_binding(bundle, "0123456789ABCDEFGHIJKL", "https://kb.example",
                                              INSTALL.bundle_snapshot_digest(bundle))
        for label, spec in choices.items():
            for exact_after_race in (True, False):
                with self.subTest(kind=label, exact=exact_after_race), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw); root.chmod(0o700)
                    path = root / INSTALL.ASSETS_MARKER_FILE
                    record = INSTALL.new_installing_record(binding, targets, "2026-08-04T12:34:56Z")
                    INSTALL.write_transaction_record(path, record, binding, targets)
                    fake = ScriptedTransactionTransport(INSTALL, bundle)
                    key = spec[0]
                    desired = fake._desired_body(key)
                    rerun_body = desired if exact_after_race else {"_raced": "divergent"}
                    kwargs = {"get": [HttpReply(404, {"error": "absent"}), HttpReply(200, rerun_body)]}
                    if label == "template":
                        kwargs["conditional"] = {"create": HttpReply(400, {"error": "exists"})}
                    elif label == "transform":
                        kwargs["put"] = HttpReply(409, {"error": "exists"})
                    elif label == "space":
                        kwargs["put"] = HttpReply(409, {"error": "exists"})
                    else:
                        kwargs["conditional"] = {"createOnly": HttpReply(409, {"error": "exists"})}
                    fake._scripts[key] = TargetScript(**kwargs)
                    with mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen), \
                         mock.patch.object(INSTALL, "_transaction_now", return_value="2026-08-04T12:34:56Z"):
                        if exact_after_race:
                            self.assertEqual(INSTALL.run_default_asset_transaction(
                                bundle, "https://es", "https://kb", "auth", path, binding, lock=mock.Mock()), "applied")
                        else:
                            with self.assertRaises(INSTALL.AssetTransactionHalt):
                                INSTALL.run_default_asset_transaction(bundle, "https://es", "https://kb", "auth", path, binding,
                                                                      lock=mock.Mock())
                    final = INSTALL.read_transaction_record_if_present(path, binding, targets)
                    self.assertEqual(final["state"], "installed" if exact_after_race else "installing")
                    if exact_after_race:
                        self.assertNotIn("possible_mutation", final)
                    else:
                        self.assertTrue(final["possible_mutation"])
                    self.assertEqual(fake.mutations, [], "a rejected raced create is not a remote write")

    def test_f6_upgrade_and_downgrade_accept_valid_predecessors_with_changed_inventory_and_remap(self):
        """F6: transition authority is the predecessor's own target binding.

        The fixtures intentionally remove one old target and use a different
        saved-object destination map.  This catches the tempting but invalid
        comparison of a predecessor against the current inventory/remap.
        """
        bundle = INSTALL.load_source()
        targets = INSTALL.transaction_targets(bundle)
        saved = next(item["key"] for item in targets if item["key"].startswith("kibana/"))
        old_saved = saved
        for flag, old_version in (("upgrade", "0.3.1"), ("allow_downgrade", "0.3.3")):
            with self.subTest(direction=flag), tempfile.TemporaryDirectory() as raw:
                root = Path(raw); root.chmod(0o700)
                path = root / INSTALL.ASSETS_MARKER_FILE
                current = INSTALL.transaction_binding(bundle, "0123456789ABCDEFGHIJKL", "https://kb.example",
                                                      INSTALL.bundle_snapshot_digest(bundle))
                prior_binding = {**current, "bundle_version": old_version, "source_commit": "b" * 40,
                                 "bundle_sha256": "c" * 64}
                predecessor = INSTALL.new_installing_record(prior_binding, targets, "2026-08-04T12:34:56Z")
                predecessor["destination_map"] = [{"submitted_key": old_saved,
                    "destination_key": old_saved.rsplit("/", 1)[0] + "/old-remap"}]
                for key in predecessor["progress"]:
                    predecessor["progress"][key] = "verified"
                predecessor = INSTALL.promote_transaction_record(predecessor, "2026-08-04T12:35:00Z")
                # A prior release has the same fixed cardinality but a
                # different physical inventory.  It is deliberately not
                # compared to the current target list by predecessor parsing.
                predecessor["targets"][-1] = {"key": "es/component-template/old-only", "digest": "d" * 64}
                predecessor["targets"].sort(key=lambda item: item["key"].encode())
                predecessor["asset_set_sha256"] = INSTALL._target_digest(predecessor["targets"])
                predecessor["verified_target_set_sha256"] = INSTALL._target_digest(predecessor["targets"])
                active = INSTALL.new_installing_record(current, targets, "2026-08-04T12:36:00Z")
                active["predecessor"] = predecessor
                active["destination_map"] = [{"submitted_key": saved,
                    "destination_key": saved.rsplit("/", 1)[0] + "/new-remap"}]
                INSTALL.write_transaction_record(path, active, current, targets)
                fake = ScriptedTransactionTransport(INSTALL, bundle)
                fake._scripts[saved] = TargetScript(live_state="exact", destination_id="new-remap")
                with mock.patch.object(INSTALL.urllib.request, "urlopen", side_effect=fake.urlopen), \
                     mock.patch.object(INSTALL, "_transaction_now", return_value="2026-08-04T12:36:00Z"):
                    self.assertEqual(INSTALL.run_default_asset_transaction(
                        bundle, "https://es", "https://kb", "auth", path, current,
                        lock=mock.Mock(), **{flag: True}), "applied")
                final = INSTALL.read_transaction_record_if_present(path, current, targets)
                self.assertEqual(final["state"], "installed")
                self.assertEqual(final["destination_map"], active["destination_map"])
                self.assertEqual(fake.mutations, [])

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

        # A possible-mutation record is classified at the failure boundary;
        # it must first reach the executor so an able rerun can reconcile and
        # promote.  The mapper remains the authority for that halt.
        with mock.patch.object(INSTALL, "asset_executor_exit_code", wraps=INSTALL.asset_executor_exit_code) as mapped:
            self._run_scenario("assets-only")
            key = INSTALL.transaction_targets(bundle)[0]["key"]
            self._run_scenario("assets-only", "I-assets-pm1", {"states": {key: "unreadable"}})
        self.assertEqual([call.args[0] for call in mapped.call_args_list], ["success", "halt"])
        source = (ROOT / "tools/install_assets.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count('asset_executor_exit_code("halt",'), 2)

    def test_t_flag_1_through_4_generated_5992_row_oracle(self):
        """T-FLAG-1..4: the checked-in corrected table is byte-pinned."""
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
        self.assertEqual(len(rows), 5992)
        self.assertEqual(hashlib.sha256("\n".join(
            line for line in raw.decode("utf-8").splitlines()
            if line.startswith("| ") and not line.startswith("| Caller")
        ).encode("utf-8")).hexdigest(), self.FLAG_DATA_SHA256)
        self.assertTrue(any(row[5] == "all-reobserved-exact-or-absent-creatable" for row in rows))

    def test_t_flag_1_through_4_all_rows_main_routed_sharded(self):
        """Execute every pinned policy row through main(), not a shadow planner.

        The default shard is the complete 5,824-row set.  CI can split it
        deterministically with ``RIGSIGNAL_FLAG_SHARDS`` and
        ``RIGSIGNAL_FLAG_SHARD``; each row has exactly one SHA-256 shard.
        """
        rows = self._flag_rows()
        self.assertEqual(len(rows), 5992)
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
                # Frozen recovery oracles T-HASH-5, T-SM-7/8/9, T-DASH-3,
                # and T-GATE-3 require a fresh process to re-observe and
                # promote once every uncertain target verifies.
                if point in {"write-after-write-issued", "verify-after-target-verification",
                             "map-after-destination-map-publication"}:
                    self.assertEqual(crashed.returncode, -9, crashed.stderr)
                    self.assertEqual(wire["writes"], 0 if point == "write-after-write-issued" else 1)
                    self.assertEqual(rerun.returncode, 0, rerun.stderr)
                    self.assertEqual(final["state"], "installed")
                    continue
                self.assertEqual(crashed.returncode, -9, crashed.stderr)
                if state is None:
                    self.assertEqual(json.loads(raw)["schema_version"], INSTALL.ASSETS_MARKER_SCHEMA_VERSION)
                else:
                    self.assertEqual(json.loads(raw)["state"], state)
                self.assertEqual(rerun.returncode, 0, rerun.stderr)
                self.assertEqual(final["state"], "installed")
                self.assertNotIn("migrated_from_v1", final)
                # The crash legs publish no remote write until the tested
                # post-dispatch edges; the fresh process owns any recovery.
                self.assertEqual(wire["writes"], 0)

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
        # The mutation sentinel sits on the invocation wire, rather than on
        # the retired policy planner: each refusal has to happen before the
        # actual executor asks its transport to mutate.
        for record, possible in (("N", False), ("I-assets-pm0", False), ("I-assets-pm1", True), ("S-current-assets", False)):
            for live in ("es-foreign-divergent", "kibana-divergent", "unreadable"):
                key = INSTALL.transaction_targets(INSTALL.load_source())[0]["key"]
                state = "divergent" if live == "es-foreign-divergent" else live
                if live == "kibana-divergent":
                    key = next(item["key"] for item in INSTALL.transaction_targets(INSTALL.load_source())
                               if item["key"].startswith("kibana/"))
                    state = "divergent"
                result = self._run_scenario("assets-only", record, {"states": {key: state}})
                with self.subTest(record=record, live=live):
                    self.assertEqual(result["operations"], [])
                    self.assertEqual(result["exit_code"], 4 if possible else 3)

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
