/// In-memory aggregator — accumulates raw SchedEvents from the ring buffer
/// and computes 1-second snapshots ready for Elasticsearch.
use crate::es_model::{
    BlockIoSnapshot, DataStream, EbpfDocument, EbpfMetricDoc, EbpfPayload, EbpfThreadDoc,
    FutexSnapshot, GpuFenceSnapshot, GpuSchedSnapshot, GpuSubmitSnapshot, HostFields,
    IrqKindSnapshot, IrqSnapshot, LatencyHistogram, MemSnapshot, MigrationSnapshot, OsFields,
    RigSignalFields, RigSignalThreadFields, RunqueueSnapshot, SessionRef, StutterCorrelation,
    ThreadMetric, VfsOpSnapshot, VfsSnapshot,
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

struct ThreadStat {
    comm: String,
    tid: u32,
    runqueue_min_us: f64,
    runqueue_max_us: f64,
    runqueue_avg_us: f64,
    switch_count: u32,
    migration_count: u32,
}

/// Aggregates SchedEvents over a 1-second window, then produces aggregate and per-thread docs.
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

    /// Consume all buffered events and produce documents for the current second.
    /// Returns an empty vec if no events were observed (no game activity this second).
    pub fn flush(&mut self, session_id: &str) -> Vec<EbpfDocument> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return Vec::new();
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
                runqueue_min_us: if t.min_wait_ns == u64::MAX {
                    0.0
                } else {
                    t.min_wait_ns as f64 / 1000.0
                },
                runqueue_max_us: t.max_wait_ns as f64 / 1000.0,
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

        let timestamp = Utc::now();
        let mut docs = Vec::with_capacity(1 + thread_breakdown.len());

        docs.push(
            EbpfMetricDoc {
                timestamp,
                data_stream: DataStream::default(),
                host: HostFields {
                    name: self.host_name.clone(),
                    os: OsFields {
                        kernel: self.kernel_version.clone(),
                    },
                },
                rigsignal: RigSignalFields {
                    session: SessionRef {
                        id: session_id.to_string(),
                    },
                    ebpf: EbpfPayload {
                        probe: "schedlatency",
                        bio: None,
                        gpu_sched: None,
                        mem: None,
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
                        stutter: None,
                        futex: None,
                        irq: None,
                        vfs: None,
                        gpu_fence: None,
                        gpu_submit: None,
                    },
                },
            }
            .into(),
        );

        docs.extend(
            thread_breakdown
                .into_iter()
                .enumerate()
                .map(|(idx, thread)| {
                    EbpfThreadDoc {
                        timestamp,
                        data_stream: DataStream::ebpf_thread(),
                        host: HostFields {
                            name: self.host_name.clone(),
                            os: OsFields {
                                kernel: self.kernel_version.clone(),
                            },
                        },
                        rigsignal: RigSignalThreadFields {
                            session: SessionRef {
                                id: session_id.to_string(),
                            },
                            ebpf_thread: ThreadMetric {
                                probe: "schedlatency",
                                rank: (idx + 1) as u8,
                                comm: thread.comm,
                                tid: thread.tid,
                                runqueue_min_us: thread.runqueue_min_us,
                                runqueue_max_us: thread.runqueue_max_us,
                                runqueue_avg_us: thread.runqueue_avg_us,
                                switch_count: thread.switch_count,
                                migration_count: thread.migration_count,
                            },
                        },
                    }
                    .into()
                }),
        );

        docs
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
        let l3_path = entry.path().join("cache/index3/shared_cpu_list");
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

// ---------------------------------------------------------------------------
// Bio aggregator
// ---------------------------------------------------------------------------

/// Raw bio event as read from the BIO_EVENTS ring buffer (mirrors BioEvent in BPF).
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawBioEvent {
    pub latency_ns: u64,
    pub bytes: u32,
    pub _pad: u32,
}

/// Aggregates BioEvents over a 1-second window, then produces an EbpfMetricDoc.
pub struct BioAggregator {
    events: Vec<RawBioEvent>,
    host_name: String,
    kernel_version: String,
}

impl BioAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        BioAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, event: RawBioEvent) {
        self.events.push(event);
    }

    /// Consume buffered events and produce a document for the current second.
    /// Returns None if no I/O was observed this interval.
    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut histogram = LatencyHistogram::new();
        let mut total_latency_ns: u64 = 0;
        let mut min_latency_ns: u64 = u64::MAX;
        let mut max_latency_ns: u64 = 0;
        let mut bytes_total: u64 = 0;
        let event_count = events.len() as u64;

        for ev in &events {
            histogram.record_ns(ev.latency_ns);
            total_latency_ns = total_latency_ns.saturating_add(ev.latency_ns);
            min_latency_ns = min_latency_ns.min(ev.latency_ns);
            max_latency_ns = max_latency_ns.max(ev.latency_ns);
            bytes_total = bytes_total.saturating_add(ev.bytes as u64);
        }

        let avg_latency_us = (total_latency_ns as f64 / 1000.0) / event_count as f64;

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "bio",
                    runqueue: None,
                    migration: None,
                    gpu_sched: None,
                    mem: None,
                    bio: Some(BlockIoSnapshot {
                        latency_histogram: histogram,
                        latency_min_us: if min_latency_ns == u64::MAX {
                            0.0
                        } else {
                            min_latency_ns as f64 / 1000.0
                        },
                        latency_max_us: max_latency_ns as f64 / 1000.0,
                        latency_avg_us: avg_latency_us,
                        event_count,
                        bytes_total,
                    }),
                    stutter: None,
                    futex: None,
                    irq: None,
                    vfs: None,
                    gpu_fence: None,
                    gpu_submit: None,
                },
            },
        })
    }
}

