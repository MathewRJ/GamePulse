import importlib.util
import hashlib
import io
import json
from copy import deepcopy
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("install_assets", ROOT / "tools/install_assets.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class FleetCoexistenceTests(unittest.TestCase):
    def _fleet_record(self, *, backing="one", mapping="keyword", lifecycle="ilm-a"):
        return {"backing": [(".ds-" + backing, "uuid-" + backing)],
                "stream_state": {"generation": 1, "ilm_policy": lifecycle,
                                 "prefer_ilm": True,
                                 "next_generation_managed_by": "Index Lifecycle Management",
                                 "lifecycle": None, "failure_store": None,
                                 "hidden": None, "system": None, "allow_custom_routing": None,
                                 "backing": [{"index_name": ".ds-" + backing,
                                              "index_uuid": "uuid-" + backing,
                                              "prefer_ilm": True, "managed_by": "Index Lifecycle Management"}]},
                "mappings": {"properties": {"sample": {"type": mapping}}},
                "settings": {"index.lifecycle.name": lifecycle}, "aliases": {}}

    def _rollback_with_fleet_snapshot(self, root, snapshot):
        with mock.patch.object(INSTALL, "_recovery_sweep", return_value=[]), \
             mock.patch.object(INSTALL, "verify_rollback_external_baselines"), \
             mock.patch.object(INSTALL, "_fence_transaction_consumer"), \
             mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
             mock.patch.object(INSTALL, "verify_m1_anchors"), \
             mock.patch.object(INSTALL, "fleet_stream_snapshot", return_value=snapshot) as fleet_snapshot:
            operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
        return operations, fleet_snapshot

    def _template_asset(self, name, patterns, priority, *, mapping="keyword", composed_of=(), version="1"):
        body = {"_meta": {"version": version}, "index_patterns": list(patterns), "priority": priority,
                "composed_of": list(composed_of),
                "template": {"mappings": {"properties": {"sample": {"type": mapping}}},
                             "settings": {"index.lifecycle.name": "ilm-a"}}}
        return INSTALL.Asset("index_templates", name, name + ".json",
                             json.dumps(body, sort_keys=True).encode("utf-8"))

    def _plan_l3(self, stream, record, target, live, *, templates=None, actions=None,
                 pre_synth=None, post_synth=None):
        templates = templates or {target.name: live}
        actions = actions or {(target.kind, target.name): "update"}
        entries = [{"name": name, "index_template": body} for name, body in templates.items()]
        pre_synth = pre_synth or {key: record[key] for key in ("mappings", "settings", "aliases")}
        post_synth = post_synth or pre_synth

        def es_response(_base, path, _method, _authorization, _payload=None):
            if path == "/_index_template":
                return {"index_templates": entries}
            if path == "/_index_template/" + target.name:
                return {"index_templates": [{"name": target.name, "index_template": live}]}
            raise AssertionError("unexpected fleet-fence request: " + path)

        with mock.patch.object(INSTALL, "es_json", side_effect=es_response), \
             mock.patch.object(INSTALL, "_simulate_template",
                               side_effect=[(pre_synth, b"pre-synth"), (post_synth, b"post-synth")]):
            return INSTALL.plan_fleet_fence("https://es", "auth", {stream: record},
                                             INSTALL.Bundle("fixture", "fixture", [target]), actions)

    def test_v2_leg1_old_outcome_equality_fails_but_planned_l3_passes_owned_update(self):
        """Leg 1: the pre-991c2fd all-outcome equality predicate rejects this valid update."""
        stream = "logs-rigsignal.events-default"
        target = self._template_asset("logs-rigsignal.events", ["logs-rigsignal.events-*"], 100,
                                      mapping="text")
        live = json.loads(target.data); live["template"]["mappings"]["properties"]["sample"]["type"] = "keyword"
        before = self._fleet_record(); before["data_stream_template"] = target.name
        after = self._fleet_record(mapping="text"); after["data_stream_template"] = target.name
        old_fence = lambda pre, post: all(pre[name] == post.get(name) for name in pre)
        self.assertFalse(old_fence({stream: before}, {stream: after}))
        pre_real = {key: before[key] for key in ("mappings", "settings", "aliases")}
        post_real = {key: after[key] for key in ("mappings", "settings", "aliases")}
        plan = self._plan_l3(stream, before, target, live, pre_synth=pre_real, post_synth=post_real)
        self.assertEqual(plan[stream]["classification"]["status"], "L3")
        INSTALL.verify_fleet_fence({stream: before}, {stream: after}, plan)

    def test_v2_leg2_same_version_mutated_template_body_is_planned_as_l3(self):
        stream = "logs-rigsignal.events-default"
        target = self._template_asset("logs-rigsignal.events", ["logs-rigsignal.events-*"], 100,
                                      mapping="text", version="7")
        live = json.loads(target.data); live["template"]["mappings"]["properties"]["sample"]["type"] = "keyword"
        self.assertEqual(live["_meta"]["version"], json.loads(target.data)["_meta"]["version"])
        before = self._fleet_record(); before["data_stream_template"] = target.name
        after = self._fleet_record(mapping="text"); after["data_stream_template"] = target.name
        plan = self._plan_l3(stream, before, target, live,
                             pre_synth={key: before[key] for key in ("mappings", "settings", "aliases")},
                             post_synth={key: after[key] for key in ("mappings", "settings", "aliases")})
        self.assertEqual(plan[stream]["classification"]["status"], "L3")
        self.assertEqual(plan[stream]["projection"]["ops"], INSTALL.rfc6901_diff(
            {key: before[key] for key in ("mappings", "settings", "aliases")},
            {key: after[key] for key in ("mappings", "settings", "aliases")}))

    def test_v2_leg7_identical_outcome_foreign_takeover_refuses_by_winner_proof(self):
        stream = "logs-rigsignal.events-default"
        target = self._template_asset("logs-rigsignal.events", ["logs-rigsignal.events-*"], 100)
        foreign = json.loads(target.data); foreign["priority"] = 200
        before = self._fleet_record(); before["data_stream_template"] = "foreign-takeover"
        # The synthetic anchor/outcome equality that the old fence relied on holds;
        # only winner proof exposes the priority takeover.
        self.assertEqual({key: before[key] for key in ("mappings", "settings", "aliases")},
                         {key: self._fleet_record()[key] for key in ("mappings", "settings", "aliases")})
        entries = [{"name": target.name, "index_template": json.loads(target.data)},
                   {"name": "foreign-takeover", "index_template": foreign}]
        with mock.patch.object(INSTALL, "es_json", return_value={"index_templates": entries}):
            with self.assertRaisesRegex(INSTALL.InputError, "owned template is not the winner"):
                INSTALL.plan_fleet_fence("https://es", "auth", {stream: before},
                                         INSTALL.Bundle("fixture", "fixture", [target]),
                                         {(target.kind, target.name): "update"})

    def test_v2_leg8_noop_metrics_template_is_l2(self):
        stream = "metrics-rigsignal.cpu-default"
        target = self._template_asset("metrics-rigsignal.profiles", ["metrics-rigsignal.cpu-*"], 100)
        record = self._fleet_record(); record["data_stream_template"] = target.name
        with mock.patch.object(INSTALL, "es_json", side_effect=AssertionError("L2 requires no winner lookup")):
            plan = INSTALL.plan_fleet_fence("https://es", "auth", {stream: record},
                                             INSTALL.Bundle("fixture", "fixture", [target]),
                                             {(target.kind, target.name): "noop"})
        self.assertEqual(plan[stream]["classification"]["status"], "L2")

    def test_v2_leg9_component_only_diagnosis_update_is_l3c(self):
        stream = INSTALL.DIAGNOSIS_STREAM
        template = self._template_asset("logs-rigsignal.stream", ["logs-rigsignal.diagnosis-*"], 100,
                                        composed_of=("logs-rigsignal.diagnosis-mappings",))
        component = INSTALL.Asset("component_templates", "logs-rigsignal.diagnosis-mappings", "component.json",
                                  b'{"template":{"mappings":{"properties":{"rigsignal":{"properties":{"diagnosis":{}}}}}}}')
        before = self._fleet_record(); before["data_stream_template"] = template.name
        entries = [{"name": template.name, "index_template": json.loads(template.data)}]
        with mock.patch.object(INSTALL, "es_json", return_value={"index_templates": entries}):
            plan = INSTALL.plan_fleet_fence("https://es", "auth", {stream: before},
                                             INSTALL.Bundle("fixture", "fixture", [template, component]),
                                             {(template.kind, template.name): "noop",
                                              (component.kind, component.name): "update"})
        self.assertEqual(plan[stream]["classification"]["status"], "L3-C")
        after = deepcopy(before)
        after["mappings"]["properties"]["rigsignal"] = {"properties": {"diagnosis": {}}}
        INSTALL.verify_fleet_fence({stream: before}, {stream: after}, plan)

    def test_v2_external_fleet_components_do_not_poison_l2_closure(self):
        """Leg-a shape: external winner/components plus a changed owned template elsewhere."""
        stream = "logs-rigsignal.events-default"
        external_components = [
            INSTALL.Asset("component_templates", ".fleet_globals-1", "globals.json", b"{}"),
            INSTALL.Asset("component_templates", "logs-rigsignal.events@package", "events.json", b"{}"),
        ]
        external_winner = self._template_asset(
            "logs-rigsignal.events", ["logs-rigsignal.events-*"], 200,
            composed_of=tuple(component.name for component in external_components))
        changed_elsewhere = self._template_asset(
            "logs-rigsignal.stream", ["logs-rigsignal.diagnosis-*"], 100)
        before = self._fleet_record(); before["data_stream_template"] = external_winner.name
        entries = [{"name": external_winner.name, "index_template": json.loads(external_winner.data)},
                   {"name": changed_elsewhere.name, "index_template": json.loads(changed_elsewhere.data)}]
        with mock.patch.object(INSTALL, "es_json", return_value={"index_templates": entries}):
            plan = INSTALL.plan_fleet_fence(
                "https://es", "auth", {stream: before},
                INSTALL.Bundle("fixture", "fixture", [*external_components, external_winner, changed_elsewhere]),
                {(changed_elsewhere.kind, changed_elsewhere.name): "update"})
        classification = plan[stream]["classification"]
        self.assertEqual(classification["status"], "L2")
        self.assertEqual(classification["closure_owned_components"], [])

    def test_v2_owned_component_create_is_l3c_for_diagnosis(self):
        stream = INSTALL.DIAGNOSIS_STREAM
        template = self._template_asset("logs-rigsignal.stream", ["logs-rigsignal.diagnosis-*"], 100,
                                        composed_of=("logs-rigsignal.diagnosis-mappings",))
        component = INSTALL.Asset("component_templates", "logs-rigsignal.diagnosis-mappings", "component.json",
                                  b'{"template":{"mappings":{"properties":{"rigsignal":{}}}}}')
        before = self._fleet_record(); before["data_stream_template"] = template.name
        entries = [{"name": template.name, "index_template": json.loads(template.data)}]
        with mock.patch.object(INSTALL, "es_json", return_value={"index_templates": entries}):
            plan = INSTALL.plan_fleet_fence("https://es", "auth", {stream: before},
                                             INSTALL.Bundle("fixture", "fixture", [template, component]),
                                             {(template.kind, template.name): "noop",
                                              (component.kind, component.name): "create"})
        self.assertEqual(plan[stream]["classification"]["status"], "L3-C")

    def test_v2_owned_component_absent_action_is_not_changed(self):
        stream = INSTALL.DIAGNOSIS_STREAM
        template = self._template_asset("logs-rigsignal.stream", ["logs-rigsignal.diagnosis-*"], 100,
                                        composed_of=("logs-rigsignal.diagnosis-mappings",))
        component = INSTALL.Asset("component_templates", "logs-rigsignal.diagnosis-mappings", "component.json",
                                  b'{"template":{"mappings":{"properties":{"rigsignal":{}}}}}')
        before = self._fleet_record(); before["data_stream_template"] = template.name
        with mock.patch.object(INSTALL, "es_json", side_effect=AssertionError("L2 requires no winner lookup")):
            plan = INSTALL.plan_fleet_fence("https://es", "auth", {stream: before},
                                             INSTALL.Bundle("fixture", "fixture", [template, component]),
                                             {(template.kind, template.name): "noop"})
        self.assertEqual(plan[stream]["classification"]["status"], "L2")

    def test_stream_composition_parse_error_names_stream(self):
        asset = self._template_asset("logs-rigsignal.stream", ["logs-rigsignal.diagnosis-*"], 100)
        with mock.patch.object(INSTALL, "request", return_value=b'{"index_templates":[]}'):
            with self.assertRaisesRegex(INSTALL.InputError, "^stream composition is invalid$"):
                INSTALL.install_asset("https://es", "https://kb", "auth", asset)

    def test_v2_fence_helpers_cover_resolution_closure_and_declared_paths(self):
        templates = {"low": {"index_template": {"index_patterns": ["logs-rigsignal.*"], "priority": 1,
                                                   "template": {}}},
                     "winner": {"index_template": {"index_patterns": ["logs-rigsignal.events-*"], "priority": 2,
                                                      "composed_of": ["owned", "foreign"], "template": {}}}}
        matches = INSTALL._matching_templates(templates, "logs-rigsignal.events-default")
        self.assertEqual([(item["name"], item["priority"]) for item in matches], [("low", 1), ("winner", 2)])
        with mock.patch.object(INSTALL, "es_json", return_value={"index_templates": [
                {"name": name, "index_template": value["index_template"]} for name, value in templates.items()]}):
            evidence = INSTALL._winner_evidence("https://es", "auth", "logs-rigsignal.events-default", "winner")
        self.assertEqual(evidence["winning_template"], "winner")
        self.assertEqual(INSTALL._owned_component_closure(evidence["winning_body"], {"owned"}), ["owned"])
        self.assertEqual(INSTALL._declared_paths({"mappings": {"a/b": {"til~de": 1}}}),
                         {"/mappings/a~1b/til~0de"})

    def test_v2_fence_failure_journal_includes_failing_stream_and_ops(self):
        stream = "logs-rigsignal.events-default"
        before = {stream: self._fleet_record()}
        after = {stream: self._fleet_record(mapping="text")}
        with tempfile.TemporaryDirectory() as directory:
            journal = INSTALL.TransactionJournal(INSTALL.secure_root(Path(directory) / "state"), "fleet-coexist")
            with self.assertRaises(INSTALL.InputError):
                INSTALL.verify_fleet_fence(before, after, {stream: {"classification": {"status": "L2"}}}, journal)
            failure = journal.value["fleet_fence"]["failure"]
        self.assertEqual(failure["stream"], stream)
        self.assertEqual(failure["ops"], INSTALL.rfc6901_diff(
            {key: before[stream][key] for key in ("mappings", "settings", "aliases")},
            {key: after[stream][key] for key in ("mappings", "settings", "aliases")}))
        with tempfile.TemporaryDirectory() as directory:
            journal = INSTALL.TransactionJournal(INSTALL.secure_root(Path(directory) / "state"), "fleet-coexist")
            with self.assertRaises(INSTALL.InputError):
                INSTALL.verify_late_fleet_fence(before, after, journal)
            late_failure = journal.value["fleet_fence"]["failure"]
        self.assertEqual(late_failure["stream"], stream)
        self.assertEqual(late_failure["ops"], INSTALL.rfc6901_diff(before[stream], after[stream]))

    def test_v2_leg6a_crash_after_template_write_leaves_intent_for_recovery_without_publication(self):
        stream = "logs-rigsignal.events-default"
        name = "logs-rigsignal.stream"
        pre_body = b'{"index_patterns":["logs-rigsignal.diagnosis-*"],"template":{}}'
        post_body = b'{"index_patterns":["logs-rigsignal.diagnosis-*"],"template":{"mappings":{"properties":{"x":{"type":"keyword"}}}}}'
        pre_hash = hashlib.sha256(pre_body).hexdigest()
        post_hash = hashlib.sha256(post_body).hexdigest()
        snapshot = {stream: self._fleet_record()}
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "state")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent("index_templates", name, "update", pre_hash, post_hash,
                                          post_body, preimage_body=pre_body)
            journal.pin_fleet_fence({stream: {"pre": snapshot[stream]}})
            with mock.patch.dict(INSTALL.os.environ, {"RIGSIGNAL_TEST_CRASH_AT":
                                                       "after-remote-mutation:index_templates/" + name}), \
                 mock.patch.object(INSTALL.os, "kill", side_effect=SystemExit(9)):
                with self.assertRaises(SystemExit):
                    INSTALL.fault("after-remote-mutation", "index_templates/" + name)
            self.assertEqual(journal.value["intents"], [intent])
            self.assertFalse((root / "state.json").exists())
            with mock.patch.object(INSTALL, "_recovery_sweep", return_value=[]), \
                 mock.patch.object(INSTALL, "verify_rollback_external_baselines"), \
                 mock.patch.object(INSTALL, "_fence_transaction_consumer"), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"), \
                 mock.patch.object(INSTALL, "fleet_stream_snapshot", side_effect=[snapshot, snapshot]), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", side_effect=[post_hash, pre_hash]), \
                 mock.patch.object(INSTALL, "request") as request:
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
        self.assertIn("asset:index_templates/" + name, operations)
        self.assertNotIn("external_rollover_observed", operations)
        request.assert_called_once_with("https://es", "/_index_template/" + name, "PUT", "auth", pre_body, None)

    def test_rollback_reports_external_rollover_against_journaled_pre_backing(self):
        stream = "logs-rigsignal.events-default"
        pre = self._fleet_record(backing="one")
        live = deepcopy(pre)
        live["backing"].append((".ds-two", "uuid-two"))
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.pin_fleet_fence({stream: {"pre": pre}})
            operations, fleet_snapshot = self._rollback_with_fleet_snapshot(root, {stream: live})
            fence = INSTALL.TransactionJournal(root, "fleet-coexist").value["fleet_fence"]
        self.assertIn("external_rollover_observed", operations)
        self.assertEqual(fleet_snapshot.call_count, 2)
        self.assertTrue(fence["external_rollover_observed"])
        self.assertEqual(fence["post_reversal_stream_state_diffs"], [])
        self.assertEqual(fence["external_rollovers"], [{"stream": stream,
                                                          "pre_backing": [[".ds-one", "uuid-one"]],
                                                          "post_backing": [[".ds-one", "uuid-one"],
                                                                           [".ds-two", "uuid-two"]]}])

    def test_rollback_reports_hybrid_index_evidence_for_l3_stream_with_tuple_backing(self):
        # The live snapshot builds backing pairs as tuples, not lists; the
        # report loop must accept both or it silently skips every new index.
        stream = "logs-rigsignal.events-default"
        pre = self._fleet_record(backing="one")
        live = deepcopy(pre)
        live["backing"] = [(".ds-one", "uuid-one"), (".ds-two", "uuid-two")]
        evidence = {"/.ds-two/_settings": {"settings": True},
                    "/.ds-two/_mapping": {"mappings": True},
                    "/.ds-two/_ilm/explain": {"lifecycle": True}}
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.pin_fleet_fence({stream: {"pre": pre,
                                              "classification": {"status": "L3"}}})
            with mock.patch.object(INSTALL, "es_json",
                                   side_effect=lambda url, path, *args, **kwargs: evidence[path]):
                operations, _ = self._rollback_with_fleet_snapshot(root, {stream: live})
            fence = INSTALL.TransactionJournal(root, "fleet-coexist").value["fleet_fence"]
        self.assertIn("rollover_under_installer_template", operations)
        self.assertEqual(fence["rollover_under_installer_template"],
                         [{"stream": stream, "index": ".ds-two",
                           "settings": {"settings": True}, "mappings": {"mappings": True},
                           "lifecycle": {"lifecycle": True}}])

    def test_rollback_does_not_report_unchanged_journaled_pre_backing(self):
        stream = "logs-rigsignal.events-default"
        pre = self._fleet_record()
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.pin_fleet_fence({stream: {"pre": pre}})
            operations, fleet_snapshot = self._rollback_with_fleet_snapshot(root, {stream: deepcopy(pre)})
            fence = INSTALL.TransactionJournal(root, "fleet-coexist").value["fleet_fence"]
        self.assertNotIn("external_rollover_observed", operations)
        self.assertEqual(fleet_snapshot.call_count, 2)
        self.assertNotIn("external_rollover_observed", fence)
        self.assertNotIn("external_rollovers", fence)

    def test_rollback_ignores_window_created_stream_absent_from_journaled_pre(self):
        stream = "logs-rigsignal.events-default"
        pre = self._fleet_record()
        created = self._fleet_record(backing="diagnosis")
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.pin_fleet_fence({stream: {"pre": pre}})
            operations, fleet_snapshot = self._rollback_with_fleet_snapshot(
                root, {stream: deepcopy(pre), INSTALL.DIAGNOSIS_STREAM: created})
            fence = INSTALL.TransactionJournal(root, "fleet-coexist").value["fleet_fence"]
        self.assertNotIn("external_rollover_observed", operations)
        self.assertEqual(fleet_snapshot.call_count, 2)
        self.assertNotIn("external_rollover_observed", fence)

    def test_rollback_without_fence_plan_skips_external_rollover_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            INSTALL.TransactionJournal(root, "fleet-coexist")
            operations, fleet_snapshot = self._rollback_with_fleet_snapshot(root, {})
            journal = INSTALL.TransactionJournal(root, "fleet-coexist").value
        self.assertNotIn("external_rollover_observed", operations)
        fleet_snapshot.assert_not_called()
        self.assertNotIn("fleet_fence", journal)

    def test_v2_matrix_l1_l2_l3_l3c_l4(self):
        """Regression legs 3/4/5/8/9/10: old whole-predicate lacked this split."""
        before = {"logs-rigsignal.events-default": self._fleet_record()}
        l2 = {"logs-rigsignal.events-default": {"classification": {"status": "L2"}}}
        INSTALL.verify_fleet_fence(before, deepcopy(before), l2)
        with self.assertRaises(INSTALL.InputError):  # leg 3: unaffected rollout
            INSTALL.verify_fleet_fence(before, {"logs-rigsignal.events-default": self._fleet_record(backing="two")}, l2)
        with self.assertRaises(INSTALL.InputError):  # leg 10: lifecycle drift is L1 even if simulate matches
            INSTALL.verify_fleet_fence(before, {"logs-rigsignal.events-default": self._fleet_record(lifecycle="ilm-b")}, l2)
        with self.assertRaises(INSTALL.InputError):  # leg 5: unexpected post-only stream
            INSTALL.verify_fleet_fence(before, {**before, "logs-rigsignal.operator-default": self._fleet_record()}, l2)
        l3_before = self._fleet_record(mapping="keyword")
        l3_after = self._fleet_record(mapping="text")
        l3 = {"logs-rigsignal.events-default": {"classification": {"status": "L3"},
              "projection": {"ops": INSTALL.rfc6901_diff(
                  {key: l3_before[key] for key in ("mappings", "settings", "aliases")},
                  {key: l3_after[key] for key in ("mappings", "settings", "aliases")})},
              "stream_state_ops": []}}
        INSTALL.verify_fleet_fence({"logs-rigsignal.events-default": l3_before},
                                   {"logs-rigsignal.events-default": l3_after}, l3)
        with self.assertRaises(INSTALL.InputError):  # leg 4: expected change does not excuse rollover
            INSTALL.verify_fleet_fence({"logs-rigsignal.events-default": l3_before},
                                       {"logs-rigsignal.events-default": self._fleet_record(backing="two", mapping="text")}, l3)
        diagnosis_stream = INSTALL.DIAGNOSIS_STREAM
        with self.assertRaisesRegex(INSTALL.InputError, "L3-C stream is unattested"):
            INSTALL.verify_fleet_fence(before, deepcopy(before),
                                       {"logs-rigsignal.events-default": {
                                           "classification": {"status": "L3-C"}, "owned_paths": ["/mappings"]}})
        l3c = {diagnosis_stream: {"classification": {"status": "L3-C"},
              "owned_leaf_payloads": {
                  "/mappings/properties/rigsignal/properties/diagnosis": {}},
              "stream_state_ops": []}}
        diagnosis_after = self._fleet_record(); diagnosis_after["mappings"] = {
            "properties": {"sample": {"type": "keyword"}, "rigsignal": {"properties": {"diagnosis": {}}}}}
        INSTALL.verify_fleet_fence({diagnosis_stream: before["logs-rigsignal.events-default"]},
                                   {diagnosis_stream: diagnosis_after}, l3c)

    def test_v2b_l3c_parent_payload_matches_exact_owned_leaves(self):
        """A parent RFC op is valid only for the exact declared leaf payload."""
        stream = INSTALL.DIAGNOSIS_STREAM
        before = self._fleet_record()
        owned = {
            "/mappings/properties/rigsignal/properties/diagnosis/type": "keyword",
            "/mappings/properties/rigsignal/properties/summary/type": "text",
        }
        plan = {stream: {"classification": {"status": "L3-C"},
                         "owned_leaf_payloads": owned, "stream_state_ops": []}}

        def candidate(rigsignal):
            after = deepcopy(before)
            after["mappings"]["properties"]["rigsignal"] = rigsignal
            return after

        exact = {"properties": {"diagnosis": {"type": "keyword"},
                                "summary": {"type": "text"}}}
        INSTALL.verify_fleet_fence({stream: before}, {stream: candidate(exact)}, plan)

        with self.assertRaisesRegex(INSTALL.InputError, "fleet L3-C outside-owned drifted"):
            INSTALL.verify_fleet_fence(
                {stream: before}, {stream: candidate({"properties": {
                    **exact["properties"], "foreign": {"type": "long"}}})}, plan)
        with self.assertRaisesRegex(INSTALL.InputError, "fleet L3-C outside-owned drifted"):
            INSTALL.verify_fleet_fence(
                {stream: before}, {stream: candidate({"properties": {
                    "diagnosis": {"type": "keyword", "properties": {"foreign": {"type": "long"}}},
                    "summary": {"type": "text"}}})}, plan)
        with self.assertRaisesRegex(INSTALL.InputError, "fleet L3-C outside-owned drifted"):
            INSTALL.verify_fleet_fence(
                {stream: before}, {stream: candidate({"properties": {
                    "diagnosis": {"type": "keyword"}}})}, plan)

    def test_v2b_l3c_lists_are_atomic_leaf_payloads(self):
        stream = INSTALL.DIAGNOSIS_STREAM
        before = self._fleet_record()
        before["mappings"]["properties"]["rigsignal"] = {"tags": ["old"]}
        path = "/mappings/properties/rigsignal/tags"
        plan = {stream: {"classification": {"status": "L3-C"},
                         "owned_leaf_payloads": {path: ["alpha", "beta"]},
                         "stream_state_ops": []}}
        after = deepcopy(before)
        after["mappings"]["properties"]["rigsignal"]["tags"] = ["alpha", "beta"]
        self.assertEqual(INSTALL.rfc6901_diff(
            {key: before[key] for key in ("mappings", "settings", "aliases")},
            {key: after[key] for key in ("mappings", "settings", "aliases")}),
            [{"op": "replace", "path": path, "value": ["alpha", "beta"]}])
        INSTALL.verify_fleet_fence({stream: before}, {stream: after}, plan)
        after["mappings"]["properties"]["rigsignal"]["tags"] = ["beta", "alpha"]
        with self.assertRaisesRegex(INSTALL.InputError, "fleet L3-C outside-owned drifted"):
            INSTALL.verify_fleet_fence({stream: before}, {stream: after}, plan)

    def test_v2_rfc6901_ops_include_values_and_escape(self):
        ops = INSTALL.rfc6901_diff({"a/b": 1, "gone": True}, {"a/b": 2, "til~de": [1]})
        self.assertEqual(ops, [{"op": "replace", "path": "/a~1b", "value": 2},
                               {"op": "remove", "path": "/gone"},
                               {"op": "add", "path": "/til~0de", "value": [1]}])

    def test_v2b_si1_stream_state_schema_matrix_is_complete(self):
        """Every normalized field has add/remove/replace classification coverage."""
        sanctioned = {"ilm_policy", "prefer_ilm", "next_generation_managed_by"}
        self.assertEqual(set(INSTALL.STREAM_STATE_FIELDS), sanctioned | {
            "lifecycle", "failure_store", "generation", "hidden", "system",
            "allow_custom_routing", "backing"})
        stream = "logs-rigsignal.events-default"
        for field in INSTALL.STREAM_STATE_FIELDS:
            for operation in ("add", "remove", "replace"):
                before = self._fleet_record()
                after = deepcopy(before)
                if operation == "add":
                    before["stream_state"].pop(field, None)
                    after["stream_state"][field] = "added"
                elif operation == "remove":
                    after["stream_state"].pop(field, None)
                else:
                    after["stream_state"][field] = "replaced"
                ops = INSTALL.rfc6901_diff({"stream_state": before["stream_state"]},
                                           {"stream_state": after["stream_state"]})
                plan = {stream: {"classification": {"status": "L3"},
                                 "projection": {"ops": []},
                                 "stream_state_ops": ops if field in sanctioned else []}}
                if field in sanctioned:
                    INSTALL.verify_fleet_fence({stream: before}, {stream: after}, plan)
                else:
                    with self.assertRaises(INSTALL.InputError):
                        INSTALL.verify_fleet_fence({stream: before}, {stream: after}, plan)

    def test_v2b_lifecycle_extractor_normalizes_renderings_and_refuses_conflict(self):
        self.assertEqual(INSTALL.lifecycle_values_from_simulation({
            "index": {"lifecycle": {"name": "ilm-a", "prefer_ilm": "false"}}}),
            {"ilm_policy": "ilm-a", "prefer_ilm": False})
        self.assertEqual(INSTALL.lifecycle_values_from_simulation({
            "index.lifecycle.name": "ilm-a", "index.lifecycle.prefer_ilm": True}),
            {"ilm_policy": "ilm-a", "prefer_ilm": True})
        self.assertEqual(INSTALL.lifecycle_values_from_simulation({
            "index": {"lifecycle": {"name": "ilm-a", "prefer_ilm": False}},
            "index.lifecycle.prefer_ilm": "false"}),
            {"ilm_policy": "ilm-a", "prefer_ilm": False})
        self.assertEqual(INSTALL.lifecycle_values_from_simulation({}), {"prefer_ilm": True})
        with self.assertRaises(INSTALL.InputError):
            INSTALL.lifecycle_values_from_simulation({"index": {"lifecycle": {"name": "a"}},
                                                       "index.lifecycle.name": "b"})

    def test_v2b_r3_decision_table_and_refusals(self):
        rows = [
            ({"ilm_policy": "a", "prefer_ilm": True}, False, "Index Lifecycle Management"),
            ({"ilm_policy": "a", "prefer_ilm": True}, True, "Index Lifecycle Management"),
            ({"ilm_policy": "a", "prefer_ilm": False}, True, "Data stream lifecycle"),
            ({"prefer_ilm": True}, True, "Data stream lifecycle"),
            ({"prefer_ilm": False}, False, "Unmanaged"),
        ]
        for values, dsl, expected in rows:
            self.assertEqual(INSTALL.projected_next_generation_managed_by(values, dsl), expected)
        for values, dsl in (({}, False), ({"ilm_policy": "", "prefer_ilm": True}, False),
                            ({"prefer_ilm": "true"}, False)):
            with self.assertRaises(INSTALL.InputError):
                INSTALL.projected_next_generation_managed_by(values, dsl)

    def test_v2b_override_and_winner_reproof_are_scoped_and_fail_closed(self):
        stream = "logs-rigsignal.events-default"
        plan = {stream: {"classification": {"status": "L3", "winning_template": "winner"}}}
        with mock.patch.object(INSTALL, "es_json", side_effect=[
                {"data_streams": [{"settings": {}}],}, {"data_streams": [{"mappings": {}}]}]):
            INSTALL.verify_fleet_stream_overrides("https://es", "auth", plan)
        with mock.patch.object(INSTALL, "es_json", return_value={"data_streams": [{"settings": {"index": {}}}]}):
            with self.assertRaises(INSTALL.InputError):
                INSTALL.verify_fleet_stream_overrides("https://es", "auth", plan)
        with mock.patch.object(INSTALL, "_winner_evidence", return_value={"unique": True,
                                                                              "winning_template": "winner"}):
            INSTALL.verify_fleet_winner_proofs("https://es", "auth", plan)
        with mock.patch.object(INSTALL, "_winner_evidence", return_value={"unique": False,
                                                                              "winning_template": "winner"}):
            with self.assertRaises(INSTALL.InputError):
                INSTALL.verify_fleet_winner_proofs("https://es", "auth", plan)

    def test_v2b_override_refusal_journals_failure_at_late_checkpoints(self):
        stream = "logs-rigsignal.events-default"
        plan = {stream: {"classification": {"status": "L3", "winning_template": "winner"}}}
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            with mock.patch.object(INSTALL, "es_json",
                                   return_value={"data_streams": [{"settings": {"index": {"number_of_replicas": "2"}}}]}):
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.verify_fleet_stream_overrides("https://es", "auth", plan,
                                                          journal, "late")
            failure = journal.value["fleet_fence"]["failure"]
        self.assertEqual(failure["layer"], "late")
        self.assertEqual(failure["stream"], stream)
        self.assertEqual(failure["reason"], "stream_overrides_present")

    def test_v2_late_fence_rejects_any_post_candidate_drift(self):
        post = {"logs-rigsignal.events-default": self._fleet_record()}
        INSTALL.verify_late_fleet_fence(post, deepcopy(post))
        with self.assertRaises(INSTALL.InputError):
            INSTALL.verify_late_fleet_fence(post, {"logs-rigsignal.events-default": self._fleet_record(backing="late")})

    def test_v2_predecessor_manifest_is_set_pinned(self):
        asset = INSTALL.Asset("pipelines", "p", "p", b'{"processors":[]}')
        bundle = INSTALL.Bundle("test", "test", [asset])
        ownership = {(asset.kind, asset.name): "bundle-owned"}
        absent = INSTALL.asset_adapters.dashboard_absent_hash()
        manifest = {"version": 1, "assets": {"pipelines/p": {"id": "approved-v1", "approved_sha256": [absent, "new"]}}}
        with mock.patch.object(INSTALL, "_predecessor_hash", return_value=absent):
            pins = INSTALL.predecessor_manifest_barrier("https://es", "https://kb", "auth", bundle, ownership, manifest)
        self.assertEqual(pins[("pipelines", "p")], absent)
        with mock.patch.object(INSTALL, "_predecessor_hash", return_value="tampered"), self.assertRaises(INSTALL.InputError):
            INSTALL.predecessor_manifest_barrier("https://es", "https://kb", "auth", bundle, ownership, manifest)

    def test_v2_dashboard_barrier_journals_per_object_pins_without_changing_manifest(self):
        asset = INSTALL.Asset("dashboard", "asset.ndjson", "fixture", (
            b'{"type":"dashboard","id":"D1","attributes":{"title":"one"}}\n'
            b'{"type":"tag","id":"T1","attributes":{"name":"shared"}}\n'))
        values = [["dashboard", "D1", {"attributes": {"title": "old"}}],
                  ["tag", "T1", "ABSENT"]]
        observed = hashlib.sha256(INSTALL.jcs(values)).hexdigest()
        manifest = {"version": 1, "assets": {"dashboard/asset.ndjson": {
            "id": "approved-v1", "approved_sha256": [observed]}}}
        original_manifest = deepcopy(manifest)
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            with mock.patch.object(INSTALL, "_dashboard_predecessor_values", return_value=values):
                INSTALL.predecessor_manifest_barrier(
                    "https://es", "https://kb", "auth", INSTALL.Bundle("test", "test", [asset]),
                    {(asset.kind, asset.name): "bundle-owned"}, manifest, journal)
        self.assertEqual(manifest, original_manifest)
        self.assertEqual(journal.value["predecessor_manifest"]["dashboard/asset.ndjson"], {
            "approved_predecessor_id": "approved-v1", "predecessor_match": observed,
            "barrier_object_pins": [
                ["dashboard", "D1", hashlib.sha256(INSTALL.jcs(values[0][2])).hexdigest()],
                ["tag", "T1", hashlib.sha256(INSTALL.jcs("ABSENT")).hexdigest()],
            ]})

    def test_v2_dashboard_predecessor_recheck_accepts_shared_tags_from_verified_journal(self):
        asset = INSTALL.Asset("dashboard", "asset2.ndjson", "fixture", b"\n".join([
            b'{"type":"dashboard","id":"D2","attributes":{}}',
            b'{"type":"index-pattern","id":"IP2","attributes":{}}',
            b'{"type":"tag","id":"T1","attributes":{}}',
            b'{"type":"tag","id":"T2","attributes":{}}',
        ]))
        barrier = [["dashboard", "D2", "D2-before"], ["index-pattern", "IP2", "IP2-before"],
                   ["tag", "T1", "T1-before"], ["tag", "T2", "T2-before"]]
        current = [["dashboard", "D2", "D2-before"], ["index-pattern", "IP2", "IP2-before"],
                   ["tag", "T1", "T1-after"], ["tag", "T2", "T2-after"]]
        with tempfile.TemporaryDirectory() as directory:
            journal = INSTALL.TransactionJournal(INSTALL.secure_root(Path(directory) / "transaction"), "fleet-coexist")
            journal.value["predecessor_manifest"] = {"dashboard/asset2.ndjson": {"barrier_object_pins": barrier}}
            for tag in ("T1", "T2"):
                record = journal.write_intent("dashboard", "asset1.ndjson", "update", "before", tag + "-after", b"{}",
                                              object_id="tag/" + tag)
                journal.write_verified(record, tag + "-after")
            with mock.patch.object(INSTALL, "_dashboard_predecessor_object_pins", return_value=current):
                INSTALL.recheck_predecessor_pins("https://es", "https://kb", "auth", asset, "whole-before", journal)

    def test_v2_dashboard_predecessor_recheck_refuses_foreign_shared_tag_and_journals_failure(self):
        asset = INSTALL.Asset("dashboard", "asset2.ndjson", "fixture",
                              b'{"type":"tag","id":"T1","attributes":{}}')
        with tempfile.TemporaryDirectory() as directory:
            journal = INSTALL.TransactionJournal(INSTALL.secure_root(Path(directory) / "transaction"), "fleet-coexist")
            journal.value["predecessor_manifest"] = {"dashboard/asset2.ndjson": {
                "barrier_object_pins": [["tag", "T1", "before"]]}}
            record = journal.write_intent("dashboard", "asset1.ndjson", "update", "before", "after", b"{}",
                                          object_id="tag/T1")
            journal.write_verified(record, "after")
            with mock.patch.object(INSTALL, "_dashboard_predecessor_object_pins",
                                   return_value=[["tag", "T1", "foreign"]]), \
                 self.assertRaises(INSTALL.PredecessorRefusal) as refused:
                INSTALL.recheck_predecessor_pins("https://es", "https://kb", "auth", asset, "whole-before", journal)
            provision = INSTALL.predecessor_recheck_provision_error(journal, refused.exception)
            self.assertEqual(provision.prefix, "install failed: predecessor recheck:")
            self.assertEqual(refused.exception.source, "journaled")
            self.assertEqual(journal.value["predecessor_manifest"]["dashboard/asset2.ndjson"]["recheck_failure"], {
                "asset": "dashboard/asset2.ndjson", "object_type": "tag", "object_id": "T1",
                "expected": "after", "observed": "foreign", "source": "journaled"})

    def test_v2_dashboard_predecessor_recheck_refuses_foreign_not_yet_written_object(self):
        asset = INSTALL.Asset("dashboard", "asset2.ndjson", "fixture",
                              b'{"type":"dashboard","id":"D2","attributes":{}}')
        with tempfile.TemporaryDirectory() as directory:
            journal = INSTALL.TransactionJournal(INSTALL.secure_root(Path(directory) / "transaction"), "fleet-coexist")
            journal.value["predecessor_manifest"] = {"dashboard/asset2.ndjson": {
                "barrier_object_pins": [["dashboard", "D2", "before"]]}}
            with mock.patch.object(INSTALL, "_dashboard_predecessor_object_pins",
                                   return_value=[["dashboard", "D2", "foreign"]]), \
                 self.assertRaises(INSTALL.PredecessorRefusal) as refused:
                INSTALL.recheck_predecessor_pins("https://es", "https://kb", "auth", asset, "whole-before", journal)
        self.assertEqual((refused.exception.source, refused.exception.expected), ("barrier", "before"))

    def test_v2_dashboard_predecessor_recheck_refuses_missing_verified_shared_tag_record(self):
        asset = INSTALL.Asset("dashboard", "asset2.ndjson", "fixture",
                              b'{"type":"tag","id":"T1","attributes":{}}')
        with tempfile.TemporaryDirectory() as directory:
            journal = INSTALL.TransactionJournal(INSTALL.secure_root(Path(directory) / "transaction"), "fleet-coexist")
            journal.value["predecessor_manifest"] = {"dashboard/asset2.ndjson": {
                "barrier_object_pins": [["tag", "T1", "before"]]}}
            journal.write_intent("dashboard", "asset1.ndjson", "update", "before", "after", b"{}",
                                 object_id="tag/T1")
            with mock.patch.object(INSTALL, "_dashboard_predecessor_object_pins",
                                   return_value=[["tag", "T1", "after"]]), \
                 self.assertRaises(INSTALL.PredecessorRefusal) as refused:
                INSTALL.recheck_predecessor_pins("https://es", "https://kb", "auth", asset, "whole-before", journal)
        self.assertEqual((refused.exception.source, refused.exception.expected), ("journaled", "MISSING"))

    def test_v2_non_dashboard_predecessor_recheck_keeps_whole_asset_barrier_rule(self):
        asset = INSTALL.Asset("pipelines", "p", "fixture", b'{"processors":[]}')
        with tempfile.TemporaryDirectory() as directory:
            journal = INSTALL.TransactionJournal(INSTALL.secure_root(Path(directory) / "transaction"), "fleet-coexist")
            journal.write_intent("dashboard", "unrelated.ndjson", "update", "before", "after", b"{}",
                                 object_id="tag/T1")
            with mock.patch.object(INSTALL, "_predecessor_hash", return_value="foreign"), \
                 self.assertRaisesRegex(INSTALL.InputError, "predecessor manifest mismatch") as refused:
                INSTALL.recheck_predecessor_pins("https://es", "https://kb", "auth", asset, "before", journal)
        self.assertNotIsInstance(refused.exception, INSTALL.PredecessorRefusal)

    def test_v2_nonfatal_candidate_drift_hook_is_env_gated(self):
        calls = []
        with mock.patch.object(INSTALL, "request", side_effect=lambda *args, **kwargs: calls.append(args)):
            INSTALL.test_candidate_drift("after-first-owned-write", "https://es", "auth", {"logs-x-default": {}})
            self.assertEqual(calls, [])
            with mock.patch.dict(INSTALL.os.environ, {"RIGSIGNAL_TEST_ROLLOVER_AT": "after-first-owned-write:logs-x-default"}):
                INSTALL.test_candidate_drift("after-first-owned-write", "https://es", "auth", {"logs-x-default": {}})
        self.assertEqual(len(calls), 1)

    def test_v2_before_publication_candidate_drift_hook_is_gated(self):
        calls = []
        with mock.patch.object(INSTALL, "request", side_effect=lambda *args, **kwargs: calls.append(args)):
            INSTALL.test_candidate_drift("before-publication", "https://es", "auth", {"logs-x-default": {}})
            self.assertEqual(calls, [])
            with mock.patch.dict(INSTALL.os.environ,
                                 {"RIGSIGNAL_TEST_ROLLOVER_AT": "before-publication:logs-x-default"}):
                INSTALL.test_candidate_drift("before-publication", "https://es", "auth", {"logs-x-default": {}})
        self.assertEqual(len(calls), 1)

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

    def test_journal_bundle_pin_persists_bundle_hash_and_source_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "state")
            bundle_path = Path(directory) / "assets.tar.gz"
            bundle_path.write_bytes(b"applied bundle")
            bundle = INSTALL.Bundle("test", "applied-commit", [])
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.pin_bundle(bundle_path, bundle)
            self.assertEqual(journal.value["bundle_pin"], {
                "sha256": hashlib.sha256(b"applied bundle").hexdigest(),
                "source_commit": "applied-commit",
                "asset_set_sha256": INSTALL.asset_set_sha256(bundle),
            })

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

    def test_remote_profile_fence_refuses_marker_table_version_mismatch(self):
        marker = {"component_templates": [{"component_template": {
            "_meta": {"ownership_profile": "fleet-coexist",
                      "ownership_table_version": "fleet-coexist-v0"}, "template": {}}}]}
        with mock.patch.object(INSTALL, "request", return_value=json.dumps(marker).encode()):
            with self.assertRaisesRegex(INSTALL.ProvisionError, "ownership_table_version_mismatch"):
                INSTALL.fence_remote_ownership_profile("https://es", "auth", "fleet-coexist", False)

    def test_remote_profile_fence_accepts_matching_marker_table_version(self):
        marker = {"component_templates": [{"component_template": {
            "_meta": {"ownership_profile": "fleet-coexist",
                      "ownership_table_version": INSTALL.OWNERSHIP_TABLE_VERSION}, "template": {}}}]}
        with mock.patch.object(INSTALL, "request", return_value=json.dumps(marker).encode()):
            INSTALL.fence_remote_ownership_profile("https://es", "auth", "fleet-coexist", False)

    def test_remote_profile_fence_accepts_absent_version_on_legacy_default_marker(self):
        marker = {"component_templates": [{"component_template": {
            "_meta": {"ownership_profile": "default"}, "template": {}}}]}
        with mock.patch.object(INSTALL, "request", return_value=json.dumps(marker).encode()):
            INSTALL.fence_remote_ownership_profile("https://es", "auth", "fleet-coexist", False)

    def test_remote_profile_fence_refuses_versionless_coexist_marker(self):
        marker = {"component_templates": [{"component_template": {
            "_meta": {"ownership_profile": "fleet-coexist"}, "template": {}}}]}
        with mock.patch.object(INSTALL, "request", return_value=json.dumps(marker).encode()):
            with self.assertRaisesRegex(INSTALL.ProvisionError, "ownership_table_version_mismatch"):
                INSTALL.fence_remote_ownership_profile("https://es", "auth", "fleet-coexist", False)

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
            "ownership_profile": None, "unsafe_test_injection": False,
        })()
        output = io.StringIO()
        with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
             mock.patch.object(INSTALL, "configure_https"), \
             mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
             mock.patch.object(INSTALL, "fence_remote_ownership_profile"), \
             mock.patch.object(INSTALL, "rollback_transaction",
                               return_value=["verify-only:transforms/rigsignal-game-timeline"]), \
             redirect_stdout(output):
            self.assertEqual(INSTALL.main(), 0)
        self.assertEqual(output.getvalue(),
                         "rollback completed from journaled intents; transform _meta absence could not be restored: "
                         "verify-only cosmetic drift accepted\n")

    def test_rollback_retains_pipeline_used_by_adopted_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent(
                "pipelines", "logs-rigsignal.stream@pipeline", "create",
                INSTALL.asset_adapters.dashboard_absent_hash(), "after", b"{}")
            journal.write_verified(intent, "after")
            response = json.dumps({"error": {"root_cause": [{
                "type": "illegal_argument_exception",
                "reason": ("pipeline [logs-rigsignal.stream@pipeline] cannot be deleted because it is "
                           "the default pipeline for 2 index(es) including [.ds-logs-rigsignal.stream-default-000001, "
                           ".ds-logs-rigsignal.stream-default-000002]")
            }]}}).encode("utf-8")
            with mock.patch.object(INSTALL, "verify_rollback_external_baselines"), \
                 mock.patch.object(INSTALL, "request", side_effect=INSTALL.RequestFailure(400, "in use", response)), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", return_value="after"):
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertIn("retained-in-use:pipelines/logs-rigsignal.stream@pipeline", operations)
            persisted = INSTALL.TransactionJournal(root, "fleet-coexist").value["intents"][0]
            self.assertEqual(persisted["pipeline_retained_in_use"], {
                "referencing_indices": [".ds-logs-rigsignal.stream-default-000001",
                                        ".ds-logs-rigsignal.stream-default-000002"]})
            self.assertTrue(INSTALL.TransactionJournal(root, "fleet-coexist").value["rollback_ok"])

    def test_rollback_retained_pipeline_records_raw_reason_when_indices_are_unparseable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent(
                "pipelines", "logs-rigsignal.stream@pipeline", "create",
                INSTALL.asset_adapters.dashboard_absent_hash(), "after", b"{}")
            journal.write_verified(intent, "after")
            reason = ("pipeline [logs-rigsignal.stream@pipeline] cannot be deleted because it is "
                      "the default pipeline for adopted stream indices")
            response = json.dumps({"error": {"root_cause": [{
                "type": "illegal_argument_exception", "reason": reason
            }]}}).encode("utf-8")
            with mock.patch.object(INSTALL, "verify_rollback_external_baselines"), \
                 mock.patch.object(INSTALL, "request", side_effect=INSTALL.RequestFailure(400, "in use", response)), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", return_value="after"):
                INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            retained = INSTALL.TransactionJournal(root, "fleet-coexist").value["intents"][0]["pipeline_retained_in_use"]
            self.assertEqual(retained, {"referencing_indices": [], "raw_reason": reason})

    def test_rollback_other_pipeline_400_still_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent("pipelines", "journaled-only", "create",
                                          INSTALL.asset_adapters.dashboard_absent_hash(), "after", b"{}")
            journal.write_verified(intent, "after")
            response = json.dumps({"error": {"root_cause": [{
                "type": "illegal_argument_exception", "reason": "some other pipeline validation failure"
            }]}}).encode("utf-8")
            with mock.patch.object(INSTALL, "verify_rollback_external_baselines"), \
                 mock.patch.object(INSTALL, "request", side_effect=INSTALL.RequestFailure(400, "other", response)), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", return_value="after"):
                with self.assertRaises(INSTALL.RequestFailure):
                    INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)

    def test_main_reports_retained_pipeline_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent("pipelines", "logs-rigsignal.stream@pipeline", "create",
                                          "before", "after", b"{}")
            intent["pipeline_retained_in_use"] = {
                "referencing_indices": [".ds-z", ".ds-a"]}
            journal._persist()
            args = type("Args", (), {
                "bundle": None, "endpoint": "https://es.invalid", "ca_file": Path("ca"),
                "kibana_endpoint": "https://kb.invalid", "kibana_ca_file": Path("kb-ca"),
                "admin_credentials_file": Path("admin"), "agent_binary": Path("agent"),
                "profile": "user", "rollback": root, "dry_run": False,
                "ownership_profile": None, "unsafe_test_injection": False,
            })()
            output = io.StringIO()
            with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
                 mock.patch.object(INSTALL, "configure_https"), \
                 mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
                 mock.patch.object(INSTALL, "fence_remote_ownership_profile"), \
                 mock.patch.object(INSTALL, "rollback_transaction",
                                   return_value=["retained-in-use:pipelines/logs-rigsignal.stream@pipeline"]), \
                 redirect_stdout(output):
                self.assertEqual(INSTALL.main(), 0)
            self.assertEqual(output.getvalue(),
                             "rollback completed from journaled intents; pipeline retained: in use as default "
                             "pipeline for adopted stream indices; logs-rigsignal.stream@pipeline; "
                             "referencing_indices: [\".ds-a\",\".ds-z\"]\n")

    def test_main_reports_retained_pipeline_raw_reason(self):
        reason = ("pipeline [logs-rigsignal.stream@pipeline] cannot be deleted because it is "
                  "the default pipeline for adopted stream indices")
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent("pipelines", "logs-rigsignal.stream@pipeline", "create",
                                          "before", "after", b"{}")
            intent["pipeline_retained_in_use"] = {"referencing_indices": [], "raw_reason": reason}
            journal._persist()
            args = type("Args", (), {
                "bundle": None, "endpoint": "https://es.invalid", "ca_file": Path("ca"),
                "kibana_endpoint": "https://kb.invalid", "kibana_ca_file": Path("kb-ca"),
                "admin_credentials_file": Path("admin"), "agent_binary": Path("agent"),
                "profile": "user", "rollback": root, "dry_run": False,
                "ownership_profile": None, "unsafe_test_injection": False,
            })()
            output = io.StringIO()
            with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
                 mock.patch.object(INSTALL, "configure_https"), \
                 mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
                 mock.patch.object(INSTALL, "fence_remote_ownership_profile"), \
                 mock.patch.object(INSTALL, "rollback_transaction",
                                   return_value=["retained-in-use:pipelines/logs-rigsignal.stream@pipeline"]), \
                 redirect_stdout(output):
                self.assertEqual(INSTALL.main(), 0)
            self.assertEqual(output.getvalue(),
                             "rollback completed from journaled intents; pipeline retained: in use as default "
                             "pipeline for adopted stream indices; logs-rigsignal.stream@pipeline; raw_reason: "
                             + json.dumps(reason) + "\n")

    def test_rollback_working_tree_loader_matches_current_tree_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle_path = Path(directory) / "assets.tar.gz"
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--source-commit",
                INSTALL.source_commit(), "--output", str(bundle_path),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(INSTALL.asset_set_sha256(INSTALL.load_source()),
                             INSTALL.asset_set_sha256(INSTALL.load_bundle(bundle_path)))

    def test_main_prints_clean_refusal_for_already_rolled_back_transaction(self):
        args = type("Args", (), {
            "bundle": None, "endpoint": "https://es.invalid", "ca_file": Path("ca"),
            "kibana_endpoint": "https://kb.invalid", "kibana_ca_file": Path("kb-ca"),
            "admin_credentials_file": Path("admin"), "agent_binary": Path("agent"),
            "profile": "user", "rollback": Path("transaction"), "dry_run": False,
            "ownership_profile": None, "unsafe_test_injection": False,
        })()
        errors = io.StringIO()
        with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
             mock.patch.object(INSTALL, "configure_https"), \
             mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
             mock.patch.object(INSTALL, "fence_remote_ownership_profile"), \
             mock.patch.object(INSTALL, "rollback_transaction",
                               side_effect=INSTALL.ProvisionError(
                                   "install refused: transaction_already_rolled_back")), \
             redirect_stderr(errors):
            self.assertEqual(INSTALL.main(), 1)
        self.assertEqual(errors.getvalue(), "install refused: transaction_already_rolled_back\n")

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

    def test_m1_anchor_pins_searches_ids_and_refuses_absence(self):
        ident = INSTALL.M1_ANCHOR_IDS[0]
        def present(_url, _path, _method, _authorization, body):
            current = body["query"]["ids"]["values"][0]
            return {"hits": {"hits": [{"_id": current, "_source": {"event": {"id": current}}}]}}
        with mock.patch.object(INSTALL, "es_json", side_effect=present) as search:
            pins = INSTALL.m1_anchor_pins("https://es", "auth")
        self.assertEqual(pins[ident], hashlib.sha256(INSTALL.jcs({"event": {"id": ident}})).hexdigest())
        self.assertEqual(search.call_args.args[1], "/" + INSTALL.DIAGNOSIS_STREAM + "/_search")
        with mock.patch.object(INSTALL, "es_json", return_value={"hits": {"hits": []}}):
            with self.assertRaisesRegex(INSTALL.ProvisionError, "m1_anchor_absent"):
                INSTALL.m1_anchor_pins("https://es", "auth")

    def test_rollback_external_oracle_legacy_journal_uses_working_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.value["external_baselines"] = [{"kind": "pipelines", "name": "metrics-rigsignal.cpu-0.5.0",
                                                   "compatibility_projection_sha256": "obsolete"}]
            with mock.patch.object(INSTALL, "verify_external_asset") as verify:
                INSTALL.verify_rollback_external_baselines("https://es", "auth", journal)
            verify.assert_called_once()

    def test_rollback_external_oracle_pinned_bundle_match_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            bundle = INSTALL.load_source()
            ownership = INSTALL.ownership_for_assets(bundle, "fleet-coexist")
            journal.value["external_baselines"] = [
                {"kind": kind, "name": name, "compatibility_projection_sha256": "pinned"}
                for (kind, name), value in ownership.items() if value == "external"
            ]
            journal.value["bundle_pin"] = {"sha256": "applied-bundle-sha",
                                           "source_commit": "applied-commit",
                                           "asset_set_sha256": INSTALL.asset_set_sha256(bundle)}
            with mock.patch.object(INSTALL, "bundle_sha256", return_value="applied-bundle-sha"), \
                 mock.patch.object(INSTALL, "load_bundle", return_value=bundle), \
                 mock.patch.object(INSTALL, "verify_external_asset") as verify:
                INSTALL.verify_rollback_external_baselines("https://es", "auth", journal,
                                                           Path("applied-assets.tar.gz"))
            self.assertEqual(verify.call_count, 39)

    def test_rollback_external_oracle_refuses_pinned_working_tree_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            bundle = INSTALL.load_source()
            ownership = INSTALL.ownership_for_assets(bundle, "fleet-coexist")
            journal.value["external_baselines"] = [
                {"kind": kind, "name": name, "compatibility_projection_sha256": "pinned"}
                for (kind, name), value in ownership.items() if value == "external"
            ]
            journal.value["bundle_pin"] = {"sha256": "applied-bundle-sha",
                                           "source_commit": "applied-commit",
                                           "asset_set_sha256": "different"}
            with self.assertRaisesRegex(INSTALL.ProvisionError,
                                        r"rollback_source_mismatch; provide the applied bundle for recorded source_commit applied-commit"):
                INSTALL.verify_rollback_external_baselines("https://es", "auth", journal)

    def test_rollback_external_oracle_refuses_mismatched_supplied_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.value["bundle_pin"] = {"sha256": "applied-bundle-sha",
                                           "source_commit": "applied-commit",
                                           "asset_set_sha256": "asset-set"}
            with mock.patch.object(INSTALL, "bundle_sha256", return_value="other-bundle-sha"), \
                 mock.patch.object(INSTALL, "load_bundle") as load_bundle, \
                 self.assertRaisesRegex(INSTALL.ProvisionError,
                                        r"rollback_source_mismatch; provide the applied bundle for recorded source_commit applied-commit"):
                INSTALL.verify_rollback_external_baselines("https://es", "auth", journal,
                                                           Path("wrong-assets.tar.gz"))
            load_bundle.assert_not_called()

    def test_rollback_external_oracle_skips_absent_baselines_from_barrier_abort(self):
        # A journal with a bundle pin but no external_baselines key models an
        # abort between pin_bundle and pin_external_baselines (F1-v4/S2-v4):
        # nothing external was journaled, so rollback must not refuse.
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            journal.value["bundle_pin"] = {
                "sha256": "applied-bundle-sha", "source_commit": "applied-commit",
                "asset_set_sha256": INSTALL.asset_set_sha256(INSTALL.load_source())}
            with mock.patch.object(INSTALL, "verify_external_asset") as verify:
                INSTALL.verify_rollback_external_baselines("https://es", "auth", journal)
            verify.assert_not_called()

    def test_external_write_negative_control_requires_env_unsafe_flag_and_loopback(self):
        with mock.patch.dict("os.environ", {"RIGSIGNAL_TEST_EXTERNAL_WRITE": "1"}, clear=False):
            self.assertFalse(INSTALL.external_write_test_allowed("https://owner.example", True))
            self.assertFalse(INSTALL.external_write_test_allowed("https://localhost", False))
            self.assertTrue(INSTALL.external_write_test_allowed("https://localhost", True))

    def test_cluster_health_gate_settles_once_before_its_decisive_check(self):
        with mock.patch.object(INSTALL, "es_json", side_effect=[{}, {"status": "green"}]) as health:
            INSTALL.cluster_health_gate("https://es", "auth")
        self.assertEqual(health.call_args_list, [
            mock.call("https://es", "/_cluster/health?wait_for_events=languid&timeout=30s", "GET", "auth"),
            mock.call("https://es", "/_cluster/health", "GET", "auth"),
        ])

    def test_rollover_injection_fires_for_point_with_stream_suffix(self):
        calls = []
        with mock.patch.object(INSTALL, "request",
                               side_effect=lambda *args, **kwargs: calls.append(args)):
            with mock.patch.dict(INSTALL.os.environ,
                                 {"RIGSIGNAL_TEST_ROLLOVER_AT": "after-fleet-snapshot:logs-x-default"}):
                INSTALL.test_rollover("after-fleet-snapshot", "https://es", "auth",
                                      {"logs-x-default": {}})
            self.assertEqual(len(calls), 1)
            self.assertIn("logs-x-default", calls[0][1])
            with mock.patch.dict(INSTALL.os.environ,
                                 {"RIGSIGNAL_TEST_ROLLOVER_AT": "after-fleet-snapshot"}):
                INSTALL.test_rollover("after-fleet-snapshot", "https://es", "auth",
                                      {"logs-y-default": {}})
            self.assertEqual(len(calls), 2)
            with mock.patch.dict(INSTALL.os.environ,
                                 {"RIGSIGNAL_TEST_ROLLOVER_AT": "other-point:logs-x-default"}):
                INSTALL.test_rollover("after-fleet-snapshot", "https://es", "auth",
                                      {"logs-x-default": {}})
            self.assertEqual(len(calls), 2)

    def test_transaction_journal_rejects_full_schema_matrix_and_tolerates_legacy_absences(self):
        base = {"version": 1, "ownership_profile": "fleet-coexist",
                "ownership_table_version": INSTALL.OWNERSHIP_TABLE_VERSION,
                "intents": [], "proofs": [], "m1_anchors": {}, "apply_ok": True}
        invalid = [
            {"version": value} for value in (None, "1", [], True, False, 2)
        ] + [
            {"transaction_id": value} for value in (None, {}, [], 1)
        ] + [
            {"intents": value} for value in (None, {}, "x")
        ] + [
            {"proofs": value} for value in (None, {}, "x")
        ] + [
            {"m1_anchors": value} for value in (None, [], "x")
        ] + [
            {"apply_ok": value} for value in (None, {}, [], "true", 1)
        ] + [
            {"rollback_ok": value} for value in ({}, [], 1, "true")
        ] + [
            {"transactions": value} for value in (None, {}, "x", 1)
        ]
        for change in invalid:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = INSTALL.secure_root(Path(directory) / "state")
                value = dict(base); value.update(change)
                INSTALL.atomic_write(root, INSTALL.JOURNAL_FILE, INSTALL.jcs(value) + b"\n")
                with self.assertRaisesRegex(INSTALL.ProvisionError, "ownership_profile_mismatch"):
                    INSTALL.TransactionJournal(root, "fleet-coexist")
        for required in ("version", "intents", "proofs", "m1_anchors", "apply_ok"):
            with self.subTest(required_absent=required), tempfile.TemporaryDirectory() as directory:
                root = INSTALL.secure_root(Path(directory) / "state")
                value = dict(base); del value[required]
                INSTALL.atomic_write(root, INSTALL.JOURNAL_FILE, INSTALL.jcs(value) + b"\n")
                with self.assertRaisesRegex(INSTALL.ProvisionError, "ownership_profile_mismatch"):
                    INSTALL.TransactionJournal(root, "fleet-coexist")
        for change in ({}, {"rollback_ok": None}):
            with self.subTest(accepted=change), tempfile.TemporaryDirectory() as directory:
                root = INSTALL.secure_root(Path(directory) / "state")
                value = dict(base); value.update(change)
                INSTALL.atomic_write(root, INSTALL.JOURNAL_FILE, INSTALL.jcs(value) + b"\n")
                journal = INSTALL.TransactionJournal(root, "fleet-coexist")
                self.assertIsInstance(journal.value["transaction_id"], str)
                self.assertEqual(journal.value["transactions"], [])

    def test_transaction_journal_archives_rolled_back_legacy_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "state")
            legacy = {"version": 1, "ownership_profile": "fleet-coexist",
                      "ownership_table_version": INSTALL.OWNERSHIP_TABLE_VERSION,
                      "intents": [], "proofs": [], "m1_anchors": {}, "apply_ok": False,
                      "rollback_ok": True}
            INSTALL.atomic_write(root, INSTALL.JOURNAL_FILE, INSTALL.jcs(legacy) + b"\n")
            fresh = INSTALL.TransactionJournal(root, "fleet-coexist", new_transaction=True)
            self.assertEqual(fresh.value["transactions"][0]["rollback_ok"], True)
            self.assertEqual(fresh.value["transactions"][0]["intents"], [])
            broken = dict(legacy, transactions=None)
            INSTALL.atomic_write(root, INSTALL.JOURNAL_FILE, INSTALL.jcs(broken) + b"\n")
            with self.assertRaisesRegex(INSTALL.ProvisionError, "ownership_profile_mismatch"):
                INSTALL.TransactionJournal(root, "fleet-coexist", new_transaction=True)

    def test_transaction_journal_w_c_terminal_state_table(self):
        for apply_ok, rollback_ok, archives in ((False, True, True), (False, False, False),
                                                (True, True, True), (True, False, True)):
            with self.subTest(apply_ok=apply_ok, rollback_ok=rollback_ok), tempfile.TemporaryDirectory() as directory:
                root = INSTALL.secure_root(Path(directory) / "state")
                value = {"version": 1, "ownership_profile": "fleet-coexist",
                         "ownership_table_version": INSTALL.OWNERSHIP_TABLE_VERSION,
                         "intents": [], "proofs": [], "m1_anchors": {}, "apply_ok": apply_ok,
                         "rollback_ok": rollback_ok, "transactions": []}
                INSTALL.atomic_write(root, INSTALL.JOURNAL_FILE, INSTALL.jcs(value) + b"\n")
                if archives:
                    fresh = INSTALL.TransactionJournal(root, "fleet-coexist", new_transaction=True)
                    self.assertEqual(len(fresh.value["transactions"]), 1)
                else:
                    with self.assertRaisesRegex(INSTALL.ProvisionError, "transaction_recovery_required"):
                        INSTALL.TransactionJournal(root, "fleet-coexist", new_transaction=True)

    def test_fault_supports_bare_matching_and_nonmatching_qualified_forms(self):
        with mock.patch.object(INSTALL.os, "getpid", return_value=1234), \
             mock.patch.object(INSTALL.os, "kill") as kill_hook:
            with mock.patch.dict(INSTALL.os.environ, {"RIGSIGNAL_TEST_CRASH_AT": "point"}, clear=False):
                INSTALL.fault("point", "asset/a")
            with mock.patch.dict(INSTALL.os.environ, {"RIGSIGNAL_TEST_CRASH_AT": "point:asset/a"}, clear=False):
                INSTALL.fault("point", "asset/a")
            with mock.patch.dict(INSTALL.os.environ, {"RIGSIGNAL_TEST_CRASH_AT": "point:asset/b"}, clear=False):
                INSTALL.fault("point", "asset/a")
        self.assertEqual(kill_hook.call_args_list, [mock.call(1234, INSTALL.signal.SIGKILL),
                                                    mock.call(1234, INSTALL.signal.SIGKILL)])

    def test_pause_requires_both_gates_and_flushes_ready_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "resume"
            with mock.patch.dict(INSTALL.os.environ, {"RIGSIGNAL_TEST_PAUSE_AT": "point"}, clear=False):
                INSTALL.test_pause("point", False)
            sentinel.touch()
            output = io.StringIO()
            with mock.patch.dict(INSTALL.os.environ, {
                    "RIGSIGNAL_TEST_PAUSE_AT": "point",
                    "RIGSIGNAL_TEST_PAUSE_SENTINEL": str(sentinel)}, clear=False), redirect_stdout(output):
                INSTALL.test_pause("point", True)
            self.assertEqual(output.getvalue(), "RIGSIGNAL_TEST_PAUSE_REACHED point\n")

    def test_dashboard_target_space_rejects_substring_name(self):
        self.assertEqual(INSTALL.dashboard_target_space(INSTALL.STREAMING_LAB_DASHBOARD), "default")
        with self.assertRaisesRegex(INSTALL.ProvisionError, "unrecognized dashboard"):
            INSTALL.dashboard_target_space("lab.ndjson")

    def test_strict_find_rejects_non_string_origin_and_duplicate_ids(self):
        for rows, expected in (([{"type": "dashboard", "id": "x", "originId": None}], "find_row_malformed"),
                               ([{"type": "dashboard", "id": "x"},
                                 {"type": "dashboard", "id": "x"}], "duplicate_row_id")):
            body = {"page": 1, "total": len(rows), "saved_objects": rows}
            with self.subTest(expected=expected), \
                 mock.patch.object(INSTALL, "request_response", return_value=(200, INSTALL.jcs(body))):
                with self.assertRaisesRegex(INSTALL.InputError, expected):
                    INSTALL._strict_saved_object_find("https://kb", "auth", "default", "dashboard")

    def test_topology_preflight_uses_es_privilege_proof_and_scoped_find_paths(self):
        asset = INSTALL.Asset("dashboard", "rigsignal-engine.ndjson", "fixture",
                              b'{"type":"dashboard","id":"literal","attributes":{}}\n')
        calls = []
        def response(base, path, method, authorization, data=None, headers=None):
            calls.append((base, path, method))
            if path == "/_security/_authenticate":
                return 200, b'{"roles":["superuser"]}'
            if path == "/api/spaces/space":
                return 200, b'[{"id":"default"},{"id":"rigsignal"}]'
            return 200, b'{"page":1,"total":0,"saved_objects":[]}'
        with mock.patch.object(INSTALL, "request_response", side_effect=response), \
             redirect_stdout(io.StringIO()):
            INSTALL.run_topology_preflight(INSTALL.Bundle("test", "test", [asset]),
                                           "https://es", "https://kb", "auth", "fleet-coexist")
        self.assertEqual(calls[0], ("https://es", "/_security/_authenticate", "GET"))
        self.assertIn(("https://kb", "/s/rigsignal/api/saved_objects/_find?type=dashboard&per_page=1000", "GET"), calls)

    def test_regeneration_refuses_non_string_destination_before_url_quoting(self):
        asset = INSTALL.Asset("dashboard", "rigsignal-engine.ndjson", "fixture", b"{}")
        response = {"successResults": [{"type": "dashboard", "id": "literal", "destinationId": 7}]}
        with mock.patch.object(INSTALL.urllib.parse, "quote", side_effect=AssertionError("must not quote")):
            with self.assertRaisesRegex(INSTALL.ProvisionError, "saved_object_id_regenerated"):
                INSTALL.assert_no_id_regeneration("https://kb", "auth", asset, response)

    def test_recovery_sweep_unknown_dashboard_degrades_and_rollback_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = INSTALL.secure_root(Path(directory) / "transaction")
            journal = INSTALL.TransactionJournal(root, "fleet-coexist")
            intent = journal.write_intent("dashboard", "lab.ndjson", "update", "before", "after", b"{}",
                                          object_id="dashboard/literal")
            journal.write_verified(intent, "after")
            with mock.patch.object(INSTALL, "verify_rollback_external_baselines"), \
                 mock.patch.object(INSTALL, "_rollback_live_hash", return_value="before"), \
                 mock.patch.object(INSTALL, "rollback_transaction_proofs"), \
                 mock.patch.object(INSTALL, "verify_m1_anchors"):
                operations = INSTALL.rollback_transaction("https://es", "https://kb", "auth", root)
            self.assertIn("unverified-orphan:dashboard/lab.ndjson", operations)
            self.assertTrue(INSTALL.TransactionJournal(root, "fleet-coexist").value["rollback_ok"])

    def test_recovery_sweep_degrades_find_failures_and_continues_other_triples(self):
        intents = [
            {"kind": "dashboard", "name": "rigsignal-engine.ndjson", "object_id": "dashboard/one",
             "intended_after_sha256": "after"},
            {"kind": "dashboard", "name": "rigsignal-home.ndjson", "object_id": "dashboard/two",
             "intended_after_sha256": "after"},
        ]
        for error in (INSTALL.RequestFailure(503, "offline"), INSTALL.InputError("not-json"),
                      INSTALL.InputError("find_row_malformed")):
            with self.subTest(error=type(error).__name__ + str(error)), \
                 mock.patch.object(INSTALL, "_strict_saved_object_find", side_effect=[error, [], []]):
                operations = INSTALL._recovery_sweep("https://kb", "auth", {"intents": intents})
            self.assertEqual(operations, ["unverified-orphan:dashboard/dashboard/one/rigsignal"])
            self._assert_main_reports_recovery_incomplete(operations)

    def test_recovery_sweep_is_idempotent_for_the_same_converged_live_state(self):
        intents = [{"kind": "dashboard", "name": "rigsignal-engine.ndjson",
                    "object_id": "dashboard/literal", "intended_after_sha256": "after"}]
        with mock.patch.object(INSTALL, "_strict_saved_object_find", return_value=[]):
            self.assertEqual(INSTALL._recovery_sweep("https://kb", "auth", {"intents": intents}), [])
            self.assertEqual(INSTALL._recovery_sweep("https://kb", "auth", {"intents": intents}), [])

    def _assert_main_reports_recovery_incomplete(self, operations):
        args = type("Args", (), {
            "bundle": None, "endpoint": "https://es.invalid", "ca_file": Path("ca"),
            "kibana_endpoint": "https://kb.invalid", "kibana_ca_file": Path("kb-ca"),
            "admin_credentials_file": Path("admin"), "agent_binary": Path("agent"), "profile": "user",
            "rollback": Path("transaction"), "dry_run": False, "ownership_profile": None,
            "unsafe_test_injection": False,
        })()
        output = io.StringIO()
        with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
             mock.patch.object(INSTALL, "configure_https"), \
             mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
             mock.patch.object(INSTALL, "fence_remote_ownership_profile"), \
             mock.patch.object(INSTALL, "rollback_transaction", return_value=operations), \
             redirect_stdout(output):
            self.assertEqual(INSTALL.main(), 0)
        self.assertIn("rollback completed from journaled intents; recovery incomplete: "
                      "unverified-orphan:dashboard/dashboard/one/rigsignal",
                      output.getvalue())

    def test_main_normal_rollback_prints_no_recovery_warning(self):
        args = type("Args", (), {
            "bundle": None, "endpoint": "https://es.invalid", "ca_file": Path("ca"),
            "kibana_endpoint": "https://kb.invalid", "kibana_ca_file": Path("kb-ca"),
            "admin_credentials_file": Path("admin"), "agent_binary": Path("agent"), "profile": "user",
            "rollback": Path("transaction"), "dry_run": False, "ownership_profile": None,
            "unsafe_test_injection": False,
        })()
        output = io.StringIO()
        with mock.patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args), \
             mock.patch.object(INSTALL, "configure_https"), \
             mock.patch.object(INSTALL, "admin_authorization", return_value="auth"), \
             mock.patch.object(INSTALL, "fence_remote_ownership_profile"), \
             mock.patch.object(INSTALL, "rollback_transaction", return_value=["marker"]), \
             redirect_stdout(output):
            self.assertEqual(INSTALL.main(), 0)
        self.assertNotIn("recovery incomplete", output.getvalue())
