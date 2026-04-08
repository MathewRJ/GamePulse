/// In-memory aggregator — accumulates raw SchedEvents from the ring buffer
/// and computes 1-second snapshots ready for Elasticsearch.
use crate::es_model::{
    EbpfMetricDoc, EbpfPayload, GamePulseFields, HostFields, LatencyHistogram, MigrationSnapshot,
    OsFields, RunqueueSnapshot, SessionRef, ThreadStat, DataStream,
};
use chrono::Utc;
use std::collections::HashMap;

/// Raw sched event as read from the ring buffer (mirrors the BPF SchedEvent struct).
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawSchedEvent {
    pub event_type: u8,
    pub _pad: [u8; 3],
    pub tid: u32,
    pub wait_ns: u64,
    pub prev_cpu: u32,
    pub next_cpu: u32,
    pub comm: [u8; 16],
}

pub const EVENT_SWITCH: u8 = 0;
pub const EVENT_MIGRATE: u8 = 1;

/// Per-thread stats accumulated within a 1-second window.
#[derive(Default)]
struct ThreadAccum {
    comm: String,
    switch_count: u32,
    migration_count: u32,
    total_wait_ns: u64,
    min_wait_ns: u64,
    max_wait_ns: u64,
}

/// Aggregates SchedEvents over a 1-second window, then produces an EbpfMetricDoc.
pub struct SchedAggregator {
    events: Vec<RawSchedEvent>,
    host_name: String,
    kernel_version: String,
    /// CCX topology: cpu_index → ccx_id. If empty, CCX detection not available.
    cpu_to_ccx: HashMap<u32, u32>,
}

impl SchedAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        SchedAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
            cpu_to_ccx: build_ccx_map(),
        }
    }

    /// Push a raw event from the ring buffer drain loop.
    pub fn push(&mut self, event: RawSchedEvent) {
        self.events.push(event);
    }

    /// Consume all buffered events and produce a document for the current second.
    /// Returns None if no events were observed (no game activity this second).
    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut histogram = LatencyHistogram::new();
        let mut total_wait_ns: u64 = 0;
        let mut min_wait_ns: u64 = u64::MAX;
        let mut max_wait_ns: u64 = 0;
        let mut switch_count: u64 = 0;

        let mut total_migrations: u32 = 0;
        let mut ccx_cross_count: u32 = 0;

        let mut per_thread: HashMap<u32, ThreadAccum> = HashMap::new();

        for ev in &events {
            let comm = comm_to_string(&ev.comm);

            match ev.event_type {
                EVENT_SWITCH => {
                    histogram.record_ns(ev.wait_ns);
                    total_wait_ns = total_wait_ns.saturating_add(ev.wait_ns);
                    min_wait_ns = min_wait_ns.min(ev.wait_ns);
                    max_wait_ns = max_wait_ns.max(ev.wait_ns);
                    switch_count += 1;

                    let t = per_thread.entry(ev.tid).or_insert_with(|| ThreadAccum {
                        comm: comm.clone(),
                        min_wait_ns: u64::MAX,
                        ..Default::default()
                    });
                    t.comm = comm;
                    t.switch_count += 1;
                    t.total_wait_ns = t.total_wait_ns.saturating_add(ev.wait_ns);
                    t.min_wait_ns = t.min_wait_ns.min(ev.wait_ns);
                    t.max_wait_ns = t.max_wait_ns.max(ev.wait_ns);
                }
                EVENT_MIGRATE => {
                    total_migrations += 1;
                    if is_ccx_cross(ev.prev_cpu, ev.next_cpu, &self.cpu_to_ccx) {
                        ccx_cross_count += 1;
                    }
                    per_thread
                        .entry(ev.tid)
                        .or_insert_with(|| ThreadAccum {
                            comm: comm.clone(),
                            min_wait_ns: u64::MAX,
                            ..Default::default()
                        })
                        .migration_count += 1;
                }
                _ => {}
            }
        }

        let avg_wait_us = if switch_count > 0 {
            (total_wait_ns as f64 / 1000.0) / switch_count as f64
        } else {
            0.0
        };

        // Build thread breakdown — top 8 threads by switch count.
        let mut thread_breakdown: Vec<ThreadStat> = per_thread
            .into_iter()
            .map(|(tid, t)| ThreadStat {
                comm: t.comm,
                tid,
                runqueue_avg_us: if t.switch_count > 0 {
                    (t.total_wait_ns as f64 / 1000.0) / t.switch_count as f64
                } else {
                    0.0
                },
                switch_count: t.switch_count,
                migration_count: t.migration_count,
            })
            .collect();
        thread_breakdown.sort_by(|a, b| b.switch_count.cmp(&a.switch_count));
        thread_breakdown.truncate(8);

        Some(EbpfMetricDoc {
            timestamp: Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            gamepulse: GamePulseFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "schedlatency",
                    runqueue: Some(RunqueueSnapshot {
                        latency_histogram: histogram,
                        latency_min_us: if min_wait_ns == u64::MAX {
                            0.0
                        } else {
                            min_wait_ns as f64 / 1000.0
                        },
                        latency_max_us: max_wait_ns as f64 / 1000.0,
                        latency_avg_us: avg_wait_us,
                        event_count: switch_count,
                    }),
                    migration: Some(MigrationSnapshot {
                        total_count: total_migrations,
                        ccx_cross_count,
                    }),
                    thread_breakdown: if thread_breakdown.is_empty() {
                        None
                    } else {
                        Some(thread_breakdown)
                    },
                },
            },
        })
    }
}

