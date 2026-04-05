#!/usr/bin/env python3
"""Set default_pipeline on all GamePulse index templates to route docs through
the corresponding ingest pipeline.

Pipeline naming: <type>-gamepulse.<dataset>-default
Template naming: metrics-gamepulse.<dataset>  (or logs-gamepulse.<dataset>)

Usage:
    python3 tools/wire_pipelines.py [--dry-run]
"""
import json
import os
import sys
import urllib.request
import urllib.error

LOGS_DATASETS = {"events"}

DATASETS = [
    "audio", "cpu", "ebpf", "events", "frame",
    "gpu", "memory", "network", "power", "session", "storage",
]


def api(method: str, path: str, body: dict | None = None, dry_run: bool = False) -> dict:
    es_url = os.environ["ES_URL"]
    api_key = os.environ["ES_API_KEY"]
    url = f"{es_url}/{path}"
    data = json.dumps(body).encode() if body else None

    if dry_run and method in ("PUT", "POST"):
        print(f"  [dry-run] {method} {url}")
        return {}

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"ApiKey {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def pipeline_id(dataset: str) -> str:
    stream_type = "logs" if dataset in LOGS_DATASETS else "metrics"
    return f"{stream_type}-gamepulse.{dataset}-default"


def template_name(dataset: str) -> str:
    # Existing templates were all created under metrics-gamepulse.* prefix
    return f"metrics-gamepulse.{dataset}"


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if not dry_run:
        if not os.environ.get("ES_URL") or not os.environ.get("ES_API_KEY"):
            print("Error: ES_URL and ES_API_KEY must be set", file=sys.stderr)
            sys.exit(1)

    print(f"Wiring default_pipeline on {len(DATASETS)} index templates{'  [DRY RUN]' if dry_run else ''}...")

    for dataset in DATASETS:
        tname = template_name(dataset)
        pid = pipeline_id(dataset)

        # Get existing template
        result = api("GET", f"_index_template/{tname}")
        templates = result.get("index_templates", [])
        if not templates:
            print(f"  SKIP {tname} (not found on cluster)")
            continue

        existing = templates[0]["index_template"]

        # Add default_pipeline to settings
        template_body = existing.get("template", {})
        settings = template_body.get("settings", {})
        index_settings = settings.get("index", {})
        index_settings["default_pipeline"] = pid
        settings["index"] = index_settings
        template_body["settings"] = settings
        existing["template"] = template_body

        # Remove read-only fields
        for key in ("created_date_millis", "modified_date_millis"):
            existing.pop(key, None)

        print(f"  {tname} -> default_pipeline: {pid}")
        r = api("PUT", f"_index_template/{tname}", existing, dry_run)
        if r:
            print(f"    {'acknowledged' if r.get('acknowledged') else r}")

    print("Done.")


if __name__ == "__main__":
    main()
