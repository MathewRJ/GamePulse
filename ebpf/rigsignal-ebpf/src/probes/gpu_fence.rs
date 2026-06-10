/// DMA fence wait latency probe — userspace side.
///
/// Attaches kprobe/kretprobe to `dma_fence_default_wait`.
/// System-wide (no PID filter — fence waits happen in compositor/RADV threads).
/// Drains GPU_FENCE_EVENTS ring buffer on collect().
///
/// Requires: `dma_fence_default_wait` in /proc/kallsyms (DRM subsystem,
/// present on any kernel with GPU support).
use anyhow::{Context, Result};
use aya::{
    maps::{MapData, RingBuf},
    programs::KProbe,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{GpuFenceAggregator, RawGpuFenceEvent};
use crate::es_model::EbpfDocument;

pub struct GpuFenceProbe {
    aggregator: GpuFenceAggregator,
    ring_buf: Option<RingBuf<MapData>>,
}

impl GpuFenceProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        GpuFenceProbe {
            aggregator: GpuFenceAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }
}

impl Probe for GpuFenceProbe {
    fn name(&self) -> &'static str {
        "gpu_fence"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![],
            kprobe_symbols: vec!["dma_fence_default_wait"],
            kernel_modules: vec![],
            min_kernel: (5, 8),
        }
    }

    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()> {
        let attach_kprobe = |ebpf: &mut Ebpf, prog_name: &str, symbol: &str| -> Result<()> {
            let prog: &mut KProbe = ebpf
                .program_mut(prog_name)
                .with_context(|| format!("program '{}' not found in BPF object", prog_name))?
                .try_into()
                .context("expected KProbe program type")?;
            prog.load()
                .with_context(|| format!("loading {}", prog_name))?;
            prog.attach(symbol, 0)
                .with_context(|| format!("attaching {} kprobe on {}", prog_name, symbol))?;
            Ok(())
        };

        attach_kprobe(
            ebpf,
            "dma_fence_default_wait_entry",
            "dma_fence_default_wait",
        )?;
        attach_kprobe(
            ebpf,
            "dma_fence_default_wait_return",
            "dma_fence_default_wait",
        )?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("GPU_FENCE_EVENTS")
                .context("GPU_FENCE_EVENTS map not found")?,
        )
        .context("GPU_FENCE_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfDocument>> {
        use std::mem::size_of;
        let event_size = size_of::<RawGpuFenceEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &item;
                if bytes.len() < event_size {
                    warn!(
                        "gpu_fence ring buf item too small: {} < {}",
                        bytes.len(),
                        event_size
                    );
                    continue;
                }
                let event =
                    unsafe { std::ptr::read_unaligned(bytes.as_ptr() as *const RawGpuFenceEvent) };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} gpu_fence events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().map(Into::into).collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        Ok(())
    }
}
