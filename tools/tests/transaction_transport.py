"""Scripted HTTP transport for v2 transaction tests.

This replaces ``urllib.request.urlopen`` rather than the installer request
helpers, so callers retain the real mutation tracker and guarded code paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit


@dataclass(frozen=True)
class HttpCall:
    method: str
    path: str
    query: Mapping[str, tuple[str, ...]]
    data: bytes | None


@dataclass(frozen=True)
class HttpReply:
    status: int = 200
    body: object = field(default_factory=dict)
    loss: bool = False


@dataclass(frozen=True)
class TargetScript:
    """Declarative response script for one v2 target.

    ``conditional`` maps ``create``, ``createOnly``, or ``if_version`` to a
    reply.  GET/PUT/resolve/import can each be one reply or a reply sequence.
    Pipeline timestamps, role ``created`` and saved-object ``destinationId``
    are explicit because the v2 detector consumes them directly.
    """

    live_state: str = "exact"
    get: object | None = None
    put: object | None = None
    resolve: object | None = None
    import_: object | None = None
    conditional: Mapping[str, object] = field(default_factory=dict)
    role_created: bool | None = None
    pipeline_created_millis: int | None = None
    pipeline_modified_millis: int | None = None
    destination_id: str | None = None
    response_loss: bool = False


@dataclass(frozen=True)
class TransactionRow:
    """Row projection for later main()-runner conversion."""

    caller: str
    live_state: str
    flags: tuple[str, ...]

    @classmethod
    def from_row(cls, row: Sequence[str] | Mapping[str, str]) -> "TransactionRow":
        if isinstance(row, Mapping):
            caller = row["caller"]
            live = row.get("ordinary_live", row.get("live_state", "exact"))
            flags = row.get("flags", "none")
        else:
            caller, live, flags = row[0], row[2], row[4]
        return cls(caller, live, () if flags == "none" else tuple(flags.split("+")))


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status, self._body = status, body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        """Match the file-like error body expected by ``urllib.HTTPError``."""

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _HttpError(HTTPError):
    """HTTPError with an in-memory body and no unclosed file wrapper."""

    def __init__(self, url: str, status: int, body: bytes):
        # Do not construct ``addinfourl``: unlike a real urllib response the
        # scripted body has no OS resource for HTTPError to clean up.
        Exception.__init__(self, url, status, "scripted response")
        self.url, self.code, self.headers, self.fp = url, status, {}, None
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __del__(self) -> None:
        # ``HTTPError`` normally owns a temporary response wrapper.  This
        # fake deliberately owns only immutable bytes.
        pass


class ScriptedTransactionTransport:
    """Reusable fake installed at the real ``urllib`` HTTP seam."""

    def __init__(self, install: Any, bundle: Any,
                 scripts: Mapping[str, TargetScript | Mapping[str, object]] | None = None,
                 *, row: TransactionRow | None = None):
        self.install, self.bundle, self.row = install, bundle, row
        self.calls: list[HttpCall] = []
        self._positions: dict[tuple[str, str], int] = {}
        self._scripts = {key: self._coerce(value) for key, value in (scripts or {}).items()}
        self._assets = {install._transaction_key_for_asset(asset): asset
                        for asset in bundle.assets if asset.kind in install._ES_ASSET_KINDS}
        self._routes = self._build_routes()

    @classmethod
    def from_table_row(cls, install: Any, bundle: Any, row: Sequence[str] | Mapping[str, str],
                       *, target_key: str | None = None,
                       target_script: TargetScript | Mapping[str, object] | None = None) -> "ScriptedTransactionTransport":
        projection = TransactionRow.from_row(row)
        target_key = target_key or cls._row_target(install, bundle, projection.live_state)
        script = target_script or TargetScript(live_state=cls._row_state(projection.live_state))
        return cls(install, bundle, {target_key: script}, row=projection)

    @staticmethod
    def _row_state(value: str) -> str:
        if value.startswith("absent:"):
            return "absent"
        return value if value in {"exact", "unreadable"} else "divergent"

    @staticmethod
    def _row_target(install: Any, bundle: Any, value: str) -> str:
        kind = "pipelines" if "pipeline-or-es-role" in value else "component_templates"
        asset = next(asset for asset in bundle.assets if asset.kind == kind)
        return install._transaction_key_for_asset(asset)

    @staticmethod
    def _coerce(value: TargetScript | Mapping[str, object]) -> TargetScript:
        if isinstance(value, TargetScript):
            return value
        copied = dict(value)
        if "import" in copied:
            copied["import_"] = copied.pop("import")
        return TargetScript(**copied)

    def _build_routes(self) -> dict[str, str]:
        routes: dict[str, str] = {}
        for key, asset in self._assets.items():
            routes[self.install.es_path(asset)] = key
        for asset in self.bundle.assets:
            if asset.kind in {"kibana_spaces", "kibana_roles"}:
                kind = "space" if asset.kind == "kibana_spaces" else "role"
                routes[self.install.kibana_path(asset)] = "kibana/default/" + kind + "/" + asset.name
        return routes

    @staticmethod
    def _bytes(body: object) -> bytes:
        if isinstance(body, bytes):
            return body
        if isinstance(body, str):
            return body.encode()
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    def _reply(self, value: object | None, fallback: HttpReply, key: str, action: str) -> HttpReply:
        if value is None:
            return fallback
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, dict)):
            index = self._positions.get((key, action), 0)
            self._positions[(key, action)] = index + 1
            value = value[min(index, len(value) - 1)]
        if isinstance(value, HttpReply):
            return value
        if isinstance(value, Mapping):
            return HttpReply(**value)
        raise AssertionError("invalid scripted HTTP reply")

    def _desired_body(self, key: str) -> object:
        asset = self._assets.get(key)
        if asset is None:
            return {"attributes": {}, "references": []}
        body = json.loads(self.install.stamped_asset(asset).data)
        if asset.kind == "component_templates":
            return {"component_templates": [{"name": asset.name, "component_template": body}]}
        if asset.kind == "index_templates":
            return {"index_templates": [{"name": asset.name, "index_template": body}]}
        if asset.kind == "security_roles":
            return {asset.name: body}
        return body

    def _key_for_saved_object(self, path: str) -> str | None:
        parts = [unquote(part) for part in path.split("/") if part]
        try:
            marker = parts.index("saved_objects")
            object_type, object_id = parts[marker + 1:marker + 3]
        except (ValueError, IndexError):
            return None
        space = parts[1] if len(parts) > 2 and parts[0] == "s" else "default"
        return "kibana/" + self.install._v2_quote(space) + "/" + self.install._v2_quote(object_type) + "/" + self.install._v2_quote(object_id)

    def _route(self, path: str) -> tuple[str | None, str]:
        if "/api/saved_objects/_import" in path:
            return "dashboard-import", "import"
        if "/api/saved_objects/resolve/" in path:
            return self._key_for_saved_object(path.replace("/resolve", "", 1)), "resolve"
        if "/api/saved_objects/" in path:
            return self._key_for_saved_object(path), "saved"
        return self._routes.get(path), "target"

    def _default_get(self, key: str, script: TargetScript) -> HttpReply:
        if script.live_state == "absent":
            return HttpReply(404, {"error": "absent"})
        if script.live_state == "unreadable":
            return HttpReply(503, {"error": "unreadable"})
        body = self._desired_body(key) if script.live_state == "exact" else {"_fake": "divergent"}
        asset = self._assets.get(key)
        if asset is not None and asset.kind == "pipelines" and isinstance(body, dict):
            body = dict(body)
            body["created_date_millis"] = 1 if script.pipeline_created_millis is None else script.pipeline_created_millis
            body["modified_date_millis"] = body["created_date_millis"] if script.pipeline_modified_millis is None else script.pipeline_modified_millis
        return HttpReply(200, body)

    def _default_put(self, key: str, script: TargetScript, query: Mapping[str, tuple[str, ...]]) -> HttpReply:
        conditional = "create" if query.get("create") == ("true",) else ("createOnly" if query.get("createOnly") == ("true",) else ("if_version" if "if_version" in query else None))
        if conditional and conditional in script.conditional:
            if script.conditional[conditional] == "echo":
                return HttpReply(200, {"if_version": query.get("if_version", ())})
            return self._reply(script.conditional[conditional], HttpReply(), key, "conditional:" + conditional)
        if conditional in {"create", "createOnly"} and script.live_state == "conflict":
            return HttpReply(400 if conditional == "create" else 409, {"error": "conflict"})
        asset = self._assets.get(key)
        if asset is not None and asset.kind == "security_roles":
            return HttpReply(200, {"role": {"created": True if script.role_created is None else script.role_created}}, script.response_loss)
        if key.startswith("kibana/") and script.destination_id is not None:
            return HttpReply(200, {"id": script.destination_id, "destinationId": script.destination_id}, script.response_loss)
        return HttpReply(200, {}, script.response_loss)

    def urlopen(self, request: Any) -> _Response:
        parsed = urlsplit(request.full_url)
        query = {name: tuple(values) for name, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        method, path = request.get_method(), parsed.path
        self.calls.append(HttpCall(method, path, query, request.data))
        key, route = self._route(path)
        script = self._scripts.get(key or "", TargetScript())
        if route == "import":
            reply = self._reply(script.import_, HttpReply(200, {"success": True, "successCount": 0, "successResults": []}), key or route, "import")
        elif route == "resolve":
            reply = self._reply(script.resolve, HttpReply(404, {"error": "not an alias"}), key or route, "resolve")
        elif method == "GET":
            reply = self._reply(script.get, self._default_get(key or "", script), key or route, "get")
        else:
            reply = self._reply(script.put, self._default_put(key or "", script, query), key or route, "put")
        body = self._bytes(reply.body)
        if reply.loss:
            raise HTTPError(request.full_url, 599, "response lost", {}, None)
        if reply.status >= 400:
            raise _HttpError(request.full_url, reply.status, body)
        return _Response(reply.status, body)
