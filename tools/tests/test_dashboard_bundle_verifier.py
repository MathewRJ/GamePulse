import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_dashboard_bundle", ROOT / "scripts" / "verify-dashboard-bundle.py"
)
VERIFY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VERIFY)


class DashboardBundleVerifierTests(unittest.TestCase):
    def test_committed_bundle_passes_all_consistency_checks(self):
        result = VERIFY.verify_bundle(ROOT / "dashboards" / "v0.3.1")
        self.assertEqual(result, {"files": 7, "objects": 29, "references": 38, "duplicate_groups": 3})

    def test_stringified_internal_reference_rewrites_only_identity_allowlist(self):
        old_id = "rigsignal-engine"
        payload = {
            "internalReferences": [{"id": old_id, "type": "dashboard"}],
            "columnId": old_id,
            "layerId": old_id,
            "sectionId": old_id,
            "paletteId": old_id,
            "explicitInput": {"id": old_id},
            "adHocDataViews": {"local-view": {"id": old_id}},
            "params": {"format": {"id": old_id}},
            "query": {"esql": "FROM metrics-rigsignal.ebpf*"},
            "title": "metrics-rigsignal.session*",
            "note": old_id,
        }
        record = {"attributes": {"panelsJSON": json.dumps(payload)}}

        rewritten = VERIFY.rewrite_stringified_internal_references(record)
        result = json.loads(rewritten["attributes"]["panelsJSON"])

        self.assertEqual(result["internalReferences"][0]["id"], "rigsignal-pkg-engine")
        self.assertEqual(result["columnId"], old_id)
        self.assertEqual(result["layerId"], old_id)
        self.assertEqual(result["sectionId"], old_id)
        self.assertEqual(result["paletteId"], old_id)
        self.assertEqual(result["explicitInput"]["id"], old_id)
        self.assertEqual(result["adHocDataViews"]["local-view"]["id"], old_id)
        self.assertEqual(result["params"]["format"]["id"], old_id)
        self.assertEqual(result["query"]["esql"], "FROM metrics-rigsignal.ebpf*")
        self.assertEqual(result["title"], "metrics-rigsignal.session*")
        self.assertEqual(result["note"], old_id)

    def test_identity_reader_excludes_lookalike_ids_and_query_text(self):
        old_id = "metrics-rigsignal.session*"
        record = {
            "attributes": {
                "state": json.dumps({
                    "internalReferences": [{"id": old_id}],
                    "explicitInput": {"id": old_id},
                    "adHocDataViews": {"view": {"id": old_id}},
                    "params": {"format": {"id": old_id}},
                    "columnId": old_id,
                    "layerId": old_id,
                    "sectionId": old_id,
                    "paletteId": old_id,
                    "query": {"esql": "FROM metrics-rigsignal.session*"},
                    "title": old_id,
                    "arbitrary": old_id,
                })
            }
        }
        self.assertEqual(VERIFY.identity_ids_in_stringified_json(record), [old_id])


if __name__ == "__main__":
    unittest.main()
