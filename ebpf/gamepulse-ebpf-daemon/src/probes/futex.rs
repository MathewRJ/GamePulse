/// Futex contention probe — userspace side.
///
/// Attaches kprobe/kretprobe to `do_futex`:
///   entry  — records start timestamp for game threads
///   return — computes futex latency, emits to FUTEX_EVENTS ring buffer
///
/// Requires: `do_futex` symbol in /proc/kallsyms (present since kernel 5.x).
use anyhow::{Context, Result};
use aya::{
    maps::{MapData, RingBuf},
    programs::KProbe,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{FutexAggregator, RawFutexEvent};
use crate::es_model::EbpfMetricDoc;

pub struct FutexProbe {
    aggregator: FutexAggregator,
    ring_buf: Option<RingBuf<MapData>>,
}

impl FutexProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        FutexProbe {
            aggregator: FutexAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }
}

impl Probe for FutexProbe {
    fn name(&self) -> &'static str {
        "futex"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![],
            kprobe_symbols: vec!["do_futex"],
            kernel_modules: vec![],
            min_kernel: (5, 8),
        }
    }

    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()> {
        // Entry kprobe
        let entry_prog: &mut KProbe = ebpf
            .program_mut("do_futex_entry")
            .context("program 'do_futex_entry' not found in BPF object")?
            .try_into()
            .context("expected KProbe program type")?;
        entry_prog.load().context("loading do_futex_entry")?;
        entry_prog
            .attach("do_futex", 0)
            .context("attaching do_futex_entry kprobe")?;

        // Return kretprobe
        let ret_prog: &mut KProbe = ebpf
            .program_mut("do_futex_return")
            .context("program 'do_futex_return' not found in BPF object")?
            .try_into()
            .context("expected KProbe program type (kretprobe)")?;
        ret_prog.load().context("loading do_futex_return")?;
        ret_prog
            .attach("do_futex", 0)
            .context("attaching do_futex_return kretprobe")?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("FUTEX_EVENTS")
                .context("FUTEX_EVENTS map not found")?,
        )
        .context("FUTEX_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfMetricDoc>> {
        use std::mem::size_of;
        let event_size = size_of::<RawFutexEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &item;
                if bytes.len() < event_size {
                    warn!("futex ring buf item too small: {} < {}", bytes.len(), event_size);
                    continue;
                }
                let event = unsafe {
                    std::ptr::read_unaligned(bytes.as_ptr() as *const RawFutexEvent)
                };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} futex events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        Ok(())
    }
}
