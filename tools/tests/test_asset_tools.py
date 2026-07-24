import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

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
    for directory in ("component-templates", "index-templates", "pipelines", "transforms", "security-roles",
                      "kibana-spaces", "kibana-roles"):
        (elastic / directory).mkdir(parents=True)
    (elastic / "security-roles" / "rigsignal_shipper.json").write_text("{}")
    if component:
        (elastic / "component-templates" / "metrics-rigsignal.cpu@package.json").write_text("{}")
    if pipeline:
        (elastic / "pipelines" / "metrics-rigsignal.cpu-0.5.0.json").write_text("{}")
    (elastic / "index-templates" / name).write_text(json.dumps({"composed_of": ["metrics-rigsignal.cpu@package"], "template": {"settings": {"index": {"default_pipeline": "metrics-rigsignal.cpu-0.5.0"}}}}))
    return elastic


def w2_asset(directory, name):
    path = ROOT / "elastic" / directory / name
    return INSTALL.path_to_asset(path.relative_to(ROOT).as_posix(), path.read_bytes())


def rewrite_bundle(source, destination, mutate):
    with tarfile.open(source, "r:gz") as archive:
        contents = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
    manifest = json.loads(contents["manifest.json"])
    mutate(manifest)
    contents["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    with tarfile.open(destination, "w:gz") as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


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
        state = INSTALL.state_template("KUrXRgwRRQu-RikmIJhm0Q", INSTALL.TARGET_GENERATION_KAT,
                                       "active", "/tmp/rigsignal-test-enrollment")
        self.assertEqual(INSTALL.validate_state(state)["phase"], "committed")
        for mutation in (
            lambda value: value.update(extra=True),
            lambda value: value.pop("enrollment_root"),
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

    def test_enrollment_root_binding_rejects_noncanonical_and_overlong_values(self):
        state = INSTALL.state_template("KUrXRgwRRQu-RikmIJhm0Q", INSTALL.TARGET_GENERATION_KAT,
                                       "active", "/tmp/rigsignal-test-enrollment")
        for binding in ("relative/enrollment", "/tmp/rigsignal-test-enrollment/",
                        "/tmp/enrollment\0root", "/" + "x" * 4096):
            bad = dict(state)
            bad["enrollment_root"] = binding
            with self.assertRaises(INSTALL.InputError):
                INSTALL.validate_state(bad)

    def test_enrollment_root_rejects_intermediate_symlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw) / "parent"
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            link = Path(raw) / "linked-parent"
            link.symlink_to(parent, target_is_directory=True)
            with self.assertRaises(INSTALL.InputError):
                INSTALL.secure_root(link / "enrollment")
            self.assertFalse((parent / "enrollment").exists())

    def test_same_root_round_trip_and_override_bind_canonical_actual_root(self):
        with tempfile.TemporaryDirectory() as raw:
            override = INSTALL.secure_root(Path(raw) / "override")
            state = INSTALL.state_template("KUrXRgwRRQu-RikmIJhm0Q", INSTALL.TARGET_GENERATION_KAT,
                                           "active", str(override))
            INSTALL.atomic_write(override, "state.json", INSTALL.jcs(state) + b"\n")
            self.assertEqual(INSTALL.load_state(override), state)
            self.assertEqual(state["enrollment_root"], os.path.realpath(override))

    def test_copied_state_refuses_before_installer_http_in_every_phase(self):
        uuid_value = "KUrXRgwRRQu-RikmIJhm0Q"
        for phase in ("committed", "mint_intent", "candidate_staged", "candidate_verified"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                source = INSTALL.secure_root(Path(raw) / "source")
                target = INSTALL.secure_root(Path(raw) / "target")
                state = INSTALL.state_template(uuid_value, INSTALL.TARGET_GENERATION_KAT, "active", str(source))
                if phase == "mint_intent":
                    state.update(phase=phase, active_key_id=None, pending_mint_name="mint")
                elif phase == "candidate_staged":
                    state.update(phase=phase, pending_mint_name="mint", candidate_key_id="candidate")
                elif phase == "candidate_verified":
                    state.update(phase=phase, pending_mint_name="mint", candidate_key_id="candidate")
                INSTALL.atomic_write(target, "state.json", INSTALL.jcs(state) + b"\n")
                before = (target / "state.json").read_bytes()
                calls = []
                args = SimpleNamespace(bundle=Path("unused"), endpoint="https://example.invalid",
                                       ca_file=Path("unused-ca"), kibana_endpoint="https://example.invalid",
                                       kibana_ca_file=Path("unused-kibana-ca"),
                                       admin_credentials_file=Path("unused-admin"), agent_binary=Path("unused-agent"),
                                       profile="user", enrollment_root=target, dry_run=False)
                old_parse = INSTALL.argparse.ArgumentParser.parse_args
                old_bundle, old_role = INSTALL.load_bundle, INSTALL.role_body
                old_configure, old_auth, old_uuid = (INSTALL.configure_https, INSTALL.admin_authorization,
                                                     INSTALL.cluster_uuid)
                INSTALL.argparse.ArgumentParser.parse_args = lambda _parser: args
                INSTALL.load_bundle = lambda _path: INSTALL.Bundle("test", "test", [])
                INSTALL.role_body = lambda _bundle: {}
                INSTALL.configure_https = lambda _path: None
                INSTALL.admin_authorization = lambda _path: "admin"
                INSTALL.cluster_uuid = lambda *_args: calls.append("http")
                stderr = io.StringIO()
                try:
                    with redirect_stderr(stderr):
                        self.assertEqual(INSTALL.main(), 1)
                finally:
                    (INSTALL.argparse.ArgumentParser.parse_args, INSTALL.load_bundle, INSTALL.role_body,
                     INSTALL.configure_https, INSTALL.admin_authorization, INSTALL.cluster_uuid) = (
                        old_parse, old_bundle, old_role, old_configure, old_auth, old_uuid)
                self.assertEqual(stderr.getvalue(),
                                 "install refused: enrollment state is not valid for this enrollment root\n")
                self.assertEqual(calls, [])
                self.assertEqual((target / "state.json").read_bytes(), before)

    def test_purge_refuses_copied_state_before_http_or_deletion(self):
        with tempfile.TemporaryDirectory() as raw:
            source = INSTALL.secure_root(Path(raw) / "source")
            target = INSTALL.secure_root(Path(raw) / "target")
            state = INSTALL.state_template("KUrXRgwRRQu-RikmIJhm0Q", INSTALL.TARGET_GENERATION_KAT,
                                           "active", str(source))
            INSTALL.atomic_write(target, "state.json", INSTALL.jcs(state) + b"\n")
            before = (target / "state.json").read_bytes()
            ca, admin = Path(raw) / "ca", Path(raw) / "admin.toml"
            ca.write_text("ca"); admin.write_text('[elasticsearch]\napi_key = "key"\n')
            ca.chmod(0o600); admin.chmod(0o600)
            result = subprocess.run([
                "bash", str(ROOT / "packaging/uninstall.sh"), "--purge", "--endpoint", "https://example.invalid",
                "--ca-file", str(ca), "--admin-credentials-file", str(admin), "--enrollment-root", str(target),
            ], text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "uninstall purge failed: enrollment state validation:\n")
            self.assertEqual((target / "state.json").read_bytes(), before)

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
        root_handle = tempfile.TemporaryDirectory()
        root = INSTALL.secure_root(Path(root_handle.name) / "enrollment")
        state = INSTALL.state_template(uuid_value, INSTALL.TARGET_GENERATION_KAT, "active", str(root))
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
            self.assertTrue(INSTALL.existing_stream_is_compatible("https://x", "auth", state, uuid_value, root))
            INSTALL.backing_owned_mapping_projection = lambda *a: {**projection, "settings": {
                "index.mapping.ignore_malformed": True, "index.failure_store.enabled": False}}
            self.assertFalse(INSTALL.existing_stream_is_compatible("https://x", "auth", state, uuid_value, root))
        finally:
            (INSTALL.es_json, INSTALL.simulated_owned_mapping_projection,
             INSTALL.canonical_owned_mapping_projection, INSTALL.backing_owned_mapping_projection) = (
                old_json, old_desired, old_canonical, old_backing)
            root_handle.cleanup()

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

    def test_w2_a1_closed_taxonomy_bundle_round_trip_and_manifest_rejections(self):
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw) / "assets.tar.gz"
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--source-commit", "test", "--output",
                str(bundle),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            loaded = INSTALL.load_bundle(bundle)
            self.assertEqual(INSTALL.count_assets(loaded.assets)["kibana_spaces"], 1)
            self.assertEqual(INSTALL.count_assets(loaded.assets)["kibana_roles"], 1)
            with self.assertRaises(INSTALL.InputError):
                INSTALL.path_to_asset("elastic/unknown/thing.json", b"{}")
            with self.assertRaises(INSTALL.InputError):
                INSTALL.path_to_asset("elastic/kibana-spaces/bad.json", b"{")
            missing_count = Path(raw) / "missing-count.tar.gz"
            rewrite_bundle(bundle, missing_count, lambda manifest: manifest["counts"].pop("kibana_spaces"))
            with self.assertRaises(INSTALL.InputError): INSTALL.load_bundle(missing_count)
            bad_checksum = Path(raw) / "bad-checksum.tar.gz"
            rewrite_bundle(bundle, bad_checksum,
                           lambda manifest: manifest["sha256"].update({
                               "elastic/kibana-spaces/rigsignal.json": "0" * 64}))
            with self.assertRaises(INSTALL.InputError): INSTALL.load_bundle(bad_checksum)

    def test_w2_a2_extended_ordering_and_dry_run_paths(self):
        assets = [
            INSTALL.Asset("dashboard", "rigsignal-home.ndjson", "dashboards/v0.3.1/rigsignal-home.ndjson", b'{"type":"dashboard","id":"home"}\n'),
            INSTALL.Asset("kibana_roles", "viewer", "elastic/kibana-roles/viewer.json", b"{}"),
            INSTALL.Asset("transforms", "transform", "elastic/transforms/transform.json", b"{}"),
            INSTALL.Asset("kibana_spaces", "rigsignal", "elastic/kibana-spaces/rigsignal.json", b"{}"),
            INSTALL.Asset("security_roles", "shipper", "elastic/security-roles/shipper.json", b"{}"),
            INSTALL.Asset("component_templates", "component", "elastic/component-templates/component.json", b"{}"),
            INSTALL.Asset("index_templates", "index", "elastic/index-templates/index.json", b"{}"),
            INSTALL.Asset("pipelines", "pipeline", "elastic/pipelines/pipeline.json", b"{}"),
        ]
        self.assertEqual([asset.kind for asset in INSTALL.ordered_assets(assets)], [
            "component_templates", "index_templates", "security_roles", "pipelines", "transforms",
            "kibana_spaces", "kibana_roles", "dashboard",
        ])
        args = SimpleNamespace(bundle=Path("unused"), endpoint="https://es.invalid", ca_file=Path("unused"),
                               kibana_endpoint="https://kb.invalid", kibana_ca_file=Path("unused"),
                               admin_credentials_file=Path("unused"), agent_binary=Path("unused"), profile="user",
                               enrollment_root=None, dry_run=True)
        old_parse, old_bundle, old_role = (INSTALL.argparse.ArgumentParser.parse_args, INSTALL.load_bundle,
                                           INSTALL.role_body)
        INSTALL.argparse.ArgumentParser.parse_args = lambda _parser: args
        INSTALL.load_bundle = lambda _path: INSTALL.Bundle("test", "test", INSTALL.ordered_assets(assets))
        INSTALL.role_body = lambda _bundle: {}
        output = io.StringIO()
        try:
            with redirect_stdout(output): self.assertEqual(INSTALL.main(), 0)
        finally:
            INSTALL.argparse.ArgumentParser.parse_args, INSTALL.load_bundle, INSTALL.role_body = (
                old_parse, old_bundle, old_role)
        self.assertIn("kibana-space rigsignal -> GET /api/spaces/space/rigsignal; POST /api/spaces/space; PUT /api/spaces/space/rigsignal", output.getvalue())
        self.assertIn("kibana-role viewer -> PUT/GET /api/security/role/viewer", output.getvalue())
        self.assertIn("dashboard rigsignal-home.ndjson -> POST /s/rigsignal/api/saved_objects/_import?overwrite=true", output.getvalue())

    def test_w2_a3_space_first_apply_and_reapply_verify_variant_a(self):
        asset = w2_asset("kibana-spaces", "rigsignal.json")
        expected = json.loads(asset.data)
        calls, statuses = [], iter((404, 200))
        old_request, old_response = INSTALL.request, INSTALL.request_response
        def fake_response(_base, path, method, _auth, data=None, headers=None):
            calls.append((method, path, data, headers))
            status = next(statuses)
            if status == 404: raise INSTALL.RequestFailure(404, "HTTP 404")
            return status, b"{}"
        def fake_request(_base, path, method, _auth, data=None, headers=None):
            calls.append((method, path, data, headers))
            return json.dumps(expected).encode() if method == "GET" else b"{}"
        INSTALL.request, INSTALL.request_response = fake_request, fake_response
        try:
            INSTALL.install_asset("https://es", "https://kb", "auth", asset)
            INSTALL.install_asset("https://es", "https://kb", "auth", asset)
        finally:
            INSTALL.request, INSTALL.request_response = old_request, old_response
        self.assertNotIn("solution", expected)
        self.assertEqual(expected["id"], "rigsignal")
        self.assertEqual(sum(method == "POST" and path == "/api/spaces/space" for method, path, *_ in calls), 1)
        self.assertEqual(sum(method == "PUT" and path == "/api/spaces/space/rigsignal" for method, path, *_ in calls), 1)
        self.assertEqual(sum(method == "GET" and path == "/api/spaces/space/rigsignal" for method, path, *_ in calls), 4)
        self.assertTrue(all(headers == {"kbn-xsrf": "true"} for _method, _path, _data, headers in calls))

    def test_w2_space_preflight_unexpected_status_hard_fails_before_mutation(self):
        asset = w2_asset("kibana-spaces", "rigsignal.json")
        old_request, old_response = INSTALL.request, INSTALL.request_response
        try:
            for status in (500, 201):
                with self.subTest(status=status):
                    calls = []
                    def fake_response(_base, path, method, _auth, data=None, headers=None):
                        calls.append((method, path, data, headers))
                        if status == 500:
                            raise INSTALL.RequestFailure(status, f"HTTP {status}")
                        return status, b"{}"
                    INSTALL.request_response = fake_response
                    with self.assertRaises((INSTALL.RequestFailure, INSTALL.InputError)):
                        INSTALL.install_asset("https://es", "https://kb", "auth", asset)
                    self.assertEqual([(method, path) for method, path, _data, _headers in calls], [
                        ("GET", "/api/spaces/space/rigsignal"),
                    ])
        finally:
            INSTALL.request, INSTALL.request_response = old_request, old_response

    def test_w2_a4_kibana_role_and_native_security_role_routes_and_projections(self):
        viewer = w2_asset("kibana-roles", "rigsignal_viewer.json")
        expected = json.loads(viewer.data)
        calls = []
        old_request = INSTALL.request
        def fake_request(_base, path, method, _auth, data=None, headers=None):
            calls.append((method, path, data, headers))
            if method == "GET":
                return json.dumps({"name": "rigsignal_viewer", **expected, "metadata": {}}).encode()
            return b"{}"
        INSTALL.request = fake_request
        try: INSTALL.install_asset("https://es", "https://kb", "auth", viewer)
        finally: INSTALL.request = old_request
        self.assertEqual([(method, path) for method, path, _data, _headers in calls], [
            ("PUT", "/api/security/role/rigsignal_viewer"), ("GET", "/api/security/role/rigsignal_viewer"),
        ])
        self.assertEqual(INSTALL.es_path(INSTALL.Asset("security_roles", "shipper", "x", b"{}")),
                         "/_security/role/shipper")

    def test_w2_a5_product_dashboards_use_space_import_and_reject_bad_results(self):
        assets = [INSTALL.path_to_asset("dashboards/v0.3.1/" + name,
                                        (ROOT / "dashboards/v0.3.1" / name).read_bytes())
                  for name in sorted(INSTALL.PRODUCT_DASHBOARDS)]
        calls = []
        old_request = INSTALL.request
        def fake_request(_base, path, method, _auth, data=None, headers=None):
            calls.append((method, path))
            if method == "POST":
                asset = next(item for item in assets if item.name.encode() in data)
                objects = INSTALL.dashboard_objects(asset.data)
                return json.dumps({"success": True, "successCount": len(objects),
                                   "successResults": [{"type": kind, "id": name} for kind, name in objects]}).encode()
            return b"{}"
        INSTALL.request = fake_request
        try:
            for asset in assets: INSTALL.install_asset("https://es", "https://kb", "auth", asset)
        finally: INSTALL.request = old_request
        imports = [path for method, path in calls if method == "POST"]
        self.assertEqual(imports, ["/s/rigsignal/api/saved_objects/_import?overwrite=true"] * 6)
        with self.assertRaises(INSTALL.InputError):
            INSTALL.assert_dashboard_import_result(assets[0], {"success": True, "successCount": 0,
                                                                "successResults": []})

    def test_w2_a6_repeat_installation_repeats_space_role_and_dashboard_checks(self):
        space, role = w2_asset("kibana-spaces", "rigsignal.json"), w2_asset("kibana-roles", "rigsignal_viewer.json")
        dashboards = [INSTALL.path_to_asset("dashboards/v0.3.1/" + name,
                                             (ROOT / "dashboards/v0.3.1" / name).read_bytes())
                      for name in sorted(INSTALL.PRODUCT_DASHBOARDS)]
        space_body, role_body, calls = json.loads(space.data), json.loads(role.data), []
        old_request, old_response = INSTALL.request, INSTALL.request_response
        INSTALL.request_response = lambda *_args, **_kwargs: (200, b"{}")
        def fake_request(_base, path, method, _auth, data=None, headers=None):
            calls.append((method, path))
            if method == "GET" and path == "/api/spaces/space/rigsignal": return json.dumps(space_body).encode()
            if method == "GET" and path == "/api/security/role/rigsignal_viewer": return json.dumps(role_body).encode()
            if method == "POST":
                for asset in dashboards:
                    if asset.name.encode() in data:
                        objects = INSTALL.dashboard_objects(asset.data)
                        return json.dumps({"success": True, "successCount": len(objects),
                                           "successResults": [{"type": kind, "id": name} for kind, name in objects]}).encode()
            return b"{}"
        INSTALL.request = fake_request
        try:
            for _run in range(2):
                for asset in [space, role, *dashboards]: INSTALL.install_asset("https://es", "https://kb", "auth", asset)
        finally:
            INSTALL.request, INSTALL.request_response = old_request, old_response
        self.assertEqual(calls.count(("PUT", "/api/spaces/space/rigsignal")), 2)
        self.assertEqual(calls.count(("PUT", "/api/security/role/rigsignal_viewer")), 2)
        self.assertEqual(calls.count(("POST", "/s/rigsignal/api/saved_objects/_import?overwrite=true")), 12)

    def test_w2_a7_streaming_lab_keeps_default_space_handling(self):
        asset = INSTALL.path_to_asset("dashboards/v0.3.1/rigsignal-streaming-lab.ndjson",
                                      (ROOT / "dashboards/v0.3.1/rigsignal-streaming-lab.ndjson").read_bytes())
        calls = []
        old_request = INSTALL.request
        def fake_request(_base, path, method, _auth, data=None, headers=None):
            calls.append((method, path))
            if method == "POST":
                objects = INSTALL.dashboard_objects(asset.data)
                return json.dumps({"success": True, "successCount": len(objects),
                                   "successResults": [{"type": kind, "id": name} for kind, name in objects]}).encode()
            return b"{}"
        INSTALL.request = fake_request
        try: INSTALL.install_asset("https://es", "https://kb", "auth", asset)
        finally: INSTALL.request = old_request
        self.assertIn(("POST", "/api/saved_objects/_import?overwrite=true"), calls)
        self.assertFalse(any("/s/rigsignal/" in path for _method, path in calls))


if __name__ == "__main__":
    unittest.main()
