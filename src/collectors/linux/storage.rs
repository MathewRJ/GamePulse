/// Storage collector — mirrors collector/gamepulse/collectors/storage.py exactly.
///
/// Reads /proc/diskstats as a delta between two snapshots. First call returns
/// None (no delta yet). Subsequent calls return one sample per tick.
///
/// Device selection: Steam library path → longest /proc/mounts prefix → /dev/
/// block device. Fallback: first non-virtual device in /proc/diskstats.
///
/// Output fields (gamepulse.storage.*):
///   read_mbps             f64  — read throughput in MB/s (2 dp)
///   write_mbps            f64  — write throughput in MB/s (2 dp)
///   read_iops             i64  — read I/Os per second (truncated)
///   write_iops            i64  — write I/Os per second (truncated)
///   io_latency_read_us    obj  — {"avg": i64} average read latency in µs
///   io_latency_write_us   obj  — {"avg": i64} average write latency in µs
///   queue_depth_current   i64  — I/Os currently in progress (instantaneous)
///   io_wait_pct           f64  — % of window the drive was busy (1 dp, capped 100)
///   merged_reads          i64  — merged read ops per second (truncated)
///   merged_writes         i64  — merged write ops per second (truncated)
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use std::time::Instant;

// ── /proc/diskstats field indices (0-based after the 3 device fields) ────────

const F_READ_IOS: usize = 0;
const F_READ_MERGES: usize = 1;
const F_READ_SECTORS: usize = 2;
const F_READ_TICKS: usize = 3;
const F_WRITE_IOS: usize = 4;
const F_WRITE_MERGES: usize = 5;
const F_WRITE_SECTORS: usize = 6;
const F_WRITE_TICKS: usize = 7;
const F_IO_IN_PROGRESS: usize = 8;
const F_IO_TICKS: usize = 9;

const SECTOR_BYTES: f64 = 512.0;

// ── Ordered diskstats snapshot (insertion order = /proc/diskstats order) ─────

/// (device_name, stats_vec) in file order so fallback candidate matches Python.
type DiskStats = Vec<(String, Vec<i64>)>;

fn parse_diskstats() -> DiskStats {
    let mut result = DiskStats::new();
    if let Ok(text) = std::fs::read_to_string("/proc/diskstats") {
        for line in text.lines() {
            let parts: Vec<&str> = line.split_ascii_whitespace().collect();
            if parts.len() < 14 {
                continue;
            }
            let dev = parts[2].to_string();
            let stats: Vec<i64> = parts[3..].iter()
                .map(|s| s.parse().unwrap_or(0))
                .collect();
            result.push((dev, stats));
        }
    }
    result
}

fn get_stats<'a>(ds: &'a DiskStats, dev: &str) -> Option<&'a Vec<i64>> {
    ds.iter().find(|(d, _)| d == dev).map(|(_, s)| s)
}

fn is_virtual(dev: &str) -> bool {
    dev.starts_with("loop")
        || dev.starts_with("ram")
        || dev.starts_with("dm")
        || dev.starts_with("zram")
}

fn first_real_device(ds: &DiskStats) -> Option<&str> {
    ds.iter()
        .find(|(d, _)| !is_virtual(d))
        .map(|(d, _)| d.as_str())
}

// ── Game device detection ─────────────────────────────────────────────────────

fn find_game_device() -> Option<String> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
    let steam_paths = [
        format!("{home}/.steam/steam/steamapps"),
        format!("{home}/.local/share/Steam/steamapps"),
        "/run/media".to_string(),
    ];

    for sp in &steam_paths {
        if !std::path::Path::new(sp).exists() {
            continue;
        }
        if let Ok(mounts) = std::fs::read_to_string("/proc/mounts") {
            let mut best_dev: Option<String> = None;
            let mut best_len = 0usize;
            for line in mounts.lines() {
                let parts: Vec<&str> = line.split_ascii_whitespace().collect();
                if parts.len() < 2 {
                    continue;
                }
                let mp = parts[1];
                let dev = parts[0];
                if sp.starts_with(mp) && mp.len() > best_len {
                    best_dev = Some(dev.to_string());
                    best_len = mp.len();
                }
            }
            if let Some(dev) = best_dev {
                if dev.starts_with("/dev/") {
                    if let Some(name) = std::path::Path::new(&dev)
                        .file_name()
                        .and_then(|n| n.to_str())
                    {
                        return Some(name.to_string());
                    }
                }
            }
        }
    }

    // Fallback: first non-virtual device in /proc/diskstats
    if let Ok(text) = std::fs::read_to_string("/proc/diskstats") {
        for line in text.lines() {
            let parts: Vec<&str> = line.split_ascii_whitespace().collect();
            if parts.len() > 2 {
                let dev = parts[2];
                if !is_virtual(dev) {
                    return Some(dev.to_string());
                }
            }
        }
    }
    None
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct StorageCollector {
    prev: Option<DiskStats>,
    prev_time: Option<Instant>,
    device: Option<String>,
}

