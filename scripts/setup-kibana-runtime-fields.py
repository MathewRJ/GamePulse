#!/usr/bin/env python3
"""
Apply session_label runtime field to all RigSignal data views.

session_label is a human-readable identifier: {game-slug}-{first-8-of-session-id}
It is used as the breakdown_by dimension in all XY chart panels so each
gaming session gets its own colour in the time-series charts.

Run after package installation or whenever data views are recreated:

    python3 scripts/setup-kibana-runtime-fields.py

Required env vars: KIBANA_URL, ES_API_KEY
"""

import os, sys, json
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = os.environ['KIBANA_URL'].rstrip('/')
KEY  = os.environ['ES_API_KEY']


def api(method, path, body=None):
    url = f"{BASE}{path}"
    headers = {
        'Authorization': f'ApiKey {KEY}',
        'kbn-xsrf': 'true',
        'Content-Type': 'application/json',
        'Elastic-Api-Version': '2023-10-31',
    }
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        resp = urlopen(req)
        content = resp.read()
        return json.loads(content) if content else {}
    except HTTPError as e:
        raise Exception(f"HTTP {e.code} {method} {path}: {e.read().decode()[:400]}")


# Full script for data views that carry rigsignal.game.name
FULL_SCRIPT = """\
if (doc['rigsignal.game.name'].size() > 0 && doc['rigsignal.session.id'].size() > 0) {
  String name = doc['rigsignal.game.name'].value.toLowerCase();
  String game = /[^a-z0-9]+/.matcher(name).replaceAll('-');
  while (game.endsWith('-')) { game = game.substring(0, game.length()-1); }
  if (game.startsWith('-')) { game = game.substring(1); }
  if (game.length() > 20) { game = game.substring(0, 20); }
  emit(game + '-' + doc['rigsignal.session.id'].value.substring(0, 8));
} else if (doc['rigsignal.session.id'].size() > 0) {
  emit('session-' + doc['rigsignal.session.id'].value.substring(0, 8));
}"""

# Simplified script for eBPF — that stream carries session.id but not game.name
EBPF_SCRIPT = """\
if (doc['rigsignal.session.id'].size() > 0) {
  emit('session-' + doc['rigsignal.session.id'].value.substring(0, 8));
}"""

# Map data view ID → which script to use
DATA_VIEWS = {
    'gp-dv-frame':   FULL_SCRIPT,
    'gp-dv-session': FULL_SCRIPT,
    'gp-dv-gpu':     FULL_SCRIPT,
    'gp-dv-cpu':     FULL_SCRIPT,
    'gp-dv-memory':  FULL_SCRIPT,
    'gp-dv-storage': FULL_SCRIPT,
    'gp-dv-power':   FULL_SCRIPT,
    'gp-dv-audio':   FULL_SCRIPT,
    'gp-dv-network': FULL_SCRIPT,
    'gp-dv-ebpf':    EBPF_SCRIPT,   # eBPF stream has no rigsignal.game.name field
}


def upsert_runtime_field(dv_id, script):
    """Delete (idempotent) then create the session_label runtime field."""
    try:
        api('DELETE', f'/api/data_views/data_view/{dv_id}/runtime_field/session_label')
    except Exception:
        pass  # not present yet — that's fine
    api('POST', f'/api/data_views/data_view/{dv_id}/runtime_field', {
        "name": "session_label",
        "runtimeField": {"type": "keyword", "script": {"source": script}}
    })


errors = []
for dv_id, script in DATA_VIEWS.items():
    try:
        upsert_runtime_field(dv_id, script)
        print(f"  OK  {dv_id}")
    except Exception as e:
        print(f"  ERR {dv_id}: {e!s:.120}")
        errors.append(dv_id)

if errors:
    print(f"\nFailed: {errors}")
    sys.exit(1)
else:
    print("\nAll session_label runtime fields applied.")
