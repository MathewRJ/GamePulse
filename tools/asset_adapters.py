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
ROLE_EMPTY_DEFAULT_KEYS = frozenset((
    "applications", "run_as", "metadata", "remote_indices", "remote_cluster", "global",
))
TRANSFORM_SERVER_FIELDS = frozenset((
    "id", "version", "create_time", "authorization", "state", "stats", "checkpointing",
))
FLEET_COMPOSITION_COMPONENTS = frozenset((
    ".fleet_globals-1", ".fleet_agent_id_verification-1",
))
# These are generated for the index being simulated, rather than resolved from
# an index template.  A synthetic expected index and a real live index must not
# differ merely because their names (or ES-generated creation identity) differ.
SIMULATION_PATTERN_DERIVED_SETTINGS = frozenset((
    "index.provided_name", "index.uuid", "index.creation_date",
    "index.creation_date_string",
    # TSDB boundaries are generated from the wall clock at simulation time
    # (second resolution): two otherwise-identical simulations straddling a
    # second boundary would falsely differ. The templates' declared
    # look_ahead/look_back settings, if any, remain comparison-significant.
    "index.time_series.start_time", "index.time_series.end_time",
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
    if kind in {"component_templates", "install_marker"}:
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
    return value


def _strip_empty_defaults(body: object, keys: frozenset[str]) -> object:
    """Drop only known, empty endpoint defaults; retain every real grant."""
    if not isinstance(body, dict):
        return body
    return {key: deepcopy(value) for key, value in body.items()
            if not (key in keys and value in ([], {}, None))}


def _strip_false_allow_restricted_indices(body: object) -> object:
    if not isinstance(body, dict):
        return body
    value = deepcopy(body)
    for key in ("indices", "remote_indices"):
        entries = value.get(key)
        if isinstance(entries, list):
            value[key] = [
                {name: member for name, member in entry.items()
                 if not (name == "allow_restricted_indices" and member is False)}
                if isinstance(entry, dict) else entry
                for entry in entries
            ]
    return value


def _normalise_security_role(body: object) -> object:
    """Mirror the installer's role GET projection, including 9.4 defaults."""
    if not isinstance(body, dict):
        return body
    value = {key: deepcopy(member) for key, member in body.items()
             if key != "transient_metadata"}
    return _strip_false_allow_restricted_indices(_strip_empty_defaults(value, ROLE_EMPTY_DEFAULT_KEYS))


def _normalise_kibana_role(body: object) -> object:
    """Project the Kibana Role API's owned privilege shape.

    The installer deliberately ignores role identity/diagnostic fields and
    compares the Elasticsearch and Kibana privileges.  Keep that same boundary
    here, while removing only empty native-role defaults.
    """
    if not isinstance(body, dict):
        return body
    value = {key: deepcopy(body.get(key)) for key in ("elasticsearch", "kibana")}
    for key in ROLE_EMPTY_DEFAULT_KEYS:
        if key in body:
            value[key] = deepcopy(body[key])
    value = _strip_empty_defaults(value, ROLE_EMPTY_DEFAULT_KEYS)
    elasticsearch = value.get("elasticsearch")
    if isinstance(elasticsearch, dict):
        value["elasticsearch"] = _strip_false_allow_restricted_indices(
            _strip_empty_defaults(elasticsearch, ROLE_EMPTY_DEFAULT_KEYS))
    return value


def _normalise_kibana_space(body: object) -> object:
    """Remove the empty space defaults returned by Kibana's GET endpoint."""
    if not isinstance(body, dict):
        return body
    value = deepcopy(body)
    # A request that omits these values is returned with these endpoint
    # defaults on 9.4.x.  An explicit non-default solution/color/etc. remains
    # significant.  ``_reserved`` is server-owned and false for a created
    # ordinary space.
    for key, default in (("color", None), ("imageUrl", ""), ("solution", "classic"),
                         ("_reserved", False)):
        if value.get(key) == default:
            value.pop(key, None)
    return value


def _normalise_kind(kind: str, body: object) -> object:
    if kind == "security_roles":
        return _normalise_security_role(body)
    if kind == "kibana_roles":
        return _normalise_kibana_role(body)
    if kind == "kibana_spaces":
        return _normalise_kibana_space(body)
    if kind == "transforms" and isinstance(body, dict):
        return {key: deepcopy(member) for key, member in body.items()
                if key not in TRANSFORM_SERVER_FIELDS}
    return body


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
    return _normalise_settings(_normalise_kind(kind, _strip_server_metadata(body)))


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
    # Fleet owns template metadata.  In particular a reinstall can add
    # ``_meta.package.version`` between invocations.  Metadata is not an
    # operational template contribution, so compare the non-meta body only.
    # (Do not apply this relaxation to bundle-owned verification.)
    body.pop("_meta", None)
    if kind == "index_templates":
        # Fleet also writes ownership metadata inside the resolved mapping.
        # It is the same non-operational ownership contribution as top-level
        # ``_meta`` (the owned-value dominance exemption covers this field).
        template = body.get("template")
        if isinstance(template, dict):
            mappings = template.get("mappings")
            if isinstance(mappings, dict):
                mappings.pop("_meta", None)
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


def concrete_index_name(index_patterns: object, suffix: str) -> str:
    """Derive a concrete, matching index name from the first template pattern."""
    if not isinstance(index_patterns, list) or not index_patterns or not isinstance(suffix, str) or not suffix:
        raise AdapterError("index template index_patterns are invalid")
    pattern = index_patterns[0]
    if not isinstance(pattern, str) or not pattern:
        raise AdapterError("index template index_patterns are invalid")
    # ES wildcard patterns used by the bundle are simple ``*`` suffix
    # patterns.  Supporting ``?`` too keeps this helper concrete for a valid
    # alternate template pattern without inventing a request body.
    return pattern.replace("*", suffix).replace("?", "x")


def synthetic_simulation_template(template: object, uniqueness: str) -> tuple[dict, str]:
    """Clone a template with a class-preserving, collision-free probe pattern.

    Elasticsearch derives some settings from an index's leading name class
    (for example ``logs-`` and ``metrics-``).  The synthetic pattern retains
    that class but replaces the remainder with the unique ``a5sim`` namespace.
    The active RigSignal patterns all continue with ``rigsignal`` after their
    class token, so this cannot match one of them at the same priority.
    """
    if not isinstance(template, dict) or not isinstance(uniqueness, str) or not uniqueness:
        raise AdapterError("index template simulation input is invalid")
    index_patterns = template.get("index_patterns")
    if not isinstance(index_patterns, list) or not index_patterns or not isinstance(index_patterns[0], str):
        raise AdapterError("index template index_patterns are invalid")
    name_class, separator, _remainder = index_patterns[0].partition("-")
    if not separator or not name_class or "*" in name_class or "?" in name_class:
        raise AdapterError("index template simulation name class is invalid")
    synthetic_pattern = f"{name_class}-a5sim{uniqueness}-*"
    synthetic = deepcopy(template)
    synthetic["index_patterns"] = [synthetic_pattern]
    return synthetic, concrete_index_name(synthetic["index_patterns"], "probe")


def _simulation_settings(settings: object) -> dict:
    if not isinstance(settings, dict):
        raise AdapterError("simulate index settings are invalid")
    value = deepcopy(settings)
    # ES may render settings in either flattened or nested form.  Strip only
    # the four generated identity/name fields listed above; all resolved
    # template settings remain comparison-significant.
    for key in SIMULATION_PATTERN_DERIVED_SETTINGS:
        value.pop(key, None)
        cursor = value
        parts = key.split(".")
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                cursor = None
                break
            cursor = child
        if isinstance(cursor, dict):
            cursor.pop(parts[-1], None)
    return value


def simulation_outcome(value: object) -> dict:
    """Normalize an ES simulation to its resolved semantic template result.

    Deliberately discarded: response wrappers (including ``overlapping``),
    ``index_patterns``/priority (not part of the resolved template), and the
    four generated index identity/name settings in
    ``SIMULATION_PATTERN_DERIVED_SETTINGS``.  Mappings, all other settings,
    aliases, and the resolved default pipeline/lifecycle remain significant.
    """
    if not isinstance(value, dict):
        raise AdapterError("simulate index response is not an object")
    template = value.get("template", value)
    if not isinstance(template, dict):
        raise AdapterError("simulate index template is missing")
    mappings = template.get("mappings", {})
    aliases = template.get("aliases", {})
    settings = _simulation_settings(template.get("settings", {}))
    if not isinstance(mappings, dict) or not isinstance(aliases, dict):
        raise AdapterError("simulate index template is invalid")
    return {
        "mappings": mappings,
        "settings": settings,
        "aliases": aliases,
    }


def _owned_value_dominates(expected: object, live: object, path: tuple[str, ...] = ()) -> bool:
    """Return whether every bundle-declared resolved path survives in live.

    Fleet may add resolved paths through its two verification components.  It
    must never override a bundle declaration.  Dynamic templates are an ES
    list but semantically keyed by their single template name, so matching by
    position would make a harmless Fleet insertion look like a conflict.
    """
    if path == ("mappings", "_meta", "managed_by"):
        # Binding owner-cluster probe: Fleet intentionally overrides this
        # ownership label.  It is not an owned mapping contribution.
        return True
    if isinstance(expected, dict):
        if not isinstance(live, dict):
            return False
        for key, member in expected.items():
            if key not in live or not _owned_value_dominates(member, live[key], path + (key,)):
                return False
        return True
    if isinstance(expected, list):
        if not isinstance(live, list):
            return False
        if path == ("settings", "index", "dimensions"):
            # ES may resolve TSDB dimensions in a different order for inline
            # and named-template simulations.  Dimensions are semantic set
            # membership, however, so neither side may add or omit one.
            try:
                return set(expected) == set(live)
            except TypeError:
                # A malformed non-scalar dimensions entry cannot be compared
                # as a dimension name and must not be treated as equivalent.
                return False
        if path == ("mappings", "dynamic_templates"):
            def named(items: list[object]) -> dict[str, object] | None:
                result: dict[str, object] = {}
                for item in items:
                    if not isinstance(item, dict) or len(item) != 1:
                        return None
                    name, body = next(iter(item.items()))
                    if not isinstance(name, str) or name in result:
                        return None
                    result[name] = body
                return result
            expected_named, live_named = named(expected), named(live)
            if expected_named is None or live_named is None:
                return False
            return all(name in live_named and _owned_value_dominates(body, live_named[name], path + (name,))
                       for name, body in expected_named.items())
        # No other resolved list is a Fleet extension point; preserve exact
        # list semantics for aliases and mapping constructs.
        return len(expected) == len(live) and all(
            _owned_value_dominates(member, live[index], path + (str(index),))
            for index, member in enumerate(expected))
    return expected == live


def simulation_outcome_dominates(expected: object, live: object) -> bool:
    """Owned-scoped `_simulate_index` oracle for Fleet index templates."""
    return _owned_value_dominates(simulation_outcome(expected), simulation_outcome(live))


def simulate_index_equivalent(request_fn, expected_path: str, expected_body: object,
                              live_path: str) -> bool:
    """Compare inline expected and named live `_simulate_index` outcomes.

    ``request_fn(path, body)`` receives ``None`` for the named live request,
    which is essential: even ``{}`` is interpreted by ES as an inline template
    definition.  The expected body is required to contain only the synthetic
    index pattern created by ``synthetic_simulation_template``.
    """
    expected = request_fn(expected_path, expected_body)
    live = request_fn(live_path, None)
    return simulation_outcome_dominates(expected, live)
