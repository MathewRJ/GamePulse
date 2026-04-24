/// Network collector — mirrors collector/gamepulse/collectors/network.py exactly.
///
/// Reads /proc/net/dev (interface counters) and /proc/net/snmp (TCP retransmits)
/// as a delta between two snapshots. First call returns None (no delta yet).
///
/// Interface selection: pick the non-virtual interface with the highest cumulative
/// rx_bytes (same heuristic as Python _primary_interface).
///
/// Output fields (gamepulse.network.*):
///   rx_mbps                  f64  — receive throughput in MB/s (3 dp)
///   tx_mbps                  f64  — transmit throughput in MB/s (3 dp)
///   rx_packets_per_sec       f64  — receive packet rate (1 dp)
///   tx_packets_per_sec       f64  — transmit packet rate (1 dp)
///   tcp_retransmits_per_sec  f64  — TCP retransmit rate (2 dp)
///   bandwidth_utilisation_mbps f64 — (rx + tx) in MB/s (3 dp)
///   connection_type          str  — "ethernet" or "wifi"
///   interface                str  — interface name (e.g. "enp14s0")
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use std::time::Instant;

const SKIP_PREFIXES: &[&str] = &[
    "lo", "docker", "br-", "veth", "virbr", "tun", "tap", "vlan",
];

// ── /proc/net/dev ─────────────────────────────────────────────────────────────

struct IfaceStats {
    rx_bytes: i64,
    rx_packets: i64,
    tx_bytes: i64,
    tx_packets: i64,
}

/// Returns interfaces in file order (insertion order = /proc/net/dev line order).
/// This preserves Python dict ordering for primary_interface() tie-breaking.
fn parse_net_dev() -> Vec<(String, IfaceStats)> {
    let mut result = Vec::new();
    let text = match std::fs::read_to_string("/proc/net/dev") {
        Ok(t) => t,
        Err(_) => return result,
    };
    // Skip first two header lines.
    for line in text.lines().skip(2) {
        let parts: Vec<&str> = line.split_ascii_whitespace().collect();
        if parts.len() < 11 {
            continue;
        }
        let iface = parts[0].trim_end_matches(':').to_string();
        let rx_bytes: i64 = parts[1].parse().unwrap_or(0);
        let rx_packets: i64 = parts[2].parse().unwrap_or(0);
        let tx_bytes: i64 = parts[9].parse().unwrap_or(0);
        let tx_packets: i64 = parts[10].parse().unwrap_or(0);
        result.push((iface, IfaceStats { rx_bytes, rx_packets, tx_bytes, tx_packets }));
    }
    result
}

// ── /proc/net/snmp ────────────────────────────────────────────────────────────

/// Returns cumulative TCP RetransSegs from /proc/net/snmp (field 12, 0-indexed).
/// The second "Tcp:" line holds values; the first contains "RetransSegs" as a label.
fn parse_tcp_retransmits() -> i64 {
    let text = match std::fs::read_to_string("/proc/net/snmp") {
        Ok(t) => t,
        Err(_) => return 0,
    };
    for line in text.lines() {
        if line.starts_with("Tcp:") && !line.contains("RetransSegs") {
            let parts: Vec<&str> = line.split_ascii_whitespace().collect();
            if parts.len() > 12 {
                return parts[12].parse().unwrap_or(0);
            }
        }
    }
    0
}

// ── Interface selection ───────────────────────────────────────────────────────

fn primary_interface<'a>(stats: &'a [(String, IfaceStats)]) -> Option<&'a str> {
    let mut best: Option<&str> = None;
    let mut best_bytes: i64 = -1;
    for (iface, s) in stats {
        if SKIP_PREFIXES.iter().any(|p| iface.starts_with(p)) {
            continue;
        }
        if s.rx_bytes > best_bytes {
            best = Some(iface.as_str());
            best_bytes = s.rx_bytes;
        }
    }
    best
}

