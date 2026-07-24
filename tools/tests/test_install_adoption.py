import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("install_assets", ROOT / "tools/install_assets.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class InstallAdoptionTests(unittest.TestCase):
    def test_enrollment_condition_keeps_adoption_path_narrow(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "enrollment"
            self.assertEqual(INSTALL.enrollment_condition(root), "clean")
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            self.assertEqual(INSTALL.enrollment_condition(root), "clean")

            state = INSTALL.state_template("KUrXRgwRRQu-RikmIJhm0Q", INSTALL.TARGET_GENERATION_KAT,
                                           "active", str(root))
            INSTALL.atomic_write(root, "state.json", INSTALL.jcs(state) + b"\n")
            self.assertEqual(INSTALL.enrollment_condition(root), "committed")

            INSTALL.secure_candidate_root(root)
            self.assertEqual(INSTALL.enrollment_condition(root), "remediation")

    def test_candidate_document_is_the_frozen_fixture_except_runtime_fields(self):
        expected = json.loads(INSTALL.PROBE_FIXTURE.read_bytes())
        document = INSTALL.candidate_document("fresh")
        self.assertEqual(document["event"]["id"], "provision-fresh")
        self.assertEqual(document["rigsignal"]["diagnosis"], expected["rigsignal"]["diagnosis"])
        self.assertEqual(set(document), {"@timestamp", "event", "host", "rigsignal"})
        self.assertEqual(document["host"]["name"], document["host"]["name"].lower())

    def test_mapping_rejection_requires_the_exact_oracle(self):
        INSTALL.assert_mapping_rejection(400, {"error": {"type": "strict_dynamic_mapping_exception"}},
                                         "strict_dynamic_mapping_exception")
        with self.assertRaises(INSTALL.InputError):
            INSTALL.assert_mapping_rejection(403, {"error": {"type": "strict_dynamic_mapping_exception"}},
                                             "strict_dynamic_mapping_exception")
        with self.assertRaises(INSTALL.InputError):
            INSTALL.assert_mapping_rejection(400, {"error": {"type": "document_parsing_exception"}},
                                             "document_parsing_exception", "number_format_exception")

    def test_clean_root_refusals_leave_no_local_artifact(self):
        old_parse = INSTALL.argparse.ArgumentParser.parse_args
        old_bundle, old_role = INSTALL.load_bundle, INSTALL.role_body
        old_configure, old_auth, old_remote = (INSTALL.configure_https, INSTALL.admin_authorization,
                                               INSTALL.remote_stream_condition)
        try:
            INSTALL.load_bundle = lambda _path: INSTALL.Bundle("test", "test", [])
            INSTALL.role_body = lambda _bundle: {}
            INSTALL.configure_https = lambda _path: None
            INSTALL.admin_authorization = lambda _path: "admin"
            for adopt, remote, code in (
                (False, "compatible", "adoption_required"),
                (True, "absent", "adoption_flag_stream_absent"),
                (True, "incompatible", "migration_required"),
            ):
                with self.subTest(adopt=adopt, remote=remote), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw) / "enrollment"
                    args = SimpleNamespace(
                        bundle=Path("unused"), endpoint="https://es.invalid", ca_file=Path("unused"),
                        kibana_endpoint="https://kb.invalid", kibana_ca_file=Path("unused"),
                        admin_credentials_file=Path("unused"), agent_binary=Path("unused"), profile="user",
                        enrollment_root=root, dry_run=False, adopt_existing_w1_stream=adopt,
                    )
                    INSTALL.argparse.ArgumentParser.parse_args = lambda _parser: args
                    INSTALL.remote_stream_condition = lambda *_args: (remote, None)
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        self.assertEqual(INSTALL.main(), 1)
                    self.assertEqual(stderr.getvalue(), f"install refused: {code}\n")
                    self.assertFalse(root.exists())
        finally:
            (INSTALL.argparse.ArgumentParser.parse_args, INSTALL.load_bundle, INSTALL.role_body,
             INSTALL.configure_https, INSTALL.admin_authorization, INSTALL.remote_stream_condition) = (
                old_parse, old_bundle, old_role, old_configure, old_auth, old_remote)


if __name__ == "__main__":
    unittest.main()
