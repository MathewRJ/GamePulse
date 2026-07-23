import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUILD, INSTALL = load("build_asset_bundle"), load("install_assets")


def tree(root, pipeline=True, component=True, name="metrics-rigsignal.cpu.json"):
    elastic = root / "elastic"
    for directory in ("component-templates", "index-templates", "pipelines", "transforms", "security-roles"):
        (elastic / directory).mkdir(parents=True)
    (elastic / "security-roles" / "rigsignal_shipper.json").write_text("{}")
    if component:
        (elastic / "component-templates" / "metrics-rigsignal.cpu@package.json").write_text("{}")
    if pipeline:
        (elastic / "pipelines" / "metrics-rigsignal.cpu-0.5.0.json").write_text("{}")
    (elastic / "index-templates" / name).write_text(json.dumps({"composed_of": ["metrics-rigsignal.cpu@package"], "template": {"settings": {"index": {"default_pipeline": "metrics-rigsignal.cpu-0.5.0"}}}}))
    return elastic


class AssetToolsTests(unittest.TestCase):
    def test_missing_referenced_pipeline_fails_build(self):
        with tempfile.TemporaryDirectory() as raw:
            elastic = tree(Path(raw), pipeline=False)
            old_root, old_asset = BUILD.ROOT, BUILD.ASSET_DIR
            BUILD.ROOT, BUILD.ASSET_DIR = elastic.parent, elastic
            try:
                with self.assertRaises(BUILD.BundleError): BUILD.validate_dependencies(BUILD.read_assets())
            finally: BUILD.ROOT, BUILD.ASSET_DIR = old_root, old_asset

    def test_missing_composed_component_and_bad_filename_fail_build(self):
        with tempfile.TemporaryDirectory() as raw:
            elastic = tree(Path(raw), component=False, name="bad name.json")
            old_root, old_asset = BUILD.ROOT, BUILD.ASSET_DIR
            BUILD.ROOT, BUILD.ASSET_DIR = elastic.parent, elastic
            try:
                with self.assertRaises(BUILD.BundleError): BUILD.read_assets()
            finally: BUILD.ROOT, BUILD.ASSET_DIR = old_root, old_asset

    def test_role_path_and_option_a_kat(self):
        role = INSTALL.Asset("security_roles", "rig signal", "elastic/security-roles/rig signal.json", b"{}")
        self.assertEqual(INSTALL.es_path(role), "/_security/role/rig%20signal")
        files = {path: (ROOT / path).read_bytes() for path in INSTALL.W1_RAW_SHA256}
        self.assertEqual(INSTALL.recompute_target_generation(files), INSTALL.TARGET_GENERATION_KAT)
        self.assertEqual(BUILD.target_generation(BUILD.read_assets())["value"], INSTALL.TARGET_GENERATION_KAT)

    def test_state_schema_and_phase_invariants_are_closed(self):
        state = INSTALL.state_template("KUrXRgwRRQu-RikmIJhm0Q", INSTALL.TARGET_GENERATION_KAT, "active")
        self.assertEqual(INSTALL.validate_state(state)["phase"], "committed")
        for mutation in (
            lambda value: value.update(extra=True),
            lambda value: value.update(active_key_id=None),
            lambda value: value.update(pending_revoke_ids=["b", "a"]),
            lambda value: value.update(expected_cluster_uuid="bad"),
            lambda value: value.update(role_jcs_sha256="A" * 64),
        ):
            bad = dict(state); mutation(bad)
            with self.assertRaises(INSTALL.InputError): INSTALL.validate_state(bad)
        intent = dict(state); intent.update(phase="mint_intent", pending_mint_name="mint", active_key_id=None)
        self.assertEqual(INSTALL.validate_state(intent)["phase"], "mint_intent")
        staged = dict(intent); staged.update(phase="candidate_staged", candidate_key_id="candidate")
        self.assertEqual(INSTALL.validate_state(staged)["phase"], "candidate_staged")

    def test_duplicate_state_keys_rejected_and_atomic_mode_is_private(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700)
            root.chmod(0o700)
            (root / "state.json").write_text('{"version":1,"version":1}')
            (root / "state.json").chmod(0o600)
            with self.assertRaises(INSTALL.InputError): INSTALL.load_state(root)
            INSTALL.atomic_write(root, "credentials.toml", b"[elasticsearch]\napi_key = \"secret\"\n")
            self.assertEqual((root / "credentials.toml").stat().st_mode & 0o777, 0o600)

    def test_canonical_get_requires_exact_single_projection(self):
        asset = INSTALL.Asset("security_roles", "rigsignal_shipper", "x", (ROOT / "elastic/security-roles/rigsignal_shipper.json").read_bytes())
        body = json.loads(asset.data)
        self.assertEqual(INSTALL.projection(asset, {asset.name: body}), body)
        with self.assertRaises(INSTALL.InputError): INSTALL.projection(asset, {asset.name: body, "other": body})
        component = INSTALL.Asset("component_templates", "name", "x", b"{}")
        with self.assertRaises(INSTALL.InputError): INSTALL.projection(component, {"component_templates": []})

    def test_fence_refuses_existing_stream_without_committed_matching_state(self):
        old = INSTALL.es_json
        INSTALL.es_json = lambda *a, **k: {"data_streams": [{"name": INSTALL.DIAGNOSIS_STREAM, "indices": [{"index_name": ".ds-x"}]}]}
        try:
            with self.assertRaises(INSTALL.ProvisionError): INSTALL.fence("https://x", "auth", None, "KUrXRgwRRQu-RikmIJhm0Q")
        finally: INSTALL.es_json = old

    def test_fence_compares_every_live_backing_projection(self):
        uuid_value = "KUrXRgwRRQu-RikmIJhm0Q"
        state = INSTALL.state_template(uuid_value, INSTALL.TARGET_GENERATION_KAT, "active")
        projection = {
            "mappings": {
                "properties": {
                    "@timestamp": {"type": "date"},
                    "event": {"properties": {"id": {"type": "keyword"}}},
                    "host": {"properties": {"name": {"type": "keyword"}}},
                    "observer": {"properties": {"name": {"type": "keyword"}}},
                    "rigsignal": {"properties": {"diagnosis": {
                        "dynamic": "strict", "properties": {"schema_version": {"type": "integer"}},
                    }}},
                },
                "dynamic": "strict",
            },
            "settings": {"index.mapping.ignore_malformed": False, "index.failure_store.enabled": False},
        }
        old_json = INSTALL.es_json
        INSTALL.es_json = lambda *a, **k: {"data_streams": [{"name": INSTALL.DIAGNOSIS_STREAM,
            "indices": [{"index_name": ".ds-one"}, {"index_name": ".ds-two"}]}]}
        old_desired, old_backing = INSTALL.simulated_owned_mapping_projection, INSTALL.backing_owned_mapping_projection
        INSTALL.simulated_owned_mapping_projection = lambda *a: projection
        INSTALL.backing_owned_mapping_projection = lambda *a: projection
        try:
            self.assertTrue(INSTALL.existing_stream_is_compatible("https://x", "auth", state, uuid_value))
            INSTALL.backing_owned_mapping_projection = lambda *a: {**projection, "settings": {
                "index.mapping.ignore_malformed": True, "index.failure_store.enabled": False}}
            self.assertFalse(INSTALL.existing_stream_is_compatible("https://x", "auth", state, uuid_value))
        finally:
            INSTALL.es_json, INSTALL.simulated_owned_mapping_projection, INSTALL.backing_owned_mapping_projection = old_json, old_desired, old_backing

    def test_stream_write_rejects_ignored_or_failure_store_and_queries_failures(self):
        old_json = INSTALL.es_json
        old_status = INSTALL.es_json_status
        calls = []
        def fake_json(_base, path, _method, _authorization, _payload=None):
            calls.append(path)
            if path.endswith("::failures/_search"):
                return {"hits": {"hits": []}}
            return {"result": "created"}
        INSTALL.es_json = fake_json
        INSTALL.es_json_status = lambda _base, path, *_a, **_k: (
            (400, {"error": {}}) if "provision-bad" in path or "provision-malformed" in path
            else (201, {"result": "created"})
        )
        try:
            INSTALL.verify_stream_behavior("https://x", "key", "admin", "clean")
            self.assertIn("/" + INSTALL.DIAGNOSIS_STREAM + "::failures/_search", calls)
            with self.assertRaises(INSTALL.InputError):
                INSTALL.assert_accepted_write_clean({"result": "created", "_ignored": ["x"]})
            with self.assertRaises(INSTALL.InputError):
                INSTALL.assert_accepted_write_clean({"result": "created", "failure_store": "used"})
        finally:
            INSTALL.es_json = old_json
            INSTALL.es_json_status = old_status

    def test_matrix_live_legs_fail_when_installer_precondition_is_absent(self):
        script = (ROOT / "scripts/clean-stack/matrix.sh").read_text()
        start = script.index("install_current() {")
        end = script.index("install_previous()", start)
        function = script[start:end]
        result = subprocess.run(["bash", "-c", function + "\nBUNDLE=x; unset CLEAN_STACK_INSTALL_COMMAND; install_current"],
                                text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ASSERT FAIL installer-precondition:", result.stderr)

    def test_fault_hook_is_inert_unless_named(self):
        old = os.environ.pop("RIGSIGNAL_TEST_CRASH_AT", None)
        try: INSTALL.fault("before-mint-response")
        finally:
            if old is not None: os.environ["RIGSIGNAL_TEST_CRASH_AT"] = old


if __name__ == "__main__":
    unittest.main()
