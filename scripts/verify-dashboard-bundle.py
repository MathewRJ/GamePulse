#!/usr/bin/env python3
"""Fail-closed consistency checks for the committed dashboard NDJSON bundle."""

import argparse
import copy
import json
import re
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / "dashboards" / "v0.3.1"

# Keep this table next to the verifier: it is the stable saved-object identity
# contract for the bundle, not a list of strings to rewrite indiscriminately.
ID_RENAMES = {
    "rigsignal-engine": "rigsignal-pkg-engine",
    "rigsignal-flamegraph-dashboard": "rigsignal-pkg-flamegraph-dashboard",
    "rigsignal-game-perf": "rigsignal-pkg-game-perf",
    "rigsignal-home": "rigsignal-pkg-home",
    "rigsignal-software": "rigsignal-pkg-software",
    "rigsignal-streaming-lab": "rigsignal-pkg-streaming-lab",
    "rigsignal-system-health": "rigsignal-pkg-system-health",
    "metrics-rigsignal.ebpf*": "rigsignal-pkg-metrics-ebpf",
    "metrics-rigsignal.session*": "rigsignal-pkg-metrics-session",
    "rigsignal-flamegraph-data-view": "rigsignal-pkg-flamegraph-data-view",
    "sl-d1-host-data-view": "rigsignal-pkg-sl-d1-host-data-view",
    "sl-d1-stream-data-view": "rigsignal-pkg-sl-d1-stream-data-view",
    "rigsignal-flamegraph-top-function-delta": "rigsignal-pkg-flamegraph-top-function-delta",
    "fleet-managed-gaming": "rigsignal-pkg-managed",
    "fleet-pkg-rigsignal-gaming": "rigsignal-pkg-bundle",
    "rigsignal-flamegraph-vega-diff": "rigsignal-pkg-flamegraph-vega-diff",
    "rigsignal-flamegraph-vega-live-diff": "rigsignal-pkg-flamegraph-vega-live-diff",
    "rigsignal-flamegraph-vega-single": "rigsignal-pkg-flamegraph-vega-single",
}

GLOB_OLD_IDS = frozenset(("metrics-rigsignal.ebpf*", "metrics-rigsignal.session*"))
UNAMBIGUOUS_OLD_IDS = frozenset(ID_RENAMES) - GLOB_OLD_IDS
EXPECTED_COUNTS = {
    "rigsignal-engine.ndjson": 4,
    "rigsignal-flamegraph-dashboard.ndjson": 6,
    "rigsignal-game-perf.ndjson": 4,
    "rigsignal-home.ndjson": 4,
    "rigsignal-software.ndjson": 4,
    "rigsignal-streaming-lab.ndjson": 3,
    "rigsignal-system-health.ndjson": 4,
}
DASHBOARD_ROUTE = re.compile(
    r"(?P<route>(?:/s/[^/]+)?/app/dashboards#/view/(?P<id>[^\s)\]\\\"'>]+))"
)
WIFI_HEALTH_ID = "825c3f75-54ac-4a0d-959d-7bc56a96e6f5"


class VerificationError(ValueError):
    pass


def dashboard_target_space(filename):
    if filename == "rigsignal-streaming-lab.ndjson":
        return "default"
    if filename in EXPECTED_COUNTS:
        return "rigsignal"
    raise VerificationError(f"unrecognized dashboard bundle file: {filename}")


def _walk_json_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_strings(item)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return
        yield parsed
        yield from _walk_json_strings(parsed)


def _internal_reference_ids(value):
    """Yield only parsed-string internalReferences[*].id identity positions."""
    if isinstance(value, dict):
        references = value.get("internalReferences")
        if isinstance(references, list):
            for reference in references:
                if isinstance(reference, dict) and isinstance(reference.get("id"), str):
                    yield reference["id"]
        for item in value.values():
            yield from _internal_reference_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _internal_reference_ids(item)