// ---------------------------------------------------------------------------
// GPU scheduler aggregator
// ---------------------------------------------------------------------------

/// Raw GPU scheduler event from GPU_SCHED_EVENTS ring buffer.
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawGpuSchedEvent {
    pub latency_ns: u64,
    pub _pad: u64,
}

/// Aggregates GpuSchedEvents over a 1-second window.
pub struct GpuAggregator {
    events: Vec<RawGpuSchedEvent>,
    host_name: String,
    kernel_version: String,
}

impl GpuAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        GpuAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, event: RawGpuSchedEvent) {
        self.events.push(event);
    }

    /// Returns None if no GPU jobs were observed this interval.
    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut histogram = LatencyHistogram::new();
        let mut total_ns: u64 = 0;
        let mut min_ns: u64 = u64::MAX;
        let mut max_ns: u64 = 0;
        let event_count = events.len() as u64;

        for ev in &events {
            histogram.record_ns(ev.latency_ns);
            total_ns = total_ns.saturating_add(ev.latency_ns);
            min_ns = min_ns.min(ev.latency_ns);
            max_ns = max_ns.max(ev.latency_ns);
        }

        let avg_us = (total_ns as f64 / 1000.0) / event_count as f64;

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "gpu_sched",
                    runqueue: None,
                    migration: None,
                    bio: None,
                    gpu_sched: Some(GpuSchedSnapshot {
                        latency_histogram: histogram,
                        latency_min_us: if min_ns == u64::MAX {
                            0.0
                        } else {
                            min_ns as f64 / 1000.0
                        },
                        latency_max_us: max_ns as f64 / 1000.0,
                        latency_avg_us: avg_us,
                        event_count,
                    }),
                    mem: None,
                    stutter: None,
                    futex: None,
                    irq: None,
                    vfs: None,
                    gpu_fence: None,
                    gpu_submit: None,
                },
            },
        })
    }
}

#[cfg(test)]
mod gpu_sched_tests {
    use super::{GpuAggregator, RawGpuSchedEvent};

