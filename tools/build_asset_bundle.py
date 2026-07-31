#!/usr/bin/env python3
"""Build a deterministic RigSignal Elasticsearch asset bundle."""

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
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
    "kibana-spaces": "kibana_spaces",
    "kibana-roles": "kibana_roles",
}
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*\.json\Z")
W1_ASSET_PATHS = (
    "elastic/component-templates/logs-rigsignal.diagnosis-mappings.json",
    "elastic/index-templates/logs-rigsignal.diagnosis.json",
    "elastic/security-roles/rigsignal_shipper.json",
)
W1_RAW_SHA256 = {
    "elastic/component-templates/logs-rigsignal.diagnosis-mappings.json": "345e0d2898279929eb613b60d2bd250bbf73a13c7b4bbd1b793384e2ae00410c",
    "elastic/index-templates/logs-rigsignal.diagnosis.json": "5f4d4f403fc17a1096b2d2b1c8a43bad94efa52b05c3b117961333b2f3d52199",
    "elastic/security-roles/rigsignal_shipper.json": "6eb0279c7e05b94bfd96083508a8c7e6ad5ca9cb65e531654bd6e0ae3eca7ed2",
}
TARGET_GENERATION_SCHEME = "rigsignal:target-generation:w1-assets:v1"
TARGET_GENERATION_KAT = "a7ed20a4b4bfe0b2e5597a065e8bdaa5161b0d962e1a502d3db3bbcc97e8ee7a"
PROBE_FIXTURE_PATH = "fixtures/diagnosis_event/v1/positive/15-diagnosis-non-finding-conditional.expected.json"
ENGINE_FILES = ("install_assets.py", "asset_adapters.py")


class BundleError(Exception):
    """An invalid source tree cannot be bundled."""


