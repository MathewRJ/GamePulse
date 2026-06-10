/// IRQ and softirq latency probe — userspace side.
///
/// Attaches four tracepoints:
///   irq/irq_handler_entry  — hard-IRQ entry
///   irq/irq_handler_exit   — hard-IRQ exit (latency computed in BPF)
///   irq/softirq_entry      — softirq entry
///   irq/softirq_exit       — softirq exit (latency computed in BPF)
///
/// Drains the IRQ_EVENTS ring buffer on every collect() call.
use anyhow::{Context, Result};
use aya::{
    maps::{MapData, RingBuf},
    programs::TracePoint,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{IrqAggregator, RawIrqEvent};
use crate::es_model::EbpfDocument;

pub struct IrqProbe {
    aggregator: IrqAggregator,
    ring_buf: Option<RingBuf<MapData>>,
}

impl IrqProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        IrqProbe {
            aggregator: IrqAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }
}

impl Probe for IrqProbe {
    fn name(&self) -> &'static str {
        "irq"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![
                "irq/irq_handler_entry",
                "irq/irq_handler_exit",
                "irq/softirq_entry",
                "irq/softirq_exit",
            ],
            kprobe_symbols: vec![],
            kernel_modules: vec![],
            min_kernel: (5, 8),
        }
    }

    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()> {
        let attach =
            |ebpf: &mut Ebpf, prog_name: &str, category: &str, tp_name: &str| -> Result<()> {
                let prog: &mut TracePoint = ebpf
                    .program_mut(prog_name)
                    .with_context(|| format!("program '{}' not found in BPF object", prog_name))?
                    .try_into()
                    .context("expected TracePoint program type")?;
                prog.load()
                    .with_context(|| format!("loading {}", prog_name))?;
                prog.attach(category, tp_name).with_context(|| {
                    format!("attaching {} to {}/{}", prog_name, category, tp_name)
                })?;
                Ok(())
            };

        attach(ebpf, "irq_handler_entry", "irq", "irq_handler_entry")?;
        attach(ebpf, "irq_handler_exit", "irq", "irq_handler_exit")?;
        attach(ebpf, "softirq_entry", "irq", "softirq_entry")?;
        attach(ebpf, "softirq_exit", "irq", "softirq_exit")?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("IRQ_EVENTS")
                .context("IRQ_EVENTS map not found")?,
        )
        .context("IRQ_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfDocument>> {
        use std::mem::size_of;
        let event_size = size_of::<RawIrqEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &item;
                if bytes.len() < event_size {
                    warn!(
                        "irq ring buf item too small: {} < {}",
                        bytes.len(),
                        event_size
                    );
                    continue;
                }
                let event =
                    unsafe { std::ptr::read_unaligned(bytes.as_ptr() as *const RawIrqEvent) };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} irq events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().map(Into::into).collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        Ok(())
    }
}
