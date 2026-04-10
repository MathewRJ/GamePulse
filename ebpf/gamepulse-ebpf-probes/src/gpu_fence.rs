/// DMA fence wait latency BPF programs.
///
/// kprobe/kretprobe on `dma_fence_default_wait`:
///   entry  — record start timestamp keyed by pid_tgid (system-wide)
///   return — compute latency, emit GpuFenceEvent to ring buffer
///
/// dma_fence_default_wait is the DRM subsystem's primary fence blocking function.
/// When the GPU has not yet finished rendering, the driver blocks the CPU here
/// waiting for the fence to signal. High latency → frame drops.
///
/// System-wide (no PID filter): same reason as gpu_sched — under Proton/RADV,
/// the waiting thread may be a Wine server or RADV submission thread, not the
/// game process itself. System-wide fence waits during a gaming session are
/// dominated by game traffic.
///
/// Map key: pid_tgid (u64) — unique per thread.
use aya_ebpf::{
    helpers::{bpf_get_current_pid_tgid, bpf_ktime_get_ns},
    macros::{kprobe, kretprobe, map},
    maps::{HashMap, RingBuf},
    programs::{ProbeContext, RetProbeContext},
};

const RING_BUF_BYTES: u32 = 256 * 1024;

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

/// In-flight fence waits: pid_tgid → entry ktime_ns.
#[map]
static GPU_FENCE_START_TS: HashMap<u64, u64> = HashMap::with_max_entries(1024, 0);

/// Ring buffer carrying GpuFenceEvents to userspace.
#[map]
static GPU_FENCE_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event type
// ---------------------------------------------------------------------------

/// Event emitted when dma_fence_default_wait completes.
#[repr(C)]
pub struct GpuFenceEvent {
    /// Elapsed time inside dma_fence_default_wait (nanoseconds).
    pub latency_ns: u64,
    pub _pad: u64,
}

// ---------------------------------------------------------------------------
// dma_fence_default_wait entry
// ---------------------------------------------------------------------------

#[kprobe(function = "dma_fence_default_wait")]
pub fn dma_fence_default_wait_entry(_ctx: ProbeContext) -> u32 {
    let pid_tgid = unsafe { bpf_get_current_pid_tgid() };
    let ts = unsafe { bpf_ktime_get_ns() };
    let _ = GPU_FENCE_START_TS.insert(&pid_tgid, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// dma_fence_default_wait return
// ---------------------------------------------------------------------------

#[kretprobe(function = "dma_fence_default_wait")]
pub fn dma_fence_default_wait_return(_ctx: RetProbeContext) -> u32 {
    let pid_tgid = unsafe { bpf_get_current_pid_tgid() };

    let entry_ts = match unsafe { GPU_FENCE_START_TS.get(&pid_tgid) } {
        Some(ts) => *ts,
        None => return 0,
    };
    let _ = GPU_FENCE_START_TS.remove(&pid_tgid);

    let now = unsafe { bpf_ktime_get_ns() };
    let latency_ns = now.saturating_sub(entry_ts);

    if let Some(mut entry) = GPU_FENCE_EVENTS.reserve::<GpuFenceEvent>(0) {
        entry.write(GpuFenceEvent {
            latency_ns,
            _pad: 0,
        });
        entry.submit(0);
    }
    0
}
