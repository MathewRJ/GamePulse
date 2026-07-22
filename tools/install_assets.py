#!/usr/bin/env python3
"""Install a RigSignal asset bundle, with post-install presence verification."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
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
}
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*\.json\Z")


class InputError(Exception):
    """The requested source or bundle is incomplete or invalid."""


class RequestFailure(Exception):
    def __init__(self, status: int | None, detail: str):
        self.status = status
        super().__init__(detail)


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
            json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InputError(f"invalid JSON asset {path}: {error}") from error
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
            members = {member.name: member for member in tar.getmembers() if member.isfile()}
            manifest_member = members.pop("manifest.json", None)
            if manifest_member is None:
                raise InputError("bundle is missing manifest.json")
            manifest_data = tar.extractfile(manifest_member)
            if manifest_data is None:
                raise InputError("bundle manifest cannot be read")
            manifest = json.loads(manifest_data.read())
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
    return Bundle(version, commit, ordered_assets(assets))


def ordered_assets(assets: list[Asset]) -> list[Asset]:
    order = {"component_templates": 0, "index_templates": 1, "pipelines": 2,
             "transforms": 3, "dashboard": 4}
    return sorted(assets, key=lambda asset: (order[asset.kind], asset.name))


def count_assets(assets: list[Asset]) -> dict[str, int]:
    counts = {name: 0 for name in ASSET_TYPES.values()}
    counts["dashboards"] = 0
    for asset in assets:
        counts["dashboards" if asset.kind == "dashboard" else asset.kind] += 1
    return counts


def auth_header(value: str) -> str:
    if value.startswith("ApiKey ") and value[7:]:
        return value
    if ":" in value:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"
    raise InputError("RIGSIGNAL_ES_AUTH must be user:pass or ApiKey <key>")


def request(base: str, path: str, method: str, authorization: str, data: bytes | None = None,
            headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"Authorization": authorization, **(headers or {})}
    if data is not None and "Content-Type" not in request_headers:
        request_headers["Content-Type"] = "application/json"
    target = base.rstrip("/") + path
    req = urllib.request.Request(target, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RequestFailure(error.code, f"HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RequestFailure(None, f"network error: {error.reason}") from error


def es_path(asset: Asset) -> str:
    name = urllib.parse.quote(asset.name, safe="")
    paths = {
        "component_templates": f"/_component_template/{name}",
        "index_templates": f"/_index_template/{name}",
        "pipelines": f"/_ingest/pipeline/{name}",
        "transforms": f"/_transform/{name}",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path, help="bundle tarball to install")
    source.add_argument("--from-source", action="store_true", help="install canonical repo assets")
    parser.add_argument("--dry-run", action="store_true", help="list API calls without network access")
    args = parser.parse_args()
    try:
        bundle = load_bundle(args.bundle) if args.bundle else load_source()
    except InputError as error:
        print(f"install failed: {error}", file=sys.stderr)
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

    required = ("RIGSIGNAL_ES_URL", "RIGSIGNAL_KB_URL", "RIGSIGNAL_ES_AUTH")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("install failed: missing required environment variable(s): " + ", ".join(missing), file=sys.stderr)
        return 1
    try:
        authorization = auth_header(os.environ["RIGSIGNAL_ES_AUTH"])
    except InputError as error:
        print(f"install failed: {error}", file=sys.stderr)
        return 1
    es_url, kb_url = os.environ["RIGSIGNAL_ES_URL"], os.environ["RIGSIGNAL_KB_URL"]
    failures: list[tuple[str, str, str]] = []
    for asset in bundle.assets:
        try:
            if asset.kind == "dashboard":
                body, boundary = multipart_dashboard(asset)
                request(kb_url, "/api/saved_objects/_import?overwrite=true", "POST", authorization, body,
                        {"Content-Type": f"multipart/form-data; boundary={boundary}", "kbn-xsrf": "true"})
                for object_type, object_id in dashboard_objects(asset.data):
                    target = "/api/saved_objects/" + urllib.parse.quote(object_type, safe="") + "/" + urllib.parse.quote(object_id, safe="")
                    request(kb_url, target, "GET", authorization, headers={"kbn-xsrf": "true"})
            elif asset.kind == "transforms":
                path = es_path(asset)
                try:
                    request(es_url, path, "GET", authorization)
                except RequestFailure as error:
                    if error.status != 404:
                        raise
                    request(es_url, path, "PUT", authorization, asset.data)
                else:
                    request(es_url, path + "/_update", "POST", authorization, asset.data)
                request(es_url, path, "GET", authorization)
            else:
                path = es_path(asset)
                request(es_url, path, "PUT", authorization, asset.data)
                request(es_url, path, "GET", authorization)
        except (RequestFailure, InputError) as error:
            failures.append((asset.kind, asset.name, str(error)))
    marker = Asset("component_templates", "rigsignal-bundle-meta", "", marker_body(bundle))
    try:
        request(es_url, es_path(marker), "PUT", authorization, marker.data)
        request(es_url, es_path(marker), "GET", authorization)
    except RequestFailure as error:
        failures.append(("bundle_marker", marker.name, str(error)))
    if failures:
        fail_table(failures)
        return 1
    print(f"installed {total}/{total} assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