impl StorageCollector {
    pub fn new(_game_pid: Option<u32>) -> Self {
        StorageCollector {
            prev: None,
            prev_time: None,
            device: find_game_device(),
        }
    }
}

impl Collector for StorageCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.storage"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let now = Instant::now();
        let current = parse_diskstats();

        let prev = match self.prev.take() {
            None => {
                self.prev = Some(current);
                self.prev_time = Some(now);
                return Ok(None); // need two snapshots for a delta
            }
            Some(p) => p,
        };

        let prev_time = self.prev_time.take().unwrap();
        let dt = now.duration_since(prev_time).as_secs_f64();

        if dt <= 0.0 {
            self.prev = Some(current);
            self.prev_time = Some(now);
            return Ok(None);
        }

        // Prefer the detected game device; fall back to first real device.
        let mut dev = self.device.clone();
        if dev.as_deref().map(|d| get_stats(&current, d).is_none()).unwrap_or(true) {
            dev = first_real_device(&current).map(|s| s.to_string());
        }

        let dev = match dev {
            Some(d) if get_stats(&prev, &d).is_some() => d,
            _ => {
                self.prev = Some(current);
                self.prev_time = Some(now);
                return Ok(None);
            }
        };

        let cur = get_stats(&current, &dev).unwrap();
        let prv = get_stats(&prev, &dev).unwrap();

        let get = |v: &Vec<i64>, idx: usize| -> i64 { v.get(idx).copied().unwrap_or(0) };

        let d_read_ios = get(cur, F_READ_IOS) - get(prv, F_READ_IOS);
        let d_read_sectors = get(cur, F_READ_SECTORS) - get(prv, F_READ_SECTORS);
        let d_read_ticks = get(cur, F_READ_TICKS) - get(prv, F_READ_TICKS);
        let d_write_ios = get(cur, F_WRITE_IOS) - get(prv, F_WRITE_IOS);
        let d_write_sectors = get(cur, F_WRITE_SECTORS) - get(prv, F_WRITE_SECTORS);
        let d_write_ticks = get(cur, F_WRITE_TICKS) - get(prv, F_WRITE_TICKS);
        let d_io_ticks = get(cur, F_IO_TICKS) - get(prv, F_IO_TICKS);
        let d_read_merges = get(cur, F_READ_MERGES) - get(prv, F_READ_MERGES);
        let d_write_merges = get(cur, F_WRITE_MERGES) - get(prv, F_WRITE_MERGES);

        let read_mbps = ((d_read_sectors as f64 * SECTOR_BYTES / dt / 1_048_576.0) * 100.0).round() / 100.0;
        let write_mbps = ((d_write_sectors as f64 * SECTOR_BYTES / dt / 1_048_576.0) * 100.0).round() / 100.0;
        let read_iops = (d_read_ios as f64 / dt) as i64;
        let write_iops = (d_write_ios as f64 / dt) as i64;

        // int(d_read_ticks * 1000 / d_read_ios) matches Python's int(float) truncation
        let read_lat_us = if d_read_ios > 0 { d_read_ticks * 1000 / d_read_ios } else { 0 };
        let write_lat_us = if d_write_ios > 0 { d_write_ticks * 1000 / d_write_ios } else { 0 };

        let io_wait_pct =
            (((d_io_ticks as f64 / (dt * 10.0)).min(100.0)) * 10.0).round() / 10.0;

        let queue_depth = get(cur, F_IO_IN_PROGRESS);
        let merged_reads = (d_read_merges as f64 / dt) as i64;
        let merged_writes = (d_write_merges as f64 / dt) as i64;

        self.prev = Some(current);
        self.prev_time = Some(now);

        Ok(Some(json!({
            "gamepulse": {
                "storage": {
                    "read_mbps": read_mbps,
                    "write_mbps": write_mbps,
                    "read_iops": read_iops,
                    "write_iops": write_iops,
                    "io_latency_read_us": { "avg": read_lat_us },
                    "io_latency_write_us": { "avg": write_lat_us },
                    "queue_depth_current": queue_depth,
                    "io_wait_pct": io_wait_pct,
                    "merged_reads": merged_reads,
                    "merged_writes": merged_writes,
                }
            }
        })))
    }
}