/// Parse a null-terminated C string from a fixed-size byte array.
fn comm_to_string(comm: &[u8; 16]) -> String {
    let end = comm.iter().position(|&b| b == 0).unwrap_or(16);
    String::from_utf8_lossy(&comm[..end]).into_owned()
}

fn is_ccx_cross(prev_cpu: u32, next_cpu: u32, map: &HashMap<u32, u32>) -> bool {
    if map.is_empty() {
        return false;
    }
    match (map.get(&prev_cpu), map.get(&next_cpu)) {
        (Some(prev_ccx), Some(next_ccx)) => prev_ccx != next_ccx,
        _ => false,
    }
}

/// Build a cpu→ccx mapping by reading /sys/devices/system/cpu/cpu*/topology/core_cpus_list.
/// Returns an empty map if the topology is not available or is a single CCX.
fn build_ccx_map() -> HashMap<u32, u32> {
    let mut map = HashMap::new();
    let mut ccx_id = 0u32;

    // Walk all CPUs and read their shared_cpu_list (all CPUs sharing the same L3 = same CCX).
    // Assign the same ccx_id to CPUs with the same shared_cpu_list string.
    let mut seen_lists: HashMap<String, u32> = HashMap::new();

    let cpu_dir = std::path::Path::new("/sys/devices/system/cpu");
    let entries = match std::fs::read_dir(cpu_dir) {
        Ok(e) => e,
        Err(_) => return map,
    };

    for entry in entries.flatten() {
        let name = entry.file_name();
        let name_str = name.to_string_lossy();
        if !name_str.starts_with("cpu") {
            continue;
        }
        let cpu_num: u32 = match name_str[3..].parse() {
            Ok(n) => n,
            Err(_) => continue,
        };

        // Try both possible paths for L3 shared CPUs
        let l3_path = entry
            .path()
            .join("cache/index3/shared_cpu_list");
        let shared_list = match std::fs::read_to_string(&l3_path) {
            Ok(s) => s.trim().to_string(),
            Err(_) => continue,
        };

        let this_ccx = *seen_lists.entry(shared_list).or_insert_with(|| {
            let id = ccx_id;
            ccx_id += 1;
            id
        });
        map.insert(cpu_num, this_ccx);
    }

    // If all CPUs share the same CCX, the map is technically correct but
    // ccx_cross_count will always be 0 (e.g. Ryzen 9800X3D — single CCX).
    map
}