fn get_iface<'a>(stats: &'a [(String, IfaceStats)], iface: &str) -> Option<&'a IfaceStats> {
    stats.iter().find(|(n, _)| n == iface).map(|(_, s)| s)
}

fn connection_type(iface: &str) -> &'static str {
    if iface.starts_with("wlan")
        || iface.starts_with("wlp")
        || iface.starts_with("wlo")
        || iface.starts_with("wifi")
    {
        "wifi"
    } else {
        "ethernet"
    }
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct NetworkCollector {
    prev_stats: Option<Vec<(String, IfaceStats)>>,
    prev_time: Option<Instant>,
    prev_retransmits: i64,
}

impl NetworkCollector {
    pub fn new(_game_pid: Option<u32>) -> Self {
        NetworkCollector {
            prev_stats: None,
            prev_time: None,
            prev_retransmits: 0,
        }
    }
}

impl Collector for NetworkCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.network"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let now = Instant::now();
        let current = parse_net_dev();
        let retransmits_total = parse_tcp_retransmits();

        let prev = match self.prev_stats.take() {
            None => {
                self.prev_stats = Some(current);
                self.prev_time = Some(now);
                self.prev_retransmits = retransmits_total;
                return Ok(None);
            }
            Some(p) => p,
        };

        let prev_time = self.prev_time.take().unwrap();
        let dt = now.duration_since(prev_time).as_secs_f64();

        if dt <= 0.0 {
            self.prev_stats = Some(current);
            self.prev_time = Some(now);
            self.prev_retransmits = retransmits_total;
            return Ok(None);
        }

        let iface = match primary_interface(&current) {
            Some(i) => i,
            None => {
                self.prev_stats = Some(current);
                self.prev_time = Some(now);
                self.prev_retransmits = retransmits_total;
                return Ok(None);
            }
        };

        if get_iface(&prev, iface).is_none() {
            self.prev_stats = Some(current);
            self.prev_time = Some(now);
            self.prev_retransmits = retransmits_total;
            return Ok(None);
        }

        let cur = get_iface(&current, iface).unwrap();
        let prv = get_iface(&prev, iface).unwrap();

        let rx_bps = (cur.rx_bytes - prv.rx_bytes) as f64 / dt;
        let tx_bps = (cur.tx_bytes - prv.tx_bytes) as f64 / dt;
        let rx_pps = (cur.rx_packets - prv.rx_packets) as f64 / dt;
        let tx_pps = (cur.tx_packets - prv.tx_packets) as f64 / dt;
        let retransmits_per_sec =
            (retransmits_total - self.prev_retransmits) as f64 / dt;

        let rx_mbps = (rx_bps / 1_048_576.0 * 1000.0).round() / 1000.0;
        let tx_mbps = (tx_bps / 1_048_576.0 * 1000.0).round() / 1000.0;
        let rx_pps_r = (rx_pps * 10.0).round() / 10.0;
        let tx_pps_r = (tx_pps * 10.0).round() / 10.0;
        let retrans_r = (retransmits_per_sec * 100.0).round() / 100.0;
        let bandwidth = ((rx_bps + tx_bps) / 1_048_576.0 * 1000.0).round() / 1000.0;

        let iface_str = iface.to_string();
        let conn_type = connection_type(iface);

        self.prev_stats = Some(current);
        self.prev_time = Some(now);
        self.prev_retransmits = retransmits_total;

        Ok(Some(json!({
            "gamepulse": {
                "network": {
                    "rx_mbps": rx_mbps,
                    "tx_mbps": tx_mbps,
                    "rx_packets_per_sec": rx_pps_r,
                    "tx_packets_per_sec": tx_pps_r,
                    "tcp_retransmits_per_sec": retrans_r,
                    "bandwidth_utilisation_mbps": bandwidth,
                    "connection_type": conn_type,
                    "interface": iface_str,
                }
            }
        })))
    }
}
