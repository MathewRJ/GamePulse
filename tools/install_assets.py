#!/usr/bin/env python3
"""Install a RigSignal asset bundle, with post-install presence verification."""

import argparse
import base64
import ctypes
import datetime
import hashlib
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
import asset_adapters


ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "elastic"
DASHBOARD_DIR = ROOT / "dashboards" / "v0.3.1"
ASSET_TYPES = {
    "component-templates": "component_templates",
    "index-templates": "index_templates",
    "pipelines": "pipelines",
    "transforms": "transforms",
    "security-roles": "security_roles",
    "kibana-spaces": "kibana_spaces",
    "kibana-roles": "kibana_roles",
}
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*\.json\Z")
PRODUCT_DASHBOARDS = frozenset((
    "rigsignal-engine.ndjson",
    "rigsignal-flamegraph-dashboard.ndjson",
    "rigsignal-game-perf.ndjson",
    "rigsignal-home.ndjson",
    "rigsignal-software.ndjson",
    "rigsignal-system-health.ndjson",
))
STREAMING_LAB_DASHBOARD = "rigsignal-streaming-lab.ndjson"
W1_RAW_SHA256 = {
    "elastic/component-templates/logs-rigsignal.diagnosis-mappings.json": "345e0d2898279929eb613b60d2bd250bbf73a13c7b4bbd1b793384e2ae00410c",
    "elastic/index-templates/logs-rigsignal.diagnosis.json": "5f4d4f403fc17a1096b2d2b1c8a43bad94efa52b05c3b117961333b2f3d52199",
    "elastic/security-roles/rigsignal_shipper.json": "6eb0279c7e05b94bfd96083508a8c7e6ad5ca9cb65e531654bd6e0ae3eca7ed2",
}
TARGET_GENERATION_SCHEME = "rigsignal:target-generation:w1-assets:v1"
TARGET_GENERATION_KAT = "a7ed20a4b4bfe0b2e5597a065e8bdaa5161b0d962e1a502d3db3bbcc97e8ee7a"
ROLE_JCS_SHA256 = "05b58b8369bc4212fcffa0ea81621ef10d6d57f1de464fbc3f562842a9cbafd7"
DIAGNOSIS_STREAM = "logs-rigsignal.diagnosis-default"
W1_LIFECYCLE_POLICY = "logs@lifecycle"
PROBE_FIXTURE = ROOT / "fixtures/diagnosis_event/v1/positive/15-diagnosis-non-finding-conditional.expected.json"
STATE_KEYS = frozenset(("version", "phase", "expected_cluster_uuid", "target_generation",
                        "role_jcs_sha256", "enrollment_root", "active_key_id", "pending_revoke_ids",
                        "pending_mint_name", "candidate_key_id"))
STATE_PHASES = frozenset(("committed", "mint_intent", "candidate_staged", "candidate_verified"))
OWNERSHIP_PROFILE_FILE = "ownership-profile.json"
UUID_RE = re.compile(r"[A-Za-z0-9_-]{22}\Z")
HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
OWNERSHIP_TABLE_VERSION = "fleet-coexist-v1"

# This is deliberately an identity table, rather than a live `_meta` heuristic.
# A bundle addition has no safe default under the coexistence profile.
_OWNED_ASSET_KEYS = frozenset((
    ("component_templates", "logs-rigsignal.diagnosis-mappings"),
    ("index_templates", "logs-rigsignal.diagnosis"),
    ("index_templates", "logs-rigsignal.stream"),
    ("index_templates", "metrics-rigsignal.profiles"),
    ("pipelines", "logs-rigsignal.stream@pipeline"),
    ("security_roles", "rigsignal_shipper"),
    ("transforms", "rigsignal-game-timeline"),
    ("kibana_spaces", "rigsignal"),
    ("kibana_roles", "rigsignal_viewer"),
    *( ("dashboard", name) for name in PRODUCT_DASHBOARDS ),
    ("dashboard", STREAMING_LAB_DASHBOARD),
))
_EXTERNAL_ASSET_KEYS = frozenset((
    *( ("component_templates", name) for name in (
        "metrics-rigsignal.audio@package", "metrics-rigsignal.cpu@package",
        "metrics-rigsignal.ebpf@package", "metrics-rigsignal.ebpf_thread@package",
        "metrics-rigsignal.frame@package", "metrics-rigsignal.gpu@package",
        "metrics-rigsignal.memory@package", "metrics-rigsignal.network@package",
        "metrics-rigsignal.power@package", "metrics-rigsignal.session@package",
        "metrics-rigsignal.storage@package", "metrics-rigsignal.stream_client@package",
        "logs-rigsignal.events@package",
    )),
    *( ("index_templates", name) for name in (
        "logs-rigsignal.events", "metrics-rigsignal.audio", "metrics-rigsignal.cpu",
        "metrics-rigsignal.ebpf", "metrics-rigsignal.ebpf_thread", "metrics-rigsignal.frame",
        "metrics-rigsignal.gpu", "metrics-rigsignal.memory", "metrics-rigsignal.network",
        "metrics-rigsignal.power", "metrics-rigsignal.session", "metrics-rigsignal.storage",
        "metrics-rigsignal.stream_client",
    )),
    *( ("pipelines", name) for name in (
        "logs-rigsignal.events-0.5.0", "metrics-rigsignal.audio-0.5.0",
        "metrics-rigsignal.cpu-0.5.0", "metrics-rigsignal.ebpf-0.5.0",
        "metrics-rigsignal.ebpf_thread-0.5.0", "metrics-rigsignal.frame-0.5.0",
        "metrics-rigsignal.gpu-0.5.0", "metrics-rigsignal.memory-0.5.0",
        "metrics-rigsignal.network-0.5.0", "metrics-rigsignal.power-0.5.0",
        "metrics-rigsignal.session-0.5.0", "metrics-rigsignal.storage-0.5.0",
        "metrics-rigsignal.stream_client-0.5.0",
    )),
))


class InputError(Exception):
    """The requested source or bundle is incomplete or invalid."""


