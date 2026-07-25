import importlib.util
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("install_assets", ROOT / "tools/install_assets.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class FleetCoexistenceTests(unittest.TestCase):
    def test_coexist_fence_accepts_cosmetic_external_drift_and_refuses_operational_drift(self):
        asset = INSTALL.Asset(
            "component_templates", "metrics-rigsignal.audio@package", "fixture.json",
            b'{"_meta":{"managed_by":"rigsignal-asset-bundle"},"template":{"mappings":{"properties":{"sample":{"type":"keyword"}}}}}',
        )
        bundle = INSTALL.Bundle("test", "test", [asset])
        ownership = {(asset.kind, asset.name): "external"}

        def transport_for(body):
            return json.dumps({"component_templates": [{
                "name": asset.name, "component_template": body,
            }]}).encode("utf-8")

        cosmetic_live = json.loads(asset.data)
        cosmetic_live["_meta"]["managed_by"] = "fleet"
        with mock.patch.object(INSTALL, "request", return_value=transport_for(cosmetic_live)):
            INSTALL.prepublication_asset_fence("https://es", "https://kb", "auth", bundle,
                                               "fleet-coexist", ownership)

        operational_live = json.loads(asset.data)
        operational_live["_meta"]["managed_by"] = "fleet"
        operational_live["template"]["mappings"]["properties"]["sample"]["type"] = "text"
        with mock.patch.object(INSTALL, "request", return_value=transport_for(operational_live)), \
             self.assertRaisesRegex(INSTALL.ProvisionError, r"^install failed: pre-publication fence:$"):
            INSTALL.prepublication_asset_fence("https://es", "https://kb", "auth", bundle,
                                               "fleet-coexist", ownership)

    def test_coexist_fence_uses_pinned_verification_only_for_owned_assets(self):
        external = INSTALL.Asset("component_templates", "metrics-rigsignal.audio@package", "external", b"{}")
        owned = INSTALL.Asset("component_templates", "logs-rigsignal.diagnosis-mappings", "owned", b"{}")
        bundle = INSTALL.Bundle("test", "test", [external, owned])
        ownership = {(external.kind, external.name): "external", (owned.kind, owned.name): "bundle-owned"}
        with mock.patch.object(INSTALL, "verify_external_asset") as external_verify, \
             mock.patch.object(INSTALL, "verify_asset") as pinned_verify:
            INSTALL.prepublication_asset_fence("https://es", "https://kb", "auth", bundle,
                                               "fleet-coexist", ownership)
        external_verify.assert_called_once_with("https://es", "auth", external)
        pinned_verify.assert_called_once_with("https://es", "auth", owned)

    def test_default_fence_retains_pinned_verification_for_every_fenced_asset(self):
        assets = [
            INSTALL.Asset("component_templates", "component", "component", b"{}"),
            INSTALL.Asset("index_templates", "index", "index", b"{}"),
            INSTALL.Asset("security_roles", "role", "role", b"{}"),
        ]
        bundle = INSTALL.Bundle("test", "test", assets)
        ownership = {(asset.kind, asset.name): "external" for asset in assets}
        with mock.patch.object(INSTALL, "verify_external_asset") as external_verify, \
             mock.patch.object(INSTALL, "verify_asset") as pinned_verify:
            INSTALL.prepublication_asset_fence("https://es", "https://kb", "auth", bundle,
                                               "default", ownership)
        external_verify.assert_not_called()
        self.assertEqual(pinned_verify.call_args_list, [
            mock.call("https://es", "auth", asset) for asset in assets
        ])

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

    def test_ownership_refusals_name_assets_and_cardinality_has_its_own_code(self):
        bundle = INSTALL.load_source()
        bundle.assets.append(INSTALL.Asset("pipelines", "unclassified", "", b"{}"))
        with self.assertRaisesRegex(INSTALL.OwnershipTableError,
                                    r"ownership_table_unresolved: pipelines/unclassified"):
            INSTALL.ownership_for_assets(bundle, "fleet-coexist")
        moved = next(iter(INSTALL._OWNED_ASSET_KEYS))
        with mock.patch.object(INSTALL, "_OWNED_ASSET_KEYS", INSTALL._OWNED_ASSET_KEYS - {moved}), \
             mock.patch.object(INSTALL, "_EXTERNAL_ASSET_KEYS", INSTALL._EXTERNAL_ASSET_KEYS | {moved}):
            with self.assertRaisesRegex(INSTALL.OwnershipTableError, "ownership_table_cardinality"):
                INSTALL.ownership_for_assets(INSTALL.load_source(), "fleet-coexist")

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

    def test_new_transaction_archives_prior_proofs_and_opens_empty_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "state")
            first = INSTALL.TransactionJournal(root, "fleet-coexist")
            first.proof_intent("provision-one")
            first.apply_ok()
            second = INSTALL.TransactionJournal(root, "fleet-coexist", new_transaction=True)
            self.assertEqual(second.value["proofs"], [])
            self.assertFalse(second.value["apply_ok"])
            self.assertEqual(second.value["transactions"][0]["proofs"][0]["event_id"], "provision-one")

    def test_remote_profile_fence_refuses_default_against_coexist_marker(self):
        marker = {"component_templates": [{"component_template": {
            "_meta": {"ownership_profile": "fleet-coexist"}, "template": {}}}]}
        with mock.patch.object(INSTALL, "request", return_value=json.dumps(marker).encode()):
            with self.assertRaisesRegex(INSTALL.ProvisionError, "omitted_profile_on_coexist"):
                INSTALL.fence_remote_ownership_profile("https://es", "auth", "default", True)

    def test_external_index_template_requires_both_fleet_components(self):
        asset = INSTALL.Asset("index_templates", "metrics-rigsignal.cpu", "fixture",
                              b'{"index_patterns":["metrics-rigsignal.cpu-*"],"composed_of":[],"template":{}}')
        live = {"index_templates": [{"name": asset.name, "index_template": {
            "index_patterns": ["metrics-rigsignal.cpu-*"], "composed_of": [".fleet_globals-1"], "template": {}}}]}
        with mock.patch.object(INSTALL, "request", return_value=json.dumps(live).encode()):
            with self.assertRaisesRegex(INSTALL.InputError, "external Fleet composition differs"):
                INSTALL.verify_external_asset("https://es", "auth", asset)

    def test_ambiguous_crash_uses_only_persisted_three_way_pins(self):
        intent = {"preimage_sha256": "before", "intended_after_sha256": "after"}
        self.assertEqual(INSTALL.ambiguous_crash_outcome(intent, "after"), "restore")
        self.assertEqual(INSTALL.ambiguous_crash_outcome(intent, "before"), "untouched")
        with self.assertRaises(INSTALL.ProvisionError):
            INSTALL.ambiguous_crash_outcome(intent, "concurrent")

    def test_recovery_actions_three_way_check_verified_and_absent_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent("pipelines", "created", "create",
                                          INSTALL.asset_adapters.dashboard_absent_hash(), "after", b"{}")
            journal.write_verified(intent, "after")
            self.assertEqual(INSTALL.journal_recovery_actions(journal, lambda _intent: "after"), [intent])
            # A GET 404 is normalized by _rollback_live_hash to this sentinel;
            # recovery must recognize the never-created/already-deleted object
            # as converged and issue no inverse request.
            self.assertEqual(INSTALL.journal_recovery_actions(
                journal, lambda _intent: INSTALL.asset_adapters.dashboard_absent_hash()), [])
            with self.assertRaisesRegex(INSTALL.ProvisionError, "transaction_concurrent_drift"):
                INSTALL.journal_recovery_actions(journal, lambda _intent: "third-party")

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
                 mock.patch.object(INSTALL, "_rollback_live_hash", side_effect=["marker-after", "asset-after", INSTALL.asset_adapters.dashboard_absent_hash()]):
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
                 mock.patch.object(INSTALL, "_rollback_live_hash", side_effect=["after", "before"]):
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
                 mock.patch.object(INSTALL, "_rollback_live_hash", side_effect=["after", "before"]):
                with self.assertRaises(INSTALL.RequestFailure):
                    INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)

    def test_transform_preapply_gate_falls_back_before_apply_on_unproven_restore(self):
        asset = INSTALL.Asset("transforms", "rigsignal-game-timeline", "fixture",
                              b'{"description":"after","pivot":{"group_by":{}}}')
        calls = []
        def request(*args, **kwargs):
            calls.append(args)
            return b"{}"
        with mock.patch.dict("os.environ", {"RIGSIGNAL_TEST_TRANSFORM_META_RESTORE_REJECT": "1"}), \
             mock.patch.object(INSTALL, "request", side_effect=request):
            self.assertFalse(INSTALL.transform_preapply_restore_proven(
                "https://es", "auth", asset,
                {"description": "before", "pivot": {"group_by": {}}}, "started"))
        self.assertEqual(len(calls), 1)  # desired rehearsal write; no normal apply follows this false gate

    def test_fresh_transform_skips_preapply_gate_creates_and_journal_verifies(self):
        asset = INSTALL.Asset("transforms", "rigsignal-game-timeline", "fixture",
                              b'{"description":"after","pivot":{"group_by":{}}}')
        live = (b'{"transforms":[{"id":"rigsignal-game-timeline",'
                b'"description":"after","pivot":{"group_by":{}},"state":"started"}]}')
        created = False
        calls = []

        def request(_base, path, method, _authorization, data=None, headers=None):
            nonlocal created
            calls.append((method, path, data))
            if path == "/_transform/rigsignal-game-timeline" and method == "GET":
                if not created:
                    raise INSTALL.RequestFailure(404, "missing")
                return live
            if path == "/_transform/rigsignal-game-timeline" and method == "PUT":
                created = True
                return b"{}"
            raise AssertionError(f"unexpected request: {method} {path}")

        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            with mock.patch.object(INSTALL, "request", side_effect=request), \
                 mock.patch.object(INSTALL, "transform_preapply_restore_proven") as rehearsal:
                records = INSTALL.journal_owned_asset(journal, "https://es", "https://kb", "auth", asset,
                                                      "create")
                self.assertFalse(INSTALL.transform_preapply_requires_verify_only(
                    journal, records[0], "https://es", "auth", asset))
                rehearsal.assert_not_called()
                INSTALL.install_asset("https://es", "https://kb", "auth", asset)
                INSTALL.journal_verify_owned_asset(journal, records, "https://es", "https://kb", "auth", asset)

            self.assertTrue(created)
            self.assertNotIn("verify_only", records[0])
            self.assertTrue(records[0]["write_verified"])
            self.assertIn(("PUT", "/_transform/rigsignal-game-timeline", asset.data), calls)

    def test_existing_transform_unproven_preapply_is_journaled_verify_only_before_apply(self):
        asset = INSTALL.Asset("transforms", "rigsignal-game-timeline", "fixture",
                              b'{"description":"after","pivot":{"group_by":{}}}')
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            record = journal.write_intent(
                "transforms", asset.name, "update", "before", "after", asset.data,
                preimage_body=INSTALL.jcs({"description": "before", "pivot": {"group_by": {}}}),
                preimage_stats_state="started")
            apply = mock.Mock()
            action = "update"
            with mock.patch.object(INSTALL, "transform_preapply_restore_proven", return_value=False) as rehearsal:
                if INSTALL.transform_preapply_requires_verify_only(journal, record, "https://es", "auth", asset):
                    journal.mark_transform_verify_only(record, "meta_absent_restore_unproven_preapply")
                    action = "noop"
                if action != "noop":
                    apply("https://es", "https://kb", "auth", asset)

            rehearsal.assert_called_once_with(
                "https://es", "auth", asset,
                {"description": "before", "pivot": {"group_by": {}}}, "started")
            apply.assert_not_called()
            self.assertTrue(record["verify_only"])
            self.assertEqual(record["verify_only_reason"], "meta_absent_restore_unproven_preapply")
            self.assertEqual(record["action"], "noop")

    def test_rollback_reports_journaled_verify_only_transform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent(
                "transforms", "rigsignal-game-timeline", "noop", "before", "after", b"{}")
            journal.mark_transform_verify_only(intent, "meta_absent_restore_unproven_preapply")
            with mock.patch.object(INSTALL, "verify_rollback_external_baselines"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash") as live_hash, \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"):
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertIn("verify-only:transforms/rigsignal-game-timeline", operations)
            live_hash.assert_not_called()

    def test_main_reports_verify_only_transform_rollback(self):
        args = type("Args", (), {
            "bundle": None, "endpoint": "https://es.invalid", "ca_file": Path("ca"),
            "kibana_endpoint": "https://kb.invalid", "kibana_ca_file": Path("kb-ca"),
            "admin_credentials_file": Path("admin"), "agent_binary": Path("agent"),
            "profile": "user", "rollback": Path("transaction"), "dry_run": False,
        })()
        output = io.StringIO()
        with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
             mock.patch.object(INSTALL, "configure_https"), \
             mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
             mock.patch.object(INSTALL, "rollback_transaction",
                               return_value=["verify-only:transforms/rigsignal-game-timeline"]), \
             redirect_stdout(output):
            self.assertEqual(INSTALL.main(), 0)
        self.assertEqual(output.getvalue(),
                         "rollback completed from journaled intents; transform _meta absence could not be restored: "
                         "verify-only cosmetic drift accepted\n")

    def test_second_rollback_is_refused_before_any_external_or_restore_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.value["rollback_ok"] = True
            journal._persist()
            with mock.patch.object(INSTALL, "verify_rollback_external_baselines") as baselines:
                with self.assertRaisesRegex(INSTALL.ProvisionError, "transaction_already_rolled_back"):
                    INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            baselines.assert_not_called()

    def test_rollback_absent_preimage_never_created_skips_inverse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.write_intent("pipelines", "never-created", "create",
                                 INSTALL.asset_adapters.dashboard_absent_hash(), "after", b"{}")
            with mock.patch.object(INSTALL, "request",
                                   side_effect=INSTALL.RequestFailure(404, "not found")) as request, \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"):
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertNotIn("asset:pipelines/never-created", operations)
            self.assertEqual(request.call_args.args[2], "GET")

    def test_rollback_absent_marker_publication_and_key_are_idempotent_pre_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            # This is the durable mint intent left by a crash before the mint
            # response.  No marker or consumer publication was ever created.
            journal.write_intent("api_key", "mint-never-created", "create", "absent", "after", b"{}")
            with mock.patch.object(INSTALL, "invalidate_mint_name") as revoke, \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"):
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertEqual(operations, ["fence", "revoke", "publication"])
            revoke.assert_called_once_with("https://es", "auth", "mint-never-created")

    def test_delete_or_absent_accepts_race_to_absence(self):
        with mock.patch.object(INSTALL, "request", side_effect=INSTALL.RequestFailure(404, "gone")):
            INSTALL._delete_or_absent("https://es", "/_ingest/pipeline/never-created", "auth")

    def test_fleet_stream_snapshot_normalizes_tsdb_boundaries_but_detects_real_drift(self):
        stream = {"name": "metrics-rigsignal.session-default", "indices": [
            {"index_name": ".ds-metrics-rigsignal.session-default-000001", "index_uuid": "uuid-1"},
        ]}

        def capture(*, start="2026-07-24T12:00:00Z", end="2026-07-24T12:01:00Z",
                    setting="30m", mapping="keyword", pipeline="pipe-a", lifecycle="ilm-a", backing=None):
            current = dict(stream)
            if backing is not None:
                current["indices"] = backing
            simulated = {"template": {"mappings": {"properties": {"sample": {"type": mapping}}},
                                        "aliases": {}, "settings": {
                                            "index.mode": "time_series",
                                            "index.time_series.start_time": start,
                                            "index.time_series.end_time": end,
                                            "index.time_series.look_ahead_time": setting,
                                            "index.default_pipeline": pipeline,
                                            "index.lifecycle.name": lifecycle,
                                        }}}
            with mock.patch.object(INSTALL, "es_json", side_effect=[{"data_streams": [current]}, simulated]):
                return INSTALL.fleet_stream_snapshot("https://es", "auth")

        baseline = capture()
        self.assertEqual(baseline, capture(start="2026-07-24T12:00:01Z", end="2026-07-24T12:01:01Z"))
        self.assertNotEqual(baseline, capture(setting="45m"))
        self.assertNotEqual(baseline, capture(mapping="text"))
        self.assertNotEqual(baseline, capture(pipeline="pipe-b"))
        self.assertNotEqual(baseline, capture(lifecycle="ilm-b"))
        self.assertNotEqual(baseline, capture(backing=[{
            "index_name": ".ds-metrics-rigsignal.session-default-000002", "index_uuid": "uuid-2",
        }]))

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