def identity_ids_in_stringified_json(record):
    """Return identity-bearing ids nested in JSON strings in attributes/references."""
    found = []
    for body_name in ("attributes", "references"):
        for parsed in _walk_json_strings(record.get(body_name, {})):
            found.extend(_internal_reference_ids(parsed))
    return found


def _markdown_content_strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "content" and isinstance(child, str):
                yield child
            yield from _markdown_content_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _markdown_content_strings(child)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _markdown_content_strings(parsed)


def markdown_dashboard_routes(record):
    """Return markdown dashboard routes and URL-decoded ids, including JSON panels."""
    found = []
    for content in _markdown_content_strings(record.get("attributes", {})):
        found.extend((match.group("route"), urllib.parse.unquote(match.group("id")))
                     for match in DASHBOARD_ROUTE.finditer(content))
    return found


def markdown_saved_object_ids(record):
    return [object_id for _, object_id in markdown_dashboard_routes(record)]


def _rewrite_internal_reference_ids(value, renames):
    if isinstance(value, dict):
        references = value.get("internalReferences")
        if isinstance(references, list):
            for reference in references:
                if isinstance(reference, dict) and reference.get("id") in renames:
                    reference["id"] = renames[reference["id"]]
        for child in value.values():
            _rewrite_internal_reference_ids(child, renames)
    elif isinstance(value, list):
        for child in value:
            _rewrite_internal_reference_ids(child, renames)


def rewrite_stringified_internal_references(record, renames=ID_RENAMES):
    """Rewrite the finite parsed-string identity allowlist without touching lookalikes."""
    result = copy.deepcopy(record)

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str):
                    try:
                        parsed = json.loads(child)
                    except json.JSONDecodeError:
                        continue
                    _rewrite_internal_reference_ids(parsed, renames)
                    value[key] = json.dumps(parsed, separators=(",", ":"))
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for body_name in ("attributes", "references"):
        visit(result.get(body_name, {}))
    return result


def _read_bundle(bundle_dir):
    bundle_dir = Path(bundle_dir)
    actual_names = {path.name for path in bundle_dir.glob("*.ndjson")}
    expected_names = set(EXPECTED_COUNTS)
    if actual_names != expected_names:
        raise VerificationError(
            f"bundle file set changed: expected {sorted(expected_names)}, got {sorted(actual_names)}"
        )
    records = []
    raw_files = {}
    for name, expected_count in EXPECTED_COUNTS.items():
        path = bundle_dir / name
        raw = path.read_text()
        raw_files[name] = raw
        lines = raw.splitlines()
        if len(lines) != expected_count:
            raise VerificationError(f"{name}: expected {expected_count} NDJSON objects, got {len(lines)}")
        for number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise VerificationError(f"{name}:{number}: invalid NDJSON: {error.msg}") from error
            if not isinstance(record, dict) or not isinstance(record.get("type"), str) or not isinstance(record.get("id"), str):
                raise VerificationError(f"{name}:{number}: object needs string type and id")
            records.append((name, number, record))
    return records, raw_files


