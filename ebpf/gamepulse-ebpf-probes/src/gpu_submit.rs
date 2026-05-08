/// GPU command submission counter BPF program.
///
/// kprobe on `amdgpu_cs_ioctl` (amdgpu kernel module):
///   entry — emit GpuSubmitEvent to ring buffer (count-only probe)
///
/// amdgpu_cs_ioctl is the AMD GPU command submission ioctl handler, called
/// once per GPU command buffer submission (CS = Command Stream). On kernel 6.x
/// with the amdgpu module loaded, it appears in /proc/kallsyms as a lowercase `t`
/// (module-internal) symbol: `t amdgpu_cs_ioctl [amdgpu]`. Kprobes work on
/// module symbols via the kernel's probing infrastructure.
///
/// System-wide (no PID filter): under Proton/RADV, submissions are made by
/// RADV's dedicated submission threads, not the game process itself. The
/// ProbeRequirements check in the daemon verifies the amdgpu module is loaded
/// before attaching this probe; if amdgpu is not loaded the probe is skipped.
///
/// This is a count-only probe — we measure frequency of GPU submissions, not
/// latency. High submission rates confirm the GPU is busy; very low rates
/// during a stutter indicate a GPU-stall upstream.
use aya_ebpf::{
    helpers::bpf_ktime_get_ns,
    macros::{kprobe, map},
    maps::RingBuf,
    programs::ProbeContext,
};

const RING_BUF_BYTES: u32 = 64 * 1024;

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

/// Ring buffer carrying GpuSubmitEvents to userspace.
#[map]
static GPU_SUBMIT_EVENTS: RingBuf = RingBuf::with_byte_size(RING_BUF_BYTES, 0);

// ---------------------------------------------------------------------------
// Event type
// ---------------------------------------------------------------------------

/// Event emitted on every amdgpu_cs_ioctl entry.
#[repr(C)]
pub struct GpuSubmitEvent {
    /// Kernel timestamp at submission time (nanoseconds).
    pub timestamp_ns: u64,
    pub _pad: u64,
}

// ---------------------------------------------------------------------------
// amdgpu_cs_ioctl entry
// ---------------------------------------------------------------------------

#[kprobe(function = "amdgpu_cs_ioctl")]
pub fn amdgpu_cs_ioctl_entry(_ctx: ProbeContext) -> u32 {
    let ts = unsafe { bpf_ktime_get_ns() };

    if let Some(mut entry) = GPU_SUBMIT_EVENTS.reserve::<GpuSubmitEvent>(0) {
        entry.write(GpuSubmitEvent {
            timestamp_ns: ts,
            _pad: 0,
        });
        entry.submit(0);
    }
    0
}