    #[test]
    fn emits_one_snapshot_for_every_second_of_a_1000_event_stream() {
        let mut aggregator = GpuAggregator::new("host".to_string(), "kernel".to_string());

        for _second in 0..5 {
            for _event in 0..1_000 {
                aggregator.push(RawGpuSchedEvent {
                    latency_ns: 7_000,
                    _pad: 0,
                });
            }
            let doc = aggregator.flush("session").expect("second has events");
            assert_eq!(
                doc.rigsignal.ebpf.gpu_sched.unwrap().event_count,
                1_000,
                "each synthetic second must retain its own complete event set"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Memory pressure aggregator
// ---------------------------------------------------------------------------

pub const MEM_FAULT: u8 = 0;
pub const MEM_RECLAIM: u8 = 1;

/// Raw mem event as read from the MEM_EVENTS ring buffer.
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawMemEvent {
    pub event_type: u8,
    pub is_write: u8,
    pub _pad: [u8; 6],
}

/// Aggregates MemEvents over a 1-second window.
pub struct MemAggregator {
    events: Vec<RawMemEvent>,
    host_name: String,
    kernel_version: String,
}

impl MemAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        MemAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, event: RawMemEvent) {
        self.events.push(event);
    }

    /// Returns None if no memory events were observed this interval.
    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut page_fault_count: u64 = 0;
        let mut page_fault_write: u64 = 0;
        let mut direct_reclaim_count: u64 = 0;

        for ev in &events {
            match ev.event_type {
                MEM_FAULT => {
                    page_fault_count += 1;
                    if ev.is_write != 0 {
                        page_fault_write += 1;
                    }
                }
                MEM_RECLAIM => {
                    direct_reclaim_count += 1;
                }
                _ => {}
            }
        }

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "mem",
                    runqueue: None,
                    migration: None,
                    bio: None,
                    gpu_sched: None,
                    mem: Some(MemSnapshot {
                        page_fault_count,
                        page_fault_write,
                        direct_reclaim_count,
                    }),
                    stutter: None,
                    futex: None,
                    irq: None,
                    vfs: None,
                    gpu_fence: None,
                    gpu_submit: None,
                },
            },
        })
    }
}

// ---------------------------------------------------------------------------
// Stutter correlator
// ---------------------------------------------------------------------------

/// Latency threshold above which a probe is considered to have "spiked" (μs).
/// 16 ms = one dropped frame at 60 fps.
const SPIKE_THRESHOLD_US: f64 = 16_000.0;

/// Inspect a batch of docs produced in the same 1-second window and emit a
/// `stutter_correlation` doc if ≥2 subsystems show simultaneous spikes.
///
/// `docs` should be the full slice returned by all probes in one tick.
pub fn correlate(
    docs: &[&EbpfMetricDoc],
    host_name: &str,
    kernel_version: &str,
    session_id: &str,
) -> Option<EbpfMetricDoc> {
    let mut sched_max_us: f64 = 0.0;
    let mut bio_max_us: f64 = 0.0;
    let mut gpu_sched_max_us: f64 = 0.0;
    let mut mem_pressure = false;
    let mut contributing: Vec<String> = Vec::new();

    for doc in docs {
        let p = &doc.rigsignal.ebpf;
        if let Some(rq) = &p.runqueue {
            sched_max_us = rq.latency_max_us;
            if rq.latency_max_us > SPIKE_THRESHOLD_US {
                contributing.push("schedlatency".to_string());
            }
        }
        if let Some(bio) = &p.bio {
            bio_max_us = bio.latency_max_us;
            if bio.latency_max_us > SPIKE_THRESHOLD_US {
                contributing.push("bio".to_string());
            }
        }
        if let Some(gpu) = &p.gpu_sched {
            gpu_sched_max_us = gpu.latency_max_us;
            if gpu.latency_max_us > SPIKE_THRESHOLD_US {
                contributing.push("gpu_sched".to_string());
            }
        }
        if let Some(mem) = &p.mem {
            if mem.direct_reclaim_count > 0 {
                mem_pressure = true;
                contributing.push("mem".to_string());
            }
        }
    }

    if contributing.len() < 2 {
        return None;
    }

    let severity_score = contributing.len() as u8;

    Some(EbpfMetricDoc {
        timestamp: chrono::Utc::now(),
        data_stream: DataStream::default(),
        host: HostFields {
            name: host_name.to_string(),
            os: OsFields {
                kernel: kernel_version.to_string(),
            },
        },
        rigsignal: RigSignalFields {
            session: SessionRef {
                id: session_id.to_string(),
            },
            ebpf: EbpfPayload {
                probe: "stutter_correlation",
                runqueue: None,
                migration: None,
                bio: None,
                gpu_sched: None,
                mem: None,
                stutter: Some(StutterCorrelation {
                    contributing_probes: contributing,
                    sched_max_us,
                    bio_max_us,
                    gpu_sched_max_us,
                    mem_pressure,
                    severity_score,
                }),
                futex: None,
                irq: None,
                vfs: None,
                gpu_fence: None,
                gpu_submit: None,
            },
        },
    })
}