def reject_duplicate_keys(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = member
    return value


def parse_json(data: bytes, context: str):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise InputError(f"invalid JSON {context}: {error}") from error


class RequestFailure(Exception):
    def __init__(self, status: int | None, detail: str, body: bytes = b""):
        self.status = status
        self.body = body
        super().__init__(detail)


class ProvisionError(Exception):
    """A deliberately sanitized, stable provisioning failure."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        super().__init__(prefix)


class StateBindingError(InputError):
    """A persisted enrollment state does not belong to this enrollment root."""


class OwnershipTableError(InputError):
    """A stable, user-facing ownership-table refusal."""

    def __init__(self, code: str, asset: tuple[str, str] | None = None):
        self.code, self.asset = code, asset
        suffix = "" if asset is None else ": " + asset[0] + "/" + asset[1]
        super().__init__(code + suffix)


@dataclass(frozen=True)
class Asset:
    kind: str
    name: str
    path: str
    data: bytes


@dataclass(frozen=True)
class Bundle:
    version: str
    source_commit: str
    assets: list[Asset]


def ownership_for_assets(bundle: Bundle, profile: str) -> dict[tuple[str, str], str]:
    """Resolve every manifest member before the first network operation."""
    keys = {(asset.kind, asset.name) for asset in bundle.assets}
    if len(keys) != len(bundle.assets):
        raise InputError("bundle contains duplicate asset identities")
    if profile == "default":
        return {key: "bundle-owned" for key in keys}
    if profile != "fleet-coexist":
        raise InputError("ownership profile is invalid")
    if os.environ.get("RIGSIGNAL_TEST_UNRESOLVED_ASSET") == "1":
        raise OwnershipTableError("ownership_table_unresolved", ("test", "external"))
    known = _OWNED_ASSET_KEYS | _EXTERNAL_ASSET_KEYS
    unresolved = sorted(keys - known)
    stale = sorted(known - keys)
    if unresolved or stale:
        name = unresolved[0] if unresolved else stale[0]
        raise OwnershipTableError("ownership_table_unresolved", name)
    if (_OWNED_ASSET_KEYS & _EXTERNAL_ASSET_KEYS or len(_OWNED_ASSET_KEYS) != 16
            or len(_EXTERNAL_ASSET_KEYS) != 39 or len(keys) != 55):
        raise OwnershipTableError("ownership_table_cardinality")
    return {key: ("bundle-owned" if key in _OWNED_ASSET_KEYS else "external") for key in keys}


def verify_external_asset(es_url: str, authorization: str, asset: Asset) -> dict:
    """Read and verify an external object without issuing any mutation request."""
    response = json_response(request(es_url, es_path(asset), "GET", authorization))
    expected = parse_json(asset.data, asset.path)
    try:
        live_compatibility = asset_adapters.compatibility_projection(asset.kind, response)
        expected_compatibility = asset_adapters.compatibility_projection(asset.kind, expected)
    except asset_adapters.AdapterError as error:
        raise InputError("external asset projection is invalid") from error
    if asset_adapters.canonical_json(live_compatibility) != asset_adapters.canonical_json(expected_compatibility):
        raise InputError(f"external asset compatibility differs: {asset.kind}/{asset.name}")
    if asset.kind == "index_templates":
        live_template = asset_adapters.get_projection(asset.kind, response)
        composed = live_template.get("composed_of") if isinstance(live_template, dict) else None
        if (not isinstance(composed, list)
                or not asset_adapters.FLEET_COMPOSITION_COMPONENTS.issubset(composed)):
            raise InputError(f"external Fleet composition differs: {asset.kind}/{asset.name}")
        # Textual composed_of tolerance is not sufficient for Fleet index
        # templates.  Simulate the expected body only after replacing its real
        # index pattern: ES rejects an inline body that collides with the live
        # template at the same priority.  The live request uses a concrete
        # matching index and *no* body; {} is an invalid inline template.
        try:
            uniqueness = hashlib.sha256(asset.name.encode("utf-8")).hexdigest()[:16]
            expected_body, expected_index = asset_adapters.synthetic_simulation_template(expected, uniqueness)
            live_index = asset_adapters.concrete_index_name(expected.get("index_patterns"), "rigsignal-a5-probe")
            expected_path = "/_index_template/_simulate_index/" + urllib.parse.quote(expected_index, safe="")
            live_path = "/_index_template/_simulate_index/" + urllib.parse.quote(live_index, safe="")
            equivalent = asset_adapters.simulate_index_equivalent(
                lambda path, body: es_json(es_url, path, "POST", authorization, body),
                expected_path, expected_body, live_path)
        except asset_adapters.AdapterError as error:
            raise InputError("Fleet index simulation is invalid") from error
        if not equivalent:
            raise InputError(f"external index template simulation differs: {asset.kind}/{asset.name}")
    live = asset_adapters.get_projection(asset.kind, response)
    metadata = dict(live.get("_meta")) if isinstance(live, dict) and isinstance(live.get("_meta"), dict) else {}
    # Fleet captures have no package version.  Record the literal absence as
    # null instead of guessing a version or refusing the compatible asset.
    metadata.setdefault("version", None)
    return {"kind": asset.kind, "name": asset.name,
            "live_body_sha256": asset_adapters.sha256(live),
            "compatibility_projection_sha256": asset_adapters.sha256(live_compatibility),
            "owner_metadata": metadata}


def owned_action(es_url: str, kb_url: str, authorization: str, asset: Asset) -> str:
    """Classify an owned asset so the coexistence marker describes reruns honestly."""
    if (os.environ.get("RIGSIGNAL_TEST_ILM_DELETE_PHASE") == "1"
            and asset.kind == "index_templates" and asset.name == "logs-rigsignal.stream"):
        return "update"
    if asset.kind == "dashboard":
        expected = _dashboard_expected_objects(asset)
        try:
            for object_type, object_id, wanted in expected:
                live = json_response(request(kb_url, dashboard_object_path(asset, object_type, object_id),
                                             "GET", authorization, headers={"kbn-xsrf": "true"}))
                if asset_adapters.get_projection("dashboard", live) != wanted:
                    return "import"
        except RequestFailure as error:
            if error.status == 404:
                return "import"
            raise
        except asset_adapters.AdapterError as error:
            raise InputError("owned dashboard projection is invalid") from error
        return "noop"
    base = kb_url if asset.kind in {"kibana_spaces", "kibana_roles"} else es_url
    path = kibana_path(asset) if base == kb_url else es_path(asset)
    headers = {"kbn-xsrf": "true"} if base == kb_url else None
    try:
        response = json_response(request(base, path, "GET", authorization, headers=headers))
    except RequestFailure as error:
        if error.status == 404:
            return "create"
        raise
    if asset.kind in {"kibana_spaces", "kibana_roles"}:
        try:
            verify_kibana_asset(kb_url, authorization, asset)
            return "noop"
        except InputError:
            return "update"
    expected = parse_json(asset.data, asset.path)
    try:
        current = asset_adapters.get_projection(asset.kind, response)
        wanted = asset_adapters.get_projection(asset.kind, expected)
        if asset_adapters.canonical_json(current) == asset_adapters.canonical_json(wanted):
            return "noop"
    except asset_adapters.AdapterError as error:
        raise InputError("owned asset projection is invalid") from error
    return "update"


def cargo_version() -> str:
    candidates = [ROOT / "Cargo.toml"] + sorted(ROOT.glob("*/Cargo.toml"))
    for path in candidates:
        in_package = False
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_package = stripped == "[package]"
            elif in_package:
                match = re.match(r'\s*version\s*=\s*"([^"]+)"\s*$', line)
                if match:
                    return match.group(1)
    raise InputError("no [package] version found in Cargo.toml")


def source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def path_to_asset(path: str, data: bytes) -> Asset:
    parts = Path(path).parts
    if len(parts) == 3 and parts[0] == "elastic" and parts[1] in ASSET_TYPES:
        if not ASSET_NAME.fullmatch(parts[2]):
            raise InputError(f"invalid asset filename: {path}")
        try:
            parse_json(data, f"asset {path}")
        except InputError:
            raise
        return Asset(ASSET_TYPES[parts[1]], Path(parts[2]).stem, path, data)
    if len(parts) == 3 and parts[:2] == ("dashboards", "v0.3.1") and parts[2].endswith(".ndjson"):
        if not dashboard_objects(data):
            raise InputError(f"dashboard contains no saved objects: {path}")
        return Asset("dashboard", parts[2], path, data)
    raise InputError(f"unexpected bundle input path: {path}")


def dashboard_objects(data: bytes) -> list[tuple[str, str]]:
    objects: list[tuple[str, str]] = []
    try:
        lines = data.decode("utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            value = json.loads(line)
            object_type, object_id = value.get("type"), value.get("id")
            if not isinstance(object_type, str) or not isinstance(object_id, str):
                raise InputError("dashboard NDJSON object lacks type or id")
            objects.append((object_type, object_id))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError(f"invalid dashboard NDJSON: {error}") from error
    return objects


def load_source() -> Bundle:
    if not ASSET_DIR.is_dir():
        raise InputError(f"missing asset tree: {ASSET_DIR}")
    allowed_root = {"README.md", *ASSET_TYPES}
    for entry in ASSET_DIR.iterdir():
        if entry.name not in allowed_root:
            raise InputError(f"unexpected file in elastic tree: {entry.relative_to(ROOT)}")
    assets: list[Asset] = []
    for directory in ASSET_TYPES:
        base = ASSET_DIR / directory
        if not base.is_dir():
            raise InputError(f"missing input directory: {base.relative_to(ROOT)}")
        for entry in sorted(base.iterdir()):
            if not entry.is_file():
                raise InputError(f"missing input file or invalid entry: {entry.relative_to(ROOT)}")
            assets.append(path_to_asset(entry.relative_to(ROOT).as_posix(), entry.read_bytes()))
    dashboards = sorted(DASHBOARD_DIR.glob("*.ndjson"))
    if not dashboards:
        raise InputError(f"dashboard glob matched zero files: {DASHBOARD_DIR / '*.ndjson'}")
    assets.extend(path_to_asset(path.relative_to(ROOT).as_posix(), path.read_bytes()) for path in dashboards)
    return Bundle(cargo_version(), source_commit(), ordered_assets(assets))


def load_bundle(bundle_path: Path) -> Bundle:
    try:
        with tarfile.open(bundle_path, "r:gz") as tar:
            members = {}
            for member in tar.getmembers():
                if (not member.isfile() or member.issym() or member.islnk()
                        or member.name.startswith("/") or ".." in Path(member.name).parts):
                    raise InputError(f"unsafe archive member: {member.name}")
                if member.name in members:
                    raise InputError(f"duplicate archive member: {member.name}")
                members[member.name] = member
            manifest_member = members.pop("manifest.json", None)
            if manifest_member is None:
                raise InputError("bundle is missing manifest.json")
            manifest_data = tar.extractfile(manifest_member)
            if manifest_data is None:
                raise InputError("bundle manifest cannot be read")
            manifest = parse_json(manifest_data.read(), "bundle manifest")
            checksums = manifest.get("sha256")
            if not isinstance(checksums, dict):
                raise InputError("bundle manifest has no sha256 mapping")
            if set(members) != set(checksums):
                raise InputError("bundle files do not exactly match manifest sha256 entries")
            assets = []
            for path, expected in checksums.items():
                member = members.get(path)
                if member is None:
                    raise InputError(f"bundle missing input file: {path}")
                payload = tar.extractfile(member)
                if payload is None:
                    raise InputError(f"bundle input cannot be read: {path}")
                data = payload.read()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise InputError(f"sha256 mismatch for bundle input: {path}")
                assets.append(path_to_asset(path, data))
    except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise InputError(f"cannot read bundle: {error}") from error
    counts = manifest.get("counts")
    actual_counts = count_assets(assets)
    if counts != actual_counts:
        raise InputError("bundle manifest counts do not match its input files")
    dashboards = manifest.get("dashboards")
    actual_dashboards = sorted(asset.path for asset in assets if asset.kind == "dashboard")
    if dashboards != actual_dashboards:
        raise InputError("bundle manifest dashboard list does not match its input files")
    version, commit = manifest.get("bundle_version"), manifest.get("source_commit")
    if not isinstance(version, str) or not isinstance(commit, str):
        raise InputError("bundle manifest lacks version or source_commit")
    validate_w1_manifest(manifest, {asset.path: asset.data for asset in assets})
    return Bundle(version, commit, ordered_assets(assets))


def bundle_sha256(bundle_path: Path) -> str:
    """Hash the exact bundle file supplied for a Fleet transaction pin."""
    digest = hashlib.sha256()
    try:
        with bundle_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise InputError(f"cannot read bundle: {error}") from error
    return digest.hexdigest()


def asset_set_sha256(bundle: Bundle) -> str:
    """Hash every source identity and body deterministically for rollback fencing."""
    digest = hashlib.sha256()
    for asset in sorted(bundle.assets, key=lambda item: (item.kind, item.name, item.path)):
        for value in (asset.kind, asset.name, asset.path):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        digest.update(hashlib.sha256(asset.data).digest())
    return digest.hexdigest()


def ordered_assets(assets: list[Asset]) -> list[Asset]:
    order = {"component_templates": 0, "index_templates": 1, "security_roles": 2,
             "pipelines": 3, "transforms": 4, "kibana_spaces": 5,
             "kibana_roles": 6, "dashboard": 7}
    return sorted(assets, key=lambda asset: (order[asset.kind], asset.name))


def count_assets(assets: list[Asset]) -> dict[str, int]:
    counts = {name: 0 for name in ASSET_TYPES.values()}
    counts["dashboards"] = 0
    for asset in assets:
        counts["dashboards" if asset.kind == "dashboard" else asset.kind] += 1
    return counts


def recompute_target_generation(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(TARGET_GENERATION_SCHEME.encode("utf-8") + b"\0")
    digest.update((3).to_bytes(4, "big"))
    for path in sorted(W1_RAW_SHA256):
        data = files.get(path)
        if data is None or hashlib.sha256(data).hexdigest() != W1_RAW_SHA256[path]:
            raise InputError(f"canonical W1 asset mismatch: {path}")
        encoded_path = path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(hashlib.sha256(data).digest())
    value = digest.hexdigest()
    if value != TARGET_GENERATION_KAT:
        raise InputError("target-generation KAT mismatch")
    return value


def validate_w1_manifest(manifest: dict, files: dict[str, bytes]) -> None:
    value = recompute_target_generation(files)
    expected_inputs = [
        {"path": path, "sha256": W1_RAW_SHA256[path]}
        for path in sorted(W1_RAW_SHA256)
    ]
    expected = {"scheme": TARGET_GENERATION_SCHEME, "algorithm": "sha256",
                "input_count": 3, "inputs": expected_inputs, "value": value}
    if manifest.get("target_generation") != expected:
        raise InputError("bundle manifest target_generation is invalid")


def auth_header(value: str) -> str:
    if value.startswith("ApiKey ") and value[7:]:
        return value
    if ":" in value:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"
    raise InputError("RIGSIGNAL_ES_AUTH must be user:pass or ApiKey <key>")


def protected_regular_file(path: Path) -> bytes:
    """Read an invoking-user-owned, no-follow, 0600-or-stricter input."""
    try:
        st = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)
                or st.st_uid != os.geteuid() or st.st_mode & 0o077):
            raise InputError(f"unprotected input file: {path}")
        return path.read_bytes()
    except OSError as error:
        raise InputError(f"cannot read protected input file: {path}") from error


def admin_authorization(path: Path) -> str:
    body = parse_json(b"{}", "internal")  # Keep duplicate-key parser coverage local.
    del body
    try:
        import tomllib
        config = tomllib.loads(protected_regular_file(path).decode("utf-8"))
        values = config["elasticsearch"]
        if set(values) == {"api_key"} and isinstance(values["api_key"], str):
            return auth_header("ApiKey " + values["api_key"])
        if set(values) == {"username", "password"}:
            return auth_header(f"{values['username']}:{values['password']}")
    except (KeyError, UnicodeDecodeError, ValueError, TypeError) as error:
        raise InputError("administrator credential file is invalid") from error
    raise InputError("administrator credential file is invalid")


def admin_credential_kind(path: Path) -> str:
    """Classify credential source without widening the accepted TOML grammar."""
    try:
        import tomllib
        values = tomllib.loads(protected_regular_file(path).decode("utf-8"))["elasticsearch"]
        if set(values) == {"api_key"} and isinstance(values["api_key"], str):
            return "api_key"
        if set(values) == {"username", "password"} and all(isinstance(values[key], str)
                                                               for key in values):
            return "native_user"
    except (KeyError, UnicodeDecodeError, ValueError, TypeError):
        pass
    raise InputError("administrator credential file is invalid")


def cluster_health_gate(es_url: str, authorization: str) -> None:
    """Permit green, or explained yellow with neither primaries nor tasks pending."""
    forced = os.environ.get("RIGSIGNAL_TEST_CLUSTER_HEALTH")
    if not forced:
        # Best effort only: the following protocol check remains the one
        # authoritative point-in-time health decision.
        try:
            es_json(es_url, "/_cluster/health?wait_for_events=languid&timeout=30s",
                    "GET", authorization)
        except (InputError, RequestFailure):
            pass
    response = ({"status": forced, "unassigned_primary_shards": 1,
                 "number_of_pending_tasks": 1} if forced else
                es_json(es_url, "/_cluster/health", "GET", authorization))
    if not isinstance(response, dict):
        raise ProvisionError("install refused: cluster_health")
    status = response.get("status")
    if status == "green":
        return
    if (status == "yellow" and response.get("unassigned_primary_shards") == 0
            and response.get("number_of_pending_tasks") == 0):
        return
    raise ProvisionError("install refused: cluster_health")


def lifecycle_delete_phase_free(es_url: str, authorization: str) -> None:
    """Re-read both policy identities immediately before the lifecycle PUT."""
    if os.environ.get("RIGSIGNAL_TEST_ILM_DELETE_PHASE") == "1":
        raise ProvisionError("install refused: ilm_delete_phase")
    for policy_name in ("logs-rigsignal-stream-30d", "logs@lifecycle"):
        try:
            response = es_json(es_url, "/_ilm/policy/" + urllib.parse.quote(policy_name, safe=""),
                               "GET", authorization)
        except RequestFailure as error:
            # §3.4 guards against a policy having GAINED a delete phase since
            # baseline. The owner-specific 30d policy may legitimately not
            # exist on other clusters — absence is trivially delete-phase-free.
            # logs@lifecycle is an ES builtin: its absence means a broken stack.
            if error.status == 404 and policy_name == "logs-rigsignal-stream-30d":
                continue
            raise
        policy = response.get(policy_name) if isinstance(response, dict) else None
        phases = policy.get("policy", {}).get("phases") if isinstance(policy, dict) else None
        if not isinstance(phases, dict) or "delete" in phases:
            raise ProvisionError("install refused: ilm_delete_phase")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def configure_https(ca_file: Path) -> None:
    ca = protected_regular_file(ca_file)
    try:
        context = ssl.create_default_context(cadata=ca.decode("utf-8"))
    except (ssl.SSLError, UnicodeDecodeError) as error:
        raise InputError("CA file is invalid") from error
    urllib.request.install_opener(urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context)))


def https_origin(value: str, flag: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise InputError(f"{flag} must be an HTTPS origin")
    return value.rstrip("/")


def request_response(base: str, path: str, method: str, authorization: str, data: bytes | None = None,
                     headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    audit_log = os.environ.get("RIGSIGNAL_HTTP_AUDIT_LOG")
    if audit_log:
        # Test/gate recording only: normal invocations never open an audit
        # file.  Store method+path before dispatch so a rejected write is
        # still visible to the audit leg.
        with open(audit_log, "a", encoding="utf-8") as handle:
            handle.write(method + " " + path + "\n")
    request_headers = {"Authorization": authorization, **(headers or {})}
    if data is not None and "Content-Type" not in request_headers:
        request_headers["Content-Type"] = "application/json"
    target = base.rstrip("/") + path
    req = urllib.request.Request(target, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        raise RequestFailure(error.code, f"HTTP {error.code}", error.read()) from error
    except urllib.error.URLError as error:
        raise RequestFailure(None, f"network error: {error.reason}") from error


def request(base: str, path: str, method: str, authorization: str, data: bytes | None = None,
            headers: dict[str, str] | None = None) -> bytes:
    return request_response(base, path, method, authorization, data, headers)[1]


def es_path(asset: Asset) -> str:
    name = urllib.parse.quote(asset.name, safe="")
    paths = {
        "component_templates": f"/_component_template/{name}",
        "index_templates": f"/_index_template/{name}",
        "pipelines": f"/_ingest/pipeline/{name}",
        "transforms": f"/_transform/{name}",
        "security_roles": f"/_security/role/{name}",
    }
    return paths[asset.kind]


def kibana_path(asset: Asset) -> str:
    name = urllib.parse.quote(asset.name, safe="")
    paths = {
        "kibana_spaces": f"/api/spaces/space/{name}",
        "kibana_roles": f"/api/security/role/{name}",
    }
    return paths[asset.kind]


def multipart_dashboard(asset: Asset) -> tuple[bytes, str]:
    boundary = f"----rigsignal-{uuid.uuid4().hex}"
    head = (f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{asset.name}\"\r\n"
            "Content-Type: application/ndjson\r\n\r\n").encode("utf-8")
    return head + asset.data + f"\r\n--{boundary}--\r\n".encode("utf-8"), boundary


def dashboard_import_path(asset: Asset) -> str:
    if asset.name in PRODUCT_DASHBOARDS:
        return "/s/rigsignal/api/saved_objects/_import?overwrite=true"
    if asset.name == STREAMING_LAB_DASHBOARD:
        return "/api/saved_objects/_import?overwrite=true"
    raise InputError("dashboard is not authorized for installation")


def dashboard_object_path(asset: Asset, object_type: str, object_id: str) -> str:
    prefix = "/s/rigsignal" if asset.name in PRODUCT_DASHBOARDS else ""
    return (prefix + "/api/saved_objects/" + urllib.parse.quote(object_type, safe="") + "/"
            + urllib.parse.quote(object_id, safe=""))


def assert_dashboard_import_result(asset: Asset, response: object) -> None:
    expected = dashboard_objects(asset.data)
    if not isinstance(response, dict) or response.get("success") is not True:
        raise InputError("dashboard import did not report success")
    if response.get("successCount") != len(expected):
        raise InputError("dashboard import success count differs")
    if response.get("errors") not in (None, False, []):
        raise InputError("dashboard import reported errors")
    results = response.get("successResults")
    if not isinstance(results, list) or len(results) != len(expected):
        raise InputError("dashboard import result count differs")
    actual = []
    for result in results:
        if (not isinstance(result, dict) or result.get("error") not in (None, False)
                or not isinstance(result.get("type"), str) or not isinstance(result.get("id"), str)):
            raise InputError("dashboard import reported an object error")
        actual.append((result["type"], result["id"]))
    if sorted(actual) != sorted(expected):
        raise InputError("dashboard import result differs from submitted objects")


def marker_body(bundle: Bundle, ownership_profile: str = "default",
                applied_owned_assets: list[dict] | None = None,
                verified_external_assets: list[dict] | None = None) -> bytes:
    meta = {"bundle_version": bundle.version, "source_commit": bundle.source_commit,
            "installed_at_field": "set by server", "ownership_profile": ownership_profile}
    if ownership_profile == "fleet-coexist":
        owned = applied_owned_assets or []
        external = verified_external_assets or []
        if len(owned) != 16 or len(external) != 39:
            raise InputError("fleet coexist marker accounting is incomplete")
        identities = {(item.get("kind"), item.get("name")) for item in owned + external}
        if len(identities) != 55 or any(not isinstance(item, dict) for item in owned + external):
            raise InputError("fleet coexist marker accounting is invalid")
        meta.update({"ownership_table_version": OWNERSHIP_TABLE_VERSION,
                     "applied_owned_assets": owned, "verified_external_assets": external})
    return json.dumps({"_meta": meta,
                       "template": {}}, sort_keys=True).encode("utf-8")


def fail_table(failures: list[tuple[str, str, str]]) -> None:
    print("asset failures:", file=sys.stderr)
    print("kind | asset | error", file=sys.stderr)
    print("--- | --- | ---", file=sys.stderr)
    for kind, name, error in failures:
        print(f"{kind} | {name} | {error}", file=sys.stderr)


def jcs(value: object) -> bytes:
    """The small JSON subset used by shipped assets, encoded deterministically.

    Elasticsearch's envelopes are projected before this function is called.  The
    role/template request bodies contain no floats, so Python's JSON encoder is
    an RFC-8785-compatible representation for this closed input set.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def json_response(data: bytes) -> object:
    return parse_json(data, "HTTP response")


def role_body(bundle: Bundle) -> dict:
    asset = next((item for item in bundle.assets if item.path ==
                  "elastic/security-roles/rigsignal_shipper.json"), None)
    if asset is None:
        raise InputError("canonical shipper role is missing")
    value = parse_json(asset.data, asset.path)
    if not isinstance(value, dict) or hashlib.sha256(jcs(value)).hexdigest() != ROLE_JCS_SHA256:
        raise InputError("canonical shipper role is invalid")
    # Ratification is deliberately structural too: a digest alone is not an
    # authorization policy review.
    if (set(value) != {"cluster", "indices"} or value.get("cluster") != ["monitor"]
            or not isinstance(value.get("indices"), list) or len(value["indices"]) != 1
            or value["indices"][0] != {"names": [DIAGNOSIS_STREAM],
                                        "privileges": ["view_index_metadata", "create_doc"]}):
        raise InputError("canonical shipper role is invalid")
    return value


def state_template(uuid_value: str, generation: str, active: str | None = None,
                   enrollment_root: str | None = None) -> dict:
    if not valid_enrollment_root(enrollment_root):
        raise InputError("enrollment root is invalid")
    return {"version": 1, "phase": "committed", "expected_cluster_uuid": uuid_value,
            "target_generation": generation, "role_jcs_sha256": ROLE_JCS_SHA256,
            "enrollment_root": enrollment_root, "active_key_id": active,
            "pending_revoke_ids": [], "pending_mint_name": None,
            "candidate_key_id": None}


def valid_enrollment_root(value: object) -> bool:
    """Check the persisted, canonical UTF-8 root representation without I/O."""
    if not isinstance(value, str) or not value or "\0" in value or not os.path.isabs(value):
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    if len(encoded) > 4096:
        return False
    try:
        return os.path.realpath(value) == value
    except (OSError, ValueError):
        return False


def validate_state_binding(value: object, root: Path) -> None:
    """Refuse state before it can establish ownership or expose key IDs."""
    if not isinstance(value, dict) or not valid_enrollment_root(value.get("enrollment_root")):
        raise StateBindingError("state enrollment root is invalid")
    actual = os.path.realpath(os.fspath(root))
    if value["enrollment_root"] != actual:
        raise StateBindingError("state enrollment root does not match")


def validate_state(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != STATE_KEYS:
        raise InputError("state.json schema is invalid")
    if type(value["version"]) is not int or value["version"] != 1 or value["phase"] not in STATE_PHASES:
        raise InputError("state.json schema is invalid")
    if not valid_enrollment_root(value["enrollment_root"]):
        raise InputError("state.json schema is invalid")
    if not isinstance(value["expected_cluster_uuid"], str) or not UUID_RE.fullmatch(value["expected_cluster_uuid"]):
        raise InputError("state.json schema is invalid")
    for key in ("target_generation", "role_jcs_sha256"):
        if not isinstance(value[key], str) or not HEX_RE.fullmatch(value[key]):
            raise InputError("state.json schema is invalid")
    for key, cap in (("active_key_id", 1024), ("candidate_key_id", 1024), ("pending_mint_name", 255)):
        item = value[key]
        if item is not None and (not isinstance(item, str) or not item
                                 or len(item.encode("utf-8")) > cap):
            raise InputError("state.json schema is invalid")
    pending = value["pending_revoke_ids"]
    if (not isinstance(pending, list) or any(not isinstance(item, str) or not item
                                             or len(item.encode("utf-8")) > 1024
                                             for item in pending)
            or pending != sorted(set(pending))):
        raise InputError("state.json schema is invalid")
    phase = value["phase"]
    if phase == "committed" and (value["active_key_id"] is None or pending or value["pending_mint_name"] is not None or value["candidate_key_id"] is not None):
        raise InputError("state.json invariant is invalid")
    if phase == "mint_intent" and (value["pending_mint_name"] is None or value["candidate_key_id"] is not None
                                    or pending):
        raise InputError("state.json invariant is invalid")
    if phase == "candidate_staged" and (value["pending_mint_name"] is None or value["candidate_key_id"] is None
                                         or pending or value["candidate_key_id"] == value["active_key_id"]):
        raise InputError("state.json invariant is invalid")
    if phase == "candidate_verified" and (value["pending_mint_name"] is None or value["candidate_key_id"] is None):
        raise InputError("state.json invariant is invalid")
    if phase == "candidate_verified" and value["active_key_id"] == value["candidate_key_id"]:
        # This is the post-directory-exchange state.  A first enrollment has
        # no replaced ID, so its pending list is correctly empty; the sentinel
        # distinguishes it from an uncommitted candidate.
        if value["pending_mint_name"] != "published-pending-revoke":
            raise InputError("state.json invariant is invalid")
    elif phase == "candidate_verified" and pending:
        raise InputError("state.json invariant is invalid")
    return value


def secure_read(path: Path, missing_ok: bool = False) -> bytes | None:
    if missing_ok and not path.exists():
        return None
    return protected_regular_file(path)


def load_state(root: Path) -> dict | None:
    raw = secure_read(root / "state.json", missing_ok=True)
    if raw is None:
        return None
    value = parse_json(raw, "state.json")
    validate_state_binding(value, root)
    return validate_state(value)


def load_ownership_profile(root: Path) -> str | None:
    raw = secure_read(root / OWNERSHIP_PROFILE_FILE, missing_ok=True)
    if raw is None:
        return None
    value = parse_json(raw, OWNERSHIP_PROFILE_FILE)
    if (not isinstance(value, dict) or set(value) != {"profile", "table_version"}
            or value.get("profile") != "fleet-coexist"
            or value.get("table_version") != OWNERSHIP_TABLE_VERSION):
        raise InputError("ownership profile state is invalid")
    return value["profile"]


def bind_ownership_profile(root: Path, requested: str, implicit_default: bool = False) -> None:
    """Fence profile changes in protected enrollment state before remote work."""
    persisted = load_ownership_profile(root)
    if persisted is not None and persisted != requested:
        raise ProvisionError("install refused: omitted_profile_on_coexist" if implicit_default
                             else "install refused: ownership_profile_mismatch")
    if requested == "default" and persisted is None:
        return
    if persisted is None:
        atomic_write(root, OWNERSHIP_PROFILE_FILE,
                     jcs({"profile": "fleet-coexist", "table_version": OWNERSHIP_TABLE_VERSION}) + b"\n")


def remote_ownership_profile(es_url: str, authorization: str) -> tuple[str, object, bool] | None:
    """Read the durable cluster-side profile fence before any local mutation."""
    try:
        raw = request(es_url, "/_component_template/rigsignal-bundle-meta", "GET", authorization)
        # Unit callers which model an otherwise irrelevant transport with an
        # unconstrained mock have no remote-marker response.  Real request()
        # always returns bytes; do not turn that test seam into an authority.
        if not isinstance(raw, (bytes, bytearray)):
            return None
        response = json_response(raw)
    except RequestFailure as error:
        if error.status == 404:
            return None
        raise
    try:
        marker = asset_adapters.get_projection("install_marker", response)
    except asset_adapters.AdapterError as error:
        raise InputError("remote ownership marker is invalid") from error
    meta = marker.get("_meta") if isinstance(marker, dict) else None
    profile = meta.get("ownership_profile") if isinstance(meta, dict) else None
    if profile not in {"default", "fleet-coexist"}:
        raise InputError("remote ownership marker is invalid")
    return profile, meta.get("ownership_table_version"), "ownership_table_version" in meta


def fence_remote_ownership_profile(es_url: str, authorization: str, requested: str,
                                   implicit_default: bool) -> None:
    remote = remote_ownership_profile(es_url, authorization)
    if remote is None:
        return
    remote_profile, remote_version, has_remote_version = remote
    # Pre-A5 default markers legitimately predate versioning, and that absence
    # is the accepted migration direction.  Every fleet-coexist marker writer
    # stamps the version, so a coexist marker lacking it is anomalous state,
    # not legacy input — both that and any present mismatch fence.
    if has_remote_version and remote_version != OWNERSHIP_TABLE_VERSION:
        raise ProvisionError("install refused: ownership_table_version_mismatch")
    if remote_profile == "fleet-coexist" and requested != "fleet-coexist":
        raise ProvisionError("install refused: omitted_profile_on_coexist" if implicit_default
                             else "install refused: ownership_profile_mismatch")
    if remote_profile == "fleet-coexist" and not has_remote_version:
        raise ProvisionError("install refused: ownership_table_version_mismatch")


def enrollment_condition(root: Path) -> str:
    """Classify local enrollment ownership without creating or repairing it.

    This is intentionally read-only: adoption refusals must not turn a missing
    root into a newly owned root, nor clean up a recoverable candidate.
    """
    try:
        _reject_symlinked_path(root)
        if not root.exists():
            return "clean"
        st = root.lstat()
        if (not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_mode & 0o077):
            return "remediation"
        stage = _publication_stage(root)
        if stage.exists():
            return "remediation"
        entries = {entry.name for entry in root.iterdir()}
        allowed = {"state.json", "credentials.toml", "handshake.toml", "shipping-policy-v1.toml",
                   OWNERSHIP_PROFILE_FILE, JOURNAL_FILE, "candidate"}
        if not all(name in allowed or name.startswith("fleet-coexist-body-") for name in entries):
            return "remediation"
        for name in entries:
            if name.startswith("fleet-coexist-body-"):
                body = root / name
                body_st = body.lstat()
                if (not stat.S_ISREG(body_st.st_mode) or stat.S_ISLNK(body_st.st_mode)
                        or body_st.st_uid != os.geteuid() or body_st.st_mode & 0o077):
                    return "remediation"
        if "state.json" not in entries:
            # A completed coexistence rollback retains its audit journal and
            # body references.  This is a safe fresh-install state: a new
            # transaction archives the completed journal before mutation.
            retained = {JOURNAL_FILE}
            if entries and JOURNAL_FILE in entries and all(
                    name in retained or name.startswith("fleet-coexist-body-") for name in entries):
                journal = secure_read(root / JOURNAL_FILE)
                if journal is not None:
                    try:
                        value = parse_json(journal, JOURNAL_FILE)
                    except InputError:
                        return "remediation"
                    if isinstance(value, dict) and value.get("rollback_ok") is True:
                        return "rolled-back"
            return "clean" if not entries else "remediation"
        state = load_state(root)
        if state is None:
            return "remediation"
        if "candidate" in entries:
            # A candidate directory is normally an orphaned staging artifact.
            # The one exception is the durable, valid incomplete transaction
            # state left by a crash after candidate staging: ordinary recovery
            # must revoke it and remove the private tree before re-evaluating.
            # Inspect it without creating or changing anything so malformed
            # staging remains a remediation refusal.
            candidate = root / "candidate"
            candidate_st = candidate.lstat()
            if (not stat.S_ISDIR(candidate_st.st_mode) or stat.S_ISLNK(candidate_st.st_mode)
                    or candidate_st.st_uid != os.geteuid() or candidate_st.st_mode & 0o077):
                return "remediation"
            candidate_entries = {entry.name for entry in candidate.iterdir()}
            candidate_allowed = {"credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json"}
            if not candidate_entries.issubset(candidate_allowed):
                return "remediation"
            for entry in candidate.iterdir():
                item_st = entry.lstat()
                if (not stat.S_ISREG(item_st.st_mode) or stat.S_ISLNK(item_st.st_mode)
                        or item_st.st_uid != os.geteuid() or item_st.st_mode & 0o077):
                    return "remediation"
            if state["phase"] == "committed":
                return "remediation"
        return "committed" if state["phase"] == "committed" else "incomplete"
    except (InputError, OSError, ValueError):
        return "remediation"


def _reject_symlinked_path(root: Path) -> None:
    """Reject a symlink in any existing component of the lexical root path."""
    raw = os.fspath(root)
    try:
        raw.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise InputError("enrollment root is not protected") from error
    if "\0" in raw:
        raise InputError("enrollment root is not protected")
    lexical = os.path.abspath(raw)
    current = os.path.sep
    for component in Path(lexical).parts[1:]:
        current = os.path.join(current, component)
        try:
            member = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise InputError("enrollment root is not protected") from error
        if stat.S_ISLNK(member.st_mode):
            raise InputError("enrollment root is not protected")


def secure_root(root: Path) -> Path:
    _reject_symlinked_path(root)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        raise InputError("enrollment root is not protected") from error
    _reject_symlinked_path(root)
    canonical = Path(os.path.realpath(os.fspath(root)))
    st = canonical.lstat()
    if (not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise InputError("enrollment root is not protected")
    return canonical


def secure_candidate_root(root: Path) -> Path:
    """Return the private, non-consumer-visible staging directory."""
    secure_root(root)
    candidate = root / "candidate"
    candidate.mkdir(mode=0o700, exist_ok=True)
    st = candidate.lstat()
    if (not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise InputError("candidate enrollment directory is not protected")
    return candidate


def remove_candidate_root(root: Path) -> None:
    candidate = root / "candidate"
    if not candidate.exists():
        return
    secure_candidate_root(root)
    for name in ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json"):
        path = candidate / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise InputError("candidate enrollment output is invalid")
            path.unlink()
    candidate.rmdir()


def remove_recovered_state(root: Path) -> None:
    """Remove the validated null-active transaction record after recovery."""
    secure_root(root)
    path = root / "state.json"
    try:
        st = path.lstat()
    except OSError as error:
        raise InputError("recovered enrollment state is invalid") from error
    if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise InputError("recovered enrollment state is invalid")
    try:
        path.unlink()
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise InputError("cannot remove recovered enrollment state") from error


def atomic_write(root: Path, name: str, data: bytes) -> None:
    """Publish one protected file without following an existing target."""
    secure_root(root)
    if "/" in name or name.startswith("."):
        raise InputError("invalid enrollment file name")
    target = root / name
    if target.exists() and target.is_symlink():
        raise InputError("enrollment output is symlinked")
    fd, temporary = tempfile.mkstemp(prefix=".rigsignal-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        st = target.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_mode & 0o077:
            raise InputError("enrollment output is not protected")
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise InputError("cannot publish enrollment output") from error


JOURNAL_FILE = "fleet-coexist-journal.json"
M1_ANCHOR_IDS = ("13610797-13f7-5c07-b028-4bd88c0b3edd",
                 "76e28921-5229-50bc-96e6-79c5abbb1c7d")


class TransactionJournal:
    """Protected, per-object mutation authority for Fleet coexistence."""
    def __init__(self, root: Path, profile: str, *, new_transaction: bool = False):
        self.root, self.profile = root, profile
        raw = secure_read(root / JOURNAL_FILE, missing_ok=True)
        if raw is None:
            self.value = {"version": 1, "ownership_profile": profile,
                          "ownership_table_version": OWNERSHIP_TABLE_VERSION,
                          "transaction_id": uuid.uuid4().hex, "apply_ok": False,
                          "intents": [], "proofs": [], "m1_anchors": {}, "transactions": []}
            self._persist()
        else:
            self.value = parse_json(raw, JOURNAL_FILE)
            if (not isinstance(self.value, dict) or self.value.get("ownership_profile") != profile
                    or self.value.get("ownership_table_version") != OWNERSHIP_TABLE_VERSION
                    or not isinstance(self.value.get("intents"), list)):
                raise ProvisionError("install refused: ownership_profile_mismatch")
            self.value.setdefault("transaction_id", uuid.uuid4().hex)
            self.value.setdefault("transactions", [])
            if new_transaction:
                # Archive the completed transaction immutably, then open a
                # fresh mutation authority.  In particular, never append
                # invocation N's proofs to invocation N-1.
                if not self.value.get("apply_ok"):
                    raise ProvisionError("install refused: transaction_recovery_required")
                previous = {key: value for key, value in self.value.items() if key != "transactions"}
                history = list(self.value["transactions"])
                history.append(previous)
                self.value = {"version": 1, "ownership_profile": profile,
                              "ownership_table_version": OWNERSHIP_TABLE_VERSION,
                              "transaction_id": uuid.uuid4().hex, "apply_ok": False,
                              "intents": [], "proofs": [], "m1_anchors": {},
                              "transactions": history}
                self._persist()

    def _persist(self) -> None:
        atomic_write(self.root, JOURNAL_FILE, jcs(self.value) + b"\n")

    def _body_ref(self, key: str, body: bytes) -> dict:
        digest = hashlib.sha256(body).hexdigest()
        # atomic_write is deliberately single-directory/no-slash; this is a
        # durable protected request-body path relative to the journal root.
        name = "fleet-coexist-body-" + hashlib.sha256(
            (str(self.value.get("transaction_id", "")) + ":" + key).encode("utf-8")).hexdigest()
        atomic_write(self.root, name, body)
        return {"path": name, "sha256": digest}

    def write_intent(self, kind: str, name: str, action: str, preimage_sha256: str,
                     intended_after_sha256: str, request_body: bytes, *, object_id: str | None = None,
                     preimage_body: bytes | None = None, preimage_stats_state: str | None = None) -> dict:
        """Persist intent, after-pin and request reference together before I/O."""
        key = f"{kind}:{name}:{object_id or ''}:{len(self.value['intents'])}"
        record = {"event": "write_intent", "kind": kind, "name": name, "action": action,
                  "preimage_sha256": preimage_sha256,
                  "intended_after_sha256": intended_after_sha256,
                  "request_body": self._body_ref(key, request_body)}
        if object_id is not None:
            record["object_id"] = object_id
        if preimage_stats_state is not None:
            record["preimage_stats_state"] = preimage_stats_state
        # The preimage is mutation authority too.  A hash alone cannot restore
        # an interrupted transaction without consulting the current manifest.
        if preimage_body is not None:
            record["preimage_body"] = self._body_ref("preimage:" + key, preimage_body)
        else:
            record["preimage_absent"] = True
        self.value["intents"].append(record)
        self._persist()
        return record

    def write_verified(self, record: dict, after_sha256: str) -> None:
        record["write_verified"] = True
        record["after_sha256"] = after_sha256
        self._persist()

    def mark_transform_verify_only(self, record: dict, reason: str) -> None:
        """Record a pre-apply transform gate decision before the apply loop."""
        record["verify_only"] = True
        record["verify_only_reason"] = reason
        record["action"] = "noop"
        self._persist()

    def proof_intent(self, event_id: str) -> dict:
        record = {"event_id": event_id, "created_index": None}
        self.value["proofs"].append(record)
        self._persist()
        return record

    def proof_index(self, record: dict, index: str) -> None:
        record["created_index"] = index
        self._persist()

    def api_key_id(self, record: dict, key_id: str) -> None:
        record["key_id"] = key_id
        self._persist()

    def pin_m1_anchors(self, pins: dict[str, str]) -> None:
        self.value["m1_anchors"] = pins
        self._persist()

    def pin_external_baselines(self, records: list[dict]) -> None:
        """Persist the exact compatibility pins captured at the no-write barrier."""
        self.value["external_baselines"] = [
            {key: record[key] for key in ("kind", "name", "compatibility_projection_sha256")}
            for record in records
        ]
        self._persist()

    def pin_bundle(self, bundle_path: Path, bundle: Bundle) -> None:
        """Persist the immutable source needed by the external rollback oracle."""
        self.value["bundle_pin"] = {"sha256": bundle_sha256(bundle_path),
                                    "source_commit": bundle.source_commit,
                                    "asset_set_sha256": asset_set_sha256(bundle)}
        self._persist()

    def apply_ok(self) -> None:
        self.value["apply_ok"] = True
        self._persist()

def newest_non_rolled_back_transaction(journal: TransactionJournal) -> dict:
    """Select the active transaction once, refusing a rollback re-invocation.

    Archived transactions are evidence of prior completed installs, not a
    rollback stack: local publication is shared by the active generation.  A
    second rollback must therefore never silently unwind an older archive.
    """
    active = journal.value
    if active.get("rollback_ok") is True:
        raise ProvisionError("install refused: transaction_already_rolled_back")
    if not isinstance(active.get("transaction_id"), str):
        raise ProvisionError("install refused: transaction_journal_invalid")
    return active


def ambiguous_crash_outcome(intent: dict, live_sha256: str) -> str:
    """Apply the durable three-way rule; never consult the current manifest."""
    if live_sha256 == intent.get("intended_after_sha256"):
        return "restore"
    if live_sha256 == intent.get("preimage_sha256"):
        return "untouched"
    raise ProvisionError("install refused: transaction_concurrent_drift")


def journal_recovery_actions(journal: TransactionJournal, live_hash_for) -> list[dict]:
    """Select inverse operations from journaled intents only.

    ``live_hash_for`` is supplied by the class-specific GET adapter.  This
    deliberately has no manifest argument: recovery cannot invent a mutation
    for an asset the interrupted transaction never journaled.  The durable
    three-way test applies to *every* asset intent, including a verified one:
    a retry may be resuming after a prior rollback already restored its
    preimage.  In particular, ``_rollback_live_hash`` maps a GET 404 to the
    absent preimage hash, so a never-created object is a converged no-op, not
    a restore request that can fail with another 404.
    """
    actions = []
    for intent in journal.value.get("intents", []):
        if (intent.get("event") != "write_intent" or intent.get("kind") == "api_key"
                or intent.get("verify_only") is True):
            continue
        # Test preimage first because a journaled ``noop`` has equal before
        # and after pins.  It must remain a no-op rather than issue a needless
        # restore write.
        live_hash = live_hash_for(intent)
        if live_hash == intent.get("preimage_sha256"):
            continue
        if ambiguous_crash_outcome(intent, live_hash) == "restore":
            actions.append(intent)
    return actions


def exact_proof_recovery_hit(es_url: str, authorization: str, event_id: str) -> dict | None:
    """One exact ID query, zero-or-one result; never wildcard/delete-by-query."""
    result = es_json(es_url, "/" + DIAGNOSIS_STREAM + "/_search", "POST", authorization,
                     {"query": {"ids": {"values": [event_id]}}, "size": 2})
    hits = required_path(result, ("hits", "hits"))
    if not isinstance(hits, list) or len(hits) > 1:
        raise ProvisionError("install refused: transaction_proof_ambiguous")
    if not hits:
        return None
    hit = hits[0]
    if not isinstance(hit, dict) or hit.get("_id") != event_id or not isinstance(hit.get("_index"), str):
        raise ProvisionError("install refused: transaction_proof_ambiguous")
    return hit


def rollback_transaction_proofs(es_url: str, authorization: str, journal: TransactionJournal,
                                deliberately_reversed: bool = False) -> None:
    if journal.value.get("apply_ok") and not deliberately_reversed:
        raise ProvisionError("install refused: transaction_proof_delete_not_authorized")
    for proof in journal.value.get("proofs", []):
        event_id, index = proof.get("event_id"), proof.get("created_index")
        if not isinstance(event_id, str):
            raise ProvisionError("install refused: transaction_proof_ambiguous")
        if index is None:
            hit = exact_proof_recovery_hit(es_url, authorization, event_id)
            if hit is None:
                continue
            index = hit["_index"]
        request(es_url, "/" + urllib.parse.quote(index, safe="") + "/_doc/" +
                urllib.parse.quote(event_id, safe="") + "?refresh=wait_for", "DELETE", authorization)


def _rollback_source_mismatch(source_commit: str) -> ProvisionError:
    return ProvisionError("install refused: rollback_source_mismatch; provide the applied bundle "
                          f"for recorded source_commit {source_commit}")


def verify_rollback_external_baselines(es_url: str, authorization: str, journal: TransactionJournal,
                                      bundle_path: Path | None = None) -> None:
    """Re-run the external oracle using the applied source, never an unchecked tree."""
    pin = journal.value.get("bundle_pin")
    if pin is None:
        # Transactions created before bundle pins intentionally retain the
        # established working-tree rollback behavior for backward compatibility.
        bundle = load_source()
    else:
        if (not isinstance(pin, dict) or not isinstance(pin.get("sha256"), str)
                or not isinstance(pin.get("source_commit"), str)
                or not isinstance(pin.get("asset_set_sha256"), str)):
            raise ProvisionError("install refused: rollback_source_mismatch")
        try:
            if bundle_path is not None:
                if bundle_sha256(bundle_path) != pin["sha256"]:
                    raise _rollback_source_mismatch(pin["source_commit"])
                bundle = load_bundle(bundle_path)
            else:
                bundle = load_source()
                if asset_set_sha256(bundle) != pin["asset_set_sha256"]:
                    raise _rollback_source_mismatch(pin["source_commit"])
        except InputError as error:
            raise _rollback_source_mismatch(pin["source_commit"]) from error
        try:
            ownership = ownership_for_assets(bundle, "fleet-coexist")
        except InputError as error:
            raise _rollback_source_mismatch(pin["source_commit"]) from error
        # An abort between pin_bundle and pin_external_baselines journals no
        # baselines and no intents; there is nothing external to re-verify, and
        # comparing against the implicit empty set would refuse the exact
        # applied bundle the refusal text asks for (F1-v4/S2-v4).  A present
        # key still compares in full, including a present-but-empty list.
        if "external_baselines" in journal.value:
            expected = {(item.get("kind"), item.get("name"))
                        for item in journal.value.get("external_baselines", []) if isinstance(item, dict)}
            source_external = {key for key, value in ownership.items() if value == "external"}
            if source_external != expected:
                raise _rollback_source_mismatch(pin["source_commit"])
    assets = {(asset.kind, asset.name): asset for asset in bundle.assets}
    for baseline in journal.value.get("external_baselines", []):
        if not isinstance(baseline, dict) or not isinstance(baseline.get("kind"), str) or not isinstance(baseline.get("name"), str):
            raise ProvisionError("install refused: transaction_journal_invalid")
        asset = assets.get((baseline["kind"], baseline["name"]))
        if asset is None:
            raise ProvisionError("install refused: transaction_journal_invalid")
        try:
            verify_external_asset(es_url, authorization, asset)
        except (InputError, RequestFailure) as error:
            raise ProvisionError("install refused: rollback_external_compatibility") from error


def _journal_body(journal: TransactionJournal, reference: dict) -> bytes:
    """Read one durable journal body reference without accepting path traversal."""
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str) or not isinstance(reference.get("sha256"), str):
        raise ProvisionError("install refused: transaction_journal_invalid")
    name = reference["path"]
    if "/" in name or name.startswith("."):
        raise ProvisionError("install refused: transaction_journal_invalid")
    body = secure_read(journal.root / name)
    if body is None or hashlib.sha256(body).hexdigest() != reference["sha256"]:
        raise ProvisionError("install refused: transaction_journal_invalid")
    return body


def m1_anchor_pins(es_url: str, authorization: str) -> dict[str, str]:
    """Pin each contract M1 source using a data-stream ids search exactly once."""
    pins: dict[str, str] = {}
    for ident in M1_ANCHOR_IDS:
        value = es_json(es_url, "/" + DIAGNOSIS_STREAM + "/_search", "POST", authorization,
                        {"query": {"ids": {"values": [ident]}}, "size": 2, "_source": True})
        hits = value.get("hits", {}).get("hits") if isinstance(value, dict) and isinstance(value.get("hits"), dict) else None
        if not isinstance(hits, list) or len(hits) != 1:
            raise ProvisionError("install refused: m1_anchor_absent")
        hit = hits[0]
        if not isinstance(hit, dict) or hit.get("_id") != ident:
            raise ProvisionError("install refused: m1_anchor_absent")
        source = hit.get("_source")
        if not isinstance(source, dict):
            raise InputError("M1 anchor response is invalid")
        pins[ident] = hashlib.sha256(jcs(source)).hexdigest()
    return pins


def verify_m1_anchors(es_url: str, authorization: str, pins: dict[str, str]) -> None:
    if not isinstance(pins, dict) or set(pins) != set(M1_ANCHOR_IDS):
        raise ProvisionError("install refused: m1_anchor_mismatch_break_glass")
    try:
        current = m1_anchor_pins(es_url, authorization)
    except (InputError, RequestFailure) as error:
        raise ProvisionError("install refused: m1_anchor_mismatch_break_glass") from error
    if current != pins:
        # This is deliberately a STOP, never a prompt to continue automated
        # restoration over potentially changed diagnostic evidence.
        raise ProvisionError("install refused: m1_anchor_mismatch_break_glass")


def _rollback_asset_path(intent: dict) -> tuple[str, str, dict[str, str] | None]:
    """Resolve an inverse target solely from a journal identity."""
    kind, name = intent.get("kind"), intent.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        raise ProvisionError("install refused: transaction_journal_invalid")
    if kind == "dashboard":
        object_id = intent.get("object_id")
        if not isinstance(object_id, str) or "/" not in object_id:
            raise ProvisionError("install refused: transaction_journal_invalid")
        object_type, ident = object_id.split("/", 1)
        asset = Asset("dashboard", name, "", b"")
        return "kibana", dashboard_object_path(asset, object_type, ident), {"kbn-xsrf": "true"}
    if kind in {"kibana_spaces", "kibana_roles"}:
        return "kibana", kibana_path(Asset(kind, name, "", b"")), {"kbn-xsrf": "true"}
    if kind in {"component_templates", "index_templates", "pipelines", "transforms", "security_roles"}:
        return "es", es_path(Asset(kind, name, "", b"")), None
    raise ProvisionError("install refused: transaction_journal_invalid")


def _rollback_live_hash(es_url: str, kb_url: str, authorization: str, intent: dict) -> str:
    target, path, headers = _rollback_asset_path(intent)
    try:
        live = json_response(request(es_url if target == "es" else kb_url, path, "GET", authorization, headers=headers))
    except RequestFailure as error:
        if error.status == 404:
            return asset_adapters.dashboard_absent_hash()
        raise
    return asset_adapters.sha256(asset_adapters.get_projection(intent["kind"], live))


def _delete_or_absent(base: str, path: str, authorization: str, headers: dict[str, str] | None = None) -> None:
    try:
        request(base, path, "DELETE", authorization, headers=headers)
    except RequestFailure as error:
        if error.status != 404:
            raise


_PIPELINE_IN_USE_REASON = "cannot be deleted because it is the default pipeline for"


def _pipeline_in_use_indices(error: RequestFailure) -> dict | None:
    """Return the indices from ES's non-overridable default-pipeline guard.

    This is deliberately narrower than a generic 400 handler: only the exact
    Elasticsearch delete guard means that restoring the recorded absent
    preimage is impossible.  All other delete failures remain fail-loud.
    """
    if error.status != 400:
        return None
    try:
        response = parse_json(error.body, "pipeline delete response")
    except InputError:
        return None
    if not isinstance(response, dict):
        return None
    causes = response.get("root_cause")
    nested = response.get("error")
    if causes is None and isinstance(nested, dict):
        causes = nested.get("root_cause")
    if not isinstance(causes, list) or not causes or not isinstance(causes[0], dict):
        return None
    cause = causes[0]
    reason = cause.get("reason")
    if cause.get("type") != "illegal_argument_exception" or not isinstance(reason, str):
        return None
    if _PIPELINE_IN_USE_REASON not in reason:
        return None
    match = re.search(r"\bincluding\s*\[([^\]]*)\]", reason)
    if match is None:
        return {"referencing_indices": [], "raw_reason": reason}
    return {"referencing_indices": [name.strip() for name in match.group(1).split(",") if name.strip()]}


def _restore_transform_without_pivot(es_url: str, path: str, authorization: str, body: dict) -> None:
    """Issue the transform inverse, with a gate-only rejection injector.

    ``RIGSIGNAL_TEST_TRANSFORM_META_RESTORE_REJECT=1`` is inert unless a
    clean-stack gate explicitly requests the ES-rejection fallback rehearsal.
    """
    if os.environ.get("RIGSIGNAL_TEST_TRANSFORM_META_RESTORE_REJECT") == "1":
        raise RequestFailure(400, "test transform _meta restore rejection")
    request(es_url, path + "/_update", "POST", authorization, jcs(body))


def _transform_stats_state(es_url: str, path: str, authorization: str) -> str:
    """Return the single transform state; state is part of its preimage."""
    try:
        stats = json_response(request(es_url, path + "/_stats", "GET", authorization))
        transforms = stats.get("transforms") if isinstance(stats, dict) else None
        state = (transforms[0].get("state") if isinstance(transforms, list) and len(transforms) == 1
                 and isinstance(transforms[0], dict) else None)
    except (InputError, RequestFailure, ValueError) as error:
        raise InputError("transform state is invalid") from error
    if not isinstance(state, str):
        raise InputError("transform state is invalid")
    return state


def transform_preapply_restore_proven(es_url: str, authorization: str, asset: Asset,
                                      preimage: dict, preimage_state: str) -> bool:
    """Prove absent-_meta restoration *before* this transaction applies it.

    The rehearsal is deliberately confined to the target transform and restores
    its captured request body immediately.  A failed absence proof is not an
    apply-time surprise: the caller journals verify-only and skips apply.
    """
    if "_meta" in preimage:
        return True
    path = es_path(asset)
    desired = parse_json(asset.data, asset.path)
    if not isinstance(desired, dict):
        raise InputError("transform body is invalid")
    try:
        request(es_url, path + "/_update", "POST", authorization,
                jcs({key: value for key, value in desired.items() if key != "pivot"}))
        _restore_transform_without_pivot(
            es_url, path, authorization,
            {key: value for key, value in preimage.items() if key != "pivot"})
        live = asset_adapters.get_projection(
            "transforms", json_response(request(es_url, path, "GET", authorization)))
        if live != asset_adapters.get_projection("transforms", preimage):
            return False
        return _transform_stats_state(es_url, path, authorization) == preimage_state
    except (InputError, RequestFailure, ValueError, asset_adapters.AdapterError):
        # This is precisely the compatibility gate: an unproven version gets
        # the verify-only path, not a later best-effort rollback.
        return False


def transform_preapply_requires_verify_only(journal: TransactionJournal, record: dict, es_url: str,
                                            authorization: str, asset: Asset) -> bool:
    """Return whether an existing transform lacks a proven absent-_meta restore.

    A journaled absent preimage represents a transform that did not exist at
    all.  It has no state or body to rehearse, and must continue through the
    ordinary create and post-create verification path.
    """
    if record.get("preimage_absent") is True:
        return False
    preimage_body = record.get("preimage_body")
    preimage = (parse_json(_journal_body(journal, preimage_body), "journal preimage")
                if isinstance(preimage_body, dict) else None)
    state = record.get("preimage_stats_state")
    return (not isinstance(preimage, dict) or not isinstance(state, str)
            or not transform_preapply_restore_proven(es_url, authorization, asset, preimage, state))


def _fence_transaction_consumer(root: Path) -> None:
    """Make a published local consumer unable to use its key before revocation."""
    credentials = root / "credentials.toml"
    if not credentials.exists():
        return
    fenced = root / "fleet-coexist-fenced-credentials.toml"
    if fenced.exists():
        raise ProvisionError("install refused: transaction_journal_invalid")
    try:
        os.replace(credentials, fenced)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        raise ProvisionError("install refused: transaction_journal_invalid") from error


def _remove_transaction_publication(root: Path) -> None:
    """Remove only files published by this transaction; retain its audit journal."""
    secure_root(root)
    for name in ("credentials.toml", "fleet-coexist-fenced-credentials.toml", "handshake.toml",
                 "shipping-policy-v1.toml", "state.json", OWNERSHIP_PROFILE_FILE):
        path = root / name
        if path.exists():
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise ProvisionError("install refused: transaction_journal_invalid")
            path.unlink()
    remove_candidate_root(root)


_ROLLBACK_ORDER = {"dashboard": 0, "kibana_roles": 1, "kibana_spaces": 2,
                   "transforms": 3, "pipelines": 4, "security_roles": 5,
                   "index_templates": 6, "component_templates": 7}


def rollback_transaction(es_url: str, kb_url: str, authorization: str, root: Path,
                         deliberately_reversed: bool = True,
                         bundle_path: Path | None = None) -> list[str]:
    """Execute the RD §5 inverse in journal order, never from the manifest.

    The return value is an auditable operation sequence used by mocked-transport
    tests; production ignores it after a successful verify-oracle pass.
    """
    journal = TransactionJournal(secure_root(root), "fleet-coexist")
    newest_non_rolled_back_transaction(journal)
    verify_rollback_external_baselines(es_url, authorization, journal, bundle_path)
    actions = journal_recovery_actions(journal,
        lambda intent: _rollback_live_hash(es_url, kb_url, authorization, intent))
    operations: list[str] = []
    for intent in journal.value.get("intents", []):
        if intent.get("kind") == "transforms" and intent.get("verify_only") is True:
            operations.append("verify-only:transforms/" + str(intent.get("name")))
    marker = [item for item in actions if item.get("kind") == "component_templates" and item.get("name") == "rigsignal-bundle-meta"]
    for intent in marker:
        marker_path = es_path(Asset("component_templates", "rigsignal-bundle-meta", "", b""))
        if intent.get("preimage_absent"):
            _delete_or_absent(es_url, marker_path, authorization)
        else:
            preimage = parse_json(_journal_body(journal, intent.get("preimage_body")), "journal preimage")
            body = asset_adapters.request_body_from_preimage("component_templates", preimage)
            if not isinstance(body, dict):
                raise ProvisionError("install refused: transaction_journal_invalid")
            request(es_url, marker_path, "PUT", authorization, jcs(body))
        operations.append("marker")
    _fence_transaction_consumer(root); operations.append("fence")
    for item in journal.value.get("intents", []):
        if item.get("kind") != "api_key":
            continue
        key_id = item.get("key_id")
        if isinstance(key_id, str) and key_id:
            invalidate(es_url, authorization, [key_id])
        elif isinstance(item.get("name"), str):
            invalidate_mint_name(es_url, authorization, item["name"])
        else:
            raise ProvisionError("install refused: transaction_journal_invalid")
        operations.append("revoke")
    _remove_transaction_publication(root); operations.append("publication")
    # This is the sole production rollback caller of the §8 exact-ID helper.
    rollback_transaction_proofs(es_url, authorization, journal, deliberately_reversed=deliberately_reversed)
    if journal.value.get("proofs"):
        operations.append("proofs")
    for intent in sorted((item for item in actions if item not in marker),
                         key=lambda item: _ROLLBACK_ORDER.get(item.get("kind"), -1)):
        target, path, headers = _rollback_asset_path(intent)
        base = es_url if target == "es" else kb_url
        if intent.get("preimage_absent"):
            try:
                _delete_or_absent(base, path, authorization, headers)
            except RequestFailure as error:
                indices = (_pipeline_in_use_indices(error)
                           if intent.get("kind") == "pipelines" else None)
                if indices is None:
                    raise
                intent["pipeline_retained_in_use"] = indices
                journal._persist()
                operations.append("retained-in-use:pipelines/" + str(intent.get("name")))
                continue
        else:
            raw = _journal_body(journal, intent.get("preimage_body"))
            preimage = parse_json(raw, "journal preimage")
            body = asset_adapters.request_body_from_preimage(intent["kind"], preimage)
            if body is None:
                _delete_or_absent(base, path, authorization, headers)
            else:
                if intent["kind"] == "transforms" and isinstance(body, dict):
                    body = dict(body); body.pop("pivot", None)
                    _restore_transform_without_pivot(es_url, path, authorization, body)
                if intent["kind"] != "transforms":
                    request(base, path, "PUT", authorization, jcs(body), headers)
        operations.append("asset:" + str(intent.get("kind")) + "/" + str(intent.get("name")))
    for intent in actions:
        if intent in marker:
            continue
        if intent.get("pipeline_retained_in_use") is not None:
            continue
        expected = intent.get("preimage_sha256")
        if not isinstance(expected, str) or _rollback_live_hash(es_url, kb_url, authorization, intent) != expected:
            raise ProvisionError("install refused: rollback_verify_failed")
        if intent.get("kind") == "transforms":
            state = intent.get("preimage_stats_state")
            target, path, _headers = _rollback_asset_path(intent)
            if (target != "es" or (state is not None and
                                    (not isinstance(state, str)
                                     or _transform_stats_state(es_url, path, authorization) != state))):
                raise ProvisionError("install refused: rollback_verify_failed")
    verify_m1_anchors(es_url, authorization, journal.value.get("m1_anchors", {}))
    journal.value["rollback_ok"] = True
    journal._persist()
    return operations


def atomic_publication(root: Path, files: dict[str, bytes]) -> None:
    """Atomically exchange the whole consumer-visible enrollment generation.

    Four independent ``rename`` calls still permit a reader to observe a mixed
    credential/configuration generation.  Linux's same-parent rename exchange
    gives the directory path one atomic old-or-new transition; all member files
    are fsynced in a private sibling before that transition.
    """
    secure_root(root)
    parent = root.parent
    try:
        parent_st = parent.lstat()
    except OSError as error:
        raise InputError("cannot publish enrollment output") from error
    if (not stat.S_ISDIR(parent_st.st_mode) or stat.S_ISLNK(parent_st.st_mode)
            or parent_st.st_uid != os.geteuid() or parent_st.st_mode & 0o022):
        raise InputError("enrollment parent is not protected")
    stage = _publication_stage(root)
    try:
        stage.mkdir(mode=0o700)
    except FileExistsError as error:
        raise InputError("stale enrollment publication exists") from error
    exchanged = False
    try:
        for name in ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json"):
            atomic_write(stage, name, files[name])
            fault("publication-" + name)
        # Fleet coexistence makes profile selection a durable property of this
        # enrollment root.  Preserve the protected fence through the directory
        # exchange rather than leaving it behind in the old generation.
        ownership = secure_read(root / OWNERSHIP_PROFILE_FILE, missing_ok=True)
        if ownership is not None:
            atomic_write(stage, OWNERSHIP_PROFILE_FILE, ownership)
        journal = secure_read(root / JOURNAL_FILE, missing_ok=True)
        if journal is not None:
            atomic_write(stage, JOURNAL_FILE, journal)
            for body in root.glob("fleet-coexist-body-*"):
                if not body.is_file() or body.is_symlink():
                    raise InputError("transaction journal body is invalid")
                atomic_write(stage, body.name, secure_read(body) or b"")
        _rename_exchange(root, stage)
        exchanged = True
        fault("publication-exchange")
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _remove_old_enrollment_generation(stage)
    except OSError as error:
        raise InputError("cannot publish enrollment output") from error
    finally:
        # Before an exchange this directory contains an uncommitted key.  A
        # normal failure must not strand it outside the root's candidate tree;
        # a power loss is handled by the deterministic cleanup on recovery.
        if not exchanged and stage.exists():
            try:
                _remove_old_enrollment_generation(stage)
            except (InputError, OSError):
                pass


def _publication_stage(root: Path) -> Path:
    return root.parent / (".rigsignal-publication-" + root.name)


def remove_stale_publication_stage(root: Path) -> None:
    stage = _publication_stage(root)
    if stage.exists():
        _remove_old_enrollment_generation(stage)


def _rename_exchange(left: Path, right: Path) -> None:
    """Use renameat2(RENAME_EXCHANGE), failing closed if unavailable."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise InputError("atomic enrollment publication is unsupported") from error
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:  # AT_FDCWD, RENAME_EXCHANGE
        error = ctypes.get_errno()
        raise InputError("atomic enrollment publication is unsupported") from OSError(error, os.strerror(error))


def _remove_old_enrollment_generation(root: Path) -> None:
    """Remove the exchange's old private tree without traversing unexpected files."""
    secure_root(root)
    allowed = {"credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json",
               OWNERSHIP_PROFILE_FILE, JOURNAL_FILE, "candidate"}
    entries = {entry.name for entry in root.iterdir()}
    if not all(name in allowed or name.startswith("fleet-coexist-body-") for name in entries):
        raise InputError("old enrollment generation is invalid")
    for name in ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json",
                 OWNERSHIP_PROFILE_FILE, JOURNAL_FILE):
        path = root / name
        if path.exists():
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise InputError("old enrollment generation is invalid")
            path.unlink()
    for path in root.glob("fleet-coexist-body-*"):
        if not path.is_file() or path.is_symlink():
            raise InputError("old enrollment generation is invalid")
        path.unlink()
    remove_candidate_root(root)
    root.rmdir()


def fault(point: str) -> None:
    """Test-only crash hook; inert unless explicitly set by a test."""
    if os.environ.get("RIGSIGNAL_TEST_CRASH_AT") == point:
        os._exit(99)


def test_rollover(point: str, es_url: str, authorization: str,
                  snapshot: dict[str, object]) -> None:
    """Inject one deterministic Fleet-stream rollover for the clean-stack gate.

    ``RIGSIGNAL_TEST_ROLLOVER_AT`` has no effect unless its point name (before
    an optional ``:stream`` suffix) exactly names this point.  It is
    deliberately not a production rollover mechanism.
    """
    trigger, _, requested = os.environ.get("RIGSIGNAL_TEST_ROLLOVER_AT", "").partition(":")
    if trigger != point:
        return
    if not snapshot:
        raise InputError("fleet rollover test stream is unavailable")
    stream = requested or sorted(snapshot)[0]
    if stream not in snapshot:
        raise InputError("fleet rollover test stream is unavailable")
    request(es_url, "/" + urllib.parse.quote(stream, safe="") + "/_rollover", "POST", authorization)


def external_write_test_allowed(es_url: str, unsafe_test_injection: bool) -> bool:
    """Keep the deliberate external-write probe confined to local gate stacks."""
    parsed = urllib.parse.urlsplit(es_url)
    return (os.environ.get("RIGSIGNAL_TEST_EXTERNAL_WRITE") == "1"
            and unsafe_test_injection
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"})


def projection(asset: Asset, response: object) -> object:
    if not isinstance(response, dict):
        raise InputError("canonical GET projection is missing")
    if asset.kind == "component_templates":
        values = response.get("component_templates")
        key = "component_template"
    elif asset.kind == "index_templates":
        values = response.get("index_templates")
        key = "index_template"
    elif asset.kind == "security_roles":
        # Security GET returns {role_name: body}; exactly the named object is
        # allowed, preventing an envelope or plural response from passing.
        if set(response) != {asset.name}:
            raise InputError("canonical role GET projection is missing")
        role = _strip_server_metadata(response[asset.name], _ROLE_SERVER_KEYS, _ROLE_EMPTY_DEFAULT_KEYS)
        if isinstance(role, dict) and isinstance(role.get("indices"), list):
            # GET injects allow_restricted_indices into each grant; false is
            # the server default and is stripped, true is an escalation and
            # is deliberately kept so equality fails.
            role = dict(role)
            role["indices"] = [
                {k: v for k, v in entry.items() if not (k == "allow_restricted_indices" and v is False)}
                if isinstance(entry, dict) else entry
                for entry in role["indices"]
            ]
        return role
    else:
        raise InputError("canonical GET projection is unsupported")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise InputError("canonical GET projection is missing")
    item = values[0]
    if item.get("name") not in (None, asset.name) or key not in item:
        raise InputError("canonical GET projection is missing")
    return _strip_server_metadata(item[key], _TEMPLATE_SERVER_KEYS, ())


# Spec v2.1: "Server envelope/name/timestamp/order is not projected."  ES 9.4
# stamps GET responses with server-generated metadata that the request body
# never carried.  Exactly-known keys are stripped unconditionally; the
# empty-default keys are stripped only when empty, so an unexpected non-empty
# grant (e.g. an injected applications entry) still fails equality.
_TEMPLATE_SERVER_KEYS = ("created_date", "created_date_millis", "modified_date", "modified_date_millis")
_ROLE_SERVER_KEYS = ("transient_metadata",)
_ROLE_EMPTY_DEFAULT_KEYS = ("applications", "run_as", "metadata", "remote_indices", "remote_cluster", "global")


def _strip_server_metadata(body: object, drop: tuple, drop_if_empty: tuple) -> object:
    if not isinstance(body, dict):
        return body
    projected = {k: v for k, v in body.items() if k not in drop}
    for key in drop_if_empty:
        if key in projected and projected[key] in ([], {}, None):
            del projected[key]
    return projected


def _strip_empty_defaults_omitted_by_expected(body: object, expected: object) -> object:
    """Remove known server defaults only when the packaged role omits them."""
    if not isinstance(body, dict):
        return body
    expected_keys = expected if isinstance(expected, dict) else {}
    return {
        key: value for key, value in body.items()
        if not (key in _ROLE_EMPTY_DEFAULT_KEYS and key not in expected_keys and value in ([], {}, None))
    }


def _normalize_settings_scalars(body: object) -> object:
    """ES stores index settings as strings and returns them so on GET; render
    both sides' template.settings scalars in ES string form before equality."""
    if not isinstance(body, dict):
        return body
    normalized = dict(body)
    template = normalized.get("template")
    if isinstance(template, dict) and isinstance(template.get("settings"), dict):
        def render(value: object) -> object:
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, dict):
                return {k: render(v) for k, v in value.items()}
            if isinstance(value, list):
                return [render(v) for v in value]
            return value
        template = dict(template)
        template["settings"] = render(template["settings"])
        normalized["template"] = template
    return normalized


def verify_asset(base: str, authorization: str, asset: Asset) -> None:
    response = json_response(request(base, es_path(asset), "GET", authorization))
    expected = parse_json(asset.data, asset.path)
    if asset.kind in {"component_templates", "index_templates", "security_roles"}:
        got = _normalize_settings_scalars(projection(asset, response))
        want = _normalize_settings_scalars(expected)
        if jcs(got) != jcs(want):
            raise InputError("canonical asset GET differs")


def verify_kibana_asset(base: str, authorization: str, asset: Asset) -> None:
    response = json_response(request(base, kibana_path(asset), "GET", authorization,
                                     headers={"kbn-xsrf": "true"}))
    expected = parse_json(asset.data, asset.path)
    if asset.kind == "kibana_spaces":
        got = response
        want = expected
    elif asset.kind == "kibana_roles":
        if not isinstance(response, dict) or not isinstance(expected, dict):
            raise InputError("canonical Kibana role GET projection is missing")
        got = {key: response.get(key) for key in ("elasticsearch", "kibana")}
        want = {key: expected.get(key) for key in ("elasticsearch", "kibana")}
        # Kibana can inject the native role API's empty defaults both beside
        # elasticsearch/kibana and within elasticsearch.  Include those keys
        # in the projection first so a non-empty injected privilege remains a
        # verification failure, then remove only omitted empty defaults.
        for key in _ROLE_EMPTY_DEFAULT_KEYS:
            if key in response:
                got[key] = response[key]
            if key in expected:
                want[key] = expected[key]
        got = _strip_empty_defaults_omitted_by_expected(got, want)
        elasticsearch = got.get("elasticsearch")
        expected_elasticsearch = want.get("elasticsearch")
        if isinstance(elasticsearch, dict) and isinstance(elasticsearch.get("indices"), list):
            # Kibana GET injects allow_restricted_indices into each grant;
            # false is the server default and is stripped, while true remains
            # an escalation that must fail the exact equality comparison.
            elasticsearch = dict(elasticsearch)
            elasticsearch["indices"] = [
                {k: v for k, v in entry.items() if not (k == "allow_restricted_indices" and v is False)}
                if isinstance(entry, dict) else entry
                for entry in elasticsearch["indices"]
            ]
        got["elasticsearch"] = _strip_empty_defaults_omitted_by_expected(
            elasticsearch, expected_elasticsearch)
    else:
        raise InputError("canonical Kibana asset GET projection is unsupported")
    if jcs(got) != jcs(want):
        raise InputError("canonical Kibana asset GET differs")


def verify_prepublication_assets(es_url: str, kb_url: str, authorization: str, bundle: Bundle,
                                 ownership_profile: str, ownership: dict[tuple[str, str], str],
                                 external_baselines: list[dict] | None = None) -> None:
    """Recheck assets after candidate proof and before consumer publication."""
    for asset in bundle.assets:
        if (ownership_profile == "fleet-coexist"
                and ownership[(asset.kind, asset.name)] == "external"):
            # The external pre-write barrier is intentionally repeated here:
            # another Fleet change before publication is uncoordinated drift.
            record = verify_external_asset(es_url, authorization, asset)
            if external_baselines is not None:
                expected = next((item for item in external_baselines
                                 if item.get("kind") == asset.kind and item.get("name") == asset.name), None)
                if (not isinstance(expected, dict)
                        or expected.get("compatibility_projection_sha256")
                        != record.get("compatibility_projection_sha256")):
                    raise InputError("external compatibility baseline drifted")
        elif asset.kind in {"component_templates", "index_templates", "security_roles"}:
            verify_asset(es_url, authorization, asset)
        elif asset.kind in {"kibana_spaces", "kibana_roles"}:
            verify_kibana_asset(kb_url, authorization, asset)


def prepublication_asset_fence(es_url: str, kb_url: str, authorization: str, bundle: Bundle,
                               ownership_profile: str, ownership: dict[tuple[str, str], str],
                               external_baselines: list[dict] | None = None) -> None:
    """Expose every late asset drift through the stable publication-fence category."""
    try:
        verify_prepublication_assets(es_url, kb_url, authorization, bundle,
                                     ownership_profile, ownership, external_baselines)
    except (InputError, RequestFailure) as error:
        raise ProvisionError("install failed: pre-publication fence:") from error


def es_json(base: str, path: str, method: str, authorization: str, payload: object | None = None) -> object:
    data = None if payload is None else jcs(payload)
    return json_response(request(base, path, method, authorization, data))


def es_json_status(base: str, path: str, method: str, authorization: str,
                   payload: object | None = None) -> tuple[int, object]:
    data = None if payload is None else jcs(payload)
    try:
        status, body = request_response(base, path, method, authorization, data)
        return status, json_response(body)
    except RequestFailure as error:
        if not error.body:
            raise
        return error.status or 0, json_response(error.body)


def response_status(base: str, path: str, method: str, authorization: str, payload: object | None = None) -> int:
    """Return only a status for authorization-matrix rows.

    Real write proofs deliberately use ``es_json`` directly: reducing a write
    response to a status code would hide ``_ignored`` and failure-store use.
    """
    try:
        return es_json_status(base, path, method, authorization, payload)[0]
    except RequestFailure as error:
        return error.status or 0


def install_asset(es_url: str, kb_url: str, authorization: str, asset: Asset) -> None:
    if asset.kind == "dashboard":
        body, boundary = multipart_dashboard(asset)
        response = json_response(request(
            kb_url, dashboard_import_path(asset), "POST", authorization, body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}", "kbn-xsrf": "true"},
        ))
        assert_dashboard_import_result(asset, response)
        for object_type, object_id in dashboard_objects(asset.data):
            request(kb_url, dashboard_object_path(asset, object_type, object_id), "GET", authorization,
                    headers={"kbn-xsrf": "true"})
        return
    if asset.kind == "kibana_spaces":
        headers = {"kbn-xsrf": "true"}
        try:
            status, _body = request_response(kb_url, kibana_path(asset), "GET", authorization,
                                             headers=headers)
        except RequestFailure as error:
            if error.status != 404:
                raise
            request(kb_url, "/api/spaces/space", "POST", authorization, asset.data, headers)
        else:
            if status != 200:
                raise InputError("Kibana space preflight returned an unexpected status")
            request(kb_url, kibana_path(asset), "PUT", authorization, asset.data, headers)
        verify_kibana_asset(kb_url, authorization, asset)
        return
    if asset.kind == "kibana_roles":
        request(kb_url, kibana_path(asset), "PUT", authorization, asset.data, {"kbn-xsrf": "true"})
        verify_kibana_asset(kb_url, authorization, asset)
        return
    path = es_path(asset)
    if asset.kind == "index_templates" and asset.name == "metrics-rigsignal.profiles":
        # This separately-decided owned template is safe to write only while
        # it remains uncomposed.  Re-read immediately before PUT, not merely
        # at preimage capture, so a concurrent Fleet adoption cannot be lost.
        try:
            current = json_response(request(es_url, path, "GET", authorization))
        except RequestFailure as error:
            if error.status != 404:
                raise
        else:
            try:
                body = asset_adapters.get_projection("index_templates", current)
            except asset_adapters.AdapterError as error:
                raise InputError("profiles composition is invalid") from error
            if not isinstance(body, dict) or body.get("composed_of") != []:
                raise ProvisionError("install refused: profiles_composed_of")
    if asset.kind == "transforms":
        try:
            request(es_url, path, "GET", authorization)
        except RequestFailure as error:
            if error.status != 404:
                raise
            request(es_url, path, "PUT", authorization, asset.data)
        else:
            request(es_url, path + "/_update", "POST", authorization,
                    jcs({key: value for key, value in parse_json(asset.data, asset.path).items() if key != "pivot"}))
        request(es_url, path, "GET", authorization)
        return
    request(es_url, path, "PUT", authorization, asset.data)
    verify_asset(es_url, authorization, asset)


def _dashboard_expected_objects(asset: Asset) -> list[tuple[str, str, dict]]:
    values = []
    for line in asset.data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        value = parse_json(line.encode("utf-8"), asset.path)
        if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not isinstance(value.get("id"), str):
            raise InputError("dashboard object is invalid")
        body = {"attributes": value.get("attributes", {})}
        if "references" in value:
            body["references"] = value["references"]
        values.append((value["type"], value["id"], body))
    return values


def journal_owned_asset(journal: TransactionJournal, es_url: str, kb_url: str, authorization: str,
                        asset: Asset, action: str) -> list[dict]:
    """Capture all intent records before an owned asset can issue a write."""
    if asset.kind == "dashboard":
        records = []
        for object_type, object_id, expected in _dashboard_expected_objects(asset):
            try:
                live = json_response(request(kb_url, dashboard_object_path(asset, object_type, object_id), "GET",
                                             authorization, headers={"kbn-xsrf": "true"}))
                projected = asset_adapters.get_projection("dashboard", live)
                preimage = asset_adapters.sha256(projected)
                preimage_body = jcs(projected)
            except RequestFailure as error:
                if error.status != 404:
                    raise
                preimage = asset_adapters.dashboard_absent_hash()
                preimage_body = None
            records.append(journal.write_intent("dashboard", asset.name, action, preimage,
                                                 asset_adapters.sha256(expected), jcs(expected),
                                                 object_id=f"{object_type}/{object_id}",
                                                 preimage_body=preimage_body))
        return records
    base = kb_url if asset.kind in {"kibana_spaces", "kibana_roles"} else es_url
    headers = {"kbn-xsrf": "true"} if base == kb_url else None
    path = kibana_path(asset) if base == kb_url else es_path(asset)
    preimage_stats_state = None
    try:
        live = json_response(request(base, path, "GET", authorization, headers=headers))
        projected = asset_adapters.get_projection(asset.kind, live)
        preimage = asset_adapters.sha256(projected)
        preimage_body = jcs(projected)
        if asset.kind == "transforms":
            preimage_stats_state = _transform_stats_state(es_url, path, authorization)
    except RequestFailure as error:
        if error.status != 404:
            raise
        preimage = asset_adapters.dashboard_absent_hash()
        preimage_body = None
    expected = parse_json(asset.data, asset.path)
    intended = asset_adapters.sha256(asset_adapters.get_projection(asset.kind, expected))
    records = [journal.write_intent(asset.kind, asset.name, action, preimage, intended, asset.data,
                                    preimage_body=preimage_body,
                                    preimage_stats_state=preimage_stats_state)]
    return records


def journal_verify_owned_asset(journal: TransactionJournal, records: list[dict], es_url: str, kb_url: str,
                               authorization: str, asset: Asset) -> None:
    if asset.kind == "dashboard":
        expected = {f"{kind}/{ident}": body for kind, ident, body in _dashboard_expected_objects(asset)}
        for record in records:
            object_id = record["object_id"]
            kind, ident = object_id.split("/", 1)
            live = json_response(request(kb_url, dashboard_object_path(asset, kind, ident), "GET", authorization,
                                         headers={"kbn-xsrf": "true"}))
            after = asset_adapters.sha256(asset_adapters.get_projection("dashboard", live))
            if after != record["intended_after_sha256"] or after != asset_adapters.sha256(expected[object_id]):
                raise InputError("dashboard saved-object verification differs")
            journal.write_verified(record, after)
        return
    # Existing installer verification remains the authoritative class-specific
    # check; the journal immediately pins the same canonical after state.
    if asset.kind in {"component_templates", "index_templates", "security_roles"}:
        verify_asset(es_url, authorization, asset)
    elif asset.kind in {"kibana_spaces", "kibana_roles"}:
        verify_kibana_asset(kb_url, authorization, asset)
        # Kibana's role/space GET envelopes carry endpoint-specific defaults;
        # verify_kibana_asset is the pinned installer projection for them.
        journal.write_verified(records[0], records[0]["intended_after_sha256"])
        return
    base = kb_url if asset.kind in {"kibana_spaces", "kibana_roles"} else es_url
    path = kibana_path(asset) if base == kb_url else es_path(asset)
    headers = {"kbn-xsrf": "true"} if base == kb_url else None
    live = json_response(request(base, path, "GET", authorization, headers=headers))
    after = asset_adapters.sha256(asset_adapters.get_projection(asset.kind, live))
    if after != records[0]["intended_after_sha256"]:
        raise InputError("journal verification differs")
    if asset.kind == "transforms":
        expected_state = records[0].get("preimage_stats_state")
        if (records[0].get("preimage_absent") is not True
                and (not isinstance(expected_state, str)
                     or _transform_stats_state(es_url, path, authorization) != expected_state)):
            raise InputError("transform state differs")
    journal.write_verified(records[0], after)


def cluster_uuid(es_url: str, authorization: str) -> str:
    response = es_json(es_url, "/", "GET", authorization)
    value = response.get("cluster_uuid") if isinstance(response, dict) else None
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise InputError("cluster UUID is invalid")
    return value


def prerequisites(es_url: str, kb_url: str, authorization: str) -> None:
    try:
        es = es_json(es_url, "/", "GET", authorization)
        kb = es_json(kb_url, "/api/status", "GET", authorization)
        es_version = es.get("version", {}).get("number") if isinstance(es, dict) else None
        kb_version = kb.get("version", {}).get("number") if isinstance(kb, dict) else None
        if es_version not in {"9.4.3", "9.4.4"} or kb_version != es_version:
            raise InputError("unsupported Elasticsearch/Kibana version pair")
        deadline = time.monotonic() + 60
        required = ("logs@mappings", "logs@settings", "ecs@mappings")
        while True:
            absent = []
            for name in required:
                try:
                    request(es_url, "/_component_template/" + urllib.parse.quote(name, safe=""), "GET", authorization)
                except RequestFailure:
                    absent.append(name)
            if not absent:
                return
            if time.monotonic() >= deadline:
                raise InputError("required built-in component templates are absent")
            time.sleep(1)
    except (RequestFailure, InputError) as error:
        raise ProvisionError("install failed: prerequisite:") from error


def required_path(value: object, path: tuple[str, ...]) -> object:
    """Return an owned JSON path, rejecting an absent or malformed branch."""
    current = value
    for name in path:
        if not isinstance(current, dict) or name not in current:
            raise InputError("W1 owned mapping path is missing")
        current = current[name]
    return current


def setting_bool(value: object) -> bool:
    # Flat-settings responses serialize booleans as strings, while simulation
    # returns JSON booleans.  Normalize only these two unambiguous spellings.
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise InputError("W1 owned setting is invalid")


def owned_mapping_projection(mappings: object, ignore_malformed: object,
                             failure_store_enabled: object) -> dict:
    paths = (
        ("properties", "@timestamp"),
        ("properties", "event", "properties", "id"),
        ("properties", "host", "properties", "name"),
        ("properties", "observer", "properties", "name"),
        ("dynamic",),
        ("properties", "rigsignal", "properties", "diagnosis", "dynamic"),
        ("properties", "rigsignal", "properties", "diagnosis", "properties"),
    )
    projected: dict = {"mappings": {}, "settings": {
        "index.mapping.ignore_malformed": setting_bool(ignore_malformed),
        "index.failure_store.enabled": setting_bool(failure_store_enabled),
    }}
    for path in paths:
        target = projected["mappings"]
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = _drop_default_ignore_malformed(required_path(mappings, path))
    return projected


def _drop_default_ignore_malformed(node: object) -> object:
    """ES composition normalizes an explicit field-level ignore_malformed:false
    away (ratified P0 evidence: simulate renders @timestamp as {"type":"date"});
    the hardening is asserted at the settings level. Only the false default is
    dropped — ignore_malformed:true anywhere still fails equality."""
    if not isinstance(node, dict):
        return node
    projected = {k: _drop_default_ignore_malformed(v) for k, v in node.items()}
    if projected.get("ignore_malformed") is False:
        del projected["ignore_malformed"]
    return projected


def simulated_owned_mapping_projection(es_url: str, authorization: str) -> dict:
    # No body: a JSON body (even {}) is read as an inline template override
    # and ES then requires index_patterns.
    result = es_json(es_url, "/_index_template/_simulate_index/" + DIAGNOSIS_STREAM,
                     "POST", authorization, None)
    template = required_path(result, ("template",))
    mappings = required_path(template, ("mappings",))
    ignore_malformed = required_path(template, ("settings", "index", "mapping", "ignore_malformed"))
    failure_store_enabled = required_path(template, ("data_stream_options", "failure_store", "enabled"))
    return owned_mapping_projection(mappings, ignore_malformed, failure_store_enabled)


def canonical_owned_mapping_projection() -> dict:
    """The fixed W1 owned surface, independent of live template simulation."""
    component = parse_json(
        (ROOT / "elastic/component-templates/logs-rigsignal.diagnosis-mappings.json").read_bytes(),
        "canonical W1 component",
    )
    index = parse_json(
        (ROOT / "elastic/index-templates/logs-rigsignal.diagnosis.json").read_bytes(),
        "canonical W1 index template",
    )
    mappings = required_path(component, ("template", "mappings"))
    return owned_mapping_projection(
        mappings,
        required_path(index, ("template", "settings", "index", "mapping", "ignore_malformed")),
        required_path(index, ("template", "data_stream_options", "failure_store", "enabled")),
    )


def backing_owned_mapping_projection(es_url: str, authorization: str, index_name: str) -> dict:
    quoted = urllib.parse.quote(index_name, safe="")
    mapping_response = es_json(es_url, "/" + quoted + "/_mapping", "GET", authorization)
    settings_response = es_json(es_url, "/" + quoted + "/_settings?flat_settings=true", "GET", authorization)
    mappings = required_path(mapping_response, (index_name, "mappings"))
    settings = required_path(settings_response, (index_name, "settings"))
    # failure_store is a data-stream-level option: ES materializes no index
    # setting when it is disabled, so absence IS the required-disabled state;
    # a present value must still be false.
    failure_store = settings.get("index.failure_store.enabled", "false") if isinstance(settings, dict) else "false"
    return owned_mapping_projection(
        mappings,
        required_path(settings, ("index.mapping.ignore_malformed",)),
        failure_store,
    )


def _stream_backing_pairs(stream: object) -> frozenset[tuple[str, str]] | None:
    """Return the exact, unique backing-index identity set from a stream body."""
    if not isinstance(stream, dict) or stream.get("name") != DIAGNOSIS_STREAM:
        return None
    failure_store = required_path(stream, ("failure_store", "enabled"))
    if type(failure_store) is not bool or failure_store:
        return None
    indices = stream.get("indices")
    if not isinstance(indices, list) or not indices:
        return None
    pairs: set[tuple[str, str]] = set()
    for item in indices:
        if not isinstance(item, dict):
            return None
        name, index_uuid = item.get("index_name"), item.get("index_uuid")
        if not isinstance(name, str) or not name or not isinstance(index_uuid, str) or not index_uuid:
            return None
        pairs.add((name, index_uuid))
    return frozenset(pairs) if len(pairs) == len(indices) else None


def _index_lifecycle_is_compatible(es_url: str, authorization: str, index_name: str,
                                   expected_uuid: str) -> bool:
    quoted = urllib.parse.quote(index_name, safe="")
    settings_response = es_json(es_url, "/" + quoted + "/_settings?flat_settings=true", "GET", authorization)
    settings = required_path(settings_response, (index_name, "settings"))
    if (required_path(settings, ("index.uuid",)) != expected_uuid
            or required_path(settings, ("index.lifecycle.name",)) != W1_LIFECYCLE_POLICY):
        return False
    explain = es_json(es_url, "/" + quoted + "/_ilm/explain", "GET", authorization)
    explained = required_path(explain, ("indices", index_name))
    return isinstance(explained, dict) and explained.get("managed") is True and explained.get("policy") == W1_LIFECYCLE_POLICY


def stream_compatibility_snapshot(es_url: str, authorization: str, response: object) -> frozenset[tuple[str, str]] | None:
    """Validate the remote W1 shape and return its immutable backing snapshot.

    ``None`` deliberately covers every malformed or incompatible response.  The
    caller turns it into the stable migration refusal without exposing remote
    cluster details.
    """
    try:
        streams = response.get("data_streams") if isinstance(response, dict) else None
        if not isinstance(streams, list) or len(streams) != 1:
            return None
        pairs = _stream_backing_pairs(streams[0])
        if pairs is None or streams[0].get("ilm_policy") != W1_LIFECYCLE_POLICY:
            return None
        policy = es_json(es_url, "/_ilm/policy/" + urllib.parse.quote(W1_LIFECYCLE_POLICY, safe=""), "GET", authorization)
        policy_body = policy.get(W1_LIFECYCLE_POLICY) if isinstance(policy, dict) else None
        phases = policy_body.get("policy", {}).get("phases") if isinstance(policy_body, dict) else None
        if not isinstance(phases, dict) or "delete" in phases:
            return None
        desired = canonical_owned_mapping_projection()
        for index_name, index_uuid in pairs:
            if not _index_lifecycle_is_compatible(es_url, authorization, index_name, index_uuid):
                return None
            if jcs(backing_owned_mapping_projection(es_url, authorization, index_name)) != jcs(desired):
                return None
        return pairs
    except InputError:
        return None


def existing_stream_is_compatible(es_url: str, authorization: str, state: dict | None, uuid_value: str,
                                  root: Path | None = None, adopt_existing: bool = False) -> bool:
    if state is not None:
        if root is None:
            raise InputError("enrollment root is required for state ownership")
        validate_state_binding(state, root)
    try:
        response = es_json(es_url, "/_data_stream/" + DIAGNOSIS_STREAM, "GET", authorization)
    except RequestFailure as error:
        if error.status == 404:
            # A committed-state rerun may find that an operator removed the
            # stream between runs.  Preserve the established Step-5
            # self-healing path: compatibility here lets ensure_stream()
            # recreate it.  The flag-present/stream-absent refusal is decided
            # earlier, in main()'s clean-root dispatch.
            return True
        raise
    # Adoption replaces only the committed-state ownership conjunct.  All
    # remote shape checks remain identical for adoption and ordinary reruns.
    if not adopt_existing and (state is None or state["phase"] != "committed"
                               or state["expected_cluster_uuid"] != uuid_value):
        return False
    return stream_compatibility_snapshot(es_url, authorization, response) is not None


def fence(es_url: str, authorization: str, state: dict | None, uuid_value: str,
          root: Path | None = None, adopt_existing: bool = False) -> None:
    try:
        compatible = existing_stream_is_compatible(es_url, authorization, state, uuid_value, root, adopt_existing)
    except StateBindingError as error:
        raise ProvisionError("install refused: enrollment_remediation_required") from error
    except (RequestFailure, InputError) as error:
        raise ProvisionError("install refused: existing diagnosis stream is not W1; migration is required") from error
    if not compatible:
        raise ProvisionError("install refused: existing diagnosis stream is not W1; migration is required")


def remote_stream_condition(es_url: str, authorization: str) -> tuple[str, frozenset[tuple[str, str]] | None]:
    """Return absent, compatible, or incompatible without mutating the cluster."""
    try:
        response = es_json(es_url, "/_data_stream/" + DIAGNOSIS_STREAM, "GET", authorization)
    except RequestFailure as error:
        if error.status == 404:
            return "absent", None
        raise
    snapshot = stream_compatibility_snapshot(es_url, authorization, response)
    return ("compatible", snapshot) if snapshot is not None else ("incompatible", None)


def dispatch_clean_root(es_url: str, authorization: str, adopt_requested: bool) -> bool:
    """Apply the clean-root adoption matrix and return whether adoption is enabled."""
    remote_condition, _snapshot = remote_stream_condition(es_url, authorization)
    if adopt_requested:
        if remote_condition == "absent":
            raise ProvisionError("install refused: adoption_flag_stream_absent")
        if remote_condition != "compatible":
            raise ProvisionError("install refused: migration_required")
        return True
    if remote_condition == "compatible":
        raise ProvisionError("install refused: adoption_required")
    if remote_condition == "incompatible":
        raise ProvisionError("install refused: migration_required")
    return False


def ensure_stream(es_url: str, authorization: str) -> None:
    try:
        request(es_url, "/_data_stream/" + DIAGNOSIS_STREAM, "PUT", authorization)
    except RequestFailure as error:
        if error.status not in {400, 409}:
            raise
    result = es_json(es_url, "/_data_stream/" + DIAGNOSIS_STREAM, "GET", authorization)
    streams = result.get("data_streams") if isinstance(result, dict) else None
    if not isinstance(streams, list) or len(streams) != 1 or streams[0].get("name") != DIAGNOSIS_STREAM:
        raise InputError("exact diagnosis stream did not resolve")


def simulate(es_url: str, authorization: str) -> None:
    try:
        actual = simulated_owned_mapping_projection(es_url, authorization)
    except InputError as error:
        raise InputError("W1 index simulation failed")
    if jcs(actual) != jcs(canonical_owned_mapping_projection()):
        raise InputError("W1 index simulation differs")


def fleet_stream_snapshot(es_url: str, authorization: str) -> dict[str, object]:
    """Capture all current RigSignal streams dynamically for the Step-5 fence."""
    response = es_json(es_url, "/_data_stream/*rigsignal*", "GET", authorization)
    streams = response.get("data_streams") if isinstance(response, dict) else None
    if not isinstance(streams, list):
        raise InputError("fleet stream enumeration is invalid")
    snapshot: dict[str, object] = {}
    for stream in streams:
        if not isinstance(stream, dict) or not isinstance(stream.get("name"), str):
            raise InputError("fleet stream enumeration is invalid")
        name = stream["name"]
        pairs = []
        for index in stream.get("indices", []):
            if not isinstance(index, dict) or not isinstance(index.get("index_name"), str) or not isinstance(index.get("index_uuid"), str):
                raise InputError("fleet backing index is invalid")
            pairs.append((index["index_name"], index["index_uuid"]))
        # `_simulate_index` is the effective mappings/settings/default-pipeline/
        # lifecycle oracle, not merely a composed_of textual comparison.
        simulated = es_json(es_url, "/_index_template/_simulate_index/" + urllib.parse.quote(name, safe=""),
                            "POST", authorization, None)
        try:
            # This is the single ratified normalization for simulate results.
            # In particular, ES generates TSDB start/end boundaries from the
            # wall clock, so raw settings would make an unchanged stream look
            # different if the two Step-5 captures straddle a second.
            outcome = asset_adapters.simulation_outcome(simulated)
        except ValueError as error:
            raise InputError("fleet index simulation is invalid") from error
        snapshot[name] = {"backing": sorted(pairs), **outcome}
    return snapshot


def mint_key(es_url: str, authorization: str, role: dict, name: str) -> tuple[str, str]:
    response = es_json(es_url, "/_security/api_key", "POST", authorization,
                       {"name": name, "role_descriptors": {"rigsignal_shipper": role}})
    if not isinstance(response, dict) or not isinstance(response.get("id"), str) or not isinstance(response.get("encoded"), str):
        raise InputError("API key mint response is invalid")
    return response["id"], response["encoded"]


def invalidate(es_url: str, authorization: str, ids: list[str]) -> None:
    if not ids:
        return
    response = es_json(es_url, "/_security/api_key", "DELETE", authorization, {"ids": ids})
    if not isinstance(response, dict):
        raise InputError("API key invalidation was not confirmed")
    invalidated = response.get("invalidated_api_keys")
    previously = response.get("previously_invalidated_api_keys")
    error_count = response.get("error_count", 0)
    error_details = response.get("error_details", [])
    if (not isinstance(invalidated, list) or not all(isinstance(item, str) for item in invalidated)
            or not isinstance(previously, list) or not all(isinstance(item, str) for item in previously)
            or type(error_count) is not int or error_count != 0
            or not isinstance(error_details, list) or not all(isinstance(item, dict) for item in error_details)
            or any(item.get("id") in ids for item in error_details)):
        raise InputError("API key invalidation was not confirmed")
    # ES 9.4.3 returns both ID lists empty, with error_count=0, when an
    # already-invalidated key is invalidated again.  With a valid successful
    # response, absence from both lists is therefore affirmative inactive
    # state, not an unconfirmed revocation.


def invalidate_mint_name(es_url: str, authorization: str, mint_name: str) -> None:
    """Find and revoke every candidate made after a persisted mint intent.

    A process can die after Elasticsearch creates a key but before it can persist
    the returned ID.  The intent name is therefore a recovery handle, not just
    diagnostic text.  Refuse a malformed lookup response rather than treating
    it as proof that no orphan exists.
    """
    response = es_json(es_url, "/_security/api_key?name=" + urllib.parse.quote(mint_name, safe="")
                       + "&active_only=true",
                       "GET", authorization)
    keys = response.get("api_keys") if isinstance(response, dict) else None
    if not isinstance(keys, list):
        raise InputError("API key recovery lookup is invalid")
    ids: list[str] = []
    for item in keys:
        if (not isinstance(item, dict) or item.get("name") != mint_name
                or not isinstance(item.get("id"), str) or not item["id"]
                or len(item["id"].encode("utf-8")) > 1024):
            raise InputError("API key recovery lookup is invalid")
        ids.append(item["id"])
    invalidate(es_url, authorization, sorted(set(ids)))


def candidate_document(suffix: str) -> dict:
    document = parse_json(PROBE_FIXTURE.read_bytes(), "provision proof fixture")
    if not isinstance(document, dict):
        raise InputError("provision proof fixture is invalid")
    event_id = "provision-" + suffix
    document["@timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    document["event"] = {"id": event_id}
    document["host"] = {"name": socket.gethostname().lower()}
    return document


def assert_write_has_no_artifacts(response: object) -> None:
    if not isinstance(response, dict):
        raise InputError("candidate write response is invalid")
    if "_ignored" in response or response.get("failure_store") == "used":
        raise InputError("candidate write used ignored fields or failure store")


def assert_accepted_write_clean(response: object) -> None:
    assert_write_has_no_artifacts(response)


def assert_no_failure_store_document(es_url: str, authorization: str, event_id: str) -> None:
    try:
        result = es_json(es_url, "/" + DIAGNOSIS_STREAM + "::failures/_search", "POST", authorization,
                         {"query": {"term": {"event.id": event_id}}, "size": 1})
    except RequestFailure as error:
        # A disabled failure store may expose no searchable failure index.  A
        # 404 is therefore affirmative absence; every other response failure
        # leaves the proof incomplete.
        if error.status == 404:
            return
        raise
    hits = required_path(result, ("hits", "hits"))
    if not isinstance(hits, list) or hits:
        raise InputError("failure-store document exists after candidate write")


def assert_exact_probe_refetch(es_url: str, authorization: str, event_id: str, document: dict) -> None:
    result = es_json(es_url, "/" + DIAGNOSIS_STREAM + "/_search", "POST", authorization, {
        "query": {"ids": {"values": [event_id]}}, "size": 2,
    })
    hits = required_path(result, ("hits", "hits"))
    if (not isinstance(hits, list) or len(hits) != 1 or not isinstance(hits[0], dict)
            or hits[0].get("_id") != event_id or "_ignored" in hits[0]
            or jcs(hits[0].get("_source")) != jcs(document)):
        raise InputError("candidate exact-stream refetch failed")


def assert_mapping_rejection(status: int, response: object, error_type: str,
                             caused_by: str | None = None) -> None:
    error = response.get("error") if isinstance(response, dict) else None
    if status != 400 or not isinstance(error, dict) or error.get("type") != error_type:
        raise InputError("candidate mapping rejection proof failed")
    if caused_by is not None:
        cause = error.get("caused_by")
        if not isinstance(cause, dict) or cause.get("type") != caused_by:
            raise InputError("candidate mapping rejection proof failed")


def verify_stream_behavior(es_url: str, authorization: str, admin_authorization: str, suffix: str,
                           journal: TransactionJournal | None = None) -> None:
    document = candidate_document(suffix)
    event_id = document["event"]["id"]
    path = "/" + DIAGNOSIS_STREAM + "/_create/" + event_id + "?refresh=wait_for"
    proof_record = journal.proof_intent(event_id) if journal is not None else None
    fault("proof-create")
    status, response = es_json_status(es_url, path, "POST", authorization, document)
    if status != 201 or not isinstance(response, dict) or response.get("result") != "created":
        raise InputError("candidate exact-stream create failed")
    if proof_record is not None:
        index = response.get("_index")
        if not isinstance(index, str):
            raise InputError("candidate exact-stream create failed")
        journal.proof_index(proof_record, index)
    assert_accepted_write_clean(response)
    assert_exact_probe_refetch(es_url, admin_authorization, event_id, document)
    assert_no_failure_store_document(es_url, admin_authorization, event_id)
    # Real strictness proof; do not infer it from _simulate_index.
    bad = json.loads(json.dumps(document)); bad["unknown_root"] = True
    bad_id = "provision-bad-" + suffix
    status, response = es_json_status(es_url, "/" + DIAGNOSIS_STREAM + "/_create/" + bad_id,
                                      "POST", authorization, bad)
    assert_mapping_rejection(status, response, "strict_dynamic_mapping_exception")
    assert_write_has_no_artifacts(response)
    assert_no_failure_store_document(es_url, admin_authorization, bad_id)
    nested = json.loads(json.dumps(document))
    nested["rigsignal"]["diagnosis"]["unknown_probe_field"] = True
    nested_id = "provision-nested-" + suffix
    status, response = es_json_status(es_url, "/" + DIAGNOSIS_STREAM + "/_create/" + nested_id,
                                      "POST", authorization, nested)
    assert_mapping_rejection(status, response, "strict_dynamic_mapping_exception")
    assert_write_has_no_artifacts(response)
    assert_no_failure_store_document(es_url, admin_authorization, nested_id)
    malformed = json.loads(json.dumps(document))
    malformed["rigsignal"]["diagnosis"]["confidence"] = "not-a-number"
    malformed_id = "provision-malformed-" + suffix
    status, response = es_json_status(es_url, "/" + DIAGNOSIS_STREAM + "/_create/" + malformed_id,
                                      "POST", authorization, malformed)
    assert_mapping_rejection(status, response, "document_parsing_exception", "number_format_exception")
    assert_write_has_no_artifacts(response)
    assert_no_failure_store_document(es_url, admin_authorization, malformed_id)


def verify_role_matrix(es_url: str, authorization: str, suffix: str) -> None:
    document = candidate_document(suffix)
    path = "/" + DIAGNOSIS_STREAM + "/_create/provision-" + suffix
    # Exact CAN rows and deny matrix.  A duplicate _create is delivery idempotency
    # (409), while PUT to the existing ID must be an authorization failure (403).
    can_paths = ("/", "/_component_template/logs-rigsignal.diagnosis-mappings?filter_path=component_templates.name,component_templates.component_template._meta.accepted_schema_versions",
                 "/" + DIAGNOSIS_STREAM + "/_mapping")
    for item in can_paths:
        if response_status(es_url, item, "GET", authorization) != 200:
            raise InputError("candidate privilege CAN check failed")
    if response_status(es_url, path, "POST", authorization, document) != 409:
        raise InputError("candidate duplicate create check failed")
    # Overwrite proof, two layers (live wire disproved the 403 expectation:
    # data streams reject index-ops at request validation BEFORE authorization,
    # so PUT _doc returns 400 for any principal — structural impossibility):
    # (1) the 400 op_type guard below, (2) _has_privileges must show every
    # mutating privilege false on the exact stream name.
    if response_status(es_url, "/" + DIAGNOSIS_STREAM + "/_doc/provision-" + suffix,
                       "PUT", authorization, document) != 400:
        raise InputError("candidate overwrite op_type guard check failed")
    privileges = es_json(es_url, "/_security/user/_has_privileges", "POST", authorization, {
        "index": [{"names": [DIAGNOSIS_STREAM],
                   "privileges": ["index", "write", "delete", "delete_index", "manage"]}]})
    granted = privileges.get("index", {}).get(DIAGNOSIS_STREAM, {}) if isinstance(privileges, dict) else {}
    if not granted or any(granted.get(p) is not False for p in
                          ("index", "write", "delete", "delete_index", "manage")):
        raise InputError("candidate overwrite privilege check failed")
    denied = (("/" + DIAGNOSIS_STREAM + "/_doc/provision-" + suffix, "GET", None),
              ("/" + DIAGNOSIS_STREAM + "/_search", "POST", {"query": {"match_all": {}}}),
              # Bodies must be minimally VALID: ES validates the request shape
              # before authorization, so {} would 400 without proving denial.
              ("/_component_template/forbidden", "PUT", {"template": {"settings": {}}}),
              ("/_index_template/forbidden", "PUT",
               {"index_patterns": ["forbidden-provision-probe-*"], "template": {"settings": {}}}),
              ("/logs-rigsignal.diagnosis-other/_create/no", "POST", document))
    for item, method, payload in denied:
        if response_status(es_url, item, method, authorization, payload) != 403:
            raise InputError("candidate privilege CANNOT check failed")


def enrollment_files(endpoint: str, ca_file: Path, root: Path, uuid_value: str, generation: str,
                     encoded: str, state: dict) -> dict[str, bytes]:
    # Paths are JSON quoted to produce valid TOML basic strings without leaking a
    # shell interpolation path into configuration.
    q = lambda value: json.dumps(value, ensure_ascii=False)
    return {"credentials.toml": ("[elasticsearch]\napi_key = " + q(encoded) + "\n").encode(),
            "handshake.toml": ("[elasticsearch]\nendpoint = " + q(endpoint) + "\nca_cert = "
                               + q(str(ca_file.resolve())) + "\n").encode(),
            "shipping-policy-v1.toml": ("ship_mode = \"on\"\ninstall_profile = \"user\"\noutbox_root = "
                                        + q(str(root.parent / "outbox")) + "\ntarget_generation = \"" + generation
                                        + "\"\nexpected_cluster_uuid = \"" + uuid_value + "\"\n").encode(),
            "state.json": jcs(state) + b"\n"}


def run_handshake(agent: Path, root: Path) -> None:
    environment = os.environ.copy()
    for key in ("RIGSIGNAL_ENDPOINT", "RIGSIGNAL_CA_FILE", "RIGSIGNAL_EXPECTED_CLUSTER_UUID",
                "RIGSIGNAL_PENDING_ENROLLMENT", "RIGSIGNAL_TARGET_GENERATION", "RIGSIGNAL_API_KEY"):
        environment.pop(key, None)
    result = subprocess.run([str(agent), "handshake", "check", "--config", str(root / "handshake.toml"),
                             "--credentials-file", str(root / "credentials.toml")], env=environment,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        raise InputError("published handshake failed")


def default_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "rigsignal" / "enrollment"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="bundle tarball to install")
    parser.add_argument("--endpoint", required=True, help="Elasticsearch HTTPS origin")
    parser.add_argument("--ca-file", type=Path, required=True, help="Elasticsearch CA file")
    parser.add_argument("--kibana-endpoint", required=True, help="Kibana HTTPS origin")
    parser.add_argument("--kibana-ca-file", type=Path, required=True, help="Kibana CA file")
    parser.add_argument("--admin-credentials-file", type=Path, required=True,
                        help="protected administrator TOML credential")
    parser.add_argument("--agent-binary", type=Path, required=True)
    parser.add_argument("--profile", choices=("user", "system"), required=True)
    parser.add_argument("--enrollment-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--adopt-existing-w1-stream", action="store_true",
                        help="one-shot adoption of a compatible pre-existing W1 diagnosis stream")
    parser.add_argument("--ownership-profile", choices=("default", "fleet-coexist"), default=None,
                        help="ownership policy for a Fleet-coexisting cluster")
    parser.add_argument("--rollback", type=Path, metavar="TRANSACTION",
                        help="explicitly reverse the journaled Fleet-coexist transaction at TRANSACTION")
    parser.add_argument("--dry-run", action="store_true", help="list API calls without network access")
    parser.add_argument("--unsafe-test-injection", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    active_test_hooks = sorted(key for key, value in os.environ.items()
                               if key.startswith("RIGSIGNAL_TEST_") and value)
    if active_test_hooks:
        print("test hooks active: " + ",".join(active_test_hooks), file=sys.stderr)
    raw_ownership_profile = args.ownership_profile
    ownership_profile = raw_ownership_profile or "default"
    try:
        if args.profile != "user":
            raise InputError("profile system is unsupported/broker-required")
        es_url = https_origin(args.endpoint, "--endpoint")
        kb_url = https_origin(args.kibana_endpoint, "--kibana-endpoint")
        if args.rollback is not None:
            if args.dry_run:
                raise InputError("rollback dry-run is unsupported")
            configure_https(args.ca_file)
            configure_https(args.kibana_ca_file)
            authorization = admin_authorization(args.admin_credentials_file)
            # Rollback is an invocation boundary too: the ratified invariant
            # (RD "every boundary", ruling 5 stamp) fences profile and table
            # version before any journaled reversal begins (S1-v4).
            fence_remote_ownership_profile(es_url, authorization,
                                           load_ownership_profile(args.rollback) or "default",
                                           False)
            operations = rollback_transaction(es_url, kb_url, authorization, args.rollback,
                                             deliberately_reversed=True, bundle_path=args.bundle)
            reported = False
            if any(item.startswith("verify-only:transforms/") for item in operations):
                print("rollback completed from journaled intents; transform _meta absence could not be restored: "
                      "verify-only cosmetic drift accepted")
                reported = True
            if any(item.startswith("retained-in-use:pipelines/") for item in operations):
                print("rollback completed from journaled intents; pipeline retained: in use as default pipeline for adopted stream indices")
                reported = True
            if not reported:
                print("rollback completed from journaled intents")
            return 0
        if args.bundle is None:
            raise InputError("--bundle is required unless --rollback is used")
        bundle = load_bundle(args.bundle)  # Step 1: no HTTP before this line succeeds.
        role = role_body(bundle)
        ownership = ownership_for_assets(bundle, ownership_profile)
    except ProvisionError as error:
        print(error.prefix, file=sys.stderr)
        return 1
    except InputError as error:
        if isinstance(error, OwnershipTableError):
            print("install refused: " + str(error), file=sys.stderr)
            return 1
        print(f"install failed: bundle validation:", file=sys.stderr)
        return 1

    total = len(bundle.assets)
    if args.dry_run:
        for asset in bundle.assets:
            if ownership[(asset.kind, asset.name)] == "external":
                print(f"external {asset.kind} {asset.name} -> GET {es_path(asset)} (verify-only)")
                continue
            if asset.kind == "dashboard":
                print(f"dashboard {asset.name} -> POST {dashboard_import_path(asset)}")
            elif asset.kind == "kibana_spaces":
                print(f"kibana-space {asset.name} -> GET {kibana_path(asset)}; "
                      f"POST /api/spaces/space; PUT {kibana_path(asset)}")
            elif asset.kind == "kibana_roles":
                print(f"kibana-role {asset.name} -> PUT/GET {kibana_path(asset)}")
            elif asset.kind == "transforms":
                print(f"transform {asset.name} -> PUT/POST {es_path(asset)}")
            else:
                print(f"{asset.kind} {asset.name} -> PUT {es_path(asset)}")
        print("bundle marker rigsignal-bundle-meta -> PUT /_component_template/rigsignal-bundle-meta")
        print(f"ownership profile: {ownership_profile}")
        print(f"source assets: {total}")
        return 0

    try:
        requested_root = args.enrollment_root or default_root()
        condition = enrollment_condition(requested_root)
        if condition == "remediation":
            raise ProvisionError("install refused: enrollment_remediation_required")
        adopt_requested = getattr(args, "adopt_existing_w1_stream", False)
        if adopt_requested and condition in {"committed", "incomplete"}:
            raise ProvisionError("install refused: adoption_flag_state_present")
        configure_https(args.ca_file)
        configure_https(args.kibana_ca_file)
        authorization = admin_authorization(args.admin_credentials_file)
        if admin_credential_kind(args.admin_credentials_file) != "native_user":
            # API keys may still parse for dry-run/read-only tooling, but this
            # invocation will mint a descriptor-bearing shipper key.
            raise ProvisionError("install refused: admin_credential_api_key")
        # A clean root or the owner-ratified rolled-back audit-only root can
        # adopt a compatible remote stream.  Decide it before creating the
        # root or running recovery.
        adoption = (dispatch_clean_root(es_url, authorization, adopt_requested)
                     if condition in {"clean", "rolled-back"} else False)

        # The marker survives local rollback and a fresh enrollment root.  It
        # is therefore the authoritative rerun fence, ahead of secure_root()
        # and every subsequent mutation.
        fence_remote_ownership_profile(es_url, authorization, ownership_profile,
                                       raw_ownership_profile is None)
        root = secure_root(requested_root)
        try:
            prior = load_state(root)
        except StateBindingError as error:
            raise ProvisionError("install refused: enrollment_remediation_required") from error
        bind_ownership_profile(root, ownership_profile, implicit_default=raw_ownership_profile is None)

        # Step 2: recover/pin before normal work.  Unpublished credentials are
        # never reused; their identifiers survive in state for deterministic
        # recovery.  A later run can safely revoke the listed candidate.
        uuid_value = cluster_uuid(es_url, authorization)
        if prior is not None and prior["expected_cluster_uuid"] != uuid_value:
            # A retained enrollment root pointed at another cluster is the
            # v2.4 rerun refusal, with the same no-mutation contract as an
            # incompatible existing diagnosis stream.
            raise ProvisionError("install refused: existing diagnosis stream is not W1; migration is required")
        if prior is not None and prior["phase"] != "committed":
            # candidate_verified with candidate already named as active is the
            # only recoverable post-publication state: credentials/configuration
            # were atomically released and only old-key cleanup was interrupted.
            if (prior["phase"] == "candidate_verified" and prior["candidate_key_id"]
                    and prior["active_key_id"] == prior["candidate_key_id"]):
                try:
                    # The exchanged directory is coherent, but a crash may
                    # have happened before Step 10's zero-environment probe.
                    # Do not declare it committed until that exact consumer
                    # check succeeds on the published paths.
                    run_handshake(args.agent_binary, root)
                    invalidate(es_url, authorization, prior["pending_revoke_ids"])
                except (InputError, RequestFailure) as error:
                    raise ProvisionError("install failed: old shipper API key revocation:") from error
                prior = state_template(uuid_value, prior["target_generation"], prior["active_key_id"],
                                       prior["enrollment_root"])
                atomic_write(root, "state.json", jcs(prior) + b"\n")
            else:
                # mint_intent is durable before the request.  A returned key ID
                # is therefore insufficient for recovery: a crash after the
                # server creates it but before our response is stored leaves an
                # otherwise unreachable live key.  Discover by the exact intent
                # name first, then invalidate the recorded ID as well.
                invalidate_mint_name(es_url, authorization, prior["pending_mint_name"])
                candidates = [item for item in (prior["candidate_key_id"],) if item]
                if candidates:
                    invalidate(es_url, authorization, candidates)
                remove_candidate_root(root)
                # Do not silently preserve an incomplete candidate as active.
                if prior["active_key_id"] is None:
                    remove_recovered_state(root)
                    prior = None
                else:
                    prior = state_template(uuid_value, prior["target_generation"], prior["active_key_id"],
                                           prior["enrollment_root"])
                    atomic_write(root, "state.json", jcs(prior) + b"\n")
        # A pre-exchange crash leaves only this deterministic private staging
        # path.  After phase recovery revokes/finishes its key lifecycle, it is
        # safe to remove whichever old or unpublished generation remains here.
        remove_stale_publication_stage(root)

        if condition == "incomplete" and prior is None:
            # A null-active recovery restores the clean-root condition.  Apply
            # the same remote decision matrix now that recovery side effects
            # are durable; adoption is one-shot and was already rejected above.
            dispatch_clean_root(es_url, authorization, False)

        prerequisites(es_url, kb_url, authorization)  # Step 3
        cluster_health_gate(es_url, authorization)  # protocol invariant, all profiles
        fence(es_url, authorization, prior, uuid_value, root, adoption)  # Step 4, before W1 PUT
        pre_put_condition, pre_put_snapshot = remote_stream_condition(es_url, authorization)
        if pre_put_condition == "absent":
            pre_put_snapshot = None
        elif pre_put_condition != "compatible":
            raise ProvisionError("install refused: migration_required")

        # Step 5: external members are verified as one no-write barrier before
        # any bundle-owned mutation.  The default profile deliberately retains
        # the established PUT-everything path.
        applied_owned_assets: list[dict] = []
        verified_external_assets: list[dict] = []
        journal: TransactionJournal | None = None
        pre_fleet_snapshot: dict[str, object] | None = None
        if ownership_profile == "fleet-coexist":
            try:
                # This capture dynamically enumerates the active stream set;
                # a rollover during this transaction is a fail-closed drift.
                pre_fleet_snapshot = fleet_stream_snapshot(es_url, authorization)
                test_rollover("after-fleet-snapshot", es_url, authorization, pre_fleet_snapshot)
                journal = TransactionJournal(root, ownership_profile, new_transaction=True)
                journal.pin_bundle(args.bundle, bundle)
                if not journal.value.get("m1_anchors"):
                    journal.pin_m1_anchors(m1_anchor_pins(es_url, authorization))
                for asset in bundle.assets:
                    if ownership[(asset.kind, asset.name)] == "external":
                        if external_write_test_allowed(es_url, args.unsafe_test_injection):
                            # Gate-only negative control for the recording
                            # transport.  It is deliberately impossible to
                            # trigger without an explicit test environment.
                            request(es_url, es_path(asset), "PUT", authorization, asset.data)
                        verified_external_assets.append(verify_external_asset(es_url, authorization, asset))
                journal.pin_external_baselines(verified_external_assets)
            except (RequestFailure, InputError) as error:
                raise ProvisionError(f"install refused: external asset compatibility: {error}") from error
        for asset in bundle.assets:
            if ownership[(asset.kind, asset.name)] == "external":
                continue
            try:
                action = (owned_action(es_url, kb_url, authorization, asset)
                          if ownership_profile == "fleet-coexist" else "update")
                records: list[dict] = []
                if journal is not None:
                    if asset.kind == "index_templates" and asset.name == "logs-rigsignal.stream" and action != "noop":
                        lifecycle_delete_phase_free(es_url, authorization)
                    records = journal_owned_asset(journal, es_url, kb_url, authorization, asset, action)
                    if (asset.kind == "transforms" and action != "noop"
                            and transform_preapply_requires_verify_only(
                                journal, records[0], es_url, authorization, asset)):
                        journal.mark_transform_verify_only(
                            records[0], "meta_absent_restore_unproven_preapply")
                        action = "noop"
                    fault("after-write-intent")
                if action != "noop":
                    if asset.kind == "dashboard":
                        fault("dashboard-multipart")
                    install_asset(es_url, kb_url, authorization, asset)
                    fault("after-remote-mutation")
                if journal is not None:
                    journal_verify_owned_asset(journal, records, es_url, kb_url, authorization, asset)
                    fault("after-write-verified")
                if ownership_profile == "fleet-coexist":
                    applied_owned_assets.append({"kind": asset.kind, "name": asset.name, "action": action,
                                                 "request_body_sha256": hashlib.sha256(asset.data).hexdigest()})
            except (RequestFailure, InputError) as error:
                if asset.kind == "security_roles":
                    category = "shipper role verification:"
                elif asset.kind in {"kibana_spaces", "kibana_roles", "dashboard"}:
                    category = "Kibana asset verification:"
                else:
                    category = "W1 asset verification:"
                raise ProvisionError("install failed: " + category) from error
        try:
            ensure_stream(es_url, authorization)
            simulate(es_url, authorization)
            if ownership_profile == "fleet-coexist":
                post_fleet_snapshot = fleet_stream_snapshot(es_url, authorization)
                # A fresh transaction may legitimately create the diagnosis
                # stream after the pre-Step-5 capture.  Every stream that was
                # active when the transaction started must nevertheless be
                # byte-for-byte invariant; a changed/missing member is an
                # in-transaction rollover or drift and fails closed.
                if any(post_fleet_snapshot.get(name) != value
                       for name, value in (pre_fleet_snapshot or {}).items()):
                    raise InputError("fleet stream snapshot drifted")
        except (RequestFailure, InputError) as error:
            raise ProvisionError("install failed: fleet stream verification:") from error
        if pre_put_snapshot is None:
            # Fresh installation has no stream to snapshot before its W1
            # creates; from here on it receives the same drift protection.
            _, pre_put_snapshot = remote_stream_condition(es_url, authorization)
            if pre_put_snapshot is None:
                raise ProvisionError("install failed: diagnosis stream verification:")

        generation = recompute_target_generation({asset.path: asset.data for asset in bundle.assets})
        # A current key is only retained after a fresh proof.  Reading its secret
        # from the protected credential file is intentional and never logged.
        encoded: str | None = None
        reuse = prior is not None and prior["role_jcs_sha256"] == ROLE_JCS_SHA256
        if reuse:
            try:
                credential = parse_json(b"{}", "internal")
                del credential
                import tomllib
                encoded = tomllib.loads((secure_read(root / "credentials.toml") or b"").decode())["elasticsearch"]["api_key"]
                if not isinstance(encoded, str):
                    raise ValueError()
                proof_suffix = uuid.uuid4().hex
                verify_stream_behavior(es_url, "ApiKey " + encoded, authorization, proof_suffix, journal)
                verify_role_matrix(es_url, "ApiKey " + encoded, proof_suffix)
            except (InputError, ValueError, KeyError, TypeError, RequestFailure):
                reuse = False
                encoded = None
        old_id = prior["active_key_id"] if prior else None
        if not reuse:
            mint_name = "rigsignal-provision-" + uuid.uuid4().hex
            intent = state_template(uuid_value, generation, old_id, str(root))
            intent.update(phase="mint_intent", pending_mint_name=mint_name)
            atomic_write(root, "state.json", jcs(intent) + b"\n")
            mint_journal = None
            if journal is not None:
                mint_request = jcs({"name": mint_name, "role_descriptors": {"rigsignal_shipper": role}})
                mint_journal = journal.write_intent("api_key", mint_name, "create",
                                                    asset_adapters.dashboard_absent_hash(),
                                                    hashlib.sha256(mint_request).hexdigest(), mint_request)
            fault("before-mint-response")
            candidate_id, encoded = mint_key(es_url, authorization, role, mint_name)
            if mint_journal is not None:
                # The exact request pin is the durable recovery discriminator;
                # the returned opaque key ID is persisted by the existing state
                # transition immediately after this verification record.
                journal.write_verified(mint_journal, mint_journal["intended_after_sha256"])
                journal.api_key_id(mint_journal, candidate_id)
            fault("after-mint-response")
            staged = dict(intent)
            staged.update(phase="candidate_staged", candidate_key_id=candidate_id)
            atomic_write(root, "state.json", jcs(staged) + b"\n")
            candidate_root = secure_candidate_root(root)
            candidate_files = enrollment_files(es_url, args.ca_file, root, uuid_value, generation, encoded, staged)
            for name, contents in candidate_files.items():
                atomic_write(candidate_root, name, contents)
            fault("candidate-write")
            try:
                proof_suffix = uuid.uuid4().hex
                verify_stream_behavior(es_url, "ApiKey " + encoded, authorization, proof_suffix, journal)  # Step 7
            except (InputError, RequestFailure):
                invalidate(es_url, authorization, [candidate_id])
                raise ProvisionError("install failed: diagnosis stream verification:")
            try:
                verify_role_matrix(es_url, "ApiKey " + encoded, proof_suffix)  # Step 8
            except (InputError, RequestFailure):
                invalidate(es_url, authorization, [candidate_id])
                raise ProvisionError("install failed: shipper credential verification:")
            staged["phase"] = "candidate_verified"
            atomic_write(root, "state.json", jcs(staged) + b"\n")
            fault("candidate-verify")
        else:
            candidate_id = old_id
            staged = state_template(uuid_value, generation, candidate_id, str(root))

        assert encoded is not None and candidate_id is not None
        try:
            # The asset writes, candidate checks, and publication are separated
            # by a final read-only fence.  It closes the period in which a
            # rollover or template mutation could otherwise be published over.
            prepublication_asset_fence(es_url, kb_url, authorization, bundle,
                                       ownership_profile, ownership,
                                       journal.value.get("external_baselines") if journal is not None else None)
            simulate(es_url, authorization)
            post_condition, post_snapshot = remote_stream_condition(es_url, authorization)
            if post_condition != "compatible" or post_snapshot != pre_put_snapshot:
                raise InputError("pre-publication stream snapshot drifted")
            if journal is not None:
                verify_m1_anchors(es_url, authorization, journal.value.get("m1_anchors", {}))
        except (InputError, RequestFailure) as error:
            # The durable candidate state is deliberately retained for the
            # established recovery path; no consumer publication or marker can
            # occur after this fence fails.
            raise ProvisionError("install failed: pre-publication fence:") from error
        final = state_template(uuid_value, generation, candidate_id, str(root))
        # Step 9.  During replacement the old ID is kept pending until published
        # files verify, but state is committed only after its confirmation.
        # The directory exchange publishes a coherent but deliberately
        # uncommitted generation.  Step 10's published-file probe is the only
        # operation allowed to advance it to committed, including a reuse-only
        # template generation where no key was minted.
        publish = dict(final)
        publish.update(phase="candidate_verified", pending_mint_name="published-pending-revoke",
                       candidate_key_id=candidate_id)
        if old_id and old_id != candidate_id:
            publish["pending_revoke_ids"] = [old_id]
        publication_files = enrollment_files(es_url, args.ca_file, root, uuid_value, generation, encoded, publish)
        # A minted candidate is staged under the private candidate directory
        # before any named consumer file is touched.  Reuse has no new secret
        # to stage, so render the equivalent already-proved generation here.
        if not reuse:
            candidate_root = secure_candidate_root(root)
            for name in publication_files:
                atomic_write(candidate_root, name, publication_files[name])
        atomic_publication(root, publication_files)
        fault("published-state")

        # Step 10 has no endpoint/credential environment fallback.
        run_handshake(args.agent_binary, root)
        if old_id and old_id != candidate_id:
            try:
                fault("before-revoke")
                invalidate(es_url, authorization, [old_id])
                fault("after-revoke")
            except (InputError, RequestFailure) as error:
                raise ProvisionError("install failed: old shipper API key revocation:") from error
        atomic_write(root, "state.json", jcs(final) + b"\n")
        remove_candidate_root(root)

        # Step 11 and only step 11: marker is never an early partial-success bit.
        marker = Asset("component_templates", "rigsignal-bundle-meta", "", marker_body(
            bundle, ownership_profile, applied_owned_assets, verified_external_assets))
        try:
            if journal is not None:
                marker_records = journal_owned_asset(journal, es_url, kb_url, authorization, marker, "create")
            request(es_url, es_path(marker), "PUT", authorization, marker.data)
            verify_asset(es_url, authorization, marker)
            if journal is not None:
                journal_verify_owned_asset(journal, marker_records, es_url, kb_url, authorization, marker)
                journal.apply_ok()
        except (InputError, RequestFailure) as error:
            raise ProvisionError("install failed: bundle marker:") from error
        if ownership_profile == "fleet-coexist":
            print(f"applied {len(applied_owned_assets)} owned assets; verified "
                  f"{len(verified_external_assets)} external assets")
        else:
            print(f"installed {total}/{total} assets")
        return 0
    except ProvisionError as error:
        print(error.prefix, file=sys.stderr)
        return 1
    except (InputError, RequestFailure, OSError) as error:
        # The public contract deliberately avoids exposing response bodies and
        # exception text, which could contain credentials or cluster data.
        print("install failed: enrollment output:", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
