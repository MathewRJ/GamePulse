/// Block I/O latency probe — userspace side.
///
/// Attaches two tracepoints from the gamepulse-ebpf-probes BPF object:
///   block/block_rq_issue
///   block/block_rq_complete
///
/// Drains the BIO_EVENTS ring buffer synchronously on every collect() call
/// (same pattern as SchedProbe — avoids AsyncFd/EPOLLET race).
use anyhow::{Context, Result};
use aya::{
    maps::{MapData, RingBuf},
    programs::TracePoint,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{BioAggregator, RawBioEvent};
use crate::es_model::EbpfMetricDoc;

pub struct BioProbe {
    aggregator: BioAggregator,
    ring_buf: Option<RingBuf<MapData>>,
}

impl BioProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        BioProbe {
            aggregator: BioAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }
}

impl Probe for BioProbe {
    fn name(&self) -> &'static str {
        "bio"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![
                "block/block_rq_issue",
                "block/block_rq_complete",
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

        attach(ebpf, "block_rq_issue",    "block", "block_rq_issue")?;
        attach(ebpf, "block_rq_complete", "block", "block_rq_complete")?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("BIO_EVENTS")
                .context("BIO_EVENTS map not found")?,
        )
        .context("BIO_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfMetricDoc>> {
        use std::mem::size_of;
        let event_size = size_of::<RawBioEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &*item;
                if bytes.len() < event_size {
                    warn!("bio ring buf item too small: {} < {}", bytes.len(), event_size);
                    continue;
                }
                // SAFETY: RawBioEvent is repr(C), size verified above.
                let event = unsafe {
                    std::ptr::read_unaligned(bytes.as_ptr() as *const RawBioEvent)
                };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} bio events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        Ok(())
    }
}
