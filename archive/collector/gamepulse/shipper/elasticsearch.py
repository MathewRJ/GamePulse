"""
Elasticsearch bulk API shipper.

Buffers documents as NDJSON and flushes to /_bulk when the batch
size or flush interval is reached. Handles API key and basic auth.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BULK_PATH = "/_bulk"


class ElasticsearchShipper:
    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        batch_size: int = 100,
        flush_interval_secs: int = 5,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._batch_size = batch_size
        self._flush_interval = flush_interval_secs
        self._buffer: list[str] = []
        self._last_flush = time.monotonic()

        headers: dict[str, str] = {"Content-Type": "application/x-ndjson"}
        if api_key:
            headers["Authorization"] = f"ApiKey {api_key}"
        elif username and password:
            creds = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"

        self._client = httpx.Client(
            base_url=self._endpoint,
            headers=headers,
            timeout=30.0,
            verify=True,
        )

    def queue(self, index: str, doc: dict[str, Any]) -> None:
        """Buffer one document for the given data stream / index."""
        action = json.dumps({"create": {"_index": index}})
        body = json.dumps(doc)
        self._buffer.append(action)
        self._buffer.append(body)

        if len(self._buffer) // 2 >= self._batch_size:
            self.flush()

    def flush_if_due(self) -> None:
        elapsed = time.monotonic() - self._last_flush
        if elapsed >= self._flush_interval and self._buffer:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        payload = "\n".join(self._buffer) + "\n"
        self._buffer.clear()
        self._last_flush = time.monotonic()
        try:
            resp = self._client.post(_BULK_PATH, content=payload)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errors"):
                # Log first error without spamming
                for item in result.get("items", []):
                    for action, detail in item.items():
                        if detail.get("error"):
                            log.warning("Bulk error: %s", detail["error"])
                            break
                    break
            else:
                took = result.get("took", "?")
                log.debug("Flushed %d doc-pairs in %s ms", len(payload.splitlines()) // 2, took)
        except httpx.HTTPStatusError as e:
            log.error("ES bulk HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        except httpx.RequestError as e:
            log.error("ES bulk request failed: %s", e)

    def close(self) -> None:
        if self._buffer:
            self.flush()
        self._client.close()
