"""Abstract base for all metric collectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Collector(ABC):
    """
    Each collector reads from one data source and returns a dict of fields
    matching the corresponding ES component template mapping.

    collect() is called once per tick. Collectors that compute deltas
    (CPU %, storage throughput) keep state internally between calls.
    Returning None means no data is available this tick (e.g. GPU not present).
    """

    @abstractmethod
    def collect(self) -> dict[str, Any] | None:
        ...

    @property
    @abstractmethod
    def data_stream(self) -> str:
        """Target data stream, e.g. 'metrics-gamepulse.cpu-default'."""
        ...
