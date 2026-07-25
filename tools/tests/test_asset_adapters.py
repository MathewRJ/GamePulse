import importlib.util
import unittest
from copy import deepcopy
from fnmatch import fnmatchcase
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("asset_adapters", ROOT / "tools/asset_adapters.py")
ADAPTERS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ADAPTERS)


class AssetAdapterTests(unittest.TestCase):
    def test_get_projection_get_envelope_parity_by_class(self):
        """9.4.x GET envelopes/defaults must hash like their request bodies."""
        cases = (
            ("component_templates",
             {"template": {"settings": {"index.number_of_shards": 1}, "mappings": {"dynamic": "strict"}}},
             {"component_templates": [{"name": "component", "component_template": {
                 "template": {"settings": {"index.number_of_shards": "1"}, "mappings": {"dynamic": "strict"}},
                 "created_date_millis": 1}}]},
             lambda value: value["component_templates"][0]["component_template"]["template"]["mappings"].update(dynamic="false")),
            ("index_templates",
             {"index_patterns": ["logs-rigsignal-*"], "priority": 200,
              "template": {"settings": {"index.default_pipeline": "p"}}},
             {"index_templates": [{"name": "template", "index_template": {
                 "index_patterns": ["logs-rigsignal-*"], "priority": 200,
                 "template": {"settings": {"index.default_pipeline": "p"}}, "modified_date": "now"}}]},
             lambda value: value["index_templates"][0]["index_template"].update(priority=201)),
            ("pipelines", {"description": "ingest", "processors": [{"set": {"field": "x", "value": 1}}]},
             {"pipeline": {"description": "ingest", "processors": [{"set": {"field": "x", "value": 1}}],
                           "created_date_millis": 1, "modified_date_millis": 2}},
             lambda value: value["pipeline"]["processors"][0]["set"].update(value=2)),
            ("transforms", {"source": {"index": ["metrics-*"]}, "dest": {"index": "timeline"},
                            "pivot": {"group_by": {}}, "settings": {"max_page_search_size": 500}},
             {"transforms": [{"id": "timeline", "source": {"index": ["metrics-*"]}, "dest": {"index": "timeline"},
                              "pivot": {"group_by": {}}, "settings": {"max_page_search_size": 500},
                              "version": "9.4.3", "create_time": 1, "authorization": {"roles": []},
                              "state": "started", "checkpointing": {"last": {}}}]},
             lambda value: value["transforms"][0]["dest"].update(index="other")),
            ("security_roles", {"cluster": ["monitor"], "indices": [{"names": ["logs-*"], "privileges": ["create_doc"]}]},
             {"shipper": {"cluster": ["monitor"], "indices": [{"names": ["logs-*"], "privileges": ["create_doc"],
                                                                   "allow_restricted_indices": False}], "applications": [], "run_as": [],
                           "metadata": {}, "transient_metadata": {"enabled": True}}},
             lambda value: value["shipper"]["indices"][0]["privileges"].append("read")),
            ("kibana_spaces", {"id": "rigsignal", "name": "RigSignal", "description": "locked",
                               "disabledFeatures": ["discover"]},
             {"id": "rigsignal", "name": "RigSignal", "description": "locked", "disabledFeatures": ["discover"],
              "color": None, "imageUrl": "", "solution": "classic", "_reserved": False},
             lambda value: value["disabledFeatures"].clear()),
            ("kibana_roles", {"description": "viewer", "elasticsearch": {"cluster": [], "indices": [{"names": ["logs-*"], "privileges": ["read"]}]},
                              "kibana": [{"base": [], "feature": {"dashboard_v2": ["read"]}, "spaces": ["rigsignal"]}]},
             {"name": "viewer", "description": "viewer", "metadata": {}, "transient_metadata": {"enabled": True},
              "elasticsearch": {"cluster": [], "indices": [{"names": ["logs-*"], "privileges": ["read"],
                                                                 "allow_restricted_indices": False}], "run_as": []},
              "kibana": [{"base": [], "feature": {"dashboard_v2": ["read"]}, "spaces": ["rigsignal"]}],
              "_transform_error": [], "_unrecognized_applications": []},
             lambda value: value["elasticsearch"]["indices"][0]["privileges"].append("write")),
            ("dashboard", {"attributes": {"title": "RigSignal"}, "references": []},
             {"id": "home", "type": "dashboard", "updated_at": "now", "version": "WzEsMV0=",
              "attributes": {"title": "RigSignal"}, "references": []},
             lambda value: value["attributes"].update(title="Other")),
            # The marker is stored as a component template but has its own
            # adapter identity in the rollback contract.
            ("install_marker", {"_meta": {"bundle_version": "v1"}, "template": {"settings": {"index.hidden": False}}},
             {"component_templates": [{"name": "rigsignal-bundle-meta", "component_template": {
                 "_meta": {"bundle_version": "v1"}, "template": {"settings": {"index.hidden": "false"}},
                 "created_date": "now"}}]},
             lambda value: value["component_templates"][0]["component_template"]["_meta"].update(bundle_version="v2")),
        )
        for kind, bundle, live, differ in cases:
            with self.subTest(kind=kind):
                self.assertEqual(ADAPTERS.get_projection(kind, live), ADAPTERS.get_projection(kind, bundle))
                different = deepcopy(live)
                differ(different)
                self.assertNotEqual(ADAPTERS.get_projection(kind, different), ADAPTERS.get_projection(kind, bundle))

    def test_pipeline_envelope_and_timestamp_projection(self):
        live = {"logs-rigsignal.events-0.5.0": {"processors": [], "_meta": {"managed_by": "fleet"},
                                                    "created_date_millis": 1, "modified_date": "now"}}
        self.assertEqual(ADAPTERS.get_projection("pipelines", live)["processors"], [])
        self.assertEqual(ADAPTERS.compatibility_projection("pipelines", live), {"processors": []})

    def test_external_template_permits_only_fleet_composition_members(self):
        live = {"index_templates": [{"name": "metrics-rigsignal.cpu", "index_template": {
            "composed_of": ["metrics-rigsignal.cpu@package", ".fleet_globals-1",
                            ".fleet_agent_id_verification-1"], "_meta": {"managed_by": "fleet"}}}]}
        expected = {"composed_of": ["metrics-rigsignal.cpu@package"],
                    "_meta": {"managed_by": "rigsignal-asset-bundle"}}
        self.assertEqual(ADAPTERS.compatibility_projection("index_templates", live),
                         ADAPTERS.compatibility_projection("index_templates", expected))

    def test_request_body_absent_is_delete_inverse(self):
        self.assertIsNone(ADAPTERS.request_body_from_preimage("dashboard", None))

    def test_dashboard_absent_and_restore_projection(self):
        self.assertIs(ADAPTERS.get_projection("dashboard", None), ADAPTERS.ABSENT)
        live = {"id": "x", "type": "dashboard", "attributes": {"title": "T"}, "references": []}
        self.assertEqual(ADAPTERS.request_body_from_preimage("dashboard", live),
                         {"attributes": {"title": "T"}, "references": []})

    def test_transform_restore_keeps_pivot(self):
        preimage = {"id": "timeline", "pivot": {"group_by": {}}, "_meta": {}}
        self.assertIn("pivot", ADAPTERS.request_body_from_preimage("transforms", preimage))

    def test_simulate_index_equivalence_uses_synthetic_body_and_concrete_live_index(self):
        bundle = {
            "index_patterns": ["logs-rigsignal.events-*"],
            "priority": 200,
            "data_stream": {"hidden": False},
            "template": {"mappings": {"dynamic": "strict"},
                         "settings": {"index.default_pipeline": "p", "index.lifecycle.name": "l"},
                         "aliases": {"rigsignal-events": {}}},
        }
        expected_body, expected_index = ADAPTERS.synthetic_simulation_template(bundle, "0123456789abcdef")
        live_index = ADAPTERS.concrete_index_name(bundle["index_patterns"], "rigsignal-a5-probe")
        self.assertEqual(expected_body["index_patterns"], ["logs-a5sim0123456789abcdef-*"])
        self.assertTrue(expected_index.startswith("logs-"))
        self.assertFalse(fnmatchcase(expected_index, bundle["index_patterns"][0]))
        self.assertEqual(expected_body["data_stream"], bundle["data_stream"])
        calls = []
        def transport(path, body):
            # A body containing the real pattern would reproduce ES's 400
            # equal-priority collision; this test proves the adapter cannot do
            # that.  The live named-template request must have no JSON body.
            if body is not None:
                self.assertNotEqual(body["index_patterns"], bundle["index_patterns"])
                self.assertEqual(body["index_patterns"], ["logs-a5sim0123456789abcdef-*"])
            calls.append((path, body))
            return {"template": {"mappings": {"dynamic": "strict"},
                                  "settings": {"index.default_pipeline": "p", "index.lifecycle.name": "l",
                                               "index.provided_name": "different-per-side"},
                                  "aliases": {"rigsignal-events": {}}}}
        self.assertTrue(ADAPTERS.simulate_index_equivalent(
            transport, "/_index_template/_simulate_index/" + expected_index, expected_body,
            "/_index_template/_simulate_index/" + live_index))
        self.assertEqual(calls[0][0], "/_index_template/_simulate_index/logs-a5sim0123456789abcdef-probe")
        self.assertEqual(calls[1], ("/_index_template/_simulate_index/logs-rigsignal.events-rigsignal-a5-probe", None))

    def test_synthetic_simulation_template_preserves_metrics_name_class(self):
        template = {
            "index_patterns": ["metrics-rigsignal.cpu-*"],
            "data_stream": {"hidden": False},
            "template": {"settings": {"index.mode": "time_series"}},
        }
        synthetic, probe = ADAPTERS.synthetic_simulation_template(template, "abcdef0123456789")
        self.assertEqual(synthetic["index_patterns"], ["metrics-a5simabcdef0123456789-*"])
        self.assertEqual(probe, "metrics-a5simabcdef0123456789-probe")
        self.assertEqual(synthetic["data_stream"], template["data_stream"])
        self.assertFalse(fnmatchcase(probe, template["index_patterns"][0]))

    def test_simulation_outcome_ignores_wall_clock_tsdb_boundaries_only(self):
        before = {"template": {"mappings": {}, "aliases": {}, "settings": {
            "index.mode": "time_series", "index.time_series.start_time": "2026-07-24T12:00:00Z",
            "index.time_series.end_time": "2026-07-24T12:01:00Z", "index.time_series.look_ahead_time": "30m"}}}
        after = deepcopy(before)
        after["template"]["settings"].update({
            "index.time_series.start_time": "2026-07-24T12:00:01Z",
            "index.time_series.end_time": "2026-07-24T12:01:01Z",
        })
        self.assertEqual(ADAPTERS.simulation_outcome(before), ADAPTERS.simulation_outcome(after))
        after["template"]["settings"]["index.time_series.look_ahead_time"] = "45m"
        self.assertNotEqual(ADAPTERS.simulation_outcome(before), ADAPTERS.simulation_outcome(after))

    def test_simulation_dominance_compares_tsdb_dimensions_as_exact_set(self):
        expected = {"template": {"mappings": {}, "aliases": {}, "settings": {
            "index": {"dimensions": ["host.name", "service.name", "data_stream.dataset"]}}}}
        reordered = deepcopy(expected)
        reordered["template"]["settings"]["index"]["dimensions"] = [
            "data_stream.dataset", "host.name", "service.name"]
        self.assertTrue(ADAPTERS.simulation_outcome_dominates(expected, reordered))

        missing = deepcopy(expected)
        missing["template"]["settings"]["index"]["dimensions"] = ["host.name", "service.name"]
        self.assertFalse(ADAPTERS.simulation_outcome_dominates(expected, missing))

        extra = deepcopy(expected)
        extra["template"]["settings"]["index"]["dimensions"].append("container.id")
        self.assertFalse(ADAPTERS.simulation_outcome_dominates(expected, extra))

    def test_simulation_dominance_tolerates_real_fleet_additions_but_not_owned_conflicts(self):
        expected = {"template": {"mappings": {"_meta": {"managed_by": "rigsignal"},
                                               "properties": {"event": {"properties": {"id": {"type": "keyword"}}}}},
                                  "settings": {}, "aliases": {}}}
        live = deepcopy(expected)
        live["template"]["mappings"].update({"date_detection": False, "dynamic_templates": [
            {"strings_as_keyword": {"match_mapping_type": "string", "mapping": {"type": "keyword"}}}]})
        live["template"]["mappings"]["_meta"]["managed_by"] = "fleet"
        live["template"]["settings"] = {"index": {"final_pipeline": ".fleet_final_pipeline-1"}}
        self.assertTrue(ADAPTERS.simulation_outcome_dominates(expected, live))
        live["template"]["mappings"]["properties"]["event"]["properties"]["id"]["type"] = "text"
        self.assertFalse(ADAPTERS.simulation_outcome_dominates(expected, live))
