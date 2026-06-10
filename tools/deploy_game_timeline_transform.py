#!/usr/bin/env python3
"""
Deploy the rigsignal-game-timeline Elasticsearch transform.

This transform reads session summary documents from metrics-rigsignal.session-default
and writes one pre-aggregated document per game/session into rigsignal-game-timeline.

After the transform runs, a post-enrichment step computes cumulative_playtime_hours
(a running total of playtime per game, sorted by session_start) and patches each
document via the bulk API.

Note: cumulative_playtime_hours cannot be computed inside a pivot transform because
ES transforms have no window function support. The Python post-enrichment step is
the reliable, guaranteed-correct approach.

Usage:
    export ES_URL=https://...
    export ES_API_KEY=...
    python3 tools/deploy_game_timeline_transform.py [--dry-run] [--reset]

Options:
    --dry-run   Print what would be done, make no changes.
    --reset     Delete and recreate the transform + destination index.
    --enrich-only  Skip transform setup; only run the post-enrichment step.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

TRANSFORM_ID = "rigsignal-game-timeline"
DEST_INDEX = "rigsignal-game-timeline"

# Correct source index (session summaries ship to metrics, not logs).
SOURCE_INDEX = "metrics-rigsignal.session-default"

TRANSFORM_DEF = {
    "id": TRANSFORM_ID,
    "description": (
        "Pre-computes per-session metrics per game for the Games continuous "
        "line dashboard. Source: metrics-rigsignal.session-default. "
        "One output doc per (game_name, session_id)."
    ),
    "source": {
        "index": [SOURCE_INDEX],
        "query": {
            "bool": {
                "filter": [
                    # Only completed sessions with a known game name.
                    {"range": {"rigsignal.summary.duration_s": {"gt": 0}}},
                    {"term": {"rigsignal.summary.ended": True}},
                    {"exists": {"field": "rigsignal.game.name"}},
                    # Restrict to sessions on/after 2026-04-12.
                    # The backing index created on 2026-03-30 has rigsignal.game.name
                    # mapped as text (no keyword), which causes top_metrics and terms
                    # aggregations to fail with "Fielddata is disabled". The 2026-04-12
                    # backing index has the correct keyword mappings from the fixed
                    # integration package. The one Starfield session in the old index
                    # (34 s) is excluded; all substantive gaming sessions are captured.
                    {"range": {"@timestamp": {"gte": "2026-04-12"}}},
                ]
            }
        },
    },
    "dest": {"index": DEST_INDEX},
    "sync": {
        "time": {
            # Sync whenever @timestamp advances; 60 s delay absorbs late docs.
            # The Rust agent also calls _schedule_now immediately after summary,
            # so real-time updates land within seconds of a session ending.
            "field": "@timestamp",
            "delay": "60s",
        }
    },
    "pivot": {
        "group_by": {
            # One output document per unique (game, session) pair.
            # Use base field paths (no .keyword suffix). The source date range
            # (gte: 2026-04-12) limits the query to the 2026-04-12 backing index
            # where rigsignal.game.name and rigsignal.session.id are mapped as
            # native keyword. The .keyword multi-field only exists in the older
            # 2026.03.30 backing index and does NOT exist in the 2026.04.12 index,
            # so using .keyword paths causes the composite aggregation to return
            # empty results on the new index.
            "game_name": {"terms": {"field": "rigsignal.game.name"}},
            "session_id": {"terms": {"field": "rigsignal.session.id"}},
        },
        "aggregations": {
            # Use min(@timestamp) as session_start — the earliest doc timestamp
            # for this session, which is the session-start document.
            "session_start": {"min": {"field": "@timestamp"}},
            # Summary metrics: use max() because the session-end summary doc
            # contains the authoritative values and we want it to win.
            "duration_s": {"max": {"field": "rigsignal.summary.duration_s"}},
            "avg_fps": {"max": {"field": "rigsignal.summary.avg_fps"}},
            "low_1pct_fps": {"max": {"field": "rigsignal.summary.low_1pct_fps"}},
            "p99_frametime_ms": {"max": {"field": "rigsignal.summary.p99_frametime_ms"}},
            "peak_gpu_temp_c": {"max": {"field": "rigsignal.summary.peak_gpu_temp_c"}},
            "peak_cpu_temp_c": {"max": {"field": "rigsignal.summary.peak_cpu_temp_c"}},
            "peak_gpu_power_w": {"max": {"field": "rigsignal.summary.peak_gpu_power_w"}},
            "total_frames": {"max": {"field": "rigsignal.summary.total_frames"}},
            "stutter_count": {"max": {"field": "rigsignal.summary.stutter_count"}},
            # NOTE: keyword context fields (bottleneck_dominant, gpu_model,
            # driver_version, kernel_version, proton_version) are NOT computed
            # here. ES transforms' top_metrics aggregation wraps nested-path
            # fields in the full object hierarchy when writing to the destination,
            # making them incompatible with flat keyword mappings. These fields
            # are added by the post-enrichment step, which performs a source
            # lookup by rigsignal.session.id and bulk-updates the destination docs.
        },
    },
    "settings": {
        # Larger page size = fewer rounds but more memory. 500 is safe for
        # session docs (they're small) and avoids the default 500-bucket limit
        # triggering premature flushes.
        "max_page_search_size": 500,
    },
}

DEST_MAPPING = {
    "mappings": {
        "properties": {
            "game_name": {"type": "keyword"},
            "session_id": {"type": "keyword"},
            "session_start": {"type": "date"},
            "duration_s": {"type": "float"},
            "avg_fps": {"type": "float"},
            "low_1pct_fps": {"type": "float"},
            "p99_frametime_ms": {"type": "float"},
            "peak_gpu_temp_c": {"type": "float"},
            "peak_cpu_temp_c": {"type": "float"},
            "peak_gpu_power_w": {"type": "float"},
            "bottleneck_dominant": {"type": "keyword"},
            "gpu_model": {"type": "keyword"},
            "driver_version": {"type": "keyword"},
            "kernel_version": {"type": "keyword"},
            "proton_version": {"type": "keyword"},
            "total_frames": {"type": "long"},
            "stutter_count": {"type": "long"},
            # Computed by the post-enrichment step (not the transform).
            # cumulative_playtime_hours cannot be derived inside a pivot transform
            # because ES transforms have no window functions. The Python
            # post-enrichment step queries all sessions per game, sorts by
            # session_start, and bulk-updates this field.
            "cumulative_playtime_hours": {"type": "float"},
        }
    }
}

# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"ApiKey {api_key}",
        "Content-Type": "application/json",
    }


def _req(
    method: str,
    url: str,
    api_key: str,
    body: dict | None = None,
    expected_codes: tuple = (200, 201),
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=_headers(api_key),
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        if e.code not in expected_codes:
            raise RuntimeError(f"HTTP {e.code} {method} {url}: {body_text[:600]}") from e
        return json.loads(body_text)


def _get(url: str, api_key: str) -> dict:
    return _req("GET", url, api_key)


def _put(url: str, api_key: str, body: dict) -> dict:
    return _req("PUT", url, api_key, body)


def _post(url: str, api_key: str, body: dict | None = None) -> dict:
    return _req("POST", url, api_key, body)


def _delete(url: str, api_key: str) -> dict:
    return _req("DELETE", url, api_key, expected_codes=(200, 404))


# ── Transform management ──────────────────────────────────────────────────────


def index_exists(es: str, key: str, index: str) -> bool:
    req = urllib.request.Request(
        f"{es}/{index}",
        method="HEAD",
        headers=_headers(key),
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 200


def transform_exists(es: str, key: str) -> bool:
    try:
        _get(f"{es}/_transform/{TRANSFORM_ID}", key)
        return True
    except RuntimeError:
        return False


def create_dest_index(es: str, key: str, dry_run: bool) -> None:
    print(f"  Creating destination index: {DEST_INDEX}")
    if dry_run:
        print("  [dry-run] PUT /" + DEST_INDEX)
        return
    result = _put(f"{es}/{DEST_INDEX}", key, DEST_MAPPING)
    print(f"  OK: {result.get('acknowledged', result)}")


def delete_transform(es: str, key: str, dry_run: bool) -> None:
    print(f"  Stopping and deleting transform: {TRANSFORM_ID}")
    if dry_run:
        print("  [dry-run] DELETE /_transform/" + TRANSFORM_ID)
        return
    try:
        _post(f"{es}/_transform/{TRANSFORM_ID}/_stop?force=true&wait_for_completion=true", key)
    except RuntimeError:
        pass  # Already stopped
    _delete(f"{es}/_transform/{TRANSFORM_ID}?force=true", key)
    print("  Transform deleted.")


def create_transform(es: str, key: str, dry_run: bool) -> None:
    print(f"  Creating transform: {TRANSFORM_ID}")
    if dry_run:
        print(f"  [dry-run] PUT /_transform/{TRANSFORM_ID}")
        print("  Definition:")
        print(json.dumps(TRANSFORM_DEF, indent=2))
        return
    result = _put(f"{es}/_transform/{TRANSFORM_ID}", key, TRANSFORM_DEF)
    print(f"  OK: {result.get('acknowledged', result)}")


def start_transform(es: str, key: str, dry_run: bool) -> None:
    print(f"  Starting transform: {TRANSFORM_ID}")
    if dry_run:
        print("  [dry-run] POST /_transform/" + TRANSFORM_ID + "/_start")
        return
    result = _post(f"{es}/_transform/{TRANSFORM_ID}/_start", key)
    print(f"  OK: {result.get('acknowledged', result)}")


def trigger_sync(es: str, key: str) -> None:
    """Request an immediate transform run (does not wait for completion)."""
    try:
        _post(f"{es}/_transform/{TRANSFORM_ID}/_schedule_now", key)
        print("  Transform sync triggered.")
    except RuntimeError as e:
        print(f"  Warning: trigger_sync failed (non-fatal): {e}")


def wait_for_docs(es: str, key: str, timeout_s: int = 120) -> int:
    """Poll until the destination index has at least one document."""
    print(f"  Waiting up to {timeout_s}s for transform to produce documents…")
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            result = _get(f"{es}/{DEST_INDEX}/_count", key)
            count = result.get("count", 0)
            if count > 0:
                print(f"  Found {count} document(s) in {DEST_INDEX}.")
                return count
        except RuntimeError:
            pass
        time.sleep(5)
    print(f"  Timeout — no documents in {DEST_INDEX} after {timeout_s}s.")
    return 0


# ── Post-enrichment: cumulative playtime ─────────────────────────────────────


def _fetch_source_docs_by_session(es: str, key: str, session_ids: list[str]) -> dict[str, dict]:
    """
    Fetch source session docs from metrics-rigsignal.session-default by session ID.
    Returns a dict of session_id → source fields (keyword context fields only).
    """
    if not session_ids:
        return {}
    body = json.dumps({
        "size": len(session_ids),
        "query": {
            "bool": {
                "filter": [
                    {"terms": {"rigsignal.session.id": session_ids}},
                    {"term": {"rigsignal.summary.ended": True}},
                ]
            }
        },
        "_source": [
            "rigsignal.session.id",
            "rigsignal.summary.bottleneck_dominant",
            "rigsignal.hardware.gpu.model",
            "rigsignal.hardware.gpu.driver_version",
            "host.os.kernel",
            "rigsignal.compatibility.proton_version",
        ],
    }).encode()
    req = urllib.request.Request(
        f"{es}/{SOURCE_INDEX}/_search",
        data=body,
        headers={
            "Authorization": f"ApiKey {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    result = {}
    for hit in data.get("hits", {}).get("hits", []):
        src = hit["_source"]
        sid = (
            src.get("rigsignal", {}).get("session", {}).get("id")
            or src.get("rigsignal.session.id")
        )
        if sid:
            result[sid] = src
    return result


def run_post_enrichment(es: str, key: str, dry_run: bool) -> None:
    """
    Post-enrichment runs after the transform and does two things:

    1. Adds keyword context fields (bottleneck_dominant, gpu_model, driver_version,
       kernel_version, proton_version) — these are NOT in the transform pivot because
       ES transforms' top_metrics agg wraps nested-path fields in the full object
       hierarchy, making them incompatible with flat keyword destination mappings.
       Post-enrichment fetches these values directly from the source docs by session_id.

    2. Computes cumulative_playtime_hours — a running total of playtime per game sorted
       by session_start. This cannot be a pivot aggregation (no window functions in ES
       transforms) and cannot be a runtime field (runtime fields are per-document and
       cannot query sibling documents). Python is the reliable, correct approach.
    """
    print("\n── Post-enrichment: keyword fields + cumulative_playtime_hours ──")

    # Fetch all docs from destination index.
    result = _post(
        f"{es}/{DEST_INDEX}/_search",
        key,
        {"size": 10000, "query": {"match_all": {}}, "_source": True},
    )
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        print("  No documents found in destination index — skipping post-enrichment.")
        return
    print(f"  Fetched {len(hits)} document(s) from destination index.")

    # Fetch source keyword fields for all sessions in one query.
    session_ids = [h["_source"].get("session_id", "") for h in hits if h["_source"].get("session_id")]
    source_by_session = _fetch_source_docs_by_session(es, key, session_ids)
    print(f"  Fetched {len(source_by_session)} source session doc(s) for keyword enrichment.")

    # Group by game_name.
    by_game: dict[str, list] = {}
    for hit in hits:
        src = hit["_source"]
        game = src.get("game_name", "")
        if not game:
            continue
        by_game.setdefault(game, []).append({"id": hit["_id"], "src": src})

    # Compute cumulative playtime + gather keyword context fields.
    updates: list[tuple[str, dict]] = []  # (doc_id, update_fields)
    for game, sessions in sorted(by_game.items()):
        # Sort by session_start ascending (earliest session = 0 prior hours).
        sessions.sort(key=lambda s: s["src"].get("session_start", ""))
        cumulative_s = 0.0
        for sess in sessions:
            cumulative_hours = cumulative_s / 3600.0
            dur = sess["src"].get("duration_s") or 0
            cumulative_s += float(dur)

            update_fields: dict = {"cumulative_playtime_hours": round(cumulative_hours, 4)}

            # Add keyword context from source doc.
            sid = sess["src"].get("session_id", "")
            source = source_by_session.get(sid, {})
            gp = source.get("rigsignal", {})
            if gp:
                bn = gp.get("summary", {}).get("bottleneck_dominant")
                if bn:
                    update_fields["bottleneck_dominant"] = bn
                gpu = gp.get("hardware", {}).get("gpu", {})
                if gpu.get("model"):
                    update_fields["gpu_model"] = gpu["model"]
                if gpu.get("driver_version"):
                    update_fields["driver_version"] = gpu["driver_version"]
                pv = gp.get("compatibility", {}).get("proton_version")
                if pv:
                    update_fields["proton_version"] = pv
            kernel = source.get("host", {}).get("os", {}).get("kernel")
            if kernel:
                update_fields["kernel_version"] = kernel

            updates.append((sess["id"], update_fields))
            print(
                f"  {game}: session {sid[:8] if sid else '?'} "
                f"cumulative_start={cumulative_hours:.2f}h "
                f"dur={dur}s "
                f"bottleneck={update_fields.get('bottleneck_dominant', '?')}"
            )

    if dry_run:
        print(f"  [dry-run] Would update {len(updates)} document(s).")
        return

    if not updates:
        print("  No updates to apply.")
        return

    # Bulk-update destination docs.
    ndjson_lines = []
    for doc_id, fields in updates:
        ndjson_lines.append(json.dumps({"update": {"_index": DEST_INDEX, "_id": doc_id}}))
        ndjson_lines.append(json.dumps({"doc": fields}))
    ndjson_body = "\n".join(ndjson_lines) + "\n"

    bulk_req = urllib.request.Request(
        f"{es}/_bulk",
        data=ndjson_body.encode(),
        method="POST",
        headers={
            "Authorization": f"ApiKey {key}",
            "Content-Type": "application/x-ndjson",
        },
    )
    with urllib.request.urlopen(bulk_req) as r:
        bulk_resp = json.loads(r.read())

    errors = bulk_resp.get("errors", False)
    items = bulk_resp.get("items", [])
    failed = sum(1 for i in items if i.get("update", {}).get("error"))
    print(
        f"  Bulk update: {len(items) - failed}/{len(items)} succeeded"
        + (" (some errors — see output)" if errors else ".")
    )
    if errors:
        for item in items:
            err = item.get("update", {}).get("error")
            if err:
                print(f"    Error: {err}")


# ── Transform stats ───────────────────────────────────────────────────────────


def print_transform_stats(es: str, key: str) -> None:
    try:
        stats = _get(f"{es}/_transform/{TRANSFORM_ID}/_stats", key)
        for s in stats.get("transforms", []):
            state = s.get("state", "unknown")
            sp = s.get("stats", {})
            docs = sp.get("documents_processed", 0)
            indexed = sp.get("documents_indexed", 0)
            triggers = sp.get("trigger_count", 0)
            print(f"  state={state} docs_processed={docs} docs_indexed={indexed} triggers={triggers}")
    except RuntimeError as e:
        print(f"  Could not fetch stats: {e}")


def print_dest_summary(es: str, key: str) -> None:
    try:
        result = _post(
            f"{es}/{DEST_INDEX}/_search",
            key,
            {
                "size": 0,
                "aggs": {
                    "by_game": {
                        "terms": {"field": "game_name", "size": 50},
                        "aggs": {"session_count": {"value_count": {"field": "session_id"}}},
                    }
                },
            },
        )
        buckets = result.get("aggregations", {}).get("by_game", {}).get("buckets", [])
        print(f"  Games in destination index ({len(buckets)}):")
        for b in buckets:
            print(f"    {b['key']}: {b['doc_count']} session(s)")
    except RuntimeError as e:
        print(f"  Could not fetch destination summary: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    reset = "--reset" in sys.argv
    enrich_only = "--enrich-only" in sys.argv

    # Read credentials from environment or fall back to rigsignal.toml.
    es_url = os.environ.get("ES_URL", "").rstrip("/")
    api_key = os.environ.get("ES_API_KEY", "")
    if not es_url or not api_key:
        toml = Path.home() / ".config" / "rigsignal" / "rigsignal.toml"
        if toml.exists():
            for line in toml.read_text().splitlines():
                if line.startswith("endpoint"):
                    es_url = line.split('"')[1].rstrip("/")
                elif line.startswith("api_key"):
                    api_key = line.split('"')[1]
    if not es_url or not api_key:
        sys.exit("Error: ES_URL and ES_API_KEY must be set (or ~/.config/rigsignal/rigsignal.toml must exist).")

    print(f"ES endpoint : {es_url}")
    print(f"Transform   : {TRANSFORM_ID}")
    print(f"Destination : {DEST_INDEX}")
    print(f"Dry-run     : {dry_run}")
    print()

    if enrich_only:
        run_post_enrichment(es_url, api_key, dry_run)
        return

    # ── Step 1: handle reset ───────────────────────────────────────────────
    if reset:
        print("── Reset: removing existing transform and destination index ─────")
        if transform_exists(es_url, api_key):
            delete_transform(es_url, api_key, dry_run)
        if not dry_run and index_exists(es_url, api_key, DEST_INDEX):
            print(f"  Deleting index: {DEST_INDEX}")
            _delete(f"{es_url}/{DEST_INDEX}", api_key)
            print("  Index deleted.")

    # ── Step 2: create destination index ──────────────────────────────────
    print("── Step 1: destination index mapping ───────────────────────────")
    if not dry_run and index_exists(es_url, api_key, DEST_INDEX):
        print(f"  Index {DEST_INDEX} already exists — skipping creation.")
    else:
        create_dest_index(es_url, api_key, dry_run)

    # ── Step 3: create and start transform ────────────────────────────────
    print("\n── Step 2: create transform ─────────────────────────────────────")
    if not dry_run and transform_exists(es_url, api_key):
        print(f"  Transform {TRANSFORM_ID} already exists — skipping creation.")
        print("  (Use --reset to delete and recreate.)")
    else:
        create_transform(es_url, api_key, dry_run)

    print("\n── Step 3: start transform ──────────────────────────────────────")
    start_transform(es_url, api_key, dry_run)

    if not dry_run:
        trigger_sync(es_url, api_key)

        # ── Step 4: wait for initial documents ────────────────────────────
        print("\n── Step 4: wait for documents ───────────────────────────────────")
        count = wait_for_docs(es_url, api_key, timeout_s=120)

        # ── Step 5: post-enrichment ────────────────────────────────────────
        if count > 0:
            run_post_enrichment(es_url, api_key, dry_run)

        # ── Summary ────────────────────────────────────────────────────────
        print("\n── Transform stats ──────────────────────────────────────────────")
        print_transform_stats(es_url, api_key)

        print("\n── Destination index summary ────────────────────────────────────")
        print_dest_summary(es_url, api_key)


if __name__ == "__main__":
    main()
