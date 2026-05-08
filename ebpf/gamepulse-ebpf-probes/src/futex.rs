/// Futex contention BPF programs.
///
/// kprobe/kretprobe on `do_futex`:
///   entry  — record start timestamp keyed by pid_tgid (game threads only)
///   return — compute latency and emit FutexEvent to ring buffer
///
/// do_futex is the internal kernel function called for all futex(2) operations.
/// It is present in /proc/kallsyms as `T do_futex` on kernel 6.x.
///
/// PID filtering: GAME_PIDS (same map as sched/mem probes). Futex operations
/// inside the game process are per-thread, so filtering by TID is correct.
///
/// Map key: pid_tgid (u64) — upper 32 bits = TGID (process), lower 32 = TID.
/// This is unique per in-flight do_futex call on a given CPU.
use aya_ebpf::{
    helpers::{bpf_get_current_pid_tgid, bpf_ktime_get_ns},
    macros::{kprobe, kretprobe, map},
    maps::{HashMap, RingBuf},
    programs::{ProbeContext, RetProbeContext},
};

use crate::sched::GAME_PIDS;

const RING_BUF_BYTES: u32 = 256 * 1024;

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

/// In-flight futex calls: pid_tgid → entry ktime_ns.
#[map]
static FUTEX_START_TS: HashMap<u64, u64> = HashMap::with_max_entries(1024, 0);

/// Ring buffer carrying FutexEvents to userspace.
#[map]
static FUTEX_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event type
// ---------------------------------------------------------------------------

/// Event emitted when a futex operation completes.
#[repr(C)]
pub struct FutexEvent {
    /// Elapsed time inside do_futex (nanoseconds).
    pub latency_ns: u64,
    pub _pad: u64,
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

#[inline(always)]
fn is_game_tid(tid: u32) -> bool {
    unsafe { GAME_PIDS.get(&tid).is_some() }
}

// ---------------------------------------------------------------------------
// do_futex entry
// ---------------------------------------------------------------------------

#[kprobe(function = "do_futex")]
pub fn do_futex_entry(_ctx: ProbeContext) -> u32 {
    let pid_tgid = bpf_get_current_pid_tgid();
    let tid = (pid_tgid & 0xffff_ffff) as u32;

    if !is_game_tid(tid) {
        return 0;
    }

    let ts = bpf_ktime_get_ns();
    let _ = FUTEX_START_TS.insert(&pid_tgid, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// do_futex return
// ---------------------------------------------------------------------------

#[kretprobe(function = "do_futex")]
pub fn do_futex_return(_ctx: RetProbeContext) -> u32 {
    let pid_tgid = bpf_get_current_pid_tgid();

    let entry_ts = match unsafe { FUTEX_START_TS.get(&pid_tgid) } {
        Some(ts) => *ts,
        None => return 0,
    };
    let _ = FUTEX_START_TS.remove(&pid_tgid);

    let now = bpf_ktime_get_ns();
    let latency_ns = now.saturating_sub(entry_ts);

    if let Some(mut entry) = FUTEX_EVENTS.reserve::<FutexEvent>(0) {
        entry.write(FutexEvent {
            latency_ns,
            _pad: 0,
        });
        entry.submit(0);
    }
    0
}
