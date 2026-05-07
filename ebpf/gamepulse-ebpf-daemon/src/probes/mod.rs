/// Probe trait and registry.
///
/// Every eBPF probe implements `Probe`. The loader iterates probes, checks
/// requirements, calls `attach()`, then spawns drain tasks that call `collect()`
/// on each aggregation interval.
use anyhow::Result;
use aya::Ebpf;

use crate::es_model::EbpfDocument;

pub mod bio;
pub mod futex;
pub mod gpu_fence;
pub mod gpu_sched;
pub mod gpu_submit;
pub mod irq;
pub mod mem;
pub mod sched;
pub mod vfs;

/// Requirements that must be met for a probe to load.
#[derive(Debug, Default)]
pub struct ProbeRequirements {
    /// Tracepoint names that must exist in /sys/kernel/tracing/available_events.
    pub tracepoints: Vec<&'static str>,
    /// Kernel symbols required in /proc/kallsyms (for kprobes).
    pub kprobe_symbols: Vec<&'static str>,
    /// Kernel modules that must be loaded.
    pub kernel_modules: Vec<&'static str>,
    /// Minimum kernel version (major, minor).
    pub min_kernel: (u32, u32),
}

/// A single eBPF probe — kernel attachment + userspace drain + aggregation.
pub trait Probe: Send + 'static {
    fn name(&self) -> &'static str;
    fn requirements(&self) -> ProbeRequirements;

    /// Load BPF programs from `ebpf` and attach to the kernel.
    /// `game_pids_map` is the aya map handle for PID filtering.
    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()>;

    /// Drain the ring buffer / perf buffer, aggregate into a 1-second snapshot.
    /// Returns 0..N documents ready for shipping.
    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfDocument>>;

    /// Detach and release kernel resources.
    fn detach(&mut self) -> Result<()>;
}

/// Check that all requirements for a probe are met on the running kernel.
/// Returns Ok(()) if met, Err with a human-readable reason if not.
pub fn check_requirements(req: &ProbeRequirements) -> Result<()> {
    use anyhow::bail;

    // Check minimum kernel version
    let (major, minor) = req.min_kernel;
    if major > 0 {
        let version = read_kernel_version()?;
        if version < (major, minor) {
            bail!(
                "kernel {}.{} required, running {}.{}",
                major,
                minor,
                version.0,
                version.1
            );
        }
    }

    // Check tracepoints
    for tp in &req.tracepoints {
        if !tracepoint_exists(tp) {
            bail!("tracepoint '{}' not available", tp);
        }
    }

    // Check kprobe symbols
    for sym in &req.kprobe_symbols {
        if !kallsyms_contains(sym) {
            bail!("kernel symbol '{}' not found in /proc/kallsyms", sym);
        }
    }

    // Check kernel modules
    for module in &req.kernel_modules {
        if !module_loaded(module) {
            bail!("kernel module '{}' not loaded", module);
        }
    }

    Ok(())
}

fn read_kernel_version() -> Result<(u32, u32)> {
    let release = std::fs::read_to_string("/proc/sys/kernel/osrelease").unwrap_or_default();
    let mut parts = release.trim().split('.');
    let major: u32 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    let minor: u32 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    Ok((major, minor))
}

fn tracepoint_exists(tp: &str) -> bool {
    // tp is in "category/name" form, e.g. "sched/sched_switch"
    let path = format!("/sys/kernel/tracing/events/{}", tp);
    std::path::Path::new(&path).exists()
}

fn kallsyms_contains(sym: &str) -> bool {
    if let Ok(content) = std::fs::read_to_string("/proc/kallsyms") {
        // kallsyms lines: "address type name [module]"
        content
            .lines()
            .any(|line| line.split_whitespace().nth(2).map_or(false, |s| s == sym))
    } else {
        false
    }
}

fn module_loaded(module: &str) -> bool {
    if let Ok(content) = std::fs::read_to_string("/proc/modules") {
        content.lines().any(|line| {
            line.split_whitespace()
                .next()
                .map_or(false, |s| s == module)
        })
    } else {
        false
    }
}