def reject_duplicate_keys(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = member
    return value


def parse_json(data: bytes, path: str):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise BundleError(f"invalid JSON asset {path}: {error}") from error


def package_version() -> str:
    """Return the first package version found in this Cargo workspace."""
    candidates = [ROOT / "Cargo.toml"] + sorted(ROOT.glob("*/Cargo.toml"))
    for path in candidates:
        in_package = False
        for line in path.read_text(encoding="utf-8").splitlines():
            section = line.strip()
            if section.startswith("[") and section.endswith("]"):
                in_package = section == "[package]"
                continue
            if in_package:
                match = re.match(r'\s*version\s*=\s*"([^"]+)"\s*$', line)
                if match:
                    return match.group(1)
    raise BundleError("no [package] version found in Cargo.toml")


def read_assets() -> dict[str, bytes]:
    if not ASSET_DIR.is_dir():
        raise BundleError(f"missing asset tree: {ASSET_DIR}")
    paths: dict[str, bytes] = {}
    allowed_root = {"README.md", *ASSET_TYPES}
    for entry in ASSET_DIR.iterdir():
        if entry.name not in allowed_root:
            raise BundleError(f"unexpected file in elastic tree: {entry.relative_to(ROOT)}")
        if entry.name == "README.md" and not entry.is_file():
            raise BundleError("elastic/README.md must be a file")
    for directory in ASSET_TYPES:
        base = ASSET_DIR / directory
        if not base.is_dir():
            raise BundleError(f"missing asset directory: {base.relative_to(ROOT)}")
        for entry in sorted(base.iterdir()):
            if not entry.is_file() or not ASSET_NAME.fullmatch(entry.name):
                raise BundleError(f"invalid asset filename: {entry.relative_to(ROOT)}")
            data = entry.read_bytes()
            parse_json(data, entry.relative_to(ROOT).as_posix())
            paths[entry.relative_to(ROOT).as_posix()] = data
    return paths


def find_default_pipelines(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "default_pipeline" and isinstance(child, str):
                found.append(child)
            found.extend(find_default_pipelines(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_default_pipelines(child))
    return found


def requires_component(name: str, bundled: set[str]) -> bool:
    """Keep RigSignal package components strict; ignore stock and @custom names."""
    if name.endswith("@custom"):
        return False
    if name in bundled or name.startswith(("metrics-rigsignal.", "logs-rigsignal.")):
        return True
    return not ("@" in name or name.startswith(".fleet_"))


def validate_dependencies(files: dict[str, bytes]) -> None:
    decoded = {path: parse_json(data, path) for path, data in files.items()}
    pipelines = {
        Path(path).stem for path in files if path.startswith("elastic/pipelines/")
    }
    components = {
        Path(path).stem
        for path in files
        if path.startswith("elastic/component-templates/")
    }
    missing_pipelines: list[str] = []
    for path, body in decoded.items():
        if path.startswith(("elastic/index-templates/", "elastic/component-templates/")):
            for pipeline in find_default_pipelines(body):
                if pipeline not in pipelines:
                    missing_pipelines.append(f"{path} -> {pipeline}")
    missing_components: list[str] = []
    for path, body in decoded.items():
        if not path.startswith("elastic/index-templates/"):
            continue
        for component in body.get("composed_of", []):
            if isinstance(component, str) and requires_component(component, components):
                if component not in components:
                    missing_components.append(f"{path} -> {component}")
    if missing_pipelines or missing_components:
        details = missing_pipelines + missing_components
        raise BundleError("unresolved asset dependency:\n  " + "\n  ".join(details))


def target_generation(files: dict[str, bytes]) -> dict[str, object]:
    """Return the ratified Option A generation tuple, using raw asset bytes."""
    if set(W1_ASSET_PATHS) - set(files):
        raise BundleError("missing required W1 asset")
    entries = []
    digest = hashlib.sha256()
    digest.update(TARGET_GENERATION_SCHEME.encode("utf-8") + b"\0")
    digest.update(len(W1_ASSET_PATHS).to_bytes(4, "big"))
    for path in sorted(W1_ASSET_PATHS):
        raw_hash = hashlib.sha256(files[path]).hexdigest()
        if raw_hash != W1_RAW_SHA256[path]:
            raise BundleError(f"W1 raw sha256 mismatch: {path}")
        encoded_path = path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(bytes.fromhex(raw_hash))
        entries.append({"path": path, "sha256": raw_hash})
    value = digest.hexdigest()
    if value != TARGET_GENERATION_KAT:
        raise BundleError("W1 target-generation KAT mismatch")
    return {"scheme": TARGET_GENERATION_SCHEME, "algorithm": "sha256",
            "input_count": 3, "inputs": entries, "value": value}


def read_dashboards() -> dict[str, bytes]:
    dashboards = sorted(DASHBOARD_DIR.glob("*.ndjson"))
    if not dashboards:
        raise BundleError(f"dashboard glob matched zero files: {DASHBOARD_DIR / '*.ndjson'}")
    return {path.relative_to(ROOT).as_posix(): path.read_bytes() for path in dashboards}


def read_auxiliary_files() -> dict[str, bytes]:
    """Read non-asset inputs that the installation proof consumes."""
    path = ROOT / PROBE_FIXTURE_PATH
    if not path.is_file():
        raise BundleError(f"missing auxiliary bundle input: {PROBE_FIXTURE_PATH}")
    # The installer parses this before sending it to Elasticsearch; reject an
    # accidental non-JSON fixture while building rather than at installation.
    parse_json(path.read_bytes(), PROBE_FIXTURE_PATH)
    return {PROBE_FIXTURE_PATH: path.read_bytes()}


def stage_engine(output: Path, version: str, source_commit: str) -> None:
    """Stage the small executable engine with an immutable release stamp."""
    try:
        output.mkdir(parents=True, exist_ok=True)
        for name in ENGINE_FILES:
            shutil.copyfile(ROOT / "tools" / name, output / name)
        (output / "_version.py").write_text(
            '# Generated by tools/build_asset_bundle.py; do not edit.\n'
            f'ENGINE_VERSION = {json.dumps(version)}\n'
            f'SOURCE_COMMIT = {json.dumps(source_commit)}\n', encoding="utf-8")
    except OSError as error:
        raise BundleError(f"cannot stage engine: {error}") from error


def tar_member(tar: tarfile.TarFile, path: str, data: bytes) -> None:
    info = tarfile.TarInfo(path)
    info.size = len(data)
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="bundle version (defaults to Cargo package version)")
    parser.add_argument("--source-commit", required=True, help="source git commit supplied by the orchestrator")
    parser.add_argument("--output", type=Path, help="tarball destination")
    parser.add_argument("--engine-output", type=Path,
                        help="directory in which to stage install_assets.py and its version stamp")
    args = parser.parse_args()
    try:
        version = args.version or package_version()
        assets = read_assets()
        validate_dependencies(assets)
        generation = target_generation(assets)
        dashboards = read_dashboards()
        auxiliary = read_auxiliary_files()
        all_files = {**assets, **dashboards, **auxiliary}
        counts = {output_name: sum(path.startswith(f"elastic/{directory}/") for path in assets)
                  for directory, output_name in ASSET_TYPES.items()}
        counts["dashboards"] = len(dashboards)
        manifest = {
            "bundle_version": version,
            "auxiliary": sorted(auxiliary),
            "counts": counts,
            "dashboards": sorted(dashboards),
            "sha256": {
                path: hashlib.sha256(data).hexdigest()
                for path, data in sorted(all_files.items())
            },
            "source_commit": args.source_commit,
            "target_generation": generation,
        }
        manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        output = args.output or ROOT / "dist" / f"rigsignal-assets-{version}.tar.gz"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as tar:
                    tar_member(tar, "manifest.json", manifest_data)
                    for path, data in sorted(all_files.items()):
                        tar_member(tar, path, data)
        if args.engine_output is not None:
            stage_engine(args.engine_output, version, args.source_commit)
    except BundleError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1
    print(f"built {output} ({sum(counts.values())} assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
