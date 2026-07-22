#!/usr/bin/env python3
"""Build a deterministic RigSignal Elasticsearch asset bundle."""

import argparse
import gzip
import hashlib
import io
import json
import re
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
}
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*\.json\Z")


class BundleError(Exception):
    """An invalid source tree cannot be bundled."""


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
            try:
                json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise BundleError(f"invalid JSON asset {entry.relative_to(ROOT)}: {error}") from error
            paths[entry.relative_to(ROOT).as_posix()] = entry.read_bytes()
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
    decoded = {path: json.loads(data) for path, data in files.items()}
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


def read_dashboards() -> dict[str, bytes]:
    dashboards = sorted(DASHBOARD_DIR.glob("*.ndjson"))
    if not dashboards:
        raise BundleError(f"dashboard glob matched zero files: {DASHBOARD_DIR / '*.ndjson'}")
    return {path.relative_to(ROOT).as_posix(): path.read_bytes() for path in dashboards}


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
    args = parser.parse_args()
    try:
        version = args.version or package_version()
        assets = read_assets()
        validate_dependencies(assets)
        dashboards = read_dashboards()
        all_files = {**assets, **dashboards}
        counts = {output_name: sum(path.startswith(f"elastic/{directory}/") for path in assets)
                  for directory, output_name in ASSET_TYPES.items()}
        counts["dashboards"] = len(dashboards)
        manifest = {
            "bundle_version": version,
            "counts": counts,
            "dashboards": sorted(dashboards),
            "sha256": {
                path: hashlib.sha256(data).hexdigest()
                for path, data in sorted(all_files.items())
            },
            "source_commit": args.source_commit,
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
    except BundleError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1
    print(f"built {output} ({sum(counts.values())} assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
