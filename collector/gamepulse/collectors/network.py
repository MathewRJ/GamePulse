"""
Network collector — delta reads from /proc/net/dev and /proc/net/snmp.

Fields match gamepulse-network-mappings component template exactly.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from gamepulse.collectors.base import Collector

# Interfaces to skip
_SKIP_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap", "vlan")


def _parse_net_dev() -> dict[str, dict[str, int]]:
    """Return {iface: {rx_bytes, rx_packets, tx_bytes, tx_packets}}."""
    result: dict[str, dict[str, int]] = {}
    try:
        lines = Path("/proc/net/dev").read_text().splitlines()
    except OSError:
        return result

    for line in lines[2:]:  # skip two header lines
        parts = line.split()
        if len(parts) < 10:
            continue
        iface = parts[0].rstrip(":")
        result[iface] = {
            "rx_bytes": int(parts[1]),
            "rx_packets": int(parts[2]),
            "tx_bytes": int(parts[9]),
            "tx_packets": int(parts[10]),
        }
    return result


def _parse_tcp_retransmits() -> int:
    """Read cumulative TCP retransmit segments from /proc/net/snmp."""
    try:
        for line in Path("/proc/net/snmp").read_text().splitlines():
            if line.startswith("Tcp:") and "RetransSegs" not in line:
                # Second "Tcp:" line has the values; RetransSegs is field 12 (0-indexed)
                parts = line.split()
                if len(parts) > 12:
                    return int(parts[12])
    except (OSError, IndexError, ValueError):
        pass
    return 0


def _primary_interface(stats: dict[str, dict[str, int]]) -> str | None:
    """Pick the interface with the most rx_bytes, excluding virtual ones."""
    best, best_bytes = None, -1
    for iface, s in stats.items():
        if any(iface.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if s["rx_bytes"] > best_bytes:
            best, best_bytes = iface, s["rx_bytes"]
    return best


def _connection_type(iface: str) -> str:
    """Classify as ethernet or wifi based on interface name."""
    if iface.startswith(("wlan", "wlp", "wlo", "wifi")):
        return "wifi"
    return "ethernet"


class NetworkCollector(Collector):
    data_stream = "metrics-gamepulse.network-default"

    def __init__(self) -> None:
        self._prev: dict[str, dict[str, int]] | None = None
        self._prev_time: float = 0.0
        self._prev_retransmits: int = 0

    def collect(self) -> dict[str, Any] | None:
        now = time.monotonic()
        current = _parse_net_dev()
        retransmits_total = _parse_tcp_retransmits()

        if self._prev is None:
            self._prev = current
            self._prev_time = now
            self._prev_retransmits = retransmits_total
            return None

        dt = now - self._prev_time
        if dt <= 0:
            return None

        iface = _primary_interface(current)
        if not iface or iface not in self._prev:
            self._prev = current
            self._prev_time = now
            self._prev_retransmits = retransmits_total
            return None

        cur = current[iface]
        prv = self._prev[iface]

        rx_bps = (cur["rx_bytes"] - prv["rx_bytes"]) / dt
        tx_bps = (cur["tx_bytes"] - prv["tx_bytes"]) / dt
        rx_pps = (cur["rx_packets"] - prv["rx_packets"]) / dt
        tx_pps = (cur["tx_packets"] - prv["tx_packets"]) / dt
        retransmits_per_sec = (retransmits_total - self._prev_retransmits) / dt

        self._prev = current
        self._prev_time = now
        self._prev_retransmits = retransmits_total

        return {
            "gamepulse": {
                "network": {
                    "rx_mbps": round(rx_bps / 1_048_576, 3),
                    "tx_mbps": round(tx_bps / 1_048_576, 3),
                    "rx_packets_per_sec": round(rx_pps, 1),
                    "tx_packets_per_sec": round(tx_pps, 1),
                    "tcp_retransmits_per_sec": round(retransmits_per_sec, 2),
                    "bandwidth_utilisation_mbps": round((rx_bps + tx_bps) / 1_048_576, 3),
                    "connection_type": _connection_type(iface),
                    "interface": iface,
                }
            }
        }
