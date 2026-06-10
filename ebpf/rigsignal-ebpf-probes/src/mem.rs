/// Memory pressure BPF programs.
///
/// Two tracepoints:
///   exceptions/page_fault_user        — user-space page faults from game threads
///   vmscan/mm_vmscan_direct_reclaim_begin — game/system thread stalling for memory reclaim
///
/// Field offsets verified against CachyOS kernel 6.19.11 format files:
///
/// page_fault_user:
///   common_pid:   offset 4  (int)           — filtered against GAME_PIDS
///   address:      offset 8  (unsigned long)  — faulting address (not read)
///   ip:           offset 16 (unsigned long)  — instruction pointer (not read)
///   error_code:   offset 24 (unsigned long)  — x86 page fault error bits
///     bit 0 (P):  0=page not present, 1=protection violation
///     bit 1 (W):  0=read fault, 1=write fault
///
/// mm_vmscan_direct_reclaim_begin:
///   common_pid:   offset 4  (int)   — issuing thread (not filtered — system-wide pressure)
///   order:        offset 8  (int)   — allocation order (not read)
///   gfp_flags:    offset 16 (ulong) — GFP flags (not read)
use aya_ebpf::{
    macros::{map, tracepoint},
    maps::RingBuf,
    programs::TracePointContext,
};

use crate::sched::GAME_PIDS;

/// Event type discriminant.
pub const MEM_FAULT: u8 = 0;
pub const MEM_RECLAIM: u8 = 1;

const RING_BUF_BYTES: u32 = 256 * 1024;

#[map]
static MEM_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

/// Event emitted for both fault and reclaim events.
#[repr(C)]
pub struct MemEvent {
    /// MEM_FAULT or MEM_RECLAIM
    pub event_type: u8,
    /// For MEM_FAULT: 1=write fault (COW/alloc), 0=read fault.
    /// For MEM_RECLAIM: unused (0).
    pub is_write: u8,
    pub _pad: [u8; 6],
}

#[inline(always)]
fn is_game_pid(pid: u32) -> bool {
    unsafe { GAME_PIDS.get(&pid).is_some() }
}

// ---------------------------------------------------------------------------
// exceptions/page_fault_user
// ---------------------------------------------------------------------------

#[tracepoint(name = "page_fault_user", category = "exceptions")]
pub fn page_fault_user(ctx: TracePointContext) -> u32 {
    let pid: i32 = match unsafe { ctx.read_at(4) } {
        Ok(v) => v,
        Err(_) => return 1,
    };
    if !is_game_pid(pid as u32) {
        return 0;
    }

    // error_code bit 1: 0=read fault, 1=write fault
    let error_code: u64 = match unsafe { ctx.read_at(24) } {
        Ok(v) => v,
        Err(_) => return 1,
    };
    let is_write: u8 = ((error_code >> 1) & 1) as u8;

    if let Some(mut entry) = MEM_EVENTS.reserve::<MemEvent>(0) {
        entry.write(MemEvent {
            event_type: MEM_FAULT,
            is_write,
            _pad: [0; 6],
        });
        entry.submit(0);
    }
    0
}

// ---------------------------------------------------------------------------
// vmscan/mm_vmscan_direct_reclaim_begin
// ---------------------------------------------------------------------------

#[tracepoint(name = "mm_vmscan_direct_reclaim_begin", category = "vmscan")]
pub fn mm_vmscan_direct_reclaim_begin(_ctx: TracePointContext) -> u32 {
    // No PID filter — direct reclaim is a system-wide pressure signal.
    // Even if kswapd triggers it, reclaim hurts any thread waiting for memory.

    if let Some(mut entry) = MEM_EVENTS.reserve::<MemEvent>(0) {
        entry.write(MemEvent {
            event_type: MEM_RECLAIM,
            is_write: 0,
            _pad: [0; 6],
        });
        entry.submit(0);
    }
    0
}
