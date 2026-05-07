/// GPU scheduler latency probe — userspace side.
///
/// Attaches two tracepoints from the gamepulse-ebpf-probes BPF object:
///   gpu_scheduler/drm_sched_job_queue
///   gpu_scheduler/drm_sched_job_run
///
/// Measures time from job queue entry to hardware dispatch per game GPU job.
use anyhow::{Context, Result};
use aya::{
    maps::{MapData, RingBuf},
    programs::TracePoint,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{GpuAggregator, RawGpuSchedEvent};
use crate::es_model::EbpfMetricDoc;

pub struct GpuSchedProbe {
    aggregator: GpuAggregator,
    ring_buf: Option<RingBuf<MapData>>,
}

impl GpuSchedProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        GpuSchedProbe {
            aggregator: GpuAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }
}

impl Probe for GpuSchedProbe {
    fn name(&self) -> &'static str {
        "gpu_sched"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![
                "gpu_scheduler/drm_sched_job_queue",
                "gpu_scheduler/drm_sched_job_run",
            ],
            kprobe_symbols: vec![],
            kernel_modules: vec![],
            min_kernel: (5, 8),
        }
    }

    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()> {
        let attach = |ebpf: &mut Ebpf, prog_name: &str, category: &str, tp_name: &str| -> Result<()> {
            let prog: &mut TracePoint = ebpf
                .program_mut(prog_name)
                .with_context(|| format!("program '{}' not found in BPF object", prog_name))?
                .try_into()
                .context("expected TracePoint program type")?;
            prog.load().with_context(|| format!("loading {}", prog_name))?;
            prog.attach(category, tp_name)
                .with_context(|| format!("attaching {} to {}/{}", prog_name, category, tp_name))?;
            Ok(())
        };

        attach(ebpf, "drm_sched_job_queue", "gpu_scheduler", "drm_sched_job_queue")?;
        attach(ebpf, "drm_sched_job_run",   "gpu_scheduler", "drm_sched_job_run")?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("GPU_SCHED_EVENTS")
                .context("GPU_SCHED_EVENTS map not found")?,
        )
        .context("GPU_SCHED_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfMetricDoc>> {
        use std::mem::size_of;
        let event_size = size_of::<RawGpuSchedEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &*item;
                if bytes.len() < event_size {
                    warn!("gpu_sched ring buf item too small: {} < {}", bytes.len(), event_size);
                    continue;
                }
                let event = unsafe {
                    std::ptr::read_unaligned(bytes.as_ptr() as *const RawGpuSchedEvent)
                };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} gpu_sched events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        Ok(())
    }
}
