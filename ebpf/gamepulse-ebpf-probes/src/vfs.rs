/// VFS read/write latency BPF programs.
///
/// kprobe/kretprobe on `vfs_read` and `vfs_write`:
///   vfs_read entry   — record start timestamp (game threads only)
///   vfs_read return  — compute latency, emit VfsEvent (op=READ)
///   vfs_write entry  — record start timestamp (game threads only)
///   vfs_write return — compute latency, emit VfsEvent (op=WRITE)
///
/// vfs_read and vfs_write are exported kernel symbols (T in /proc/kallsyms
/// on kernel 6.19, not inlined). These functions are called for all file
/// reads/writes from user-space via the VFS layer.
///
/// PID filtering: GAME_PIDS (same map as sched/mem probes).
///
/// Map key: pid_tgid (u64) packed from bpf_get_current_pid_tgid().
/// Used to correlate entry and return across both functions.
/// We use separate maps for read and write to avoid key collisions
/// (a thread could theoretically have concurrent read and write in flight
/// in the case of sendfile-like operations, though this is rare).
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

/// In-flight vfs_read calls: pid_tgid → entry ktime_ns.
#[map]
static VFS_READ_START_TS: HashMap<u64, u64> = HashMap::with_max_entries(1024, 0);

/// In-flight vfs_write calls: pid_tgid → entry ktime_ns.
#[map]
static VFS_WRITE_START_TS: HashMap<u64, u64> = HashMap::with_max_entries(1024, 0);

/// Ring buffer carrying VfsEvents to userspace.
#[map]
static VFS_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event type
// ---------------------------------------------------------------------------

pub const VFS_OP_READ: u8 = 0;
pub const VFS_OP_WRITE: u8 = 1;

/// Event emitted for both vfs_read and vfs_write completions.
#[repr(C)]
pub struct VfsEvent {
    /// Elapsed time inside vfs_read/vfs_write (nanoseconds).
    pub latency_ns: u64,
    /// VFS_OP_READ or VFS_OP_WRITE.
    pub op: u8,
    pub _pad: [u8; 7],
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

#[inline(always)]
fn is_game_tid(tid: u32) -> bool {
    unsafe { GAME_PIDS.get(&tid).is_some() }
}

// ---------------------------------------------------------------------------
// vfs_read entry
// ---------------------------------------------------------------------------

#[kprobe(function = "vfs_read")]
pub fn vfs_read_entry(_ctx: ProbeContext) -> u32 {
    let pid_tgid = bpf_get_current_pid_tgid();
    let tid = (pid_tgid & 0xffff_ffff) as u32;

    if !is_game_tid(tid) {
        return 0;
    }

    let ts = bpf_ktime_get_ns();
    let _ = VFS_READ_START_TS.insert(&pid_tgid, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// vfs_read return
// ---------------------------------------------------------------------------

#[kretprobe(function = "vfs_read")]
pub fn vfs_read_return(_ctx: RetProbeContext) -> u32 {
    let pid_tgid = bpf_get_current_pid_tgid();

    let entry_ts = match unsafe { VFS_READ_START_TS.get(&pid_tgid) } {
        Some(ts) => *ts,
        None => return 0,
    };
    let _ = VFS_READ_START_TS.remove(&pid_tgid);

    let now = bpf_ktime_get_ns();
    let latency_ns = now.saturating_sub(entry_ts);

    if let Some(mut entry) = VFS_EVENTS.reserve::<VfsEvent>(0) {
        entry.write(VfsEvent {
            latency_ns,
            op: VFS_OP_READ,
            _pad: [0; 7],
        });
        entry.submit(0);
    }
    0
}

// ---------------------------------------------------------------------------
// vfs_write entry
// ---------------------------------------------------------------------------

#[kprobe(function = "vfs_write")]
pub fn vfs_write_entry(_ctx: ProbeContext) -> u32 {
    let pid_tgid = bpf_get_current_pid_tgid();
    let tid = (pid_tgid & 0xffff_ffff) as u32;

    if !is_game_tid(tid) {
        return 0;
    }

    let ts = bpf_ktime_get_ns();
    let _ = VFS_WRITE_START_TS.insert(&pid_tgid, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// vfs_write return
// ---------------------------------------------------------------------------

#[kretprobe(function = "vfs_write")]
pub fn vfs_write_return(_ctx: RetProbeContext) -> u32 {
    let pid_tgid = bpf_get_current_pid_tgid();

    let entry_ts = match unsafe { VFS_WRITE_START_TS.get(&pid_tgid) } {
        Some(ts) => *ts,
        None => return 0,
    };
    let _ = VFS_WRITE_START_TS.remove(&pid_tgid);

    let now = bpf_ktime_get_ns();
    let latency_ns = now.saturating_sub(entry_ts);

    if let Some(mut entry) = VFS_EVENTS.reserve::<VfsEvent>(0) {
        entry.write(VfsEvent {
            latency_ns,
            op: VFS_OP_WRITE,
            _pad: [0; 7],
        });
        entry.submit(0);
    }
    0
}
