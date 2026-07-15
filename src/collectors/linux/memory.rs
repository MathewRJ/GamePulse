/// Memory collector — mirrors collector/rigsignal/collectors/memory.py exactly.
///
/// Reads /proc/meminfo (system memory) and optionally /proc/<pid>/status and
/// /proc/<pid>/stat for game-process memory and page fault metrics.
///
/// Output fields (rigsignal.memory.*):
///   total_mb          u64  — MemTotal in MB
///   system_used_mb    u64  — MemTotal - MemAvailable in MB
///   page_cache_mb     u64  — Cached in MB
///   shared_mb         u64  — Shmem in MB
///   swap_used_mb      u64  — SwapTotal - SwapFree in MB
///   game_rss_mb       u64  — VmRSS from /proc/<pid>/status in MB (when game running)
///   virtual_mb        u64  — VmSize from /proc/<pid>/status in MB (when game running)
///   page_faults_major u64  — field 9 from /proc/<pid>/stat (matches Python assignment)
///   page_faults_minor u64  — field 11 from /proc/<pid>/stat (matches Python assignment)
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use std::collections::HashMap;

// ── /proc/meminfo ────────────────────────────────────────────────────────────

fn read_meminfo() -> HashMap<String, u64> {
    let mut result = HashMap::new();
    if let Ok(text) = std::fs::read_to_string("/proc/meminfo") {
        for line in text.lines() {
            let mut parts = line.split_ascii_whitespace();
            let key = match parts.next() {
                Some(k) => k.trim_end_matches(':').to_string(),
                None => continue,
            };
            if let Some(v) = parts.next().and_then(|s| s.parse::<u64>().ok()) {
                result.insert(key, v);
            }
        }
    }
    result
}

// ── /proc/<pid>/status ───────────────────────────────────────────────────────

fn game_rss_mb(pid: u32) -> Option<u64> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    for line in text.lines() {
        if line.starts_with("VmRSS:") {
            let kb: u64 = line.split_ascii_whitespace().nth(1)?.parse().ok()?;
            return Some(kb / 1024);
        }
    }
    None
}

fn game_virtual_mb(pid: u32) -> Option<u64> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    for line in text.lines() {
        if line.starts_with("VmSize:") {
            let kb: u64 = line.split_ascii_whitespace().nth(1)?.parse().ok()?;
            return Some(kb / 1024);
        }
    }
    None
}

// ── /proc/<pid>/stat ─────────────────────────────────────────────────────────

/// Returns (field_9, field_11) from /proc/<pid>/stat, matching Python:
///   parts[9] → assigned to page_faults_major in collect()
///   parts[11] → assigned to page_faults_minor in collect()
/// Note: field 9 is minflt and field 11 is majflt in the kernel format, but
/// the Python code assigns them in this order — we replicate that exactly.
fn page_faults(pid: u32) -> Option<(u64, u64)> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let parts: Vec<&str> = text.split_ascii_whitespace().collect();
    if parts.len() < 12 {
        return None;
    }
    let f9: u64 = parts[9].parse().ok()?;
    let f11: u64 = parts[11].parse().ok()?;
    Some((f9, f11))
}

// ── Collector ────────────────────────────────────────────────────────────────

pub struct MemoryCollector {
    game_pid: Option<u32>,
}

impl MemoryCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        MemoryCollector { game_pid }
    }

    pub fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }
}

impl Collector for MemoryCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.memory"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let info = read_meminfo();

        let mem_total_kb = info.get("MemTotal").copied().unwrap_or(0);
        let mem_available_kb = info.get("MemAvailable").copied().unwrap_or(0);
        let mem_used_kb = mem_total_kb.saturating_sub(mem_available_kb);
        let page_cache_kb = info.get("Cached").copied().unwrap_or(0);
        let shared_kb = info.get("Shmem").copied().unwrap_or(0);
        let swap_total_kb = info.get("SwapTotal").copied().unwrap_or(0);
        let swap_free_kb = info.get("SwapFree").copied().unwrap_or(0);

        let total_mb = mem_total_kb / 1024;
        let system_used_mb = mem_used_kb / 1024;
        let page_cache_mb = page_cache_kb / 1024;
        let shared_mb = shared_kb / 1024;
        let swap_used_mb = swap_total_kb.saturating_sub(swap_free_kb) / 1024;

        let mut mem = json!({
            "total_mb": total_mb,
            "system_used_mb": system_used_mb,
            "page_cache_mb": page_cache_mb,
            "shared_mb": shared_mb,
            "swap_used_mb": swap_used_mb,
        });

        if let Some(pid) = self.game_pid {
            let obj = mem.as_object_mut().unwrap();
            if let Some(rss) = game_rss_mb(pid) {
                obj.insert("game_rss_mb".to_string(), Value::from(rss));
            }
            if let Some(virt) = game_virtual_mb(pid) {
                obj.insert("virtual_mb".to_string(), Value::from(virt));
            }
            if let Some((f9, f11)) = page_faults(pid) {
                obj.insert("page_faults_major".to_string(), Value::from(f9));
                obj.insert("page_faults_minor".to_string(), Value::from(f11));
            }
        }

        Ok(Some(json!({ "rigsignal": { "memory": mem } })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn collect_emits_total_mb_from_memtotal() {
        let expected_total_mb = read_meminfo()
            .get("MemTotal")
            .copied()
            .expect("MemTotal should be present in /proc/meminfo")
            / 1024;

        let mut collector = MemoryCollector::new(None);
        let doc = collector
            .collect()
            .expect("memory collection should succeed")
            .expect("memory collection should return a document");

        let total_mb = doc["rigsignal"]["memory"]["total_mb"]
            .as_u64()
            .expect("rigsignal.memory.total_mb should be a u64");

        assert!(total_mb > 0);
        assert_eq!(total_mb, expected_total_mb);
    }
}
