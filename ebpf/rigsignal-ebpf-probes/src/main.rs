#![no_std]
#![no_main]
// aya-ebpf helper safety varies across nightly versions — some treat
// bpf_ktime_get_ns / bpf_get_current_pid_tgid as safe, others as unsafe.
// Keep the unsafe{} wrappers and suppress the lint either way.
#![allow(unused_unsafe)]

// Silence the unused-import warning from aya-log-ebpf when no logging is active.
#[allow(unused_imports)]
use aya_log_ebpf as _;

mod bio;
mod futex;
mod gpu_fence;
mod gpu_sched;
mod gpu_submit;
mod irq;
mod mem;
mod sched;
mod vfs;

/// Panic handler required for no_std BPF programs.
/// BPF programs should never actually panic — the verifier enforces safe code
/// paths — but the no_std target requires this symbol to exist.
#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    unsafe { core::hint::unreachable_unchecked() }
}
