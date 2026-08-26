import importlib.util
import inspect
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.tests.transaction_transport import ScriptedTransactionTransport

TEST_SOURCE_COMMIT = "a" * 40


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
    def test_lifecycle_delete_phase_free_handles_policy_absence_and_delete_phase(self):
        old_json = INSTALL.es_json
        calls = []
        def responses(_base, path, _method, _authorization):
            calls.append(path)
            if path.endswith("logs-rigsignal-stream-30d"):
                raise INSTALL.RequestFailure(404, "HTTP 404")
            return {"logs@lifecycle": {"policy": {"phases": {"hot": {}}}}}
        INSTALL.es_json = responses
        try:
            INSTALL.lifecycle_delete_phase_free("https://es", "auth")
        finally:
            INSTALL.es_json = old_json
        self.assertEqual(len(calls), 2)

        def missing_builtin(_base, path, _method, _authorization):
            if path.endswith("logs-rigsignal-stream-30d"):
                return {"logs-rigsignal-stream-30d": {"policy": {"phases": {"hot": {}}}}}
            raise INSTALL.RequestFailure(404, "HTTP 404")
        INSTALL.es_json = missing_builtin
        try:
            with self.assertRaises(INSTALL.RequestFailure):
                INSTALL.lifecycle_delete_phase_free("https://es", "auth")
        finally:
            INSTALL.es_json = old_json

        def delete_present(_base, path, _method, _authorization):
            policy = path.rsplit("/", 1)[-1]
            return {policy: {"policy": {"phases": {"delete": {}}}}}
        INSTALL.es_json = delete_present
        try:
            with self.assertRaises(INSTALL.ProvisionError) as error:
                INSTALL.lifecycle_delete_phase_free("https://es", "auth")
        finally:
            INSTALL.es_json = old_json
        self.assertEqual(error.exception.prefix, "install refused: ilm_delete_phase")

    def test_lifecycle_delete_phase_free_test_injector_remains_first(self):
        with patch.dict(os.environ, {"RIGSIGNAL_TEST_ILM_DELETE_PHASE": "1"}):
            with self.assertRaises(INSTALL.ProvisionError) as error:
                INSTALL.lifecycle_delete_phase_free("https://es", "auth")
        self.assertEqual(error.exception.prefix, "install refused: ilm_delete_phase")

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

    def test_canonical_shipper_and_viewer_role_source_pins(self):
        shipper = INSTALL.Asset("security_roles", "rigsignal_shipper",
                                "elastic/security-roles/rigsignal_shipper.json",
                                (ROOT / "elastic/security-roles/rigsignal_shipper.json").read_bytes())
        viewer = INSTALL.Asset("kibana_roles", "rigsignal_viewer",
                               "elastic/kibana-roles/rigsignal_viewer.json",
                               (ROOT / "elastic/kibana-roles/rigsignal_viewer.json").read_bytes())
        bundle = INSTALL.Bundle("test", "test", [shipper, viewer])
        self.assertEqual(hashlib.sha256(INSTALL.jcs(json.loads(shipper.data))).hexdigest(),
                         INSTALL.ROLE_JCS_SHA256)
        self.assertEqual(hashlib.sha256(INSTALL.jcs(json.loads(viewer.data))).hexdigest(),
                         INSTALL.VIEWER_ROLE_JCS_SHA256)
        self.assertEqual(INSTALL.role_body(bundle)["cluster"], ["monitor"])
        self.assertEqual(INSTALL.viewer_role_body(bundle)["kibana"][0]["spaces"], ["rigsignal"])

        mutated = json.loads(viewer.data)
        mutated["description"] = "mutated"
        bad_bundle = INSTALL.Bundle("test", "test", [shipper,
                                  INSTALL.Asset("kibana_roles", "rigsignal_viewer", viewer.path,
                                                json.dumps(mutated).encode())])
        with self.assertRaises(INSTALL.InputError):
            INSTALL.viewer_role_body(bad_bundle)

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
        requests = []
        old_json, old_invalidate = INSTALL.es_json, INSTALL.invalidate
        def lookup(_url, path, *_args):
            requests.append(path)
            if path == "/_security/api_key?name=mint":
                # This is the actual default lookup behavior for an invalidated
                # key; active_only must prevent it reaching invalidate().
                return {"api_keys": [{"name": "mint", "id": "invalidated-key"}]}
            if path == "/_security/api_key?name=mint&active_only=true":
                return {"api_keys": [{"name": "mint", "id": "new-a"},
                                     {"name": "mint", "id": "new-b"}]}
            raise AssertionError(path)
        INSTALL.es_json = lookup
        INSTALL.invalidate = lambda _url, _auth, ids: calls.append(ids)
        try:
            INSTALL.invalidate_mint_name("https://x", "admin", "mint")
            self.assertEqual(calls, [["new-a", "new-b"]])
            self.assertEqual(requests, ["/_security/api_key?name=mint&active_only=true"])
            INSTALL.es_json = lambda *_args: {"api_keys": [{"name": "other", "id": "new-a"}]}
            with self.assertRaises(INSTALL.InputError):
                INSTALL.invalidate_mint_name("https://x", "admin", "mint")
        finally:
            INSTALL.es_json, INSTALL.invalidate = old_json, old_invalidate

    def test_invalidate_accepts_the_es_double_invalidation_response(self):
        responses = iter((
            {"invalidated_api_keys": ["YeEplJ8Ba7kxd7pTgpZq"],
             "previously_invalidated_api_keys": [], "error_count": 0},
            {"invalidated_api_keys": [], "previously_invalidated_api_keys": [], "error_count": 0},
        ))
        old_json = INSTALL.es_json
        INSTALL.es_json = lambda *_args: next(responses)
        try:
            INSTALL.invalidate("https://x", "admin", ["YeEplJ8Ba7kxd7pTgpZq"])
            INSTALL.invalidate("https://x", "admin", ["YeEplJ8Ba7kxd7pTgpZq"])
        finally:
            INSTALL.es_json = old_json

    def test_invalidate_refuses_malformed_or_error_responses(self):
        responses = (
            {"invalidated_api_keys": "key", "previously_invalidated_api_keys": [], "error_count": 0},
            {"invalidated_api_keys": [], "previously_invalidated_api_keys": [], "error_count": 1},
            {"invalidated_api_keys": [], "previously_invalidated_api_keys": [], "error_count": False},
            {"invalidated_api_keys": [], "previously_invalidated_api_keys": [], "error_count": 0,
             "error_details": "unexpected"},
            {"invalidated_api_keys": [], "previously_invalidated_api_keys": [], "error_count": 0,
             "error_details": ["key"]},
            {"invalidated_api_keys": [], "previously_invalidated_api_keys": [], "error_count": 0,
             "error_details": [{"id": "key"}]},
        )
        for response in responses:
            with self.subTest(response=response), \
                 patch.object(INSTALL, "es_json", return_value=response):
                with self.assertRaises(INSTALL.InputError):
                    INSTALL.invalidate("https://x", "admin", ["key"])

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
                                       profile="user", enrollment_root=target, dry_run=False,
                                       ownership_profile=None, rollback=None, unsafe_test_injection=False)
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
                        self.assertEqual(INSTALL.main(), 3)
                finally:
                    (INSTALL.argparse.ArgumentParser.parse_args, INSTALL.load_bundle, INSTALL.role_body,
                     INSTALL.configure_https, INSTALL.admin_authorization, INSTALL.cluster_uuid) = (
                        old_parse, old_bundle, old_role, old_configure, old_auth, old_uuid)
                self.assertEqual(stderr.getvalue(),
                                 "install refused: enrollment_remediation_required\n"
                                 "RIGSIGNAL_FAILURE_SITE preflight\n")
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
            self.assertFalse(any(INSTALL._is_publication_stage_name(root, os.fsencode(path.name))
                                 for path in root.parent.iterdir()))

    def test_publication_stage_forms_and_owned_debris_classifier(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            legacy = root.parent / os.fsdecode(INSTALL._publication_stage_legacy_name(root))
            random_name = INSTALL._publication_stage_prefix(root) + b"-0123456789abcdef"
            self.assertTrue(INSTALL._is_publication_stage_name(root, os.fsencode(legacy.name)))
            self.assertTrue(INSTALL._is_publication_stage_name(root, random_name))
            self.assertFalse(INSTALL._is_publication_stage_name(root, random_name[:-1]))
            self.assertFalse(INSTALL._is_publication_stage_name(root, random_name.upper()))
            legacy.mkdir(mode=0o700); legacy.chmod(0o700)
            self.assertEqual(INSTALL.enrollment_condition(root), "remediation")
            legacy.rmdir()
            lookalike = root.parent / os.fsdecode(random_name)
            lookalike.write_bytes(b"not a directory")
            self.assertEqual(INSTALL.enrollment_condition(root), "clean")
            self.assertTrue(lookalike.exists())

    def test_debris_classifier_random_owned_and_foreign_directory_symlink_non_utf8_are_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            random_name = INSTALL._publication_stage_prefix(root) + b"-0123456789abcdef"
            candidate = root.parent / os.fsdecode(random_name)
            candidate.mkdir(mode=0o700); candidate.chmod(0o700)
            self.assertEqual(INSTALL.enrollment_condition(root), "remediation")
            candidate.rmdir()
            candidate.mkdir(mode=0o700); candidate.chmod(0o700)
            real_stat = os.stat
            def foreign(name, *args, **kwargs):
                value = real_stat(name, *args, **kwargs)
                if name == random_name:
                    fields = list(value); fields[4] = os.geteuid() + 1
                    return os.stat_result(fields)
                return value
            with patch.object(INSTALL.os, "stat", side_effect=foreign):
                self.assertEqual(INSTALL.enrollment_condition(root), "clean")
            self.assertTrue(candidate.is_dir())
            candidate.rmdir()
            target = root.parent / "safe-target"; target.mkdir(mode=0o700)
            candidate.symlink_to(target, target_is_directory=True)
            self.assertEqual(INSTALL.enrollment_condition(root), "clean")
            self.assertTrue(candidate.is_symlink())
            candidate.unlink()
            os.mkdir(os.fsencode(root.parent) + b"/non-utf8-\xff", 0o700)
            self.assertEqual(INSTALL.enrollment_condition(root), "clean")

    def test_single_instance_per_root_residual_is_documented(self):
        contract = (ROOT / "docs/assets-install-exit-contract.md").read_text()
        self.assertIn("one installer instance per enrollment root", contract)
        self.assertIn("not a publication mutex", contract)

    def test_publication_writer_is_final_name_exclusive_and_private(self):
        with tempfile.TemporaryDirectory() as raw:
            stage = Path(raw) / "stage"; stage.mkdir(mode=0o700); stage.chmod(0o700)
            fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
            try:
                INSTALL._write_publication_member(fd, b"credentials.toml", b"secret")
                with self.assertRaises(FileExistsError):
                    INSTALL._write_publication_member(fd, b"credentials.toml", b"replacement")
                self.assertEqual((stage / "credentials.toml").read_bytes(), b"secret")
                self.assertEqual(stat.S_IMODE((stage / "credentials.toml").stat().st_mode), 0o600)
            finally:
                os.close(fd)

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
            self.assertEqual(result.returncode, -9)
            self.assertEqual({name: (root / name).read_bytes() for name in names}, old)

    def test_publication_copies_every_optional_member_with_fd_writer_and_fsyncs(self):
        """The private writer is also used for coexistence-only publication files."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            required = ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json")
            old = {name: ("old-" + name).encode() for name in required}
            new = {name: ("new-" + name).encode() for name in required}
            for name, value in old.items(): INSTALL.atomic_write(root, name, value)
            optional = {
                INSTALL.OWNERSHIP_PROFILE_FILE: b'{"ownership_profile":"fleet-coexist"}',
                "fleet-coexist-journal.json": b'{"journal":true}',
                "fleet-coexist-body-first": b"first body",
                "fleet-coexist-body-second": b"second body",
            }
            for name, value in optional.items(): INSTALL.atomic_write(root, name, value)
            real_fsync = os.fsync
            fsyncs = []
            with patch.object(INSTALL, "atomic_write", side_effect=AssertionError("publication used atomic_write")), \
                 patch.object(INSTALL.os, "fsync", side_effect=lambda fd: (fsyncs.append(fd), real_fsync(fd))[1]):
                INSTALL.atomic_publication(root, new)
            self.assertEqual({name: (root / name).read_bytes() for name in new}, new)
            self.assertEqual({name: (root / name).read_bytes() for name in optional}, optional)
            self.assertGreaterEqual(len(fsyncs), len(required) + len(optional) + 3)
            self.assertTrue(all(stat.S_IMODE((root / name).stat().st_mode) == 0o600
                                for name in (*required, *optional)))

    def test_publication_identity_sites_pre_and_post_exchange_are_terminal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            files = {name: b"new" for name in INSTALL._PUBLICATION_REQUIRED_MEMBERS}
            files = {os.fsdecode(name): value for name, value in files.items()}
            # The first two checks bind root/stage while anchoring.  The
            # following values drive the pre- or post-exchange comparison.
            for position, identities in (("pre", (True, True, False)),
                                         ("post", (True, True, True, True, False))):
                with self.subTest(position=position):
                    tracker = INSTALL.FailureSiteTracker()
                    journal = MagicMock(); tracker.attach_journal(journal)
                    cleanup = None
                    with patch.object(INSTALL, "_name_has_identity", side_effect=identities):
                        if position == "post":
                            with patch.object(INSTALL, "_remove_old_enrollment_generation_fd") as cleanup:
                                with self.assertRaisesRegex(INSTALL.InputError, "identity changed"):
                                    INSTALL.atomic_publication(root, files, tracker)
                        else:
                            with self.assertRaisesRegex(INSTALL.InputError, "identity changed"):
                                INSTALL.atomic_publication(root, files, tracker)
                    self.assertIs(tracker.site, INSTALL.FailureSite.PUBLICATION_IDENTITY)
                    self.assertIsNone(tracker.journal)
                    if position == "post":
                        # Once names have exchanged an identity mismatch is
                        # terminal: it must not delete, swap back, or retry.
                        assert cleanup is not None
                        cleanup.assert_not_called()
                    # A named, private stage remains Bundle-A remediation evidence.
                    self.assertEqual(INSTALL.enrollment_condition(root), "remediation")
                    stages = [entry for entry in root.parent.iterdir()
                              if INSTALL._is_publication_stage_name(root, os.fsencode(entry.name))]
                    self.assertEqual(len(stages), 1)
                    if position == "pre":
                        self.assertEqual({child.name: child.read_bytes() for child in stages[0].iterdir()}, files)
                    for child in stages[0].iterdir():
                        child.unlink()
                    stages[0].rmdir()

    def test_publication_restored_name_aba_writes_only_held_stage_fd(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            required = {os.fsdecode(name): b"new-" + name for name in INSTALL._PUBLICATION_REQUIRED_MEMBERS}
            original = INSTALL._write_publication_member
            moved = []
            def aba(stage_fd, name, data):
                if not moved:
                    stage = next(item for item in root.parent.iterdir()
                                 if INSTALL._is_publication_stage_name(root, os.fsencode(item.name)))
                    parked = root.parent / (stage.name + ".parked")
                    os.rename(stage, parked); stage.mkdir(mode=0o700); stage.rmdir(); os.rename(parked, stage)
                    moved.append(stage)
                return original(stage_fd, name, data)
            with patch.object(INSTALL, "_write_publication_member", side_effect=aba):
                INSTALL.atomic_publication(root, required)
            self.assertEqual({name: (root / name).read_bytes() for name in required}, required)
            self.assertTrue(moved)

    def test_optional_source_identity_binding_rejects_each_source_kind(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "root"; root.mkdir(mode=0o700); root.chmod(0o700)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for name in (*INSTALL._PUBLICATION_OPTIONAL_MEMBERS,
                             b"fleet-coexist-body-one"):
                    with self.subTest(name=name):
                        path = root / os.fsdecode(name); path.write_bytes(b"private"); path.chmod(0o600)
                        with patch.object(INSTALL, "_name_has_identity", return_value=False):
                            with self.assertRaisesRegex(INSTALL.InputError, "source is invalid"):
                                INSTALL._read_publication_member(fd, name)
                        path.unlink()
            finally:
                os.close(fd)

    def test_cleanup_is_descriptor_confined_and_has_no_path_fallbacks(self):
        source = inspect.getsource(INSTALL._remove_candidate_root_fd) + inspect.getsource(
            INSTALL._remove_old_enrollment_generation_fd)
        self.assertNotIn("glob", source)
        self.assertNotIn(".exists(", source)
        self.assertNotIn("remove_candidate_root(", source)
        self.assertIn("dir_fd=", source)

    def test_cleanup_syscalls_and_candidate_recursion_are_dirfd_anchored(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
            for name in INSTALL._PUBLICATION_REQUIRED_MEMBERS:
                INSTALL.atomic_write(root, os.fsdecode(name), b"old")
            candidate = INSTALL.secure_candidate_root(root)
            INSTALL.atomic_write(candidate, "credentials.toml", b"candidate")
            files = {os.fsdecode(name): b"new" for name in INSTALL._PUBLICATION_REQUIRED_MEMBERS}
            real_unlink, real_rmdir = os.unlink, os.rmdir
            unlinks, rmdirs = [], []
            def unlink(name, *args, **kwargs):
                unlinks.append((name, kwargs)); return real_unlink(name, *args, **kwargs)
            def rmdir(name, *args, **kwargs):
                rmdirs.append((name, kwargs)); return real_rmdir(name, *args, **kwargs)
            with patch.object(INSTALL.os, "unlink", side_effect=unlink), \
                 patch.object(INSTALL.os, "rmdir", side_effect=rmdir):
                INSTALL.atomic_publication(root, files)
            self.assertTrue(unlinks and rmdirs)
            self.assertTrue(all(call[1].get("dir_fd") is not None for call in unlinks + rmdirs))

    def test_anchor_and_cleanup_failures_mark_distinct_sites(self):
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw) / "parent"; parent.mkdir(mode=0o775); parent.chmod(0o775)
            unsafe_root = INSTALL.secure_root(parent / "enrollment")
            files = {os.fsdecode(name): b"new" for name in INSTALL._PUBLICATION_REQUIRED_MEMBERS}
            tracker = INSTALL.FailureSiteTracker()
            with self.assertRaises(INSTALL.InputError): INSTALL.atomic_publication(unsafe_root, files, tracker)
            self.assertIs(tracker.site, INSTALL.FailureSite.PUBLICATION_ANCHOR)
            root = Path(raw) / "root"; root.mkdir(mode=0o700); root.chmod(0o700)
            cleanup_tracker = INSTALL.FailureSiteTracker()
            with patch.object(INSTALL, "_remove_old_enrollment_generation_fd",
                              side_effect=INSTALL.InputError("old enrollment generation is invalid")):
                with self.assertRaises(INSTALL.InputError): INSTALL.atomic_publication(root, files, cleanup_tracker)
            self.assertIs(cleanup_tracker.site, INSTALL.FailureSite.PUBLICATION_CLEANUP)

    def test_main_fleet_identity_mismatch_never_persists_journal(self):
        """The actual main() finalizer must not write via a disputed journal name."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"
            bundle = INSTALL.Bundle("test", "test", [])
            transport = ScriptedTransactionTransport(INSTALL, bundle)
            args = SimpleNamespace(
                bundle=Path("unused"), endpoint="https://es.invalid", ca_file=Path("ca"),
                kibana_endpoint="https://kb.invalid", kibana_ca_file=Path("kb"),
                admin_credentials_file=Path("admin"), agent_binary=Path("agent"), profile="user",
                dry_run=False, assets_only=False, rollback=None, ownership_profile="fleet-coexist",
                enrollment_root=root, adopt_existing_w1_stream=False, unsafe_test_injection=True,
                repair=False, upgrade=False, allow_downgrade=False, predecessor_manifest=None,
                assets_marker=root.parent / "marker",
            )
            root_name_checks = 0

            def identity(_fd, name, _expected):
                nonlocal root_name_checks
                if name == os.fsencode(root.name):
                    root_name_checks += 1
                    return root_name_checks != 2
                return True

            patches = (
                patch.object(INSTALL.argparse.ArgumentParser, "parse_args", return_value=args),
                patch.object(INSTALL, "load_bundle", return_value=bundle),
                patch.object(INSTALL, "role_body", return_value={}),
                patch.object(INSTALL, "ownership_for_assets", return_value={}),
                patch.object(INSTALL, "check_version_fence"),
                patch.object(INSTALL, "check_install_root_ancestors"),
                patch.object(INSTALL, "check_outbox_root"),
                patch.object(INSTALL, "check_install_preflight", return_value=(Path("/ca"), Path("/agent"))),
                patch.object(INSTALL, "configure_https"),
                patch.object(INSTALL, "admin_authorization", return_value="admin"),
                patch.object(INSTALL, "admin_credential_kind", return_value="native_user"),
                patch.object(INSTALL, "dispatch_clean_root", return_value=False),
                patch.object(INSTALL, "fence_remote_ownership_profile"),
                patch.object(INSTALL, "run_topology_preflight"),
                patch.object(INSTALL, "fence"),
                patch.object(INSTALL, "cluster_health_gate"),
                patch.object(INSTALL, "remote_stream_condition", return_value=("compatible", {})),
                patch.object(INSTALL, "fleet_stream_snapshot", return_value={}),
                patch.object(INSTALL, "m1_anchor_pins", return_value={}),
                patch.object(INSTALL, "plan_fleet_fence", return_value={}),
                patch.object(INSTALL, "verify_fleet_stream_overrides"),
                patch.object(INSTALL, "verify_fleet_fence"),
                patch.object(INSTALL, "verify_fleet_winner_proofs"),
                patch.object(INSTALL, "verify_late_fleet_fence"),
                patch.object(INSTALL, "verify_m1_anchors"),
                patch.object(INSTALL, "ensure_stream"),
                patch.object(INSTALL, "simulate"),
                patch.object(INSTALL, "recompute_target_generation", return_value="0" * 64),
                patch.object(INSTALL, "verify_stream_behavior"),
                patch.object(INSTALL, "verify_role_matrix"),
                patch.object(INSTALL, "prepublication_asset_fence"),
                patch.object(INSTALL, "bundle_sha256", return_value="bundle-sha"),
                patch.object(INSTALL, "mint_key", side_effect=lambda *_args: (INSTALL.mark_mutation_issued(), ("candidate", "encoded"))[1]),
                patch.object(INSTALL, "_name_has_identity", side_effect=identity),
                patch.object(INSTALL.urllib.request, "urlopen", side_effect=transport.urlopen),
            )
            with ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(INSTALL.main(), 4)
            journal = INSTALL.parse_json((root / INSTALL.JOURNAL_FILE).read_bytes(), INSTALL.JOURNAL_FILE)
            self.assertNotIn("failure_site", journal)

    def test_publication_sigkill_cut_matrix_keeps_a_complete_generation(self):
        """Exercise the four durable cut points in children, never in-process.

        The wrapper installs the scripted transport at the child's urllib seam
        before calling ``main()`` (dry-run is enough to prove that process
        boundary), then drives the real publication helper to its SIGKILL hook.
        """
        names = ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json")
        wrapper = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tools import install_assets as install
from tools.tests.transaction_transport import ScriptedTransactionTransport
root = Path(sys.argv[1])
bundle = install.Bundle("child", "child", [])
transport = ScriptedTransactionTransport(install, bundle)
args = SimpleNamespace(bundle=Path("unused"), endpoint="https://es.invalid", ca_file=Path("ca"),
    kibana_endpoint="https://kb.invalid", kibana_ca_file=Path("kb"), admin_credentials_file=Path("admin"),
    agent_binary=Path("agent"), profile="user", dry_run=True, assets_only=False, rollback=None,
    ownership_profile=None, enrollment_root=root, adopt_existing_w1_stream=False,
    unsafe_test_injection=True, repair=False, upgrade=False, allow_downgrade=False,
    predecessor_manifest=None, assets_marker=root.parent / "marker")
with patch.object(install.argparse.ArgumentParser, "parse_args", return_value=args), \
     patch.object(install, "load_bundle", return_value=bundle), \
     patch.object(install, "role_body", return_value={}), \
     patch.object(install.urllib.request, "urlopen", side_effect=transport.urlopen):
    assert install.main() == 0
files = {name: ("new-" + name).encode() for name in
         ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json")}
files["state.json"] = install.jcs(install.state_template("KUrXRgwRRQu-RikmIJhm0Q", "1" * 64,
    "new", str(root))) + b"\n"
install.atomic_publication(root, files)
'''
        # The fourth hook is intentionally after old-generation removal; it
        # proves the final-barrier state and is clean rather than a false
        # Bundle-A debris refusal.  The first three leave recognized debris.
        cases = (("publication-before-exchange", "old", "remediation"),
                 ("publication-post-exchange-pre-parent-fsync", "new", "remediation"),
                 ("publication-post-parent-fsync-pre-cleanup", "new", "remediation"),
                 ("publication-cleanup-before-final-parent-fsync", "new", "committed"))
        for point, visible, condition in cases:
            with self.subTest(point=point):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw) / "enrollment"; root.mkdir(mode=0o700); root.chmod(0o700)
                    for name in names:
                        data = ("old-" + name).encode()
                        if name == "state.json":
                            data = INSTALL.jcs(INSTALL.state_template(
                                "KUrXRgwRRQu-RikmIJhm0Q", "0" * 64, "old", str(root))) + b"\n"
                        INSTALL.atomic_write(root, name, data)
                    result = subprocess.run([sys.executable, "-c", wrapper, str(root)], cwd=ROOT,
                                            env=os.environ | {"RIGSIGNAL_TEST_CRASH_AT": point},
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, -9, result.stderr)
                    expected = {name: (visible + "-" + name).encode() for name in names}
                    expected["state.json"] = INSTALL.jcs(INSTALL.state_template(
                        "KUrXRgwRRQu-RikmIJhm0Q", ("0" if visible == "old" else "1") * 64,
                        visible, str(root))) + b"\n"
                    self.assertEqual({name: (root / name).read_bytes() for name in names}, expected)
                    self.assertEqual(INSTALL.enrollment_condition(root), condition)

    def test_canonical_get_requires_exact_single_projection(self):
        asset = INSTALL.Asset("security_roles", "rigsignal_shipper", "x", (ROOT / "elastic/security-roles/rigsignal_shipper.json").read_bytes())
        body = json.loads(asset.data)
        self.assertEqual(INSTALL.projection(asset, {asset.name: body}), body)
        with self.assertRaises(INSTALL.InputError): INSTALL.projection(asset, {asset.name: body, "other": body})
        component = INSTALL.Asset("component_templates", "name", "x", b"{}")
        with self.assertRaises(INSTALL.InputError): INSTALL.projection(component, {"component_templates": []})

    def test_external_index_template_simulation_difference_names_asset(self):
        expected = {"index_patterns": ["logs-rigsignal.events-*"], "data_stream": {}, "composed_of": [],
                    "template": {"mappings": {"dynamic": "strict"}, "settings": {}}}
        asset = INSTALL.Asset("index_templates", "logs-rigsignal.events", "x",
                              json.dumps(expected).encode("utf-8"))
        response_body = dict(expected)
        response_body["composed_of"] = [".fleet_globals-1", ".fleet_agent_id_verification-1"]
        response = {"index_templates": [{"name": asset.name, "index_template": response_body}]}

        def simulated(_url, _path, _method, _authorization, payload):
            settings = {} if payload is not None else {"index.mode": "logsdb"}
            mappings = {"dynamic": "strict"} if payload is not None else {"dynamic": "true"}
            return {"template": {"mappings": mappings, "settings": settings, "aliases": {}}}

        with patch.object(INSTALL, "request", return_value=json.dumps(response).encode("utf-8")), \
             patch.object(INSTALL, "es_json", side_effect=simulated), \
             self.assertRaisesRegex(INSTALL.InputError,
                                    r"^external index template simulation differs: index_templates/logs-rigsignal\.events$"):
            INSTALL.verify_external_asset("https://example.invalid", "auth", asset)

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
        def fake_json(_base, path, *_args, **_kwargs):
            if path.startswith("/_data_stream/"):
                return {"data_streams": [{"name": INSTALL.DIAGNOSIS_STREAM,
                    "failure_store": {"enabled": False}, "ilm_policy": INSTALL.W1_LIFECYCLE_POLICY,
                    "indices": [{"index_name": ".ds-one", "index_uuid": "uuid-one"},
                                {"index_name": ".ds-two", "index_uuid": "uuid-two"}]}]}
            if path.startswith("/_ilm/policy/"):
                return {INSTALL.W1_LIFECYCLE_POLICY: {"policy": {"phases": {"hot": {}}}}}
            index = ".ds-one" if ".ds-one" in path else ".ds-two"
            index_uuid = "uuid-one" if index == ".ds-one" else "uuid-two"
            return {index: {"settings": {"index.uuid": index_uuid,
                                         "index.lifecycle.name": INSTALL.W1_LIFECYCLE_POLICY}}} if "_settings" in path else {
                "indices": {index: {"managed": True, "policy": INSTALL.W1_LIFECYCLE_POLICY}}}
        INSTALL.es_json = fake_json
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
        writes = {}
        def fake_json(_base, path, _method, _authorization, _payload=None):
            calls.append(path)
            if path.endswith("::failures/_search"):
                return {"hits": {"hits": []}}
            if path.endswith("/_search"):
                event_id = _payload["query"]["ids"]["values"][0]
                return {"hits": {"hits": [{"_id": event_id, "_source": writes[event_id]}]}}
            return {"result": "created"}
        INSTALL.es_json = fake_json
        def fake_status(_base, path, _method, _authorization, payload=None):
            if "provision-bad" in path or "provision-nested" in path:
                return 400, {"error": {"type": "strict_dynamic_mapping_exception"}}
            if "provision-malformed" in path:
                return 400, {"error": {"type": "document_parsing_exception",
                                        "caused_by": {"type": "number_format_exception"}}}
            writes[payload["event"]["id"]] = payload
            return 201, {"result": "created"}
        INSTALL.es_json_status = fake_status
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
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--source-commit", TEST_SOURCE_COMMIT, "--output",
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
                               enrollment_root=None, dry_run=True, ownership_profile=None,
                               rollback=None, unsafe_test_injection=False)
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
        calls = []
        add_extra_index_pattern = False
        add_run_as_user = False
        live_get_response = {
            "name": "rigsignal_viewer",
            "description": "RigSignal read-only dashboard viewer. Read + view_index_metadata on the rigsignal namespace only; feature reads scoped to the rigsignal space only. No cluster privileges, no write/delete, no other Kibana feature.",
            "metadata": {},
            "transient_metadata": {"enabled": True},
            "elasticsearch": {
                "cluster": [],
                "indices": [{
                    "names": ["logs-rigsignal.*", "metrics-rigsignal.*"],
                    "privileges": ["read", "view_index_metadata"],
                    "allow_restricted_indices": False,
                }],
                "run_as": [],
            },
            "kibana": [{"base": [], "feature": {"dashboard_v2": ["read"]}, "spaces": ["rigsignal"]}],
            "_transform_error": [],
            "_unrecognized_applications": [],
        }
        old_request = INSTALL.request
        def fake_request(_base, path, method, _auth, data=None, headers=None):
            calls.append((method, path, data, headers))
            if method == "GET":
                response = json.loads(json.dumps(live_get_response))
                if add_extra_index_pattern:
                    response["elasticsearch"]["indices"][0]["names"].append("logs-other.*")
                if add_run_as_user:
                    response["elasticsearch"]["run_as"] = ["someuser"]
                return json.dumps(response).encode()
            return b"{}"
        INSTALL.request = fake_request
        try:
            INSTALL.install_asset("https://es", "https://kb", "auth", viewer)
            add_extra_index_pattern = True
            with self.assertRaises(INSTALL.InputError):
                INSTALL.verify_kibana_asset("https://kb", "auth", viewer)
            add_extra_index_pattern = False
            add_run_as_user = True
            with self.assertRaises(INSTALL.InputError):
                INSTALL.verify_kibana_asset("https://kb", "auth", viewer)
        finally: INSTALL.request = old_request
        self.assertEqual([(method, path) for method, path, _data, _headers in calls], [
            ("PUT", "/api/security/role/rigsignal_viewer"), ("GET", "/api/security/role/rigsignal_viewer"),
            ("GET", "/api/security/role/rigsignal_viewer"), ("GET", "/api/security/role/rigsignal_viewer"),
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

    def test_s5_staged_engine_uses_verified_bundle_resources_without_repo_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, engine = root / "assets.tar.gz", root / "engine"
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--version", "0.3.0",
                "--source-commit", TEST_SOURCE_COMMIT, "--output", str(bundle), "--engine-output", str(engine),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            program = """import importlib.util, json, sys
from pathlib import Path
engine, archive = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('staged_install_assets', engine / 'install_assets.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
bundle = module.load_bundle(archive)
assert module.engine_version() == '0.3.0'
assert module.candidate_document('out-of-tree', bundle)['event']['id'] == 'provision-out-of-tree'
assert module.canonical_owned_mapping_projection(bundle)['mappings']['dynamic'] == 'strict'
del bundle.files[module.PROBE_FIXTURE_PATH]
try:
    module.candidate_document('missing-resource', bundle)
except module.InputError as error:
    assert str(error) == 'bundle resource missing: ' + module.PROBE_FIXTURE_PATH
else:
    raise AssertionError('missing bundle resource was accepted')
print('bundle-resources-ok')
"""
            result = subprocess.run([sys.executable, "-c", program, str(engine), str(bundle)], cwd=root,
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "bundle-resources-ok\n")
            self.assertEqual((engine / "_version.py").read_text().splitlines()[1],
                             'ENGINE_VERSION = "0.3.0"')

    def test_s5_version_skew_refuses_before_remote_work(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, engine, agent = root / "assets.tar.gz", root / "engine", root / "agent"
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--version", "0.3.0",
                "--source-commit", TEST_SOURCE_COMMIT, "--output", str(bundle), "--engine-output", str(engine),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            agent.write_text("#!/bin/sh\nprintf 'rigsignal-agent 9.9.9\\n'\n")
            agent.chmod(0o755)
            sentinel = root / "unexpected-http-mutation"
            program = """import importlib.util, sys
from pathlib import Path
engine, archive, agent, enrollment, sentinel = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('staged_install_assets', engine / 'install_assets.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def probe(*args, **kwargs):
    method = args[2] if len(args) > 2 else kwargs.get('method')
    if method in {'PUT', 'POST', 'DELETE'}:
        sentinel.write_text(method)
    raise AssertionError('HTTP reached version fence')
module.request = probe; module.es_json = probe; module.es_json_status = probe
sys.argv = [str(engine / 'install_assets.py'), '--bundle', str(archive),
            '--endpoint', 'https://es.invalid', '--ca-file', str(engine / 'missing-ca.pem'),
            '--kibana-endpoint', 'https://kb.invalid', '--kibana-ca-file', str(engine / 'missing-kb-ca.pem'),
            '--admin-credentials-file', str(engine / 'missing-admin.toml'), '--agent-binary', str(agent),
            '--profile', 'user', '--enrollment-root', str(enrollment)]
sys.exit(module.main())
"""
            result = subprocess.run([
                sys.executable, "-c", program, str(engine), str(bundle), str(agent),
                str(root / "enrollment"), str(sentinel),
            ], cwd=root, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr,
                             "install refused: version_skew; engine=0.3.0; agent=9.9.9; bundle=0.3.0\n"
                             "RIGSIGNAL_FAILURE_SITE preflight\n")
            self.assertFalse(sentinel.exists(), "version fence allowed an HTTP mutation")

    def test_s5_rollback_skew_refuses_before_remote_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _bundle, engine, agent = root / "assets.tar.gz", root / "engine", root / "agent"
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--version", "0.3.0",
                "--source-commit", TEST_SOURCE_COMMIT, "--output", str(_bundle), "--engine-output", str(engine),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            agent.write_text("#!/bin/sh\nprintf 'rigsignal-agent 9.9.9\\n'\n")
            agent.chmod(0o755)
            sentinel, rollback = root / "unexpected-http-mutation", root / "transaction"
            rollback.mkdir()
            rollback.chmod(0o700)
            program = """import importlib.util, sys
from pathlib import Path
engine, agent, rollback, sentinel = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('staged_install_assets', engine / 'install_assets.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def probe(*args, **kwargs):
    method = args[2] if len(args) > 2 else kwargs.get('method')
    if method in {'PUT', 'POST', 'DELETE'}:
        sentinel.write_text(method)
    raise AssertionError('HTTP reached rollback version fence')
module.request = probe; module.es_json = probe; module.es_json_status = probe
sys.argv = [str(engine / 'install_assets.py'), '--endpoint', 'https://es.invalid',
            '--ca-file', str(engine / 'missing-ca.pem'), '--kibana-endpoint', 'https://kb.invalid',
            '--kibana-ca-file', str(engine / 'missing-kb-ca.pem'), '--admin-credentials-file',
            str(engine / 'missing-admin.toml'), '--agent-binary', str(agent), '--profile', 'user',
            '--rollback', str(rollback)]
sys.exit(module.main())
"""
            result = subprocess.run([sys.executable, "-c", program, str(engine), str(agent),
                                     str(rollback), str(sentinel)], cwd=root, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr,
                             "install refused: version_skew; engine=0.3.0; agent=9.9.9; bundle=none\n"
                             "RIGSIGNAL_FAILURE_SITE preflight\n")
            self.assertFalse(sentinel.exists(), "rollback fence allowed an HTTP mutation")

    def test_s5_source_commit_mismatch_refuses_before_remote_work(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundle, mismatched, engine, agent = (root / "assets.tar.gz", root / "mismatched.tar.gz",
                                                  root / "engine", root / "agent")
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--version", "0.3.0",
                "--source-commit", "a" * 40, "--output", str(bundle), "--engine-output", str(engine),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            rewrite_bundle(bundle, mismatched,
                           lambda manifest: manifest.__setitem__("source_commit", "b" * 40))
            agent.write_text("#!/bin/sh\nprintf 'rigsignal-agent 0.3.0\\n'\n")
            agent.chmod(0o755)
            sentinel = root / "unexpected-http-mutation"
            program = """import importlib.util, sys
from pathlib import Path
engine, archive, agent, enrollment, sentinel = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('staged_install_assets', engine / 'install_assets.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def probe(*args, **kwargs):
    method = args[2] if len(args) > 2 else kwargs.get('method')
    if method in {'PUT', 'POST', 'DELETE'}:
        sentinel.write_text(method)
    raise AssertionError('HTTP reached source-commit fence')
module.request = probe; module.es_json = probe; module.es_json_status = probe
sys.argv = [str(engine / 'install_assets.py'), '--bundle', str(archive),
            '--endpoint', 'https://es.invalid', '--ca-file', str(engine / 'missing-ca.pem'),
            '--kibana-endpoint', 'https://kb.invalid', '--kibana-ca-file', str(engine / 'missing-kb-ca.pem'),
            '--admin-credentials-file', str(engine / 'missing-admin.toml'), '--agent-binary', str(agent),
            '--profile', 'user', '--enrollment-root', str(enrollment)]
sys.exit(module.main())
"""
            result = subprocess.run([sys.executable, "-c", program, str(engine), str(mismatched), str(agent),
                                     str(root / "enrollment"), str(sentinel)],
                                    cwd=root, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "install refused: version_skew; engine=0.3.0; agent=0.3.0; "
                             "bundle=0.3.0; engine_commit=" + "a" * 40 + "; bundle_commit=" + "b" * 40 + "\n"
                             "RIGSIGNAL_FAILURE_SITE preflight\n")
            self.assertFalse(sentinel.exists(), "source-commit fence allowed an HTTP mutation")

    def test_s5_rollback_staged_engine_requires_verified_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root, engine = Path(raw), Path(raw) / "engine"
            bundle = root / "assets.tar.gz"
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/build_asset_bundle.py"), "--version", "0.3.0",
                "--source-commit", TEST_SOURCE_COMMIT, "--output", str(bundle), "--engine-output", str(engine),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            sentinel = root / "unexpected-http-mutation"
            agent = root / "agent"
            agent.write_text("#!/bin/sh\nprintf 'rigsignal-agent 0.3.0\\n'\n")
            agent.chmod(0o755)
            program = """import importlib.util, sys
from pathlib import Path
engine, transaction, agent, sentinel = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('staged_install_assets', engine / 'install_assets.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def probe(*args, **kwargs):
    method = args[2] if len(args) > 2 else kwargs.get('method')
    if method in {'PUT', 'POST', 'DELETE'}:
        sentinel.write_text(method)
    raise AssertionError('HTTP reached staged rollback source fence')
module.request = probe; module.es_json = probe; module.es_json_status = probe
module.configure_https = lambda _path: None
module.admin_authorization = lambda _path: 'admin'
module.fence_remote_ownership_profile = lambda *_args: None
sys.argv = [str(engine / 'install_assets.py'), '--endpoint', 'https://es.invalid',
            '--ca-file', str(engine / 'missing-ca.pem'), '--kibana-endpoint', 'https://kb.invalid',
            '--kibana-ca-file', str(engine / 'missing-kb-ca.pem'), '--admin-credentials-file',
            str(engine / 'missing-admin.toml'), '--agent-binary', str(agent), '--profile', 'user',
            '--rollback', str(transaction)]
sys.exit(module.main())
"""
            result = subprocess.run([sys.executable, "-c", program, str(engine), str(root / "transaction"),
                                     str(agent), str(sentinel)],
                                    cwd=root, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stderr,
                             "install refused: rollback_source_unavailable; provide the applied bundle\n"
                             "RIGSIGNAL_FAILURE_SITE preflight\n")
            self.assertFalse(sentinel.exists(), "staged rollback mutated before source refusal")

    def test_s5_malformed_agent_version_is_named_refusal(self):
        with tempfile.TemporaryDirectory() as raw:
            agent = Path(raw) / "agent"
            agent.write_bytes(b"#!/bin/sh\nprintf 'rigsignal-agent 0.3.0\\377\\n'\n")
            agent.chmod(0o755)
            with self.assertRaisesRegex(INSTALL.ProvisionError, "agent_version_unparseable"):
                INSTALL.agent_version(agent)


if __name__ == "__main__":
    unittest.main()
