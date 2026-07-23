#!/usr/bin/env python3
"""Install a RigSignal asset bundle, with post-install presence verification."""

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
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


ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = ROOT / "elastic"
DASHBOARD_DIR = ROOT / "dashboards" / "v0.3.1"
ASSET_TYPES = {
    "component-templates": "component_templates",
    "index-templates": "index_templates",
    "pipelines": "pipelines",
    "transforms": "transforms",
    "security-roles": "security_roles",
}
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*\.json\Z")
W1_RAW_SHA256 = {
    "elastic/component-templates/logs-rigsignal.diagnosis-mappings.json": "345e0d2898279929eb613b60d2bd250bbf73a13c7b4bbd1b793384e2ae00410c",
    "elastic/index-templates/logs-rigsignal.diagnosis.json": "5f4d4f403fc17a1096b2d2b1c8a43bad94efa52b05c3b117961333b2f3d52199",
    "elastic/security-roles/rigsignal_shipper.json": "6eb0279c7e05b94bfd96083508a8c7e6ad5ca9cb65e531654bd6e0ae3eca7ed2",
}
TARGET_GENERATION_SCHEME = "rigsignal:target-generation:w1-assets:v1"
TARGET_GENERATION_KAT = "a7ed20a4b4bfe0b2e5597a065e8bdaa5161b0d962e1a502d3db3bbcc97e8ee7a"
ROLE_JCS_SHA256 = "05b58b8369bc4212fcffa0ea81621ef10d6d57f1de464fbc3f562842a9cbafd7"
DIAGNOSIS_STREAM = "logs-rigsignal.diagnosis-default"
STATE_KEYS = frozenset(("version", "phase", "expected_cluster_uuid", "target_generation",
                        "role_jcs_sha256", "enrollment_root", "active_key_id", "pending_revoke_ids",
                        "pending_mint_name", "candidate_key_id"))
STATE_PHASES = frozenset(("committed", "mint_intent", "candidate_staged", "candidate_verified"))
UUID_RE = re.compile(r"[A-Za-z0-9_-]{22}\Z")
HEX_RE = re.compile(r"[0-9a-f]{64}\Z")


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


def ordered_assets(assets: list[Asset]) -> list[Asset]:
    order = {"component_templates": 0, "index_templates": 1, "security_roles": 2,
             "pipelines": 3, "transforms": 4, "dashboard": 5}
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


def multipart_dashboard(asset: Asset) -> tuple[bytes, str]:
    boundary = f"----rigsignal-{uuid.uuid4().hex}"
    head = (f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{asset.name}\"\r\n"
            "Content-Type: application/ndjson\r\n\r\n").encode("utf-8")
    return head + asset.data + f"\r\n--{boundary}--\r\n".encode("utf-8"), boundary


