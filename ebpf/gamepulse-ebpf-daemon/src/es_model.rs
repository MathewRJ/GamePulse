/// Elasticsearch document structs for the GamePulse eBPF data streams.
///
/// Aggregate probe snapshots ship to `metrics-gamepulse.ebpf-default`.
/// Per-thread scheduler rows ship to `metrics-gamepulse.ebpf_thread-default`.
use chrono::{DateTime, Utc};
use serde::Serialize;

/// Top-level aggregate document sent to metrics-gamepulse.ebpf-default.
#[derive(Debug, Serialize)]
pub struct EbpfMetricDoc {
    #[serde(rename = "@timestamp")]
    pub timestamp: DateTime<Utc>,

    pub data_stream: DataStream,

    pub gamepulse: GamePulseFields,

    /// Host info — populated once at startup from /etc/hostname and uname.
    pub host: HostFields,
}

/// Top-level per-thread scheduler document sent to metrics-gamepulse.ebpf_thread-default.
#[derive(Debug, Serialize)]
pub struct EbpfThreadDoc {
    #[serde(rename = "@timestamp")]
    pub timestamp: DateTime<Utc>,

    pub data_stream: DataStream,

    pub gamepulse: GamePulseThreadFields,

    pub host: HostFields,
}

#[derive(Debug)]
pub enum EbpfDocument {
    Metric(EbpfMetricDoc),
    Thread(EbpfThreadDoc),
}

impl EbpfDocument {
    pub fn index(&self) -> &'static str {
        match self {
            EbpfDocument::Metric(_) => "metrics-gamepulse.ebpf-default",
            EbpfDocument::Thread(_) => "metrics-gamepulse.ebpf_thread-default",
        }
    }

    pub fn as_metric(&self) -> Option<&EbpfMetricDoc> {
        match self {
            EbpfDocument::Metric(doc) => Some(doc),
            EbpfDocument::Thread(_) => None,
        }
    }
}

impl From<EbpfMetricDoc> for EbpfDocument {
    fn from(doc: EbpfMetricDoc) -> Self {
        EbpfDocument::Metric(doc)
    }
}

impl From<EbpfThreadDoc> for EbpfDocument {
    fn from(doc: EbpfThreadDoc) -> Self {
        EbpfDocument::Thread(doc)
    }
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

impl DataStream {
    pub fn ebpf_thread() -> Self {
        DataStream {
            ds_type: "metrics",
            dataset: "gamepulse.ebpf_thread",
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
pub struct GamePulseThreadFields {
    pub session: SessionRef,
    pub ebpf_thread: ThreadMetric,
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
    pub bio: Option<BlockIoSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpu_sched: Option<GpuSchedSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub mem: Option<MemSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub stutter: Option<StutterCorrelation>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub futex: Option<FutexSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub irq: Option<IrqSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub vfs: Option<VfsSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpu_fence: Option<GpuFenceSnapshot>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpu_submit: Option<GpuSubmitSnapshot>,
}

/// Cross-probe stutter correlation — emitted when ≥2 subsystems spike in the same window.
#[derive(Debug, Serialize)]
pub struct StutterCorrelation {
    /// Which probes contributed a spike this second (e.g. ["schedlatency", "bio"]).
    pub contributing_probes: Vec<String>,

    /// Runqueue max latency this window (μs); 0 if sched probe not active.
    pub sched_max_us: f64,

    /// Block I/O max latency this window (μs); 0 if bio probe not active.
    pub bio_max_us: f64,

    /// GPU scheduling max latency this window (μs); 0 if gpu_sched probe not active.
    pub gpu_sched_max_us: f64,

    /// True if direct memory reclaim was observed this window.
    pub mem_pressure: bool,

    /// Number of contributing probes: 2 = low, 3 = medium, 4 = high.
    pub severity_score: u8,
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

/// First-class per-thread scheduler metric document payload.
#[derive(Debug, Serialize)]
pub struct ThreadMetric {
    pub probe: &'static str,
    pub rank: u8,
    pub comm: String,
    pub tid: u32,
    pub runqueue_min_us: f64,
    pub runqueue_max_us: f64,
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

/// 1-second futex contention snapshot from the futex probe.
#[derive(Debug, Serialize)]
pub struct FutexSnapshot {
    /// Log2 histogram of futex wait latencies — 16-bucket layout.
    pub latency_histogram: LatencyHistogram,

    pub latency_min_us: f64,
    pub latency_max_us: f64,
    pub latency_avg_us: f64,

    /// Total futex operations observed this second (game threads).
    pub event_count: u64,

    /// Futex operations with latency > 1ms (contended acquisitions).
    pub contended_count: u64,
}

/// Hard-IRQ and softirq latency sub-snapshot.
#[derive(Debug, Serialize)]
pub struct IrqKindSnapshot {
    /// Log2 histogram of handler latencies.
    pub latency_histogram: LatencyHistogram,

    pub latency_avg_us: f64,

    /// Number of handler invocations this second.
    pub event_count: u64,
}

/// 1-second IRQ latency snapshot from the irq probe.
#[derive(Debug, Serialize)]
pub struct IrqSnapshot {
    /// Hard-IRQ handler latency stats.
    pub hard_irq: IrqKindSnapshot,
    /// Softirq handler latency stats.
    pub softirq: IrqKindSnapshot,
}

/// Read or write VFS latency sub-snapshot.
#[derive(Debug, Serialize)]
pub struct VfsOpSnapshot {
    /// Log2 histogram of VFS operation latencies.
    pub latency_histogram: LatencyHistogram,

    pub latency_avg_us: f64,

    /// Number of operations observed this second.
    pub event_count: u64,

    /// Total bytes transferred (0 for this probe — bytes not captured at VFS level).
    pub bytes_total: u64,
}

/// 1-second VFS I/O latency snapshot from the vfs probe.
#[derive(Debug, Serialize)]
pub struct VfsSnapshot {
    pub read: VfsOpSnapshot,
    pub write: VfsOpSnapshot,
}

/// 1-second DMA fence wait snapshot from the gpu_fence probe.
#[derive(Debug, Serialize)]
pub struct GpuFenceSnapshot {
    /// Log2 histogram of fence wait latencies.
    pub latency_histogram: LatencyHistogram,

    pub latency_min_us: f64,
    pub latency_max_us: f64,
    pub latency_avg_us: f64,

    /// Number of fence waits observed this second.
    pub event_count: u64,

    /// Fence waits that blocked for more than 1ms (frame-impacting).
    pub blocked_count: u64,
}

/// 1-second GPU command submission snapshot from the gpu_submit probe.
#[derive(Debug, Serialize)]
pub struct GpuSubmitSnapshot {
    /// Number of amdgpu_cs_ioctl calls observed this second.
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
