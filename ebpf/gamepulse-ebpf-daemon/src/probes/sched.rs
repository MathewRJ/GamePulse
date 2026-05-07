/// Schedlatency probe — userspace side.
///
/// Attaches three tracepoints from the gamepulse-ebpf-probes BPF object:
///   sched/sched_wakeup
///   sched/sched_switch
///   sched/sched_migrate_task
///
/// Drains the SCHED_EVENTS ring buffer synchronously on every collect() call
/// (once per aggregation interval). This avoids the AsyncFd/EPOLLET race
/// where events written between the last rb.next()==None and clear_ready()
/// would be silently dropped, causing the drain task to hang indefinitely.
use anyhow::{Context, Result};
use aya::{
    maps::{HashMap as AyaHashMap, MapData, RingBuf},
    programs::TracePoint,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{RawSchedEvent, SchedAggregator};
use crate::es_model::EbpfDocument;

pub struct SchedProbe {
    aggregator: SchedAggregator,
    /// Ring buffer held here and drained synchronously in collect().
    ring_buf: Option<RingBuf<MapData>>,
}

impl SchedProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        SchedProbe {
            aggregator: SchedAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }

    /// Update the BPF game_pids_map with a new set of TIDs.
    /// Called by the loader when the session state changes.
    pub fn update_pids(ebpf: &mut Ebpf, tids: &[u32]) -> Result<()> {
        let mut map: AyaHashMap<&mut MapData, u32, u8> = AyaHashMap::try_from(
            ebpf.map_mut("GAME_PIDS")
                .context("GAME_PIDS map not found")?,
        )
        .context("GAME_PIDS map type mismatch")?;

        // Clear existing entries (aya HashMap doesn't have a clear() — iterate and remove)
        // Collect keys first to avoid borrow issues
        let existing_keys: Vec<u32> = map.keys().flatten().collect();
        for key in existing_keys {
            let _ = map.remove(&key);
        }

        // Insert new TIDs (value is unused — map acts as a set)
        for &tid in tids {
            map.insert(tid, 1u8, 0)
                .context("inserting TID into GAME_PIDS")?;
        }

        debug!("updated GAME_PIDS with {} TIDs", tids.len());
        Ok(())
    }
}

impl Probe for SchedProbe {
    fn name(&self) -> &'static str {
        "schedlatency"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![
                "sched/sched_wakeup",
                "sched/sched_switch",
                "sched/sched_migrate_task",
            ],
            kprobe_symbols: vec![],
            kernel_modules: vec![],
            min_kernel: (5, 8),
        }
    }

    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()> {
        // Attach the three tracepoints from the BPF object.
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

        attach(ebpf, "sched_wakeup", "sched", "sched_wakeup")?;
        attach(ebpf, "sched_switch", "sched", "sched_switch")?;
        attach(ebpf, "sched_migrate_task", "sched", "sched_migrate_task")?;

        // Take the ring buffer map and hold it here for synchronous draining in collect().
        let ring_buf = RingBuf::try_from(
            ebpf.take_map("SCHED_EVENTS")
                .context("SCHED_EVENTS map not found")?,
        )
        .context("SCHED_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfDocument>> {
        use std::mem::size_of;
        let event_size = size_of::<RawSchedEvent>();
        let mut event_count = 0usize;

        // Drain all events that arrived since the last collect() call.
        // Synchronous drain avoids the AsyncFd/EPOLLET race: rb.next() is
        // non-blocking and returns None immediately when the buffer is empty.
        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &*item;
                if bytes.len() < event_size {
                    warn!("ring buf item too small: {} < {}", bytes.len(), event_size);
                    continue;
                }
                // SAFETY: RawSchedEvent is repr(C), size verified above.
                let event =
                    unsafe { std::ptr::read_unaligned(bytes.as_ptr() as *const RawSchedEvent) };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} sched events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().map(Into::into).collect())
    }

    fn detach(&mut self) -> Result<()> {
        // Drop the ring buffer, releasing the map fd.
        self.ring_buf = None;
        Ok(())
    }
}
