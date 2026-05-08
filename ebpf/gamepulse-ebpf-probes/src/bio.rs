/// Block I/O latency BPF programs.
///
/// Two tracepoints:
///   block/block_rq_issue    — record issue timestamp for all block I/O requests
///   block/block_rq_complete — compute latency and emit to BIO_EVENTS ring buffer
///
/// Field offsets verified against CachyOS kernel 6.19.11 format files:
///
/// block_rq_issue:
///   dev:        offset 8  (dev_t, u32)
///   sector:     offset 16 (sector_t, u64 — 4-byte padding after dev for alignment)
///   nr_sector:  offset 24 (u32)
///   bytes:      offset 28 (u32, direct byte count of the request)
///
/// block_rq_complete:
///   dev:        offset 8  (dev_t, u32)
///   sector:     offset 16 (sector_t, u64)
///   nr_sector:  offset 24 (u32)
///
/// No PID filter at issue time: buffered file I/O (most games) submits block requests
/// via kernel worker threads (kworker), not in the game process context. Filtering by
/// GAME_PIDS would silently drop everything. Instead, track all block I/O system-wide.
/// The main loop only calls collect() when a session is active, so no non-game data
/// is ever shipped. System-wide I/O latency during a session also reveals background
/// I/O contention that can cause game stutters.
///
/// Map key: sector (u64). Unique per in-flight request on a given device.
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

/// In-flight I/O: sector → issue ktime_ns.
/// Keyed on sector number (unique per in-flight request after PID filtering).
#[map]
static BIO_START_TS: HashMap<u64, u64> = HashMap::with_max_entries(4096, 0);

/// Ring buffer carrying BioEvents to userspace. 256 KB ≈ 16 000 events.
#[map]
static BIO_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event type
// ---------------------------------------------------------------------------

/// Event sent to userspace via BIO_EVENTS ring buffer.
#[repr(C)]
pub struct BioEvent {
    /// Elapsed time from block_rq_issue to block_rq_complete (nanoseconds).
    pub latency_ns: u64,
    /// Request size in bytes (from the issue event's `bytes` field).
    pub bytes: u32,
    pub _pad: u32,
}

// ---------------------------------------------------------------------------
// block/block_rq_issue
//
// Tracepoint layout (kernel 6.19, verified from format file):
//   offset  4: int common_pid        (unused — see module comment above)
//   offset  8: dev_t dev
//   offset 16: sector_t sector       ← map key
//   offset 24: unsigned int nr_sector
//   offset 28: unsigned int bytes    ← I/O size in bytes
// ---------------------------------------------------------------------------
#[tracepoint(name = "block_rq_issue", category = "block")]
pub fn block_rq_issue(ctx: TracePointContext) -> u32 {
    let sector: u64 = match unsafe { ctx.read_at(16) } {
        Ok(v) => v,
        Err(_) => return 1,
    };

    let ts = bpf_ktime_get_ns();
    let _ = BIO_START_TS.insert(&sector, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// block/block_rq_complete
//
// Tracepoint layout (kernel 6.19, same dev/sector structure as issue):
//   offset  8: dev_t dev
//   offset 16: sector_t sector       ← map key (must match issue)
//   offset 24: unsigned int nr_sector ← compute bytes = nr_sector * 512
//   offset 28: int error
// ---------------------------------------------------------------------------
#[tracepoint(name = "block_rq_complete", category = "block")]
pub fn block_rq_complete(ctx: TracePointContext) -> u32 {
    let sector: u64 = match unsafe { ctx.read_at(16) } {
        Ok(v) => v,
        Err(_) => return 1,
    };

    // Look up the issue timestamp — if missing, this was not a game request.
    let issue_ts = match unsafe { BIO_START_TS.get(&sector) } {
        Some(ts) => *ts,
        None => return 0,
    };
    let _ = BIO_START_TS.remove(&sector);

    let now = bpf_ktime_get_ns();
    let latency_ns = now.saturating_sub(issue_ts);

    let nr_sector: u32 = unsafe { ctx.read_at(24) }.unwrap_or(0);
    let bytes = nr_sector * 512;

    if let Some(mut entry) = BIO_EVENTS.reserve::<BioEvent>(0) {
        entry.write(BioEvent {
            latency_ns,
            bytes,
            _pad: 0,
        });
        entry.submit(0);
    }
    0
}