// ---------------------------------------------------------------------------
// Futex aggregator
// ---------------------------------------------------------------------------

/// Raw futex event as read from the FUTEX_EVENTS ring buffer.
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawFutexEvent {
    pub latency_ns: u64,
    pub _pad: u64,
}

/// Aggregates FutexEvents over a 1-second window.
pub struct FutexAggregator {
    events: Vec<RawFutexEvent>,
    host_name: String,
    kernel_version: String,
}

impl FutexAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        FutexAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, event: RawFutexEvent) {
        self.events.push(event);
    }

    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut histogram = LatencyHistogram::new();
        let mut total_ns: u64 = 0;
        let mut min_ns: u64 = u64::MAX;
        let mut max_ns: u64 = 0;
        let mut contended_count: u64 = 0;
        let event_count = events.len() as u64;
        const CONTENDED_NS: u64 = 1_000_000; // 1 ms

        for ev in &events {
            histogram.record_ns(ev.latency_ns);
            total_ns = total_ns.saturating_add(ev.latency_ns);
            min_ns = min_ns.min(ev.latency_ns);
            max_ns = max_ns.max(ev.latency_ns);
            if ev.latency_ns > CONTENDED_NS {
                contended_count += 1;
            }
        }

        let avg_us = (total_ns as f64 / 1000.0) / event_count as f64;

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "futex",
                    runqueue: None,
                    migration: None,
                    bio: None,
                    gpu_sched: None,
                    mem: None,
                    stutter: None,
                    irq: None,
                    vfs: None,
                    gpu_fence: None,
                    gpu_submit: None,
                    futex: Some(FutexSnapshot {
                        latency_histogram: histogram,
                        latency_min_us: if min_ns == u64::MAX {
                            0.0
                        } else {
                            min_ns as f64 / 1000.0
                        },
                        latency_max_us: max_ns as f64 / 1000.0,
                        latency_avg_us: avg_us,
                        event_count,
                        contended_count,
                    }),
                },
            },
        })
    }
}

// ---------------------------------------------------------------------------
// IRQ aggregator
// ---------------------------------------------------------------------------

pub const IRQ_KIND_HARD: u8 = 0;
pub const IRQ_KIND_SOFT: u8 = 1;

/// Raw IRQ event as read from the IRQ_EVENTS ring buffer.
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawIrqEvent {
    pub latency_ns: u64,
    pub kind: u8,
    pub _pad: [u8; 7],
}

/// Aggregates IrqEvents over a 1-second window.
pub struct IrqAggregator {
    events: Vec<RawIrqEvent>,
    host_name: String,
    kernel_version: String,
}

