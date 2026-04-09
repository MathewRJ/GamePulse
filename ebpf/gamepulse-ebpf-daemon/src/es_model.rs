/// Elasticsearch document structs for the gamepulse.ebpf data stream.
///
/// All probes ship to `metrics-gamepulse.ebpf-default`.
/// Documents are polymorphic: common fields always present, probe-specific fields
/// populated according to `probe` discriminant.
use chrono::{DateTime, Utc};
use serde::Serialize;

/// Top-level document sent to ES bulk API.
#[derive(Debug, Serialize)]
pub struct EbpfMetricDoc {
    #[serde(rename = "@timestamp")]
    pub timestamp: DateTime<Utc>,

    pub data_stream: DataStream,

    pub gamepulse: GamePulseFields,

    /// Host info — populated once at startup from /etc/hostname and uname.
    pub host: HostFields,
}

#[derive(Debug, Serialize)]
pub struct DataStream {
    #[serde(rename = "type")]
    pub ds_type: &'static str,
    pub dataset: &'static str,
    pub namespace: &'static str,
}

impl Default for DataStream {
    fn default() -> Self {
        DataStream {
            ds_type: "metrics",
            dataset: "gamepulse.ebpf",
            namespace: "default",
        }
    }
}

#[derive(Debug, Serialize)]
pub struct HostFields {
    pub name: String,
    pub os: OsFields,
}

#[derive(Debug, Serialize)]
pub struct OsFields {
    pub kernel: String,
}

#[derive(Debug, Serialize)]
pub struct GamePulseFields {
    pub session: SessionRef,
    pub ebpf: EbpfPayload,
}

#[derive(Debug, Serialize)]
pub struct SessionRef {
    pub id: String,
}

/// Probe-specific payload — only one variant is populated per document.
#[derive(Debug, Serialize)]
pub struct EbpfPayload {
    /// Probe name discriminant (e.g. "schedlatency", "bio")
    pub probe: &'static str,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub runqueue: Option<RunqueueSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub migration: Option<MigrationSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_breakdown: Option<Vec<ThreadStat>>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub bio: Option<BlockIoSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpu_sched: Option<GpuSchedSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub mem: Option<MemSnapshot>,
}

/// 1-second runqueue latency snapshot from the schedlatency probe.
#[derive(Debug, Serialize)]
pub struct RunqueueSnapshot {
    /// Log2 histogram counts — 16 buckets covering 1μs to 33ms+.
    /// Bucket i covers [2^i μs, 2^(i+1) μs).
    pub latency_histogram: LatencyHistogram,

    pub latency_min_us: f64,
    pub latency_max_us: f64,
    pub latency_avg_us: f64,

    /// Number of scheduling events (context switches) observed this second.
    pub event_count: u64,
}

/// CPU migration snapshot from the schedlatency probe.
#[derive(Debug, Serialize)]
pub struct MigrationSnapshot {
    /// Total migrations observed this second.
    pub total_count: u32,
    /// Migrations that crossed a CCX boundary (different L3 domain).
    /// Always 0 on non-AMD or single-CCX chips (Ryzen 9800X3D is single-CCX).
    pub ccx_cross_count: u32,
}

/// Per-thread breakdown for significant threads (render, audio, etc.)
#[derive(Debug, Serialize)]
pub struct ThreadStat {
    pub comm: String,
    pub tid: u32,
    pub runqueue_avg_us: f64,
    pub switch_count: u32,
    pub migration_count: u32,
}

/// 1-second block I/O latency snapshot from the bio probe.
#[derive(Debug, Serialize)]
pub struct BlockIoSnapshot {
    /// Log2 histogram of I/O latencies — same 16-bucket layout as runqueue.
    pub latency_histogram: LatencyHistogram,

    pub latency_min_us: f64,
    pub latency_max_us: f64,
    pub latency_avg_us: f64,

    /// Number of I/O completions observed this second.
    pub event_count: u64,

    /// Total bytes transferred across all observed I/O operations.
    pub bytes_total: u64,
}

/// 1-second memory pressure snapshot from the mem probe.
#[derive(Debug, Serialize)]
pub struct MemSnapshot {
    /// Total user-space page faults observed from game threads this second.
    pub page_fault_count: u64,
    /// Write faults (COW, new allocation) — subset of page_fault_count.
    pub page_fault_write: u64,
    /// Direct reclaim events (system-wide) — game thread stalled waiting for memory.
    /// Non-zero values indicate memory pressure; high values mean stutter risk.
    pub direct_reclaim_count: u64,
}

/// 1-second GPU scheduling latency snapshot from the gpu_sched probe.
#[derive(Debug, Serialize)]
pub struct GpuSchedSnapshot {
    /// Log2 histogram of GPU job scheduling latencies — same 16-bucket layout.
    pub latency_histogram: LatencyHistogram,

    pub latency_min_us: f64,
    pub latency_max_us: f64,
    pub latency_avg_us: f64,

    /// Number of GPU jobs observed this second (across all rings: gfx, comp, sdma).
    pub event_count: u64,
}

/// Compact histogram representation — matches the ES `histogram` field type.
#[derive(Debug, Serialize)]
pub struct LatencyHistogram {
    /// Upper bound of each bucket in microseconds (16 values).
    pub values: [u64; 16],
    /// Count of observations falling into each bucket.
    pub counts: [u64; 16],
}

impl LatencyHistogram {
    /// Bucket boundaries (upper bound in μs): 1, 2, 4, 8, ..., 32768, ∞
    pub const BOUNDARIES_US: [u64; 16] = [
        1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768,
    ];

    pub fn new() -> Self {
        LatencyHistogram {
            values: Self::BOUNDARIES_US,
            counts: [0; 16],
        }
    }

    /// Add a single observation (in nanoseconds) to the histogram.
    pub fn record_ns(&mut self, wait_ns: u64) {
        let wait_us = wait_ns / 1000;
        let bucket = Self::BOUNDARIES_US
            .iter()
            .position(|&b| wait_us < b)
            .unwrap_or(15);
        self.counts[bucket] += 1;
    }

    pub fn is_empty(&self) -> bool {
        self.counts.iter().all(|&c| c == 0)
    }
}
