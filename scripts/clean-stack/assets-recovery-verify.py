#!/usr/bin/env python3
"""Independent read-only verifier for the clean-stack assets recovery gate.

This process deliberately does not call the installer transaction executor.
It opens a release bundle only to derive the expected target set, then uses
direct HTTPS GETs and the published adapter projections to compare live state.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import asset_adapters  # noqa: E402
import install_assets as install  # noqa: E402

REQUESTS: list[str] = []


def request(base: str, path: str, authorization: str, context: ssl.SSLContext) -> tuple[int, object]:
    # Keep the evidence transport-safe: paths identify every independent GET,
    # while credentials and full endpoint spellings never leave this process.
    REQUESTS.append("GET " + path)
    request = urllib.request.Request(base.rstrip("/") + path, headers={
        "Authorization": authorization, "kbn-xsrf": "true"}, method="GET")
    try:
        with urllib.request.urlopen(request, context=context) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error.code, None


def auth_header(password: str) -> str:
    token = base64.b64encode(("elastic:" + password).encode("utf-8")).decode("ascii")
    return "Basic " + token


def expected_specs(bundle: install.Bundle) -> list[tuple[str, install.Asset | None, dict | None]]:
    return install._transaction_specs(bundle)  # target expansion only; no installer HTTP path


def read_spec(es_url: str, kb_url: str, authorization: str, context: ssl.SSLContext,
              key: str, asset: install.Asset | None, saved: dict | None) -> tuple[bool, str]:
    if saved is not None:
        assert asset is not None
        space = install.dashboard_target_space(asset)
        path = (install.space_prefix(space) + "/api/saved_objects/" +
                urllib.parse.quote(saved["type"], safe="") + "/" +
                urllib.parse.quote(saved["id"], safe=""))
        status, live = request(kb_url, path, authorization, context)
        if status == 404:
            return False, "absent"
        if status != 200 or not isinstance(live, dict):
            return False, f"HTTP {status}"
        wanted = {"space": space, "type": saved["type"], "attributes": saved.get("attributes", {}),
                  "references": saved.get("references", [])}
        actual = {"space": space, "type": saved["type"], "attributes": live.get("attributes"),
                  "references": live.get("references", [])}
        return install.jcs(actual) == install.jcs(wanted), "exact" if install.jcs(actual) == install.jcs(wanted) else "different"

    assert asset is not None
    if asset.kind in install._ES_ASSET_KINDS:
        status, live = request(es_url, install.es_path(asset), authorization, context)
        if status == 404:
            return False, "absent"
        if status != 200 or not isinstance(live, dict):
            return False, f"HTTP {status}"
        try:
            actual = asset_adapters.get_projection(asset.kind, live)
            desired = asset_adapters.get_projection(asset.kind, install.parse_json(install.stamped_asset(asset).data, asset.path))
        except (asset_adapters.AdapterError, install.InputError):
            return False, "ambiguous projection"
        # Detect-and-halt classes carry the ratified per-target ownership nonce
        # in _meta/metadata.  Validate its shape independently, then compare
        # the remainder — the raw bundle body legitimately lacks it.
        if asset.kind in {"pipelines", "security_roles"}:
            meta_key = "metadata" if asset.kind == "security_roles" else "_meta"
            for projection in (actual,):
                meta = projection.get(meta_key)
                if isinstance(meta, dict):
                    nonce = meta.pop("controller_nonce", None)
                    if nonce is not None and not (isinstance(nonce, str) and len(nonce) == 64
                                                  and all(c in "0123456789abcdef" for c in nonce)):
                        return False, "malformed controller nonce"
        return install.jcs(actual) == install.jcs(desired), "exact" if install.jcs(actual) == install.jcs(desired) else "different"

    base = kb_url
    status, live = request(base, install.kibana_path(asset), authorization, context)
    if status == 404:
        return False, "absent"
    if status != 200 or not isinstance(live, dict):
        return False, f"HTTP {status}"
    try:
        actual = asset_adapters.get_projection(asset.kind, live)
        desired = asset_adapters.get_projection(asset.kind, install.parse_json(asset.data, asset.path))
    except (asset_adapters.AdapterError, install.InputError):
        return False, "ambiguous projection"
    return install.jcs(actual) == install.jcs(desired), "exact" if install.jcs(actual) == install.jcs(desired) else "different"


def no_unexpected_saved_objects(bundle: install.Bundle, kb_url: str, authorization: str,
                                context: ssl.SSLContext) -> dict[str, list[str]]:
    expected: dict[tuple[str, str], set[str]] = {}
    for _key, asset, saved in expected_specs(bundle):
        if saved is not None:
            assert asset is not None
            expected.setdefault((install.dashboard_target_space(asset), saved["type"]), set()).add(saved["id"])
    unexpected: dict[str, list[str]] = {}
    for (space, object_type), wanted in sorted(expected.items()):
        path = (install.space_prefix(space) + "/api/saved_objects/_find?type=" +
                urllib.parse.quote(object_type, safe="") + "&per_page=10000")
        status, response = request(kb_url, path, authorization, context)
        if status != 200 or not isinstance(response, dict) or not isinstance(response.get("saved_objects"), list):
            unexpected[f"{space}/{object_type}"] = [f"HTTP {status}"]
            continue
        got = {row.get("id") for row in response["saved_objects"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
        extra = sorted(got - wanted)
        if extra:
            unexpected[f"{space}/{object_type}"] = extra
    return unexpected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    # Target selection is a local bundle-only operation used to configure the
    # guarded engine hooks.  Keep its interface independent of live-stack
    # credentials; the normal verification mode validates these below.
    parser.add_argument("--es-url")
    parser.add_argument("--kb-url")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--record-state", choices=("installing", "installed"))
    parser.add_argument("--record-pm", choices=("true", "false"))
    parser.add_argument("--allow-absent", action="store_true")
    parser.add_argument("--minimum-present", type=int, default=0)
    parser.add_argument("--print-target", choices=("saved-object", "dashboard-member", "pipeline", "role"))
    parser.add_argument("--no-unexpected-ids", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    password = os.environ.get("RIGSIGNAL_ASSETS_RECOVERY_PASSWORD")

    bundle = install.load_bundle(args.bundle)
    specs = expected_specs(bundle)
    if args.print_target:
        for key, asset, saved in specs:
            if args.print_target == "saved-object" and saved is not None:
                print(key); return 0
            # Select a member created by a dashboard import, rather than the
            # dashboard root itself.  This makes the recovery leg cover a
            # partial progress map within one multi-object NDJSON import.
            if (args.print_target == "dashboard-member" and saved is not None and asset is not None
                    and asset.kind == "dashboard" and saved.get("type") != "dashboard"):
                print(key); return 0
            if args.print_target == "pipeline" and asset is not None and asset.kind == "pipelines":
                print(key); return 0
            if args.print_target == "role" and asset is not None and asset.kind == "security_roles":
                print(key); return 0
        raise SystemExit("requested target class is absent from bundle")

    missing = [flag for flag, value in (
        ("--es-url", args.es_url),
        ("--kb-url", args.kb_url),
        ("--ca-file", args.ca_file),
        ("RIGSIGNAL_ASSETS_RECOVERY_PASSWORD", password),
    ) if value is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    context = ssl.create_default_context(cafile=str(args.ca_file))
    authorization = auth_header(password)
    results: dict[str, str] = {}
    present = 0
    for key, asset, saved in specs:
        exact, status = read_spec(args.es_url, args.kb_url, authorization, context, key, asset, saved)
        results[key] = status
        if status == "exact":
            present += 1
    failures = {key: status for key, status in results.items()
                if status != "exact" and not (args.allow_absent and status == "absent")}
    if present < args.minimum_present:
        failures["minimum-present"] = f"expected >= {args.minimum_present}, got {present}"

    record_result: dict[str, object] | None = None
    if args.record is not None:
        try:
            record_result = json.loads(args.record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            failures["record"] = f"unreadable: {error}"
        else:
            if args.record_state and record_result.get("state") != args.record_state:
                failures["record-state"] = str(record_result.get("state"))
            if args.record_pm and bool(record_result.get("possible_mutation")) != (args.record_pm == "true"):
                failures["record-possible-mutation"] = str(record_result.get("possible_mutation"))

    unexpected = no_unexpected_saved_objects(bundle, args.kb_url, authorization, context) if args.no_unexpected_ids else {}
    if unexpected:
        failures["unexpected-saved-object-ids"] = json.dumps(unexpected, sort_keys=True)
    payload = {"targets": len(specs), "present_exact": present, "requests": REQUESTS, "results": results,
               "record": record_result, "unexpected_saved_object_ids": unexpected,
               "pass": not failures, "failures": failures}
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