impl IrqAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        IrqAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, event: RawIrqEvent) {
        self.events.push(event);
    }

    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut hard_hist = LatencyHistogram::new();
        let mut hard_total_ns: u64 = 0;
        let mut hard_count: u64 = 0;

        let mut soft_hist = LatencyHistogram::new();
        let mut soft_total_ns: u64 = 0;
        let mut soft_count: u64 = 0;

        for ev in &events {
            match ev.kind {
                IRQ_KIND_HARD => {
                    hard_hist.record_ns(ev.latency_ns);
                    hard_total_ns = hard_total_ns.saturating_add(ev.latency_ns);
                    hard_count += 1;
                }
                IRQ_KIND_SOFT => {
                    soft_hist.record_ns(ev.latency_ns);
                    soft_total_ns = soft_total_ns.saturating_add(ev.latency_ns);
                    soft_count += 1;
                }
                _ => {}
            }
        }

        let hard_avg_us = if hard_count > 0 {
            (hard_total_ns as f64 / 1000.0) / hard_count as f64
        } else {
            0.0
        };
        let soft_avg_us = if soft_count > 0 {
            (soft_total_ns as f64 / 1000.0) / soft_count as f64
        } else {
            0.0
        };

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "irq",
                    runqueue: None,
                    migration: None,
                    bio: None,
                    gpu_sched: None,
                    mem: None,
                    stutter: None,
                    futex: None,
                    vfs: None,
                    gpu_fence: None,
                    gpu_submit: None,
                    irq: Some(IrqSnapshot {
                        hard_irq: IrqKindSnapshot {
                            latency_histogram: hard_hist,
                            latency_avg_us: hard_avg_us,
                            event_count: hard_count,
                        },
                        softirq: IrqKindSnapshot {
                            latency_histogram: soft_hist,
                            latency_avg_us: soft_avg_us,
                            event_count: soft_count,
                        },
                    }),
                },
            },
        })
    }
}

// ---------------------------------------------------------------------------
// VFS aggregator
// ---------------------------------------------------------------------------

pub const VFS_OP_READ: u8 = 0;
pub const VFS_OP_WRITE: u8 = 1;

/// Raw VFS event as read from the VFS_EVENTS ring buffer.
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawVfsEvent {
    pub latency_ns: u64,
    pub op: u8,
    pub _pad: [u8; 7],
}

/// Aggregates VfsEvents over a 1-second window.
pub struct VfsAggregator {
    events: Vec<RawVfsEvent>,
    host_name: String,
    kernel_version: String,
}

impl VfsAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        VfsAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, event: RawVfsEvent) {
        self.events.push(event);
    }

    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut read_hist = LatencyHistogram::new();
        let mut read_total_ns: u64 = 0;
        let mut read_count: u64 = 0;

        let mut write_hist = LatencyHistogram::new();
        let mut write_total_ns: u64 = 0;
        let mut write_count: u64 = 0;

        for ev in &events {
            match ev.op {
                VFS_OP_READ => {
                    read_hist.record_ns(ev.latency_ns);
                    read_total_ns = read_total_ns.saturating_add(ev.latency_ns);
                    read_count += 1;
                }
                VFS_OP_WRITE => {
                    write_hist.record_ns(ev.latency_ns);
                    write_total_ns = write_total_ns.saturating_add(ev.latency_ns);
                    write_count += 1;
                }
                _ => {}
            }
        }

        let read_avg_us = if read_count > 0 {
            (read_total_ns as f64 / 1000.0) / read_count as f64
        } else {
            0.0
        };
        let write_avg_us = if write_count > 0 {
            (write_total_ns as f64 / 1000.0) / write_count as f64
        } else {
            0.0
        };

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "vfs",
                    runqueue: None,
                    migration: None,
                    bio: None,
                    gpu_sched: None,
                    mem: None,
                    stutter: None,
                    futex: None,
                    irq: None,
                    gpu_fence: None,
                    gpu_submit: None,
                    vfs: Some(VfsSnapshot {
                        read: VfsOpSnapshot {
                            latency_histogram: read_hist,
                            latency_avg_us: read_avg_us,
                            event_count: read_count,
                            bytes_total: 0,
                        },
                        write: VfsOpSnapshot {
                            latency_histogram: write_hist,
                            latency_avg_us: write_avg_us,
                            event_count: write_count,
                            bytes_total: 0,
                        },
                    }),
                },
            },
        })
    }
}

