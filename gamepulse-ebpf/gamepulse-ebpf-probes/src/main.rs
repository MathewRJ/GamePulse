#![no_std]
#![no_main]

// Silence the unused-import warning from aya-log-ebpf when no logging is active.
#[allow(unused_imports)]
use aya_log_ebpf as _;

mod sched;
