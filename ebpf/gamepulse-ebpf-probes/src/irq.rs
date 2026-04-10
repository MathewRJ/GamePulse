/// IRQ and softirq latency BPF programs.
///
/// Four tracepoints:
///   irq/irq_handler_entry  — record hard-IRQ start timestamp
///   irq/irq_handler_exit   — compute hard-IRQ latency, emit event
///   irq/softirq_entry      — record softirq start timestamp
///   irq/softirq_exit       — compute softirq latency, emit event
///
/// System-wide (no PID filter): IRQs and softirqs are not per-process.
/// During a gaming session, high IRQ latency directly causes game stutters.
///
/// Field offsets for irq_handler_entry / irq_handler_exit
/// (standard kernel layout, kernel 5.x–6.x):
///   common_type:               offset 0  (u16)
///   common_flags:              offset 2  (u8)
///   common_preempt_count:      offset 3  (u8)
///   common_pid:                offset 4  (s32)
///   irq:                       offset 8  (s32) — IRQ line number
///   (name follows as a dynamic string starting at offset 12)
///
/// Field offsets for softirq_entry / softirq_exit:
///   common fields:             offset 0-7
///   vec:                       offset 8  (u32) — softirq vector (0=HI, 1=TIMER, …)
///
/// Map key for hard-IRQ: cpu (u32), but since a single CPU cannot run two IRQ
/// handlers concurrently, keying by irq_nr alone is sufficient.
/// However different CPUs can handle the same IRQ simultaneously. We use
/// (cpu << 16 | irq_nr) packed into u32 — but CPU count can exceed 16 bits.
/// Safer: use bpf_get_smp_processor_id() << 20 | irq_nr (max 1M CPUs, max 1M IRQs).
/// In practice CPUs < 512 and IRQ < 4096 on gaming hardware. Pack as u64.
///
/// Map key for softirq: bpf_get_smp_processor_id() only — at most one softirq
/// vector active per CPU at a time.
use aya_ebpf::{
    helpers::{bpf_get_smp_processor_id, bpf_ktime_get_ns},
    macros::{map, tracepoint},
    maps::{HashMap, RingBuf},
    programs::TracePointContext,
};

const RING_BUF_BYTES: u32 = 256 * 1024;

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

/// In-flight hard-IRQ handlers: (cpu << 32 | irq_nr) → entry ktime_ns.
#[map]
static IRQ_START_TS: HashMap<u64, u64> = HashMap::with_max_entries(1024, 0);

/// In-flight softirq handlers: cpu → entry ktime_ns.
#[map]
static SOFTIRQ_START_TS: HashMap<u32, u64> = HashMap::with_max_entries(512, 0);

/// Ring buffer carrying IrqEvents to userspace.
#[map]
static IRQ_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event type
// ---------------------------------------------------------------------------

pub const IRQ_KIND_HARD: u8 = 0;
pub const IRQ_KIND_SOFT: u8 = 1;

/// Event emitted for both hard-IRQ and softirq completions.
#[repr(C)]
pub struct IrqEvent {
    /// Latency in nanoseconds (handler duration).
    pub latency_ns: u64,
    /// IRQ_KIND_HARD or IRQ_KIND_SOFT.
    pub kind: u8,
    pub _pad: [u8; 7],
}

// ---------------------------------------------------------------------------
// irq/irq_handler_entry
//
// Layout (kernel 6.x):
//   offset  4: s32 common_pid  (unused)
//   offset  8: s32 irq         ← IRQ line number
// ---------------------------------------------------------------------------
#[tracepoint(name = "irq_handler_entry", category = "irq")]
pub fn irq_handler_entry(ctx: TracePointContext) -> u32 {
    let irq_nr: i32 = match unsafe { ctx.read_at(8) } {
        Ok(v) => v,
        Err(_) => return 1,
    };

    let cpu = unsafe { bpf_get_smp_processor_id() };
    let key: u64 = ((cpu as u64) << 32) | (irq_nr as u32 as u64);

    let ts = unsafe { bpf_ktime_get_ns() };
    let _ = IRQ_START_TS.insert(&key, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// irq/irq_handler_exit
//
// Layout same as irq_handler_entry (offset 8: s32 irq).
// ---------------------------------------------------------------------------
#[tracepoint(name = "irq_handler_exit", category = "irq")]
pub fn irq_handler_exit(ctx: TracePointContext) -> u32 {
    let irq_nr: i32 = match unsafe { ctx.read_at(8) } {
        Ok(v) => v,
        Err(_) => return 1,
    };

    let cpu = unsafe { bpf_get_smp_processor_id() };
    let key: u64 = ((cpu as u64) << 32) | (irq_nr as u32 as u64);

    let entry_ts = match unsafe { IRQ_START_TS.get(&key) } {
        Some(ts) => *ts,
        None => return 0,
    };
    let _ = IRQ_START_TS.remove(&key);

    let now = unsafe { bpf_ktime_get_ns() };
    let latency_ns = now.saturating_sub(entry_ts);

    if let Some(mut entry) = IRQ_EVENTS.reserve::<IrqEvent>(0) {
        entry.write(IrqEvent {
            latency_ns,
            kind: IRQ_KIND_HARD,
            _pad: [0; 7],
        });
        entry.submit(0);
    }
    0
}

// ---------------------------------------------------------------------------
// irq/softirq_entry
//
// Layout (kernel 6.x):
//   offset  8: u32 vec  ← softirq vector (0=HI, 1=TIMER, 2=NET_TX, …)
// ---------------------------------------------------------------------------
#[tracepoint(name = "softirq_entry", category = "irq")]
pub fn softirq_entry(_ctx: TracePointContext) -> u32 {
    let cpu = unsafe { bpf_get_smp_processor_id() };
    let ts = unsafe { bpf_ktime_get_ns() };
    let _ = SOFTIRQ_START_TS.insert(&cpu, &ts, 0);
    0
}

// ---------------------------------------------------------------------------
// irq/softirq_exit
// ---------------------------------------------------------------------------
#[tracepoint(name = "softirq_exit", category = "irq")]
pub fn softirq_exit(_ctx: TracePointContext) -> u32 {
    let cpu = unsafe { bpf_get_smp_processor_id() };

    let entry_ts = match unsafe { SOFTIRQ_START_TS.get(&cpu) } {
        Some(ts) => *ts,
        None => return 0,
    };
    let _ = SOFTIRQ_START_TS.remove(&cpu);

    let now = unsafe { bpf_ktime_get_ns() };
    let latency_ns = now.saturating_sub(entry_ts);

    if let Some(mut entry) = IRQ_EVENTS.reserve::<IrqEvent>(0) {
        entry.write(IrqEvent {
            latency_ns,
            kind: IRQ_KIND_SOFT,
            _pad: [0; 7],
        });
        entry.submit(0);
    }
    0
}
