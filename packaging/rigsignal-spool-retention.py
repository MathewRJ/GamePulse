#!/usr/bin/env python3
"""Prune only Elastic Agent-proven delivered RigSignal spool finals."""

import glob
import json
import os
import pathlib
import subprocess
import sys
import time
import tomllib


DEFAULT_SPOOL = pathlib.Path.home() / ".local/state/rigsignal/spool"
AGENT = pathlib.Path.home() / "elastic/elastic-agent-9.4.3-linux-x86_64/elastic-agent"
REGISTRY_GLOB = os.environ.get(
    "RIGSIGNAL_FILESTREAM_REGISTRY_GLOB",
    str(
        pathlib.Path.home()
        / "elastic/elastic-agent-9.4.3-linux-x86_64/data/elastic-agent-*/run/filestream-default/registry/filebeat/log.json"
    ),
)
MAX_AGE_SECONDS = 48 * 3600


def configured_spool():
    override = os.environ.get("RIGSIGNAL_SPOOL_DIR")
    if override:
        return pathlib.Path(override)
    config_path = pathlib.Path(
        os.environ.get(
            "RIGSIGNAL_CONFIG",
            pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
            / "rigsignal/rigsignal.toml",
        )
    )
    if not config_path.exists():
        return DEFAULT_SPOOL
    with config_path.open("rb") as fh:
        output = tomllib.load(fh).get("output", {})
    spool = output.get("spool_dir", DEFAULT_SPOOL)
    if not isinstance(spool, str):
        raise ValueError("output.spool_dir must be a string")
    return pathlib.Path(spool)


def agent_healthy():
    try:
        proc = subprocess.run([str(AGENT), "status"], text=True, capture_output=True, timeout=30)
    except Exception as exc:
        print(f"skip: elastic-agent status failed: {exc}")
        return False
    if proc.returncode != 0 or "status: (HEALTHY)" not in proc.stdout:
        print("skip: elastic-agent is not healthy")
        return False
    return True


def latest_registry():
    paths = [pathlib.Path(p) for p in glob.glob(REGISTRY_GLOB)]
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def registry_sources(registry, spool):
    state = {}
    with registry.open() as fh:
        for line_number, line in enumerate(fh, start=1):
            try:
                obj = json.loads(line)
            except Exception as exc:
                raise ValueError(f"malformed registry JSON on line {line_number}") from exc
            if isinstance(obj, dict) and "k" in obj and "v" in obj:
                state[obj["k"]] = obj["v"]
    sources = {}
    for value in state.values():
        if not isinstance(value, dict):
            raise ValueError("malformed registry state")
        meta = value.get("meta", {})
        if not isinstance(meta, dict):
            raise ValueError("malformed registry metadata")
        src = meta.get("source")
        if src and src.startswith(str(spool)):
            cursor = value.get("cursor", {})
            if not isinstance(cursor, dict):
                raise ValueError("malformed registry cursor")
            try:
                sources[src] = int(cursor.get("offset", -1))
            except (TypeError, ValueError) as exc:
                raise ValueError("malformed registry cursor offset") from exc
    return sources


def main():
    if not agent_healthy():
        return 0
    try:
        spool = configured_spool()
        registry = latest_registry()
        if registry is None:
            print("skip: no filestream registry")
            return 0
        sources = registry_sources(registry, spool)
    except Exception as exc:
        print(f"skip: retention inputs unavailable: {exc}")
        return 0

    now = time.time()
    deleted = 0
    skipped = 0
    bytes_deleted = 0
    for path in spool.glob("*.ndjson"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if now - stat.st_mtime <= MAX_AGE_SECONDS:
            skipped += 1
            continue
        offset = sources.get(str(path))
        if offset is None or offset < stat.st_size:
            skipped += 1
            continue
        bytes_deleted += stat.st_size
        path.unlink()
        deleted += 1
    print(f"deleted={deleted} bytes={bytes_deleted} skipped={skipped} registry={registry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
