/// schedlatency BPF programs
///
/// Three tracepoints:
///   sched/sched_wakeup        — record wakeup timestamp for game threads
///   sched/sched_switch        — compute runqueue latency when thread is scheduled in
///   sched/sched_migrate_task  — record CPU migrations for CCX-crossing analysis
///
/// Maps (shared across all three programs in this BPF object):
///   GAME_PIDS   — set of TIDs belonging to the game process tree
///   WAKEUP_TS   — per-TID wakeup timestamp (ktime_get_ns), keyed by TID
///   SCHED_EVENTS — ring buffer; receives SchedEvent structs for userspace
use aya_ebpf::{
    helpers::{bpf_ktime_get_ns, bpf_probe_read_kernel},
    macros::{map, tracepoint},
    maps::{HashMap, RingBuf},
    programs::TracePointContext,
};

const COMM_LEN: usize = 16;
const RING_BUF_BYTES: u32 = 256 * 1024;

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

/// TIDs (Linux task IDs) that belong to the tracked game process tree.
/// Populated by userspace daemon from /proc/<pid>/task/ every 5 s.
/// Up to 64 entries covers even the most thread-heavy Proton games.
#[map]
static GAME_PIDS: HashMap<u32, u8> = HashMap::with_max_entries(64, 0);

/// Wakeup timestamp (ktime_get_ns) indexed by TID.
/// Written on sched_wakeup; consumed and deleted on sched_switch.
#[map]
static WAKEUP_TS: HashMap<u32, u64> = HashMap::with_max_entries(1024, 0);

/// Ring buffer carrying SchedEvent structs to userspace.
/// 256 KB: ~4000 events at 64 bytes/event. At ~200 sched events/sec for a
/// typical game, this gives ~20 s of headroom before the 1-second userspace
/// drain cycle.
#[map]
static SCHED_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event types
// ---------------------------------------------------------------------------

pub const EVENT_SWITCH: u8 = 0;
pub const EVENT_MIGRATE: u8 = 1;

/// Event sent to userspace via SCHED_EVENTS ring buffer.
/// Total size: 44 bytes (aligned to 8). Kept small to maximise ring buf capacity.
#[repr(C)]
pub struct SchedEvent {
    /// EVENT_SWITCH or EVENT_MIGRATE
    pub event_type: u8,
    pub _pad: [u8; 3],
    /// Linux TID of the game thread
    pub tid: u32,
    /// Runqueue wait time in nanoseconds (0 for EVENT_MIGRATE)
    pub wait_ns: u64,
    /// CPU the thread was migrated FROM (EVENT_MIGRATE only; 0 otherwise)
    pub prev_cpu: u32,
    /// CPU the thread was migrated TO (EVENT_MIGRATE only; 0 otherwise)
    pub next_cpu: u32,
    /// Task comm string (thread name), null-terminated
    pub comm: [u8; COMM_LEN],
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

#[inline(always)]
fn is_game_tid(tid: u32) -> bool {
    unsafe { GAME_PIDS.get(&tid).is_some() }
}

// ---------------------------------------------------------------------------
// Tracepoint handlers
// ---------------------------------------------------------------------------

/// sched/sched_wakeup tracepoint — record the moment a game thread becomes
/// runnable. Layout (from kernel format file):
///   offset  8: char comm[16]
///   offset 24: pid_t pid          ← this is the Linux TID of the waking task
///   offset 28: int prio
///   offset 32: int target_cpu
#[tracepoint]
pub fn sched_wakeup(ctx: TracePointContext) -> u32 {
    let tid: i32 = unsafe {
        match ctx.read_at(24) {
            Ok(v) => v,
            Err(_) => return 1,
        }
    };
    let tid = tid as u32;
    if !is_game_tid(tid) {
        return 0;
    }
    let ts = unsafe { bpf_ktime_get_ns() };
    unsafe {
        let _ = WAKEUP_TS.insert(&tid, &ts, 0);
    }
    0
}

/// sched/sched_switch tracepoint — fired when the scheduler switches tasks.
/// We care about next_pid: if the incoming task is a game thread, compute
/// how long it waited in the runqueue since its wakeup.
///
/// Layout (from kernel format file):
///   offset  8: char prev_comm[16]
///   offset 24: pid_t prev_pid
///   offset 28: int prev_prio
///   offset 32: long prev_state    (8 bytes on 64-bit)
///   offset 40: char next_comm[16]
///   offset 56: pid_t next_pid
///   offset 60: int next_prio
#[tracepoint]
pub fn sched_switch(ctx: TracePointContext) -> u32 {
    let next_tid: i32 = unsafe {
        match ctx.read_at(56) {
            Ok(v) => v,
            Err(_) => return 1,
        }
    };
    let next_tid = next_tid as u32;
    if !is_game_tid(next_tid) {
        return 0;
    }

    let now = unsafe { bpf_ktime_get_ns() };

    // Compute runqueue latency: time from wakeup to this schedule-in.
    let wait_ns = unsafe {
        match WAKEUP_TS.get(&next_tid) {
            Some(wakeup_ts) => now.saturating_sub(*wakeup_ts),
            None => 0,
        }
    };
    unsafe {
        let _ = WAKEUP_TS.remove(&next_tid);
    }

    let next_comm: [u8; COMM_LEN] = unsafe {
        match ctx.read_at(40) {
            Ok(v) => v,
            Err(_) => [0u8; COMM_LEN],
        }
    };

    if let Some(mut buf_entry) = SCHED_EVENTS.reserve::<SchedEvent>(0) {
        unsafe {
            buf_entry.write(SchedEvent {
                event_type: EVENT_SWITCH,
                _pad: [0; 3],
                tid: next_tid,
                wait_ns,
                prev_cpu: 0,
                next_cpu: 0,
                comm: next_comm,
            });
        }
        buf_entry.submit(0);
    }
    0
}

/// sched/sched_migrate_task tracepoint — fired when a task is moved between CPUs.
/// Used to count CCX migrations (threads crossing L3 cache boundaries on AMD Zen).
///
/// Layout (from kernel format file):
///   offset  8: char comm[16]
///   offset 24: pid_t pid
///   offset 28: int prio
///   offset 32: int orig_cpu
///   offset 36: int dest_cpu
#[tracepoint]
pub fn sched_migrate_task(ctx: TracePointContext) -> u32 {
    let tid: i32 = unsafe {
        match ctx.read_at(24) {
            Ok(v) => v,
            Err(_) => return 1,
        }
    };
    let tid = tid as u32;
    if !is_game_tid(tid) {
        return 0;
    }

    let orig_cpu: i32 = unsafe {
        match ctx.read_at(32) {
            Ok(v) => v,
            Err(_) => return 1,
        }
    };
    let dest_cpu: i32 = unsafe {
        match ctx.read_at(36) {
            Ok(v) => v,
            Err(_) => return 1,
        }
    };
    let comm: [u8; COMM_LEN] = unsafe {
        match ctx.read_at(8) {
            Ok(v) => v,
            Err(_) => [0u8; COMM_LEN],
        }
    };

    if let Some(mut buf_entry) = SCHED_EVENTS.reserve::<SchedEvent>(0) {
        unsafe {
            buf_entry.write(SchedEvent {
                event_type: EVENT_MIGRATE,
                _pad: [0; 3],
                tid,
                wait_ns: 0,
                prev_cpu: orig_cpu as u32,
                next_cpu: dest_cpu as u32,
                comm,
            });
        }
        buf_entry.submit(0);
    }
    0
}
