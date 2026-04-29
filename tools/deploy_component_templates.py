#!/usr/bin/env python3
"""Deploy GamePulse component templates to Elasticsearch.

Component templates declare explicit field mappings for gamepulse.* fields so
Kibana shows them in the field list even before any docs with those fields
arrive. Templates use the gamepulse.* namespace to match actual document paths.

Usage:
    python3 tools/deploy_component_templates.py [--dry-run]
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).parent.parent
TEMPLATE_DIR = ROOT / "elastic" / "component-templates"

TEMPLATES = [
    "gamepulse-audio-mappings",
    "gamepulse-cpu-mappings",
    "gamepulse-ebpf-mappings",
    "gamepulse-events-mappings",
    "gamepulse-frame-mappings",
    "gamepulse-gpu-mappings",
    "gamepulse-host-environment",
    "gamepulse-memory-mappings",
    "gamepulse-network-mappings",
    "gamepulse-power-mappings",
    "gamepulse-session-context",
    "gamepulse-storage-mappings",
]


def put_template(name: str, body: dict, dry_run: bool) -> None:
    es_url = os.environ["ES_URL"]
    api_key = os.environ["ES_API_KEY"]
    url = f"{es_url}/_component_template/{name}"
    data = json.dumps(body).encode()

    if dry_run:
        print(f"  [dry-run] PUT {url}")
        return

    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"ApiKey {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            print(f"  OK  {name}: {result}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"  ERR {name}: HTTP {e.code} — {body_text[:200]}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if not dry_run:
        for var in ("ES_URL", "ES_API_KEY"):
            if not os.environ.get(var):
                print(f"Error: ${var} not set", file=sys.stderr)
                sys.exit(1)

    print(f"Deploying {len(TEMPLATES)} component templates...")
    for name in TEMPLATES:
        path = TEMPLATE_DIR / f"{name}.json"
        if not path.exists():
            print(f"  SKIP {name} (file not found)")
            continue
        body = json.loads(path.read_text())
        put_template(name, body, dry_run)


if __name__ == "__main__":
    main()
