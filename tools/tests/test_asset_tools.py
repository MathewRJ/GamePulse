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
        published = dict(staged); published.update(phase="candidate_verified", active_key_id="candidate",
                                                   pending_mint_name="published-pending-revoke")
        self.assertEqual(INSTALL.validate_state(published)["phase"], "candidate_verified")
        for mutation in (
            lambda value: value.update(phase="unknown"),
            lambda value: value.update(active_key_id=""),
            lambda value: value.update(pending_revoke_ids=["duplicate", "duplicate"]),
            lambda value: value.update(candidate_key_id="x" * 1025),
        ):
            bad = dict(state); mutation(bad)
            with self.assertRaises(INSTALL.InputError): INSTALL.validate_state(bad)

    def test_recovery_discovers_all_keys_by_mint_intent_name(self):
        calls = []
        old_json, old_invalidate = INSTALL.es_json, INSTALL.invalidate
        INSTALL.es_json = lambda *_args: {"api_keys": [
            {"name": "mint", "id": "new-a"}, {"name": "mint", "id": "new-b"},
        ]}
        INSTALL.invalidate = lambda _url, _auth, ids: calls.append(ids)
        try:
            INSTALL.invalidate_mint_name("https://x", "admin", "mint")
            self.assertEqual(calls, [["new-a", "new-b"]])
            INSTALL.es_json = lambda *_args: {"api_keys": [{"name": "other", "id": "new-a"}]}
            with self.assertRaises(INSTALL.InputError):
                INSTALL.invalidate_mint_name("https://x", "admin", "mint")
        finally:
            INSTALL.es_json, INSTALL.invalidate = old_json, old_invalidate

    def test_duplicate_state_keys_rejected_and_atomic_mode_is_private(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700)
            root.chmod(0o700)
            (root / "state.json").write_text('{"version":1,"version":1}')
            (root / "state.json").chmod(0o600)
            with self.assertRaises(INSTALL.InputError): INSTALL.load_state(root)
            INSTALL.atomic_write(root, "credentials.toml", b"[elasticsearch]\napi_key = \"secret\"\n")
            self.assertEqual((root / "credentials.toml").stat().st_mode & 0o777, 0o600)

    def test_publication_exchanges_all_consumer_files_as_one_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            old = {name: ("old-" + name).encode() for name in
                   ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json")}
            new = {name: ("new-" + name).encode() for name in old}
            for name, value in old.items(): INSTALL.atomic_write(root, name, value)
            candidate = INSTALL.secure_candidate_root(root)
            INSTALL.atomic_write(candidate, "credentials.toml", b"candidate-secret")
            INSTALL.atomic_publication(root, new)
            self.assertEqual({name: (root / name).read_bytes() for name in new}, new)
            self.assertFalse((root / "candidate").exists())
            self.assertFalse((root.parent / ".rigsignal-publication-enrollment").exists())

    def test_publication_fault_leaves_the_previous_generation_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            names = ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json")
            old = {name: ("old-" + name).encode() for name in names}
            for name, value in old.items(): INSTALL.atomic_write(root, name, value)
            program = """from pathlib import Path
from tools import install_assets as i
r=Path(__import__('sys').argv[1])
i.atomic_publication(r, {n: ('new-' + n).encode() for n in ('credentials.toml','handshake.toml','shipping-policy-v1.toml','state.json')})
"""
            environment = os.environ | {"RIGSIGNAL_TEST_CRASH_AT": "publication-credentials.toml"}
            result = subprocess.run([sys.executable, "-c", program, str(root)], cwd=ROOT,
                                    env=environment, capture_output=True, check=False)
            self.assertEqual(result.returncode, 99)
            self.assertEqual({name: (root / name).read_bytes() for name in names}, old)

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
        old_desired, old_canonical, old_backing = (INSTALL.simulated_owned_mapping_projection,
                                                   INSTALL.canonical_owned_mapping_projection,
                                                   INSTALL.backing_owned_mapping_projection)
        INSTALL.simulated_owned_mapping_projection = lambda *a: projection
        INSTALL.canonical_owned_mapping_projection = lambda: projection
        INSTALL.backing_owned_mapping_projection = lambda *a: projection
        try:
            self.assertTrue(INSTALL.existing_stream_is_compatible("https://x", "auth", state, uuid_value))
            INSTALL.backing_owned_mapping_projection = lambda *a: {**projection, "settings": {
                "index.mapping.ignore_malformed": True, "index.failure_store.enabled": False}}
            self.assertFalse(INSTALL.existing_stream_is_compatible("https://x", "auth", state, uuid_value))
        finally:
            (INSTALL.es_json, INSTALL.simulated_owned_mapping_projection,
             INSTALL.canonical_owned_mapping_projection, INSTALL.backing_owned_mapping_projection) = (
                old_json, old_desired, old_canonical, old_backing)

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

        opt_in = subprocess.run([
            "bash", "-c", function + "\n"
            + f"SCRIPT_DIR={ROOT / 'scripts/clean-stack'}; BUNDLE=x; "
            + "unset CLEAN_STACK_INSTALL_COMMAND CS_ES_URL; "
            + "CLEAN_STACK_ALLOW_DEFAULT_INSTALLER=1; install_current",
        ], text=True, capture_output=True, check=False)
        self.assertNotEqual(opt_in.returncode, 0)
        self.assertIn("CS_ES_URL must be set by the clean-stack harness", opt_in.stderr)
        self.assertNotIn("ASSERT FAIL installer-precondition:", opt_in.stderr)

    def test_clean_stack_wrapper_assembles_tls_installer_arguments(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, ca, agent = root / "assets.tar.gz", root / "ca.pem", root / "rigsignal-agent"
            bundle.touch()
            ca.touch()
            agent.touch()
            agent.chmod(0o755)
            args_file = root / "installer-args"
            installer = root / "installer-stub.py"
            installer.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['WRAPPER_ARGS_FILE']).write_text('\\n'.join(sys.argv[1:]))\n"
            )
            installer.chmod(0o755)
            enrollment_root = root / "enrollment"
            environment = os.environ.copy()
            environment.update({
                "CS_RUN_DIR": str(root),
                "CS_ES_URL": "https://localhost:9200",
                "CS_KIBANA_URL": "https://localhost:5601",
                "CS_CA_FILE": str(ca),
                "ELASTIC_PASSWORD": "test-password",
                "CLEAN_STACK_AGENT_BINARY": str(agent),
                "CLEAN_STACK_INSTALLER": str(installer),
                "WRAPPER_ARGS_FILE": str(args_file),
            })
            result = subprocess.run(
                [str(ROOT / "scripts/clean-stack/install-wrapper.sh"), "--bundle", str(bundle),
                 "--enrollment-root", str(enrollment_root)],
                text=True, capture_output=True, env=environment, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "admin-credentials.toml").stat().st_mode & 0o777, 0o600)
            self.assertEqual(args_file.read_text().splitlines(), [
                "--bundle", str(bundle),
                "--endpoint", "https://localhost:9200",
                "--ca-file", str(ca),
                "--kibana-endpoint", "https://localhost:5601",
                "--kibana-ca-file", str(ca),
                "--admin-credentials-file", str(root / "admin-credentials.toml"),
                "--agent-binary", str(agent),
                "--profile", "user",
                "--enrollment-root", str(enrollment_root),
            ])

    def test_fault_hook_is_inert_unless_named(self):
        old = os.environ.pop("RIGSIGNAL_TEST_CRASH_AT", None)
        try: INSTALL.fault("before-mint-response")
        finally:
            if old is not None: os.environ["RIGSIGNAL_TEST_CRASH_AT"] = old


if __name__ == "__main__":
    unittest.main()
