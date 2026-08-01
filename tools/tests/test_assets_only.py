#!/usr/bin/env python3
"""End-to-end ownership tests for the default-profile assets-only flow."""

import importlib.util
import io
import json
import stat
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from contextlib import redirect_stdout


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("install_assets", ROOT / "tools/install_assets.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class AssetTransport:
    """A narrow in-memory wire transport whose mutation sentinel is fail-closed."""

    def __init__(self, bundle, *, fail_on_nth_mutation=None):
        self.bundle = bundle
        self.es = {}
        self.kibana = {}
        self.calls = []
        self.fail_mutations = False
        self.fail_on_nth_mutation = fail_on_nth_mutation
        self.mutation_attempts = 0

    def _asset_for_path(self, path):
        for asset in self.bundle.assets:
            if asset.kind in INSTALL._ES_ASSET_KINDS and INSTALL.es_path(asset) == path:
                return asset
            if asset.kind not in INSTALL._ES_ASSET_KINDS and asset.kind != "dashboard" and INSTALL.kibana_path(asset) == path:
                return asset
        return None

    def request(self, base, path, method, _authorization, data=None, headers=None):
        self.calls.append((method, path))
        if method != "GET" and self.fail_mutations:
            raise AssertionError("mutation sentinel tripped: " + method + " " + path)
        if method != "GET":
            self.mutation_attempts += 1
            if self.fail_on_nth_mutation == self.mutation_attempts:
                raise INSTALL.RequestFailure(503, "deterministic mutation failure")
        if method == "GET":
            if "/api/saved_objects/" in path:
                if path not in self.kibana:
                    raise INSTALL.RequestFailure(404, "absent")
                return json.dumps(self.kibana[path]).encode()
            asset = self._asset_for_path(path)
            store = self.es if base == "https://es" else self.kibana
            if asset is None or path not in store:
                raise INSTALL.RequestFailure(404, "absent")
            body = store[path]
            if asset.kind == "component_templates":
                body = {"component_templates": [{"name": asset.name, "component_template": body}]}
            elif asset.kind == "index_templates":
                body = {"index_templates": [{"name": asset.name, "index_template": body}]}
            elif asset.kind == "security_roles":
                body = {asset.name: body}
            return json.dumps(body).encode()
        if method == "POST" and "saved_objects/_import" in path:
            asset = next(item for item in self.bundle.assets
                         if item.kind == "dashboard" and item.name.encode() in (data or b""))
            results = []
            for object_type, object_id in INSTALL.dashboard_objects(asset.data):
                self.kibana[INSTALL.dashboard_object_path(asset, object_type, object_id)] = {"attributes": {}}
                results.append({"type": object_type, "id": object_id})
            return json.dumps({"success": True, "successCount": len(results),
                               "successResults": results}).encode()
        if method == "POST" and path == "/api/spaces/space":
            asset = next(item for item in self.bundle.assets if item.kind == "kibana_spaces")
            self.kibana[INSTALL.kibana_path(asset)] = json.loads(data)
            return b"{}"
        asset = self._asset_for_path(path)
        if asset is None:
            # Transform updates carry an /_update suffix and retain the
            # preflight identity in the path.
            asset = self._asset_for_path(path.removesuffix("/_update"))
            path = path.removesuffix("/_update")
        if asset is None:
            raise AssertionError("unexpected mutation " + method + " " + path)
        body = json.loads(data)
        if asset.kind == "security_roles" and "_meta" in body:
            # Elasticsearch's role endpoint only persists caller metadata in
            # ``metadata``.  Rejecting the invalid shape makes this transport
            # expose the real API bug rather than accepting a mock-only body.
            raise AssertionError("security role PUT rejects _meta; use metadata")
        store = self.es if asset.kind in INSTALL._ES_ASSET_KINDS else self.kibana
        store[path] = body
        return b"{}"

    def request_response(self, base, path, method, authorization, data=None, headers=None):
        return 200, self.request(base, path, method, authorization, data, headers)

    @property
    def mutations(self):
        return [(method, path) for method, path in self.calls if method != "GET"]


class AssetsOnlyInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = INSTALL.load_source()
        assert len(cls.bundle.assets) == 55

    def install(self, transport, marker, **modes):
        with mock.patch.object(INSTALL, "request", transport.request), \
             mock.patch.object(INSTALL, "request_response", transport.request_response), \
             redirect_stdout(io.StringIO()):
            return INSTALL.assets_only_install(self.bundle, "https://es", "https://kb", "admin", marker,
                                               **modes)

    @staticmethod
    def main_args(marker):
        return SimpleNamespace(
            bundle=Path("fixture.tar.gz"), endpoint="https://es", ca_file=Path("fixture-ca.pem"),
            kibana_endpoint="https://kb", kibana_ca_file=Path("fixture-kb-ca.pem"),
            admin_credentials_file=Path("admin.toml"), agent_binary=Path("agent"), profile="user",
            assets_only=True, assets_marker=marker, repair=False, upgrade=False, allow_downgrade=False,
            enrollment_root=None, adopt_existing_w1_stream=False, ownership_profile=None, rollback=None,
            predecessor_manifest=None, dry_run=False, unsafe_test_injection=False,
        )

    def main_assets(self, args, transport=None, *, load_bundle=None):
        patches = [mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args),
                   mock.patch.object(INSTALL, "load_bundle", return_value=load_bundle or self.bundle),
                   mock.patch.object(INSTALL, "role_body", return_value={}),
                   mock.patch.object(INSTALL, "check_version_fence"),
                   mock.patch.object(INSTALL, "configure_https"),
                   mock.patch.object(INSTALL, "admin_authorization", return_value="admin")]
        if transport is not None:
            patches.extend((mock.patch.object(INSTALL, "request", transport.request),
                            mock.patch.object(INSTALL, "request_response", transport.request_response)))
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            return INSTALL.main()

    def test_main_assets_exit_protocol_is_tracker_driven(self):
        """Break-one-thing: removing mutation_request makes the N=2 leg return 3."""
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            stderr = io.StringIO()
            with redirect_stderr(stderr), mock.patch.object(
                    INSTALL, "load_bundle", side_effect=INSTALL.InputError("bad bundle")):
                args = self.main_args(marker)
                with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args):
                    self.assertEqual(INSTALL.main(), 2)
            self.assertEqual(stderr.getvalue(), "install failed: bundle validation:\n"
                             "RIGSIGNAL_FAILURE_SITE preflight\n")

            transport = AssetTransport(self.bundle)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(self.main_assets(self.main_args(marker), transport), 0)
            self.assertEqual(stderr.getvalue(), "")

            conflict = AssetTransport(self.bundle)
            target = next(asset for asset in self.bundle.assets if asset.kind in INSTALL._ES_ASSET_KINDS)
            conflict.es[INSTALL.es_path(target)] = {"_meta": {"managed_by": "foreign"}}
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(self.main_assets(self.main_args(marker), conflict), 3)
            self.assertIn("install refused: asset_conflict_unproven\n", stderr.getvalue())
            self.assertIn("RIGSIGNAL_FAILURE_SITE asset_apply\n", stderr.getvalue())
            self.assertEqual(conflict.mutations, [])

            partial_marker = Path(raw) / "partial-marker.json"
            partial = AssetTransport(self.bundle, fail_on_nth_mutation=2)
            stderr = io.StringIO()
            with redirect_stderr(stderr), mock.patch.object(INSTALL, "rollback_transaction") as rollback:
                self.assertEqual(self.main_assets(self.main_args(partial_marker), partial), 4)
            self.assertIn("install failed: assets-only:\n", stderr.getvalue())
            self.assertIn("RIGSIGNAL_FAILURE_SITE asset_apply\n", stderr.getvalue())
            self.assertEqual(len(partial.mutations), 2)
            self.assertFalse(partial_marker.exists())
            rollback.assert_not_called()

            # A malformed remote GET is not a local-input error.  Refuse
            # fail-closed before the first write with the safe remote code.
            remote_refusal = io.StringIO()
            with redirect_stderr(remote_refusal), \
                 mock.patch.object(INSTALL, "request", return_value=b"[]"):
                self.assertEqual(self.main_assets(self.main_args(Path(raw) / "remote-marker")), 3)
            self.assertEqual(remote_refusal.getvalue(), "install failed: assets-only:\n"
                             "RIGSIGNAL_FAILURE_SITE asset_apply\n")

    def test_all_55_absent_creates_everything_and_writes_verified_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            transport = AssetTransport(self.bundle)
            self.assertEqual(self.install(transport, marker), "applied")
            self.assertEqual(len(transport.mutations), 55)
            for asset in self.bundle.assets:
                if asset.kind in INSTALL._ES_ASSET_KINDS:
                    self.assertIn(INSTALL.es_path(asset), transport.es)
                    proof_key = "metadata" if asset.kind == "security_roles" else "_meta"
                    self.assertEqual(transport.es[INSTALL.es_path(asset)][proof_key]["managed_by"],
                                     INSTALL.RIGSIGNAL_MANAGED_BY)
                elif asset.kind == "dashboard":
                    object_type, object_id = INSTALL.dashboard_objects(asset.data)[0]
                    self.assertIn(INSTALL.dashboard_object_path(asset, object_type, object_id), transport.kibana)
                else:
                    self.assertIn(INSTALL.kibana_path(asset), transport.kibana)
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            recorded = json.loads(marker.read_text())
            self.assertEqual(recorded["schema_version"], INSTALL.ASSETS_MARKER_SCHEMA_VERSION)
            self.assertEqual(len(recorded["identities"]), 55)
            self.assertEqual(INSTALL._read_assets_marker(marker, self.bundle),
                             {(item["kind"], item["name"]) for item in recorded["identities"]})

    def test_owned_same_version_is_a_clean_zero_put_noop(self):
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            transport = AssetTransport(self.bundle)
            self.assertEqual(self.install(transport, marker), "applied")
            transport.calls.clear()
            transport.fail_mutations = True  # sentinel covers every PUT/POST/DELETE.
            self.assertEqual(self.install(transport, marker), "noop")
            self.assertEqual(transport.mutations, [])
            transport.calls.clear()
            self.assertEqual(self.install(transport, marker, upgrade=True), "noop")
            self.assertEqual(transport.mutations, [])

    def test_unproven_present_object_refuses_before_any_mutation(self):
        es_target = next(asset for asset in self.bundle.assets if asset.kind in INSTALL._ES_ASSET_KINDS)
        role_target = next(asset for asset in self.bundle.assets if asset.kind == "security_roles")
        kibana_target = next(asset for asset in self.bundle.assets if asset.kind == "kibana_spaces")
        for label, target, body in (
                ("foreign stamp", es_target, {"_meta": {"managed_by": "foreign"}}),
                ("missing stamp", es_target, {}),
                ("role _meta is not ownership proof", role_target,
                 {"_meta": {"managed_by": INSTALL.RIGSIGNAL_MANAGED_BY}}),
                ("missing local marker", kibana_target, {})):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
                transport = AssetTransport(self.bundle)
                store = transport.es if target.kind in INSTALL._ES_ASSET_KINDS else transport.kibana
                path = INSTALL.es_path(target) if target.kind in INSTALL._ES_ASSET_KINDS else INSTALL.kibana_path(target)
                store[path] = body
                transport.fail_mutations = True  # sentinel makes a partial apply an immediate test failure.
                with self.assertRaisesRegex(INSTALL.AssetConflictUnproven, "asset_conflict_unproven"):
                    self.install(transport, marker)
                self.assertEqual(transport.mutations, [])

    def test_repair_reapplies_owned_but_still_refuses_an_unproven_object(self):
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            transport = AssetTransport(self.bundle)
            self.assertEqual(self.install(transport, marker), "applied")
            transport.calls.clear()
            self.assertEqual(self.install(transport, marker, repair=True), "applied")
            self.assertEqual(len(transport.mutations), 55)
            transport.calls.clear()
            target = next(asset for asset in self.bundle.assets if asset.kind in INSTALL._ES_ASSET_KINDS)
            transport.es[INSTALL.es_path(target)] = {"_meta": {"managed_by": "foreign"}}
            transport.fail_mutations = True
            with self.assertRaisesRegex(INSTALL.AssetConflictUnproven, "asset_conflict_unproven"):
                self.install(transport, marker, repair=True)
            self.assertEqual(transport.mutations, [])

    def test_version_transition_applies_all_owned_objects_and_keeps_unproven_fence(self):
        old_bundle = INSTALL.Bundle("0.3.0", self.bundle.source_commit, self.bundle.assets, self.bundle.files)
        new_bundle = INSTALL.Bundle("0.3.1", self.bundle.source_commit, self.bundle.assets, self.bundle.files)
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            transport = AssetTransport(self.bundle)
            with mock.patch.object(INSTALL, "request", transport.request), \
                 mock.patch.object(INSTALL, "request_response", transport.request_response):
                self.assertEqual(INSTALL.assets_only_install(old_bundle, "https://es", "https://kb", "admin", marker),
                                 "applied")
                transport.calls.clear()
                self.assertEqual(INSTALL.assets_only_install(new_bundle, "https://es", "https://kb", "admin", marker,
                                                             upgrade=True), "applied")
            self.assertEqual(len(transport.mutations), 55)
            self.assertEqual(json.loads(marker.read_text())["bundle_version"], "0.3.1")

            # The reverse transition has the same explicit authorization
            # requirement and re-applies every proven-owned target.
            transport.calls.clear()
            with mock.patch.object(INSTALL, "request", transport.request), \
                 mock.patch.object(INSTALL, "request_response", transport.request_response):
                self.assertEqual(INSTALL.assets_only_install(old_bundle, "https://es", "https://kb", "admin", marker,
                                                             allow_downgrade=True), "applied")
            self.assertEqual(len(transport.mutations), 55)
            self.assertEqual(json.loads(marker.read_text())["bundle_version"], "0.3.0")

            # A transition flag never blesses a present but unproven target.
            transport.calls.clear()
            target = next(asset for asset in self.bundle.assets if asset.kind in INSTALL._ES_ASSET_KINDS)
            transport.es[INSTALL.es_path(target)] = {"_meta": {"managed_by": "foreign"}}
            transport.fail_mutations = True
            with mock.patch.object(INSTALL, "request", transport.request), \
                 mock.patch.object(INSTALL, "request_response", transport.request_response), \
                 self.assertRaisesRegex(INSTALL.AssetConflictUnproven, "asset_conflict_unproven"):
                INSTALL.assets_only_install(new_bundle, "https://es", "https://kb", "admin", marker,
                                            upgrade=True)
            self.assertEqual(transport.mutations, [])

    def test_version_delta_requires_its_matching_transition_flag(self):
        old_bundle = INSTALL.Bundle("0.3.0", self.bundle.source_commit, self.bundle.assets, self.bundle.files)
        new_bundle = INSTALL.Bundle("0.3.1", self.bundle.source_commit, self.bundle.assets, self.bundle.files)
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            transport = AssetTransport(self.bundle)
            with mock.patch.object(INSTALL, "request", transport.request), \
                 mock.patch.object(INSTALL, "request_response", transport.request_response):
                self.assertEqual(INSTALL.assets_only_install(old_bundle, "https://es", "https://kb", "admin", marker),
                                 "applied")
                transport.calls.clear()
                transport.fail_mutations = True
                with self.assertRaisesRegex(INSTALL.ProvisionError, "assets_marker_upgrade_required"):
                    INSTALL.assets_only_install(new_bundle, "https://es", "https://kb", "admin", marker)
            self.assertEqual(transport.mutations, [])
            transport.calls.clear()
            transport.fail_mutations = False
            with mock.patch.object(INSTALL, "request", transport.request), \
                 mock.patch.object(INSTALL, "request_response", transport.request_response):
                self.assertEqual(INSTALL.assets_only_install(new_bundle, "https://es", "https://kb", "admin", marker,
                                                             upgrade=True), "applied")
                transport.calls.clear()
                transport.fail_mutations = True
                with self.assertRaisesRegex(INSTALL.ProvisionError, "assets_marker_downgrade_required"):
                    INSTALL.assets_only_install(old_bundle, "https://es", "https://kb", "admin", marker)
            self.assertEqual(transport.mutations, [])

    def test_assets_only_fleet_coexist_refuses_before_planning_or_mutation(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                args = SimpleNamespace(
                    bundle=Path("unused"), endpoint="https://es", ca_file=Path("unused"),
                    kibana_endpoint="https://kb", kibana_ca_file=Path("unused"),
                    admin_credentials_file=Path("unused"), agent_binary=Path("unused"), profile="user",
                    assets_only=True, assets_marker=None, repair=False, upgrade=False, allow_downgrade=False,
                    enrollment_root=None, adopt_existing_w1_stream=False, ownership_profile="fleet-coexist",
                    rollback=None, predecessor_manifest=None, dry_run=dry_run, unsafe_test_injection=False,
                )
                stderr = io.StringIO()
                stdout = io.StringIO()
                with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
                     mock.patch.object(INSTALL, "load_bundle", return_value=self.bundle), \
                     mock.patch.object(INSTALL, "role_body", return_value={}), \
                     mock.patch.object(INSTALL, "check_version_fence") as version_fence, \
                     mock.patch.object(INSTALL, "assets_only_install") as install, \
                     redirect_stderr(stderr), redirect_stdout(stdout):
                    self.assertEqual(INSTALL.main(), 3)
                self.assertEqual(stderr.getvalue(), "install refused: fleet_coexist_requires_full_flow\n"
                                 "RIGSIGNAL_FAILURE_SITE preflight\n")
                self.assertEqual(stdout.getvalue(), "")
                version_fence.assert_not_called()
                install.assert_not_called()

    def test_full_default_flow_ownership_gate_refuses_before_enrollment_root_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"
            marker = Path(raw) / INSTALL.ASSETS_MARKER_FILE
            args = SimpleNamespace(
                bundle=Path("unused"), endpoint="https://es", ca_file=Path("unused"),
                kibana_endpoint="https://kb", kibana_ca_file=Path("unused"),
                admin_credentials_file=Path("unused"), agent_binary=Path("unused"), profile="user",
                assets_only=False, assets_marker=marker, repair=False, upgrade=False, allow_downgrade=False,
                enrollment_root=root, adopt_existing_w1_stream=False, ownership_profile=None,
                rollback=None, predecessor_manifest=None, dry_run=False, unsafe_test_injection=False,
            )
            transport = AssetTransport(self.bundle)
            target = next(asset for asset in self.bundle.assets if asset.kind in INSTALL._ES_ASSET_KINDS)
            transport.es[INSTALL.es_path(target)] = {"_meta": {"managed_by": "foreign"}}
            stderr = io.StringIO()
            with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
                 mock.patch.object(INSTALL, "load_bundle", return_value=self.bundle), \
                 mock.patch.object(INSTALL, "role_body", return_value={}), \
                 mock.patch.object(INSTALL, "enrollment_condition", return_value="clean"), \
                 mock.patch.object(INSTALL, "check_version_fence"), \
                 mock.patch.object(INSTALL, "check_install_root_ancestors"), \
                 mock.patch.object(INSTALL, "check_outbox_root"), \
                 mock.patch.object(INSTALL, "check_install_preflight",
                                   return_value=(Path("/canonical/ca.pem"), Path("/canonical/agent"))), \
                 mock.patch.object(INSTALL, "configure_https"), \
                 mock.patch.object(INSTALL, "admin_authorization", return_value="admin"), \
                 mock.patch.object(INSTALL, "admin_credential_kind", return_value="native_user"), \
                 mock.patch.object(INSTALL, "dispatch_clean_root", return_value=False), \
                 mock.patch.object(INSTALL, "fence_remote_ownership_profile"), \
                 mock.patch.object(INSTALL, "run_topology_preflight"), \
                 mock.patch.object(INSTALL, "prepare_install_root") as prepare_root, \
                 mock.patch.object(INSTALL, "request", transport.request), \
                 mock.patch.object(INSTALL, "request_response", transport.request_response), \
                 redirect_stderr(stderr):
                self.assertEqual(INSTALL.main(), 3)
            self.assertEqual(stderr.getvalue(), "install refused: asset_conflict_unproven\n"
                             "RIGSIGNAL_FAILURE_SITE preflight\n")
            prepare_root.assert_not_called()
            self.assertFalse(root.exists())
            self.assertEqual(transport.mutations, [])


if __name__ == "__main__":
    unittest.main()
