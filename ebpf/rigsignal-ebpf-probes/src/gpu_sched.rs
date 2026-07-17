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
    maps::{HashMap, LruHashMap, PerCpuArray, RingBuf},
    programs::TracePointContext,
};

const RING_BUF_BYTES: u32 = 256 * 1024;
const KEY_OFFSET_INDEX: u32 = 0;
const LOSS_KEY_READ: u32 = 0;
const LOSS_QUEUE_INSERT: u32 = 1;
const LOSS_RUN_MISS: u32 = 2;
const LOSS_RINGBUF_RESERVE: u32 = 3;
const LOSS_COUNTERS: u32 = 4;

/// Two fields identify a scheduler job. `id` and `fence_seqno` alone are only
/// scheduler/context scoped, so they cannot safely identify jobs across rings.
#[repr(C)]
pub struct GpuSchedKey {
    pub scope: u64,
    pub sequence: u64,
}

/// Tracepoint field offsets supplied by the userspace format-file parser.
#[repr(C)]
pub struct GpuSchedKeyOffsets {
    pub scope: u32,
    pub sequence: u32,
}

/// In-flight GPU jobs. LRU eviction bounds entries leaked by a queue event whose
/// matching run event is absent; an evicted job is counted as a run miss later.
#[map]
static GPU_SCHED_TS: LruHashMap<GpuSchedKey, u64> = LruHashMap::with_max_entries(4096, 0);

/// Key-field byte offset supplied by userspace after parsing the tracepoint format.
#[map]
static GPU_SCHED_KEY_OFFSET: HashMap<u32, GpuSchedKeyOffsets> = HashMap::with_max_entries(1, 0);

/// Per-CPU loss counters read and summed by userspace each collection interval.
/// They make loss visible without changing emitted ES fields.
#[map]
static GPU_SCHED_LOSS: PerCpuArray<u64> = PerCpuArray::with_max_entries(LOSS_COUNTERS, 0);

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
fn count_loss(counter: u32) {
    if let Some(value) = GPU_SCHED_LOSS.get_ptr_mut(counter) {
        unsafe {
            // Per-CPU map values are only updated by the current CPU, and BPF
            // programs are non-preemptible while this update executes.
            *value = (*value).saturating_add(1);
        }
    }
}

#[inline(always)]
fn key_from_context(ctx: &TracePointContext) -> Option<GpuSchedKey> {
    let offset = unsafe { GPU_SCHED_KEY_OFFSET.get(&KEY_OFFSET_INDEX) }?;
    let scope = match unsafe { ctx.read_at(offset.scope as usize) } {
        Ok(value) => value,
        Err(_) => {
            count_loss(LOSS_KEY_READ);
            return None;
        }
    };
    let sequence = match unsafe { ctx.read_at(offset.sequence as usize) } {
        Ok(value) => value,
        Err(_) => {
            count_loss(LOSS_KEY_READ);
            return None;
        }
    };
    Some(GpuSchedKey { scope, sequence })
}

#[inline(always)]
fn record_queue(key: GpuSchedKey) {
    let ts = unsafe { bpf_ktime_get_ns() };
    if GPU_SCHED_TS.insert(&key, &ts, 0).is_err() {
        count_loss(LOSS_QUEUE_INSERT);
    }
}

#[inline(always)]
fn record_run(key: GpuSchedKey) {
    let queue_ts = match unsafe { GPU_SCHED_TS.get(&key) } {
        Some(ts) => *ts,
        None => {
            count_loss(LOSS_RUN_MISS);
            return;
        }
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
    } else {
        count_loss(LOSS_RINGBUF_RESERVE);
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