// ---------------------------------------------------------------------------
// GPU fence aggregator
// ---------------------------------------------------------------------------

/// Raw GPU fence event as read from the GPU_FENCE_EVENTS ring buffer.
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawGpuFenceEvent {
    pub latency_ns: u64,
    pub _pad: u64,
}

/// Aggregates GpuFenceEvents over a 1-second window.
pub struct GpuFenceAggregator {
    events: Vec<RawGpuFenceEvent>,
    host_name: String,
    kernel_version: String,
}

impl GpuFenceAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        GpuFenceAggregator {
            events: Vec::new(),
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, event: RawGpuFenceEvent) {
        self.events.push(event);
    }

    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let events = std::mem::take(&mut self.events);
        if events.is_empty() {
            return None;
        }

        let mut histogram = LatencyHistogram::new();
        let mut total_ns: u64 = 0;
        let mut min_ns: u64 = u64::MAX;
        let mut max_ns: u64 = 0;
        let mut blocked_count: u64 = 0;
        let event_count = events.len() as u64;
        const BLOCKED_NS: u64 = 1_000_000; // 1 ms

        for ev in &events {
            histogram.record_ns(ev.latency_ns);
            total_ns = total_ns.saturating_add(ev.latency_ns);
            min_ns = min_ns.min(ev.latency_ns);
            max_ns = max_ns.max(ev.latency_ns);
            if ev.latency_ns > BLOCKED_NS {
                blocked_count += 1;
            }
        }

        let avg_us = (total_ns as f64 / 1000.0) / event_count as f64;

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "gpu_fence",
                    runqueue: None,
                    migration: None,
                    bio: None,
                    gpu_sched: None,
                    mem: None,
                    stutter: None,
                    futex: None,
                    irq: None,
                    vfs: None,
                    gpu_submit: None,
                    gpu_fence: Some(GpuFenceSnapshot {
                        latency_histogram: histogram,
                        latency_min_us: if min_ns == u64::MAX {
                            0.0
                        } else {
                            min_ns as f64 / 1000.0
                        },
                        latency_max_us: max_ns as f64 / 1000.0,
                        latency_avg_us: avg_us,
                        event_count,
                        blocked_count,
                    }),
                },
            },
        })
    }
}

// ---------------------------------------------------------------------------
// GPU submit aggregator
// ---------------------------------------------------------------------------

/// Raw GPU submit event as read from the GPU_SUBMIT_EVENTS ring buffer.
#[repr(C)]
#[derive(Debug, Clone)]
pub struct RawGpuSubmitEvent {
    pub timestamp_ns: u64,
    pub _pad: u64,
}

/// Aggregates GpuSubmitEvents over a 1-second window (count only).
pub struct GpuSubmitAggregator {
    count: u64,
    host_name: String,
    kernel_version: String,
}

impl GpuSubmitAggregator {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        GpuSubmitAggregator {
            count: 0,
            host_name,
            kernel_version,
        }
    }

    pub fn push(&mut self, _event: RawGpuSubmitEvent) {
        self.count += 1;
    }

    pub fn flush(&mut self, session_id: &str) -> Option<EbpfMetricDoc> {
        let count = self.count;
        self.count = 0;
        if count == 0 {
            return None;
        }

        Some(EbpfMetricDoc {
            timestamp: chrono::Utc::now(),
            data_stream: DataStream::default(),
            host: HostFields {
                name: self.host_name.clone(),
                os: OsFields {
                    kernel: self.kernel_version.clone(),
                },
            },
            rigsignal: RigSignalFields {
                session: SessionRef {
                    id: session_id.to_string(),
                },
                ebpf: EbpfPayload {
                    probe: "gpu_submit",
                    runqueue: None,
                    migration: None,
                    bio: None,
                    gpu_sched: None,
                    mem: None,
                    stutter: None,
                    futex: None,
                    irq: None,
                    vfs: None,
                    gpu_fence: None,
                    gpu_submit: Some(GpuSubmitSnapshot { event_count: count }),
                },
            },
        })
    }
}