def marker_body(bundle: Bundle) -> bytes:
    return json.dumps({"_meta": {"bundle_version": bundle.version,
                                  "source_commit": bundle.source_commit,
                                  "installed_at_field": "set by server"},
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
    allowed = {"credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json", "candidate"}
    entries = {entry.name for entry in root.iterdir()}
    if not entries.issubset(allowed):
        raise InputError("old enrollment generation is invalid")
    for name in ("credentials.toml", "handshake.toml", "shipping-policy-v1.toml", "state.json"):
        path = root / name
        if path.exists():
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise InputError("old enrollment generation is invalid")
            path.unlink()
    remove_candidate_root(root)
    root.rmdir()


def fault(point: str) -> None:
    """Test-only crash hook; inert unless explicitly set by a test."""
    if os.environ.get("RIGSIGNAL_TEST_CRASH_AT") == point:
        os._exit(99)


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
        request(kb_url, "/api/saved_objects/_import?overwrite=true", "POST", authorization, body,
                {"Content-Type": f"multipart/form-data; boundary={boundary}", "kbn-xsrf": "true"})
        for object_type, object_id in dashboard_objects(asset.data):
            request(kb_url, "/api/saved_objects/" + urllib.parse.quote(object_type, safe="") + "/"
                    + urllib.parse.quote(object_id, safe=""), "GET", authorization,
                    headers={"kbn-xsrf": "true"})
        return
    path = es_path(asset)
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


def existing_stream_is_compatible(es_url: str, authorization: str, state: dict | None, uuid_value: str,
                                  root: Path | None = None) -> bool:
    if state is not None:
        if root is None:
            raise InputError("enrollment root is required for state ownership")
        validate_state_binding(state, root)
    try:
        response = es_json(es_url, "/_data_stream/" + DIAGNOSIS_STREAM, "GET", authorization)
    except RequestFailure as error:
        if error.status == 404:
            return True
        raise
    streams = response.get("data_streams") if isinstance(response, dict) else None
    if (not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict)
            or streams[0].get("name") != DIAGNOSIS_STREAM or not isinstance(streams[0].get("indices"), list)
            or state is None or state["phase"] != "committed" or state["expected_cluster_uuid"] != uuid_value):
        return False
    indices = streams[0]["indices"]
    if not indices or not all(isinstance(item, dict) and isinstance(item.get("index_name"), str)
                              for item in indices):
        return False
    # state.json establishes installation ownership, but cannot prove that a
    # template change retrofitted every concrete backing index.  Compare every
    # live backing mapping and flat settings with the live desired simulation.
    desired = canonical_owned_mapping_projection()
    return all(jcs(backing_owned_mapping_projection(es_url, authorization, item["index_name"])) == jcs(desired)
               for item in indices)


def fence(es_url: str, authorization: str, state: dict | None, uuid_value: str,
          root: Path | None = None) -> None:
    try:
        compatible = existing_stream_is_compatible(es_url, authorization, state, uuid_value, root)
    except StateBindingError as error:
        raise ProvisionError("install refused: enrollment state is not valid for this enrollment root") from error
    except (RequestFailure, InputError) as error:
        raise ProvisionError("install refused: existing diagnosis stream is not W1; migration is required") from error
    if not compatible:
        raise ProvisionError("install refused: existing diagnosis stream is not W1; migration is required")


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
    invalidated = response.get("invalidated_api_keys", []) if isinstance(response, dict) else []
    previously = response.get("previously_invalidated_api_keys", []) if isinstance(response, dict) else []
    if not set(ids).issubset(set(invalidated) | set(previously)):
        raise InputError("API key invalidation was not confirmed")


def invalidate_mint_name(es_url: str, authorization: str, mint_name: str) -> None:
    """Find and revoke every candidate made after a persisted mint intent.

    A process can die after Elasticsearch creates a key but before it can persist
    the returned ID.  The intent name is therefore a recovery handle, not just
    diagnostic text.  Refuse a malformed lookup response rather than treating
    it as proof that no orphan exists.
    """
    response = es_json(es_url, "/_security/api_key?name=" + urllib.parse.quote(mint_name, safe=""),
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
    return {"@timestamp": "2026-07-23T00:00:00.000Z", "event": {"id": "provision-" + suffix},
            "rigsignal": {"diagnosis": {"schema_version": 1, "outcome": "finding",
            "detector_id": "provision", "rule_version": "1", "input_mode": "test",
            "verdict": "ok", "disposition": "report", "confidence": 1.0,
            "confidence_basis": "provision", "evidence": [], "plain_language": "provision"}}}


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


def verify_stream_behavior(es_url: str, authorization: str, admin_authorization: str, suffix: str) -> None:
    document = candidate_document(suffix)
    path = "/" + DIAGNOSIS_STREAM + "/_create/provision-" + suffix
    status, response = es_json_status(es_url, path, "POST", authorization, document)
    if status != 201 or not isinstance(response, dict) or response.get("result") != "created":
        raise InputError("candidate exact-stream create failed")
    assert_accepted_write_clean(response)
    assert_no_failure_store_document(es_url, admin_authorization, "provision-" + suffix)
    # Real strictness proof; do not infer it from _simulate_index.
    bad = dict(document); bad["unknown_root"] = True
    bad_id = "provision-bad-" + suffix
    status, response = es_json_status(es_url, "/" + DIAGNOSIS_STREAM + "/_create/" + bad_id,
                                      "POST", authorization, bad)
    if status < 400:
        raise InputError("unknown field was accepted")
    assert_write_has_no_artifacts(response)
    assert_no_failure_store_document(es_url, admin_authorization, bad_id)
    malformed = candidate_document("malformed-" + suffix)
    malformed["rigsignal"]["diagnosis"]["confidence"] = "not-a-number"
    malformed_id = "provision-malformed-" + suffix
    status, response = es_json_status(es_url, "/" + DIAGNOSIS_STREAM + "/_create/" + malformed_id,
                                      "POST", authorization, malformed)
    if status < 400:
        raise InputError("malformed scalar was accepted")
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
    parser.add_argument("--bundle", type=Path, required=True, help="bundle tarball to install")
    parser.add_argument("--endpoint", required=True, help="Elasticsearch HTTPS origin")
    parser.add_argument("--ca-file", type=Path, required=True, help="Elasticsearch CA file")
    parser.add_argument("--kibana-endpoint", required=True, help="Kibana HTTPS origin")
    parser.add_argument("--kibana-ca-file", type=Path, required=True, help="Kibana CA file")
    parser.add_argument("--admin-credentials-file", type=Path, required=True,
                        help="protected administrator TOML credential")
    parser.add_argument("--agent-binary", type=Path, required=True)
    parser.add_argument("--profile", choices=("user", "system"), required=True)
    parser.add_argument("--enrollment-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="list API calls without network access")
    args = parser.parse_args()
    try:
        if args.profile != "user":
            raise InputError("profile system is unsupported/broker-required")
        es_url = https_origin(args.endpoint, "--endpoint")
        kb_url = https_origin(args.kibana_endpoint, "--kibana-endpoint")
        bundle = load_bundle(args.bundle)  # Step 1: no HTTP before this line succeeds.
        role = role_body(bundle)
    except InputError as error:
        print(f"install failed: bundle validation:", file=sys.stderr)
        return 1

    total = len(bundle.assets)
    if args.dry_run:
        for asset in bundle.assets:
            if asset.kind == "dashboard":
                print(f"dashboard {asset.name} -> POST /api/saved_objects/_import?overwrite=true")
            elif asset.kind == "transforms":
                print(f"transform {asset.name} -> PUT/POST {es_path(asset)}")
            else:
                print(f"{asset.kind} {asset.name} -> PUT {es_path(asset)}")
        print("bundle marker rigsignal-bundle-meta -> PUT /_component_template/rigsignal-bundle-meta")
        print(f"source assets: {total}")
        return 0

    try:
        configure_https(args.ca_file)
        configure_https(args.kibana_ca_file)
        authorization = admin_authorization(args.admin_credentials_file)
        root = secure_root(args.enrollment_root or default_root())
        try:
            prior = load_state(root)
        except StateBindingError as error:
            raise ProvisionError("install refused: enrollment state is not valid for this enrollment root") from error

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
                prior = None if prior["active_key_id"] is None else state_template(
                    uuid_value, prior["target_generation"], prior["active_key_id"],
                    prior["enrollment_root"])
                if prior is not None:
                    atomic_write(root, "state.json", jcs(prior) + b"\n")
        # A pre-exchange crash leaves only this deterministic private staging
        # path.  After phase recovery revokes/finishes its key lifecycle, it is
        # safe to remove whichever old or unpublished generation remains here.
        remove_stale_publication_stage(root)

        prerequisites(es_url, kb_url, authorization)  # Step 3
        fence(es_url, authorization, prior, uuid_value, root)  # Step 4, before W1 PUT

        # Step 5: the ordered complete manifest barrier.
        for asset in bundle.assets:
            try:
                install_asset(es_url, kb_url, authorization, asset)
            except (RequestFailure, InputError) as error:
                category = "shipper role verification:" if asset.kind == "security_roles" else "W1 asset verification:"
                raise ProvisionError("install failed: " + category) from error
        try:
            ensure_stream(es_url, authorization)
            simulate(es_url, authorization)
        except (RequestFailure, InputError) as error:
            raise ProvisionError("install failed: diagnosis stream verification:") from error

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
                verify_stream_behavior(es_url, "ApiKey " + encoded, authorization, "active-proof")
                verify_role_matrix(es_url, "ApiKey " + encoded, "active-proof")
            except (InputError, ValueError, KeyError, TypeError, RequestFailure):
                reuse = False
                encoded = None
        old_id = prior["active_key_id"] if prior else None
        if not reuse:
            mint_name = "rigsignal-provision-" + uuid.uuid4().hex
            intent = state_template(uuid_value, generation, old_id, str(root))
            intent.update(phase="mint_intent", pending_mint_name=mint_name)
            atomic_write(root, "state.json", jcs(intent) + b"\n")
            fault("before-mint-response")
            candidate_id, encoded = mint_key(es_url, authorization, role, mint_name)
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
                verify_stream_behavior(es_url, "ApiKey " + encoded, authorization, candidate_id[-12:])  # Step 7
            except (InputError, RequestFailure):
                invalidate(es_url, authorization, [candidate_id])
                raise ProvisionError("install failed: diagnosis stream verification:")
            try:
                verify_role_matrix(es_url, "ApiKey " + encoded, candidate_id[-12:])  # Step 8
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
        marker = Asset("component_templates", "rigsignal-bundle-meta", "", marker_body(bundle))
        try:
            request(es_url, es_path(marker), "PUT", authorization, marker.data)
            verify_asset(es_url, authorization, marker)
        except (InputError, RequestFailure) as error:
            raise ProvisionError("install failed: bundle marker:") from error
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
