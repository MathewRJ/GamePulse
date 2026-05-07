/// GPU command submission probe — userspace side.
///
/// Attaches a kprobe to `amdgpu_cs_ioctl` (AMD GPU command stream ioctl).
/// System-wide count-only probe: counts GPU submissions per second.
/// Drains GPU_SUBMIT_EVENTS ring buffer on collect().
///
/// Requires: amdgpu kernel module loaded.
/// `amdgpu_cs_ioctl` appears as a lowercase `t` symbol in /proc/kallsyms
/// when the amdgpu module is loaded — kprobes work on module symbols.
use anyhow::{Context, Result};
use aya::{
    maps::{MapData, RingBuf},
    programs::KProbe,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{GpuSubmitAggregator, RawGpuSubmitEvent};
use crate::es_model::EbpfMetricDoc;

pub struct GpuSubmitProbe {
    aggregator: GpuSubmitAggregator,
    ring_buf: Option<RingBuf<MapData>>,
}

impl GpuSubmitProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        GpuSubmitProbe {
            aggregator: GpuSubmitAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }
}

impl Probe for GpuSubmitProbe {
    fn name(&self) -> &'static str {
        "gpu_submit"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![],
            kprobe_symbols: vec!["amdgpu_cs_ioctl"],
            kernel_modules: vec!["amdgpu"],
            min_kernel: (5, 8),
        }
    }

    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()> {
        let prog: &mut KProbe = ebpf
            .program_mut("amdgpu_cs_ioctl_entry")
            .context("program 'amdgpu_cs_ioctl_entry' not found in BPF object")?
            .try_into()
            .context("expected KProbe program type")?;
        prog.load().context("loading amdgpu_cs_ioctl_entry")?;
        prog.attach("amdgpu_cs_ioctl", 0)
            .context("attaching amdgpu_cs_ioctl_entry kprobe")?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("GPU_SUBMIT_EVENTS")
                .context("GPU_SUBMIT_EVENTS map not found")?,
        )
        .context("GPU_SUBMIT_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfMetricDoc>> {
        use std::mem::size_of;
        let event_size = size_of::<RawGpuSubmitEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &item;
                if bytes.len() < event_size {
                    warn!("gpu_submit ring buf item too small: {} < {}", bytes.len(), event_size);
                    continue;
                }
                let event = unsafe {
                    std::ptr::read_unaligned(bytes.as_ptr() as *const RawGpuSubmitEvent)
                };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} gpu_submit events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        Ok(())
    }
}
