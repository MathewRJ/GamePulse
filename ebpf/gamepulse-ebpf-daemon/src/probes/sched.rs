/// Schedlatency probe — userspace side.
///
/// Attaches three tracepoints from the gamepulse-ebpf-probes BPF object:
///   sched/sched_wakeup
///   sched/sched_switch
///   sched/sched_migrate_task
///
/// Maintains a SchedAggregator that consumes raw ring-buffer events and
/// produces EbpfMetricDocs at the configured interval.
use anyhow::{Context, Result};
use aya::{
    maps::{HashMap as AyaHashMap, MapData, RingBuf},
    programs::TracePoint,
    Ebpf,
};
use tokio::io::unix::AsyncFd;
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{RawSchedEvent, SchedAggregator};
use crate::es_model::EbpfMetricDoc;

pub struct SchedProbe {
    aggregator: SchedAggregator,
    /// Channel from the async drain task to this probe's collect().
    event_rx: Option<tokio::sync::mpsc::UnboundedReceiver<RawSchedEvent>>,
    /// Drain task handle (kept alive until detach).
    _drain_handle: Option<tokio::task::JoinHandle<()>>,
}

impl SchedProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        SchedProbe {
            aggregator: SchedAggregator::new(host_name, kernel_version),
            event_rx: None,
            _drain_handle: None,
        }
    }

    /// Update the BPF game_pids_map with a new set of TIDs.
    /// Called by the loader when the session state changes.
    pub fn update_pids(ebpf: &mut Ebpf, tids: &[u32]) -> Result<()> {
        let mut map: AyaHashMap<&mut MapData, u32, u8> =
            AyaHashMap::try_from(ebpf.map_mut("GAME_PIDS").context("GAME_PIDS map not found")?)
                .context("GAME_PIDS map type mismatch")?;

        // Clear existing entries (aya HashMap doesn't have a clear() — iterate and remove)
        // Collect keys first to avoid borrow issues
        let existing_keys: Vec<u32> = map.keys().flatten().collect();
        for key in existing_keys {
            let _ = map.remove(&key);
        }

        // Insert new TIDs (value is unused — map acts as a set)
        for &tid in tids {
            map.insert(tid, 1u8, 0).context("inserting TID into GAME_PIDS")?;
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

        attach(ebpf, "sched_wakeup",       "sched", "sched_wakeup")?;
        attach(ebpf, "sched_switch",       "sched", "sched_switch")?;
        attach(ebpf, "sched_migrate_task", "sched", "sched_migrate_task")?;

        // Spawn the async ring-buffer drain task.
        let ring_buf = RingBuf::try_from(
            ebpf.take_map("SCHED_EVENTS")
                .context("SCHED_EVENTS map not found")?,
        )
        .context("SCHED_EVENTS map type mismatch")?;

        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<RawSchedEvent>();

        let handle = tokio::spawn(async move {
            if let Err(e) = drain_ring_buf(ring_buf, tx).await {
                warn!("sched ring buffer drain error: {e}");
            }
        });

        self.event_rx = Some(rx);
        self._drain_handle = Some(handle);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfMetricDoc>> {
        // Drain all events that arrived since the last collect() call.
        if let Some(rx) = &mut self.event_rx {
            while let Ok(event) = rx.try_recv() {
                self.aggregator.push(event);
            }
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().collect())
    }

    fn detach(&mut self) -> Result<()> {
        if let Some(handle) = self._drain_handle.take() {
            handle.abort();
        }
        self.event_rx = None;
        Ok(())
    }
}

/// Async task: drain the ring buffer and forward events to the channel.
async fn drain_ring_buf(
    ring_buf: RingBuf<MapData>,
    tx: tokio::sync::mpsc::UnboundedSender<RawSchedEvent>,
) -> Result<()> {
    use std::mem::size_of;

    let mut async_fd = AsyncFd::new(ring_buf).context("creating AsyncFd for ring buffer")?;
    let event_size = size_of::<RawSchedEvent>();

    loop {
        let mut guard = async_fd.readable_mut().await.context("awaiting ring buf")?;
        {
            let rb = guard.get_inner_mut();
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &*item;
                if bytes.len() < event_size {
                    warn!("ring buf item too small: {} < {}", bytes.len(), event_size);
                    continue;
                }
                // SAFETY: SchedEvent is repr(C), size verified above.
                let event = unsafe {
                    std::ptr::read_unaligned(bytes.as_ptr() as *const RawSchedEvent)
                };
                if tx.send(event).is_err() {
                    return Ok(()); // Receiver dropped — daemon shutting down
                }
            }
        }
        guard.clear_ready();
    }
}
