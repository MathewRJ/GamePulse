#!/usr/bin/env python3
"""DEPRECATED: use tools/install_assets.py for the canonical asset bundle.

Deploy RigSignal ingest pipelines to Elasticsearch.

Pipeline naming convention follows the Elastic integration package standard:
  metrics-rigsignal.<dataset>-default  (metrics data streams)
  logs-rigsignal.<dataset>-default     (logs data streams, e.g. events)

Usage:
    python3 tools/deploy_pipelines.py [--dry-run]
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

import yaml

LOGS_DATASETS = {"events"}

ROOT = Path(__file__).parent.parent
DS_ROOT = ROOT / "data_stream"


def pipeline_id(dataset: str) -> str:
    stream_type = "logs" if dataset in LOGS_DATASETS else "metrics"
    return f"{stream_type}-rigsignal.{dataset}-default"


def load_pipeline(dataset: str) -> dict:
    path = DS_ROOT / dataset / "elasticsearch" / "ingest_pipeline" / "default.yml"
    with open(path) as f:
        return yaml.safe_load(f)


def put_pipeline(pipeline_id: str, body: dict, dry_run: bool) -> None:
    es_url = os.environ["ES_URL"]
    api_key = os.environ["ES_API_KEY"]
    url = f"{es_url}/_ingest/pipeline/{pipeline_id}"
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
            print(f"  OK  {pipeline_id}: {result}")
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()
        print(f"  ERR {pipeline_id}: HTTP {e.code} — {body_err}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if not dry_run:
        if not os.environ.get("ES_URL") or not os.environ.get("ES_API_KEY"):
            print("Error: ES_URL and ES_API_KEY must be set", file=sys.stderr)
            sys.exit(1)

    datasets = sorted(p.name for p in DS_ROOT.iterdir() if p.is_dir())
    print(f"Deploying pipelines for {len(datasets)} data streams{'  [DRY RUN]' if dry_run else ''}...")

    for dataset in datasets:
        pipeline_path = DS_ROOT / dataset / "elasticsearch" / "ingest_pipeline" / "default.yml"
        if not pipeline_path.exists():
            print(f"  SKIP {dataset} (no pipeline)")
            continue

        pid = pipeline_id(dataset)
        body = load_pipeline(dataset)
        print(f"  {dataset} -> {pid}")
        put_pipeline(pid, body, dry_run)

    print("Done.")


if __name__ == "__main__":
    main()
