/// GPU scheduler latency BPF programs.
///
/// The userspace loader selects one tracepoint-name variant at startup and writes the
/// selected key field offset into `GPU_SCHED_KEY_OFFSET`. Keeping the offset out of
/// this object avoids silently reading the wrong data when a kernel changes its trace
/// event layout.
///
/// Both `fence_seqno` and legacy `id` are only scheduler-scoped, so a collision may
/// replace an in-flight timestamp. This is the pre-existing statistical limitation of
/// the seqno-only map key; it does not change the emitted event schema.
///
/// NOTE: the GAME_PIDS filter is intentionally NOT applied here. Under Proton/RADV,
/// GPU jobs are submitted via RADV's dedicated submission threads or Wine server
/// threads — neither of which appear in GAME_PIDS. We capture all system-wide GPU
/// scheduler activity; during a gaming session this is dominated by game traffic.
use aya_ebpf::{
    helpers::bpf_ktime_get_ns,
    macros::{map, tracepoint},
    maps::{HashMap, RingBuf},
    programs::TracePointContext,
};

const RING_BUF_BYTES: u32 = 256 * 1024;
const KEY_OFFSET_INDEX: u32 = 0;

/// In-flight GPU jobs: variant-specific u64 key → queue ktime_ns.
#[map]
static GPU_SCHED_TS: HashMap<u64, u64> = HashMap::with_max_entries(4096, 0);

/// Key-field byte offset supplied by userspace after parsing the tracepoint format.
#[map]
static GPU_SCHED_KEY_OFFSET: HashMap<u32, u32> = HashMap::with_max_entries(1, 0);

/// Ring buffer carrying GpuSchedEvents to userspace.
#[map]
static GPU_SCHED_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

/// Event sent to userspace via GPU_SCHED_EVENTS ring buffer.
#[repr(C)]
pub struct GpuSchedEvent {
    /// Time from queueing to hardware dispatch (nanoseconds).
    pub latency_ns: u64,
    pub _pad: u64,
}

#[inline(always)]
fn key_from_context(ctx: &TracePointContext) -> Option<u64> {
    let offset = unsafe { GPU_SCHED_KEY_OFFSET.get(&KEY_OFFSET_INDEX) }?;
    unsafe { ctx.read_at(*offset as usize).ok() }
}

#[inline(always)]
fn record_queue(key: u64) {
    let ts = unsafe { bpf_ktime_get_ns() };
    let _ = GPU_SCHED_TS.insert(&key, &ts, 0);
}

#[inline(always)]
fn record_run(key: u64) {
    let queue_ts = match unsafe { GPU_SCHED_TS.get(&key) } {
        Some(ts) => *ts,
        None => return,
    };
    let _ = GPU_SCHED_TS.remove(&key);

    let now = unsafe { bpf_ktime_get_ns() };
    let latency_ns = now.saturating_sub(queue_ts);

    if let Some(mut entry) = GPU_SCHED_EVENTS.reserve::<GpuSchedEvent>(0) {
        entry.write(GpuSchedEvent {
            latency_ns,
            _pad: 0,
        });
        entry.submit(0);
    }
}

/// Renamed tracepoint pair used by newer kernels. Its key is `fence_seqno`.
#[tracepoint(name = "drm_sched_job_queue", category = "gpu_scheduler")]
pub fn drm_sched_job_queue(ctx: TracePointContext) -> u32 {
    if let Some(key) = key_from_context(&ctx) {
        record_queue(key);
    }
    0
}

#[tracepoint(name = "drm_sched_job_run", category = "gpu_scheduler")]
pub fn drm_sched_job_run(ctx: TracePointContext) -> u32 {
    if let Some(key) = key_from_context(&ctx) {
        record_run(key);
    }
    0
}

/// Legacy tracepoint pair used by Valve's 6.16 kernel. Its key is `id`.
#[tracepoint(name = "drm_sched_job", category = "gpu_scheduler")]
pub fn drm_sched_job(ctx: TracePointContext) -> u32 {
    if let Some(key) = key_from_context(&ctx) {
        record_queue(key);
    }
    0
}

#[tracepoint(name = "drm_run_job", category = "gpu_scheduler")]
pub fn drm_run_job(ctx: TracePointContext) -> u32 {
    if let Some(key) = key_from_context(&ctx) {
        record_run(key);
    }
    0
}
