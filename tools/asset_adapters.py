#!/usr/bin/env python3
"""Canonical, public asset adapters for ownership-aware installation.

This module intentionally has no dependency on ``install_assets``.  Both the
installer and a future rollback tool use these functions, which keeps their
live-object equality rules versioned in one place.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy


ADAPTER_VERSION = "fleet-coexist-v1"
SERVER_TIMESTAMPS = frozenset((
    "created_date", "created_date_millis", "modified_date", "modified_date_millis",
))
FLEET_COMPOSITION_COMPONENTS = frozenset((
    ".fleet_globals-1", ".fleet_agent_id_verification-1",
))
# A saved-object 404 is a meaningful preimage, not an empty JSON object.  Keep
# this public singleton so callers can distinguish it from a legitimate body.
ABSENT = object()


class AdapterError(ValueError):
    """A live response cannot be represented by the requested adapter."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _body_from_envelope(kind: str, live_body: object) -> object:
    if kind == "dashboard" and live_body is None:
        return ABSENT
    if not isinstance(live_body, dict):
        raise AdapterError("live response is not an object")
    if kind == "component_templates":
        if "component_templates" not in live_body:
            return live_body
        items, member = live_body.get("component_templates"), "component_template"
    elif kind == "index_templates":
        if "index_templates" not in live_body:
            return live_body
        items, member = live_body.get("index_templates"), "index_template"
    elif kind == "pipelines":
        if "processors" in live_body or "on_failure" in live_body or "_meta" in live_body:
            return live_body
        if len(live_body) != 1:
            raise AdapterError("pipeline GET did not return exactly one pipeline")
        return next(iter(live_body.values()))
    elif kind == "security_roles":
        if "cluster" in live_body or "indices" in live_body:
            return live_body
        if len(live_body) != 1:
            raise AdapterError("role GET did not return exactly one role")
        return next(iter(live_body.values()))
    elif kind == "transforms":
        if "transforms" not in live_body:
            return live_body
        transforms = live_body.get("transforms")
        if not isinstance(transforms, list) or len(transforms) != 1 or not isinstance(transforms[0], dict):
            raise AdapterError("transform GET did not return exactly one transform")
        return transforms[0]
    elif kind == "dashboard":
        # Saved-object GETs include identity/server fields beside the only
        # fields an import can restore.  The caller performs one GET per ID.
        if "attributes" not in live_body:
            raise AdapterError("saved-object GET body is missing attributes")
        value = {"attributes": deepcopy(live_body["attributes"])}
        if "references" in live_body:
            value["references"] = deepcopy(live_body["references"])
        return value
    else:
        return live_body
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise AdapterError("template GET did not return exactly one template")
    body = items[0].get(member)
    if not isinstance(body, dict):
        raise AdapterError("template GET body is missing")
    return body


def _strip_server_metadata(body: object) -> object:
    if not isinstance(body, dict):
        return body
    value = {key: deepcopy(member) for key, member in body.items() if key not in SERVER_TIMESTAMPS}
    if "id" in value and "pivot" in value:  # Transform GET-only identity field.
        value.pop("id", None)
    for key in ("version", "create_time", "authorization"):
        value.pop(key, None)
    return value


def _normalise_settings(body: object) -> object:
    if not isinstance(body, dict):
        return body
    value = deepcopy(body)
    template = value.get("template")
    if not isinstance(template, dict) or not isinstance(template.get("settings"), dict):
        return value

    def render(item: object) -> object:
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, (int, float)):
            return str(item)
        if isinstance(item, dict):
            return {key: render(member) for key, member in item.items()}
        if isinstance(item, list):
            return [render(member) for member in item]
        return item

    template["settings"] = render(template["settings"])
    return value


def get_projection(kind: str, live_body: object) -> object:
    """Return a canonical request-shaped body from a class-specific GET body."""
    body = _body_from_envelope(kind, live_body)
    if body is ABSENT:
        return ABSENT
    return _normalise_settings(_strip_server_metadata(body))


def request_body_from_preimage(kind: str, preimage: object) -> object | None:
    """Build the body an inverse PUT/POST would send for a captured preimage.

    ``None`` represents an absent saved object: its inverse is an exact DELETE,
    not an invented empty PUT body.
    """
    if preimage is None or preimage is ABSENT:
        return None
    body = get_projection(kind, preimage)
    # Transform apply omits pivot because ES does not permit changing it.
    # Rollback, however, restores the captured preimage exactly; stripping it
    # here corrupted the absent->_meta restoration proof.
    return body


def compatibility_projection(kind: str, live_body: object) -> object:
    """Return the ownership-tolerant comparison body for external assets."""
    body = get_projection(kind, live_body)
    if kind == "dashboard":
        # Dashboards are never external.  This is deliberately a defined
        # identity operation for interface uniformity, not an ownership rule.
        return body
    if not isinstance(body, dict):
        raise AdapterError("asset body is not an object")
    body = deepcopy(body)
    meta = body.get("_meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        meta.pop("managed_by", None)
        if meta:
            body["_meta"] = meta
        else:
            body.pop("_meta", None)
    if kind == "index_templates":
        composed = body.get("composed_of")
        if isinstance(composed, list):
            body["composed_of"] = [item for item in composed if item not in FLEET_COMPOSITION_COMPONENTS]
    return body


def verify(kind: str, live_body: object, pinned_hash: str) -> bool:
    """Check an adapter projection against a recorded canonical SHA-256 pin."""
    projection = get_projection(kind, live_body)
    if projection is ABSENT:
        return pinned_hash == sha256({"state": "ABSENT"})
    return sha256(projection) == pinned_hash


def dashboard_absent_hash() -> str:
    """Stable pin used when a saved object is intentionally absent."""
    return sha256({"state": "ABSENT"})


def simulate_index_equivalent(request_fn, before_path: str, after_path: str) -> bool:
    """Compare the contract-relevant `_simulate_index` outcomes.

    ``request_fn`` is intentionally injected: production uses the installer
    transport while unit tests can prove this Fleet-specific rule without a
    cluster.  Paths are supplied separately to make it impossible to silently
    compare a cached body with itself.
    """
    def outcome(value: object) -> object:
        if not isinstance(value, dict):
            raise AdapterError("simulate index response is not an object")
        template = value.get("template", value)
        if not isinstance(template, dict):
            raise AdapterError("simulate index template is missing")
        settings = template.get("settings", {})
        mappings = template.get("mappings", {})
        return {
            "mappings": mappings,
            "settings": settings,
            "default_pipeline": settings.get("index.default_pipeline") if isinstance(settings, dict) else None,
            "lifecycle": settings.get("index.lifecycle.name") if isinstance(settings, dict) else None,
        }
    return canonical_json(outcome(request_fn(before_path))) == canonical_json(outcome(request_fn(after_path)))
