#!/usr/bin/env python3
"""
add-dataset-filter.py — add a data_stream.dataset: rigsignal.* dashboard-level
filter to all 6 RigSignal dashboards deployed in Kibana.

For each dashboard:
  1. Export saved object (with references) from Kibana
  2. Inject the dataset wildcard filter into kibanaSavedObjectMeta.searchSourceJSON
  3. Re-import with overwrite=true
  4. Save updated NDJSON to dashboards/<name>-deployed.ndjson

Usage:
  python3 scripts/add-dataset-filter.py [--dry-run]
"""

import json
import os
import sys
import urllib.request
import urllib.error
import argparse

KIBANA_URL = os.environ["KIBANA_URL"].rstrip("/")
API_KEY = os.environ["ES_API_KEY"]

DASHBOARDS = [
    ("home",        "home-dashboard-2026-04-13"),
    ("games",       "5e898d7c-8de1-45b8-ae04-4cdc745f046d"),
    ("environment", "3a55c257-0537-42a8-94a7-24dc773a703b"),
    ("hardware",    "ed9d9b94-2003-429c-b294-9d3f2ef737e7"),
    ("compare",     "828db140-b330-4d26-8045-40a7895bfc41"),
    ("engine",      "7ec220c4-0c7a-4538-9b86-9a664b4a7d2f"),
]

DATASET_FILTER = {
    "meta": {
        "alias": "RigSignal integration scope",
        "disabled": False,
        "key": "data_stream.dataset",
        "negate": False,
        "type": "custom",
        "value": "rigsignal.*",
    },
    "query": {
        "wildcard": {
            "data_stream.dataset": {"value": "rigsignal.*"}
        }
    },
    "$state": {"store": "globalState"},
}


def kibana_request(method, path, body=None, content_type="application/json"):
    url = f"{KIBANA_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    if isinstance(body, str):
        data = body.encode()
        content_type = "application/x-ndjson"
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"ApiKey {API_KEY}")
    req.add_header("kbn-xsrf", "true")
    if data:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def export_dashboard(dashboard_id):
    body = {
        "objects": [{"type": "dashboard", "id": dashboard_id}],
        "includeReferencesDeep": True,
        "excludeExportDetails": True,
    }
    raw = kibana_request("POST", "/api/saved_objects/_export", body)
    objects = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return objects, raw


def inject_dataset_filter(objects, dashboard_id):
    modified = []
    injected = False
    for obj in objects:
        if obj.get("type") == "dashboard" and obj.get("id") == dashboard_id:
            attrs = obj["attributes"]
            meta_raw = attrs.get("kibanaSavedObjectMeta", {}).get("searchSourceJSON", "{}")
            meta = json.loads(meta_raw)
            filters = meta.get("filter", [])
            # Remove any existing RigSignal dataset filter to avoid duplicates
            filters = [f for f in filters if f.get("meta", {}).get("key") != "data_stream.dataset"]
            filters.append(DATASET_FILTER)
            meta["filter"] = filters
            attrs.setdefault("kibanaSavedObjectMeta", {})["searchSourceJSON"] = json.dumps(meta)
            injected = True
        modified.append(obj)
    if not injected:
        raise ValueError(f"Dashboard {dashboard_id} not found in export")
    return modified


def import_objects(objects):
    ndjson = ("\n".join(json.dumps(o) for o in objects) + "\n").encode()
    boundary = b"----RigSignalDatasetFilterBoundary"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="export.ndjson"\r\n'
        b"Content-Type: application/x-ndjson\r\n\r\n"
        + ndjson
        + b"\r\n--" + boundary + b"--\r\n"
    )
    url = f"{KIBANA_URL}/api/saved_objects/_import?overwrite=true"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"ApiKey {API_KEY}")
    req.add_header("kbn-xsrf", "true")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary.decode()}")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            if not result.get("success"):
                errors = result.get("errors", [])
                raise RuntimeError(f"Import reported failure: {errors}")
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Export and show diff without importing")
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dashboards_dir = os.path.join(repo_root, "dashboards")

    for name, dashboard_id in DASHBOARDS:
        print(f"\n--- {name} ({dashboard_id}) ---")

        print("  Exporting...", end=" ", flush=True)
        objects, _ = export_dashboard(dashboard_id)
        print(f"got {len(objects)} objects")

        print("  Injecting data_stream.dataset filter...", end=" ", flush=True)
        modified = inject_dataset_filter(objects, dashboard_id)
        print("done")

        if args.dry_run:
            print("  [dry-run] skipping import")
        else:
            print("  Re-importing with overwrite...", end=" ", flush=True)
            import_objects(modified)
            print("done")

        # Save NDJSON
        out_path = os.path.join(dashboards_dir, f"{name}-dashboard-deployed.ndjson")
        ndjson_out = "\n".join(json.dumps(o) for o in modified)
        with open(out_path, "w") as f:
            f.write(ndjson_out + "\n")
        print(f"  Saved → dashboards/{name}-dashboard-deployed.ndjson")

    print("\nAll done." if not args.dry_run else "\nDry-run complete — no changes imported.")


if __name__ == "__main__":
    main()
