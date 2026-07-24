import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("asset_adapters", ROOT / "tools/asset_adapters.py")
ADAPTERS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ADAPTERS)


class AssetAdapterTests(unittest.TestCase):
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

    def test_simulate_index_equivalence_uses_both_transport_results(self):
        calls = []
        def transport(path):
            calls.append(path)
            return {"template": {"mappings": {"dynamic": "strict"},
                                  "settings": {"index.default_pipeline": "p", "index.lifecycle.name": "l"}}}
        self.assertTrue(ADAPTERS.simulate_index_equivalent(transport, "/before", "/after"))
        self.assertEqual(calls, ["/before", "/after"])
