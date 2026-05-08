/// GPU scheduler latency BPF programs.
///
/// Two tracepoints:
///   gpu_scheduler/drm_sched_job_queue — job submitted to DRM GPU scheduler queue
///   gpu_scheduler/drm_sched_job_run   — job dispatched to hardware for execution
///
/// Measures: time from job queue entry to hardware dispatch (GPU scheduling latency).
/// High values → GPU starvation / scheduler backlog → frame drops.
///
/// Field offsets verified against CachyOS kernel 6.19.11 format files
/// (both tracepoints have identical layouts):
///   common_pid:    offset 4  (int)  — issuing thread, filtered against GAME_PIDS
///   name:          offset 8  (dynamic string — ring name, not read in BPF)
///   job_count:     offset 12 (u32)
///   hw_job_count:  offset 16 (int)
///   dev:           offset 20 (dynamic string — not read in BPF)
///   fence_context: offset 24 (u64) — fence context ID
///   fence_seqno:   offset 32 (u64) — monotonically increasing per context; used as map key
///   client_id:     offset 40 (u64)
///
/// Map key: fence_seqno (u64). Each queue has its own fence_context, and seqno values
/// across gfx/comp/sdma rings occupy different ranges, so collisions are negligible.
///
/// NOTE: GAME_PIDS filter is intentionally NOT applied here. Under Proton/RADV,
/// GPU jobs are submitted via RADV's dedicated submission threads or Wine server
/// threads — neither of which appear in GAME_PIDS. Same root cause as the
/// kworker issue with block I/O. We capture all system-wide GPU scheduler
/// activity instead; during a gaming session this is dominated by game traffic.
use aya_ebpf::{
    helpers::bpf_ktime_get_ns,
    macros::{map, tracepoint},
    maps::{HashMap, RingBuf},
    programs::TracePointContext,
};

const RING_BUF_BYTES: u32 = 256 * 1024;

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

/// In-flight GPU jobs: fence_seqno → queue ktime_ns.
#[map]
static GPU_SCHED_TS: HashMap<u64, u64> = HashMap::with_max_entries(4096, 0);

/// Ring buffer carrying GpuSchedEvents to userspace.
#[map]
static GPU_SCHED_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event type
// ---------------------------------------------------------------------------

/// Event sent to userspace via GPU_SCHED_EVENTS ring buffer.
#[repr(C)]
pub struct GpuSchedEvent {
    /// Time from drm_sched_job_queue to drm_sched_job_run (nanoseconds).
    pub latency_ns: u64,
    pub _pad: u64,
}

// ---------------------------------------------------------------------------
// gpu_scheduler/drm_sched_job_queue
// ---------------------------------------------------------------------------
#[tracepoint(name = "drm_sched_job_queue", category = "gpu_scheduler")]
pub fn drm_sched_job_queue(ctx: TracePointContext) -> u32 {
    let seqno: u64 = match unsafe { ctx.read_at(32) } {
        Ok(v) => v,
        Err(_) => return 1,
    };

    let ts = unsafe { bpf_ktime_get_ns() };
    let _ = GPU_SCHED_TS.insert(&seqno, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// gpu_scheduler/drm_sched_job_run
// ---------------------------------------------------------------------------
#[tracepoint(name = "drm_sched_job_run", category = "gpu_scheduler")]
pub fn drm_sched_job_run(ctx: TracePointContext) -> u32 {
    let seqno: u64 = match unsafe { ctx.read_at(32) } {
        Ok(v) => v,
        Err(_) => return 1,
    };

    let queue_ts = match unsafe { GPU_SCHED_TS.get(&seqno) } {
        Some(ts) => *ts,
        None => return 0, // seqno not in map (expired, overwritten by collision, or map full)
    };
    let _ = GPU_SCHED_TS.remove(&seqno);

    let now = unsafe { bpf_ktime_get_ns() };
    let latency_ns = now.saturating_sub(queue_ts);

    if let Some(mut entry) = GPU_SCHED_EVENTS.reserve::<GpuSchedEvent>(0) {
        entry.write(GpuSchedEvent {
            latency_ns,
            _pad: 0,
        });
        entry.submit(0);
    }
    0
}