def verify_bundle(bundle_dir=DEFAULT_BUNDLE):
    records, raw_files = _read_bundle(bundle_dir)
    failures = []

    for name, raw in raw_files.items():
        for old_id in UNAMBIGUOUS_OLD_IDS:
            # This remains a whole-file check, but recognizes an id token rather
            # than a substring: the mandated sl-d1 replacement ids deliberately
            # retain their old descriptor after a `rigsignal-pkg-` prefix.
            if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(old_id) + r"(?![A-Za-z0-9_-])", raw):
                failures.append(f"{name}: unambiguous old id remains anywhere: {old_id}")
        if "/s/gaming" in raw:
            failures.append(f"{name}: contains retired /s/gaming link")

    definitions = {(record["type"], record["id"]) for _, _, record in records}
    dashboard_ids = {object_id for object_type, object_id in definitions if object_type == "dashboard"}
    duplicate_groups = defaultdict(list)
    dashboard_link_count = 0
    app_links = defaultdict(list)
    for name, number, record in records:
        location = f"{name}:{number}"
        for reference in record.get("references", []):
            if not isinstance(reference, dict) or not isinstance(reference.get("type"), str) or not isinstance(reference.get("id"), str):
                failures.append(f"{location}: malformed reference")
                continue
            if (reference["type"], reference["id"]) not in definitions:
                failures.append(f"{location}: dangling reference {reference['type']}:{reference['id']}")
        identity_ids = [record["id"]]
        identity_ids.extend(reference["id"] for reference in record.get("references", []) if isinstance(reference, dict) and isinstance(reference.get("id"), str))
        routes = markdown_dashboard_routes(record)
        identity_ids.extend(object_id for _, object_id in routes)
        dashboard_link_count += len(routes)
        for route, object_id in routes:
            if not route.startswith("/s/rigsignal/app/dashboards#/view/"):
                failures.append(f"{location}: dashboard markdown route has wrong target space: {route}")
            if object_id not in dashboard_ids:
                failures.append(f"{location}: dashboard markdown route targets absent dashboard: {object_id}")
        for content in _markdown_content_strings(record.get("attributes", {})):
            for app_route in re.findall(r"(?<![A-Za-z0-9_-])(/(?:s/[^/]+/)?app/[^\s)\]\\\"'>]+)", content):
                if "/app/dashboards#/view/" not in app_route:
                    app_links[name].append(app_route)
        identity_ids.extend(identity_ids_in_stringified_json(record))
        for old_id in GLOB_OLD_IDS:
            if old_id in identity_ids:
                failures.append(f"{location}: glob old id remains in identity-bearing position: {old_id}")
        projection = json.dumps(
            {"attributes": record.get("attributes"), "references": record.get("references", [])},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        duplicate_groups[(record["type"], record["id"], dashboard_target_space(name))].append((location, projection))

    if dashboard_link_count != 12:
        failures.append(f"expected 12 internal dashboard markdown links, got {dashboard_link_count}")
    expected_app_links = {
        "rigsignal-home.ndjson": sorted([
            "/s/rigsignal/app/metrics/hosts",
            "/s/rigsignal/app/observability/alerts",
            "/s/rigsignal/app/slos",
        ]),
        "rigsignal-streaming-lab.ndjson": ["/app/apm/traces"],
    }
    actual_app_links = {name: sorted(routes) for name, routes in app_links.items() if routes}
    if actual_app_links != expected_app_links:
        failures.append(f"markdown app links differ: expected {expected_app_links}, got {actual_app_links}")
    if any(WIFI_HEALTH_ID in raw for raw in raw_files.values()):
        failures.append("Wi-Fi Health dashboard id remains in bundle")

    for key, members in duplicate_groups.items():
        canonical = members[0][1]
        if any(projection != canonical for _, projection in members[1:]):
            failures.append(f"duplicate definition differs for {key}: {[location for location, _ in members]}")

    if failures:
        raise VerificationError("\n".join(failures))
    return {
        "files": len(EXPECTED_COUNTS),
        "objects": len(records),
        "references": sum(len(record.get("references", [])) for _, _, record in records),
        "duplicate_groups": sum(1 for members in duplicate_groups.values() if len(members) > 1),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", nargs="?", type=Path, default=DEFAULT_BUNDLE)
    args = parser.parse_args(argv)
    try:
        summary = verify_bundle(args.bundle_dir)
    except VerificationError as error:
        print(f"verify-dashboard-bundle: FAIL\n{error}", file=sys.stderr)
        return 1
    print("verify-dashboard-bundle: PASS")
    print(f"files: {summary['files']}; objects: {summary['objects']}; references: {summary['references']}")
    print("checks: closed references; old-id scope; NDJSON/counts; duplicate canonicality; no /s/gaming")
    print(f"duplicate canonical groups: {summary['duplicate_groups']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
