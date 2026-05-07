/// VFS read/write latency probe — userspace side.
///
/// Attaches kprobe/kretprobe pairs to `vfs_read` and `vfs_write`.
/// Filters to game PIDs; drains VFS_EVENTS ring buffer on collect().
///
/// Requires: `vfs_read` and `vfs_write` symbols in /proc/kallsyms.
/// On kernel 6.x these are exported (T) symbols, not inlined.
use anyhow::{Context, Result};
use aya::{
    maps::{MapData, RingBuf},
    programs::KProbe,
    Ebpf,
};
use tracing::{debug, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{RawVfsEvent, VfsAggregator};
use crate::es_model::EbpfDocument;

pub struct VfsProbe {
    aggregator: VfsAggregator,
    ring_buf: Option<RingBuf<MapData>>,
}

impl VfsProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        VfsProbe {
            aggregator: VfsAggregator::new(host_name, kernel_version),
            ring_buf: None,
        }
    }
}

impl Probe for VfsProbe {
    fn name(&self) -> &'static str {
        "vfs"
    }

    fn requirements(&self) -> ProbeRequirements {
        ProbeRequirements {
            tracepoints: vec![],
            kprobe_symbols: vec!["vfs_read", "vfs_write"],
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

        attach_kprobe(ebpf, "vfs_read_entry", "vfs_read")?;
        attach_kprobe(ebpf, "vfs_read_return", "vfs_read")?;
        attach_kprobe(ebpf, "vfs_write_entry", "vfs_write")?;
        attach_kprobe(ebpf, "vfs_write_return", "vfs_write")?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("VFS_EVENTS")
                .context("VFS_EVENTS map not found")?,
        )
        .context("VFS_EVENTS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfDocument>> {
        use std::mem::size_of;
        let event_size = size_of::<RawVfsEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &item;
                if bytes.len() < event_size {
                    warn!(
                        "vfs ring buf item too small: {} < {}",
                        bytes.len(),
                        event_size
                    );
                    continue;
                }
                let event =
                    unsafe { std::ptr::read_unaligned(bytes.as_ptr() as *const RawVfsEvent) };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} vfs events from ring buffer", event_count);
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().map(Into::into).collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        Ok(())
    }
}
