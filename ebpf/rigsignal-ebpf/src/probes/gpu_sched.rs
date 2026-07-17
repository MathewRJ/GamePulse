/// GPU scheduler latency probe — userspace side.
///
/// At attach time this selects either the renamed tracepoint pair used by newer
/// kernels or Valve 6.16's legacy pair. The selected format files are parsed to
/// obtain the u64 map-key offset; the BPF object contains no layout assumptions.
#[path = "gpu_sched/format.rs"]
mod format;

use std::{
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{bail, Context, Result};
use aya::{
    maps::{HashMap as AyaHashMap, MapData, PerCpuArray, RingBuf},
    programs::TracePoint,
    Ebpf,
};
use tracing::{debug, info, warn};

use super::{Probe, ProbeRequirements};
use crate::aggregator::{GpuAggregator, RawGpuSchedEvent};
use crate::es_model::EbpfDocument;

const GPU_SCHED_EVENTS_DIR: &str = "/sys/kernel/tracing/events/gpu_scheduler";
const KEY_OFFSET_INDEX: u32 = 0;
const LOSS_COUNTERS: usize = 4;
const LOSS_WARN_INTERVAL: Duration = Duration::from_secs(30);

#[derive(Clone, Copy, Debug)]
struct TracepointVariant {
    name: &'static str,
    queue_event: &'static str,
    run_event: &'static str,
    key_field: &'static str,
    scope_field: &'static str,
}

const RENAMED: TracepointVariant = TracepointVariant {
    name: "renamed",
    queue_event: "drm_sched_job_queue",
    run_event: "drm_sched_job_run",
    key_field: "fence_seqno",
    scope_field: "fence_context",
};

const LEGACY: TracepointVariant = TracepointVariant {
    name: "legacy",
    queue_event: "drm_sched_job",
    run_event: "drm_run_job",
    key_field: "id",
    scope_field: "entity",
};

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct GpuSchedKeyOffsets {
    scope: u32,
    sequence: u32,
}

unsafe impl aya::Pod for GpuSchedKeyOffsets {}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct LossCounters([u64; LOSS_COUNTERS]);

impl LossCounters {
    fn has_losses(self) -> bool {
        self.0.iter().any(|count| *count > 0)
    }

    fn minus(self, previous: Self) -> Self {
        Self(std::array::from_fn(|index| {
            self.0[index].saturating_sub(previous.0[index])
        }))
    }
}

struct LossWarningTracker {
    last_logged_at: Option<Instant>,
    last_logged: LossCounters,
}

impl LossWarningTracker {
    fn new() -> Self {
        Self {
            last_logged_at: None,
            last_logged: LossCounters::default(),
        }
    }

    fn observe(&mut self, counters: LossCounters, now: Instant) -> Option<LossCounters> {
        let delta = counters.minus(self.last_logged);
        if !delta.has_losses()
            || self
                .last_logged_at
                .is_some_and(|last| now.duration_since(last) < LOSS_WARN_INTERVAL)
        {
            return None;
        }
        self.last_logged_at = Some(now);
        self.last_logged = counters;
        Some(delta)
    }
}

pub struct GpuSchedProbe {
    aggregator: GpuAggregator,
    ring_buf: Option<RingBuf<MapData>>,
    loss_counters: Option<PerCpuArray<MapData, u64>>,
    loss_warning: LossWarningTracker,
}

impl GpuSchedProbe {
    pub fn new(host_name: String, kernel_version: String) -> Self {
        GpuSchedProbe {
            aggregator: GpuAggregator::new(host_name, kernel_version),
            ring_buf: None,
            loss_counters: None,
            loss_warning: LossWarningTracker::new(),
        }
    }
}

fn event_exists(events_dir: &Path, event: &str) -> bool {
    events_dir.join(event).is_dir()
}

fn select_variant(events_dir: &Path) -> Result<TracepointVariant> {
    // Newer kernels take priority; the legacy pair is a fallback for pre-rename
    // kernels (e.g. valve 6.16). Order matters when a kernel exposes both.
    for variant in [RENAMED, LEGACY] {
        if event_exists(events_dir, variant.queue_event)
            && event_exists(events_dir, variant.run_event)
        {
            return Ok(variant);
        }
    }

    bail!(
        "neither complete gpu_scheduler tracepoint pair is available under {}",
        events_dir.display()
    )
}

fn format_path(events_dir: &Path, event: &str) -> PathBuf {
    events_dir.join(event).join("format")
}

fn parse_variant_offset(
    events_dir: &Path,
    variant: TracepointVariant,
) -> Result<GpuSchedKeyOffsets> {
    let queue_path = format_path(events_dir, variant.queue_event);
    let run_path = format_path(events_dir, variant.run_event);
    let queue_format = std::fs::read_to_string(&queue_path)
        .with_context(|| format!("reading {}", queue_path.display()))?;
    let run_format = std::fs::read_to_string(&run_path)
        .with_context(|| format!("reading {}", run_path.display()))?;

    let queue_sequence = format::parse_key_field_offset(&queue_format, variant.key_field)
        .with_context(|| format!("parsing {}", queue_path.display()))?;
    let run_sequence = format::parse_key_field_offset(&run_format, variant.key_field)
        .with_context(|| format!("parsing {}", run_path.display()))?;

    if queue_sequence != run_sequence {
        bail!(
            "{} key field '{}' offset differs between pair: {} vs {}",
            variant.name,
            variant.key_field,
            queue_sequence,
            run_sequence
        );
    }
    let queue_scope = format::parse_key_field_offset(&queue_format, variant.scope_field)
        .with_context(|| format!("parsing {}", queue_path.display()))?;
    let run_scope = format::parse_key_field_offset(&run_format, variant.scope_field)
        .with_context(|| format!("parsing {}", run_path.display()))?;
    if queue_scope != run_scope {
        bail!(
            "{} scope field '{}' offset differs between pair: {} vs {}",
            variant.name,
            variant.scope_field,
            queue_scope,
            run_scope
        );
    }
    Ok(GpuSchedKeyOffsets {
        scope: queue_scope,
        sequence: queue_sequence,
    })
}

fn configure_key_offset(ebpf: &mut Ebpf, offsets: GpuSchedKeyOffsets) -> Result<()> {
    let mut map: AyaHashMap<&mut MapData, u32, GpuSchedKeyOffsets> = AyaHashMap::try_from(
        ebpf.map_mut("GPU_SCHED_KEY_OFFSET")
            .context("GPU_SCHED_KEY_OFFSET map not found")?,
    )
    .context("GPU_SCHED_KEY_OFFSET map type mismatch")?;
    map.insert(KEY_OFFSET_INDEX, offsets, 0)
        .context("writing GPU_SCHED_KEY_OFFSET")
}

fn attach_tracepoint(ebpf: &mut Ebpf, program: &str, tracepoint: &str) -> Result<()> {
    let prog: &mut TracePoint = ebpf
        .program_mut(program)
        .with_context(|| format!("program '{}' not found in BPF object", program))?
        .try_into()
        .context("expected TracePoint program type")?;
    prog.load().with_context(|| format!("loading {program}"))?;
    prog.attach("gpu_scheduler", tracepoint)
        .with_context(|| format!("attaching {program} to gpu_scheduler/{tracepoint}"))?;
    Ok(())
}

impl Probe for GpuSchedProbe {
    fn name(&self) -> &'static str {
        "gpu_sched"
    }

    fn requirements(&self) -> ProbeRequirements {
        // Tracepoint names are selected in attach(), because the kernel provides exactly
        // one of two valid pairs. Keeping this list empty lets its warn-skip path report
        // missing/malformed variants from the same selection logic.
        ProbeRequirements {
            tracepoints: vec![],
            kprobe_symbols: vec![],
            kernel_modules: vec![],
            min_kernel: (5, 8),
        }
    }

    fn attach(&mut self, ebpf: &mut Ebpf) -> Result<()> {
        let events_dir = Path::new(GPU_SCHED_EVENTS_DIR);
        let variant = select_variant(events_dir)?;
        let offset = parse_variant_offset(events_dir, variant)?;
        configure_key_offset(ebpf, offset)?;

        // This single structured line is intentionally grep-able for live acceptance.
        info!(
            variant = variant.name,
            key_field = variant.key_field,
            key_offset = offset.sequence,
            scope_field = variant.scope_field,
            scope_offset = offset.scope,
            "gpu_sched tracepoint variant selected"
        );

        attach_tracepoint(ebpf, variant.queue_event, variant.queue_event)?;
        attach_tracepoint(ebpf, variant.run_event, variant.run_event)?;

        let ring_buf = RingBuf::try_from(
            ebpf.take_map("GPU_SCHED_EVENTS")
                .context("GPU_SCHED_EVENTS map not found")?,
        )
        .context("GPU_SCHED_EVENTS map type mismatch")?;

        let loss_counters = PerCpuArray::try_from(
            ebpf.take_map("GPU_SCHED_LOSS")
                .context("GPU_SCHED_LOSS map not found")?,
        )
        .context("GPU_SCHED_LOSS map type mismatch")?;

        self.ring_buf = Some(ring_buf);
        self.loss_counters = Some(loss_counters);
        Ok(())
    }

    fn collect(&mut self, session_id: &str) -> Result<Vec<EbpfDocument>> {
        use std::mem::size_of;
        let event_size = size_of::<RawGpuSchedEvent>();
        let mut event_count = 0usize;

        if let Some(rb) = &mut self.ring_buf {
            while let Some(item) = rb.next() {
                let bytes: &[u8] = &*item;
                if bytes.len() < event_size {
                    warn!(
                        "gpu_sched ring buf item too small: {} < {}",
                        bytes.len(),
                        event_size
                    );
                    continue;
                }
                let event =
                    unsafe { std::ptr::read_unaligned(bytes.as_ptr() as *const RawGpuSchedEvent) };
                self.aggregator.push(event);
                event_count += 1;
            }
        }

        if event_count > 0 {
            debug!("drained {} gpu_sched events from ring buffer", event_count);
        }

        if let Some(counters) = self.read_loss_counters() {
            if let Some(delta) = self.loss_warning.observe(counters, Instant::now()) {
                warn!(
                    key_read = delta.0[0],
                    queue_insert = delta.0[1],
                    run_miss = delta.0[2],
                    ringbuf_reserve = delta.0[3],
                    "gpu_sched BPF loss counters increased"
                );
            }
        }

        let doc = self.aggregator.flush(session_id);
        Ok(doc.into_iter().map(Into::into).collect())
    }

    fn detach(&mut self) -> Result<()> {
        self.ring_buf = None;
        self.loss_counters = None;
        Ok(())
    }
}

impl GpuSchedProbe {
    fn read_loss_counters(&self) -> Option<LossCounters> {
        let counters = self.loss_counters.as_ref()?;
        let mut values = [0; LOSS_COUNTERS];
        for (index, value) in values.iter_mut().enumerate() {
            *value = counters.get(&(index as u32), 0).ok()?.iter().copied().sum();
        }
        Some(LossCounters(values))
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        path::PathBuf,
        time::{SystemTime, UNIX_EPOCH},
    };

    use super::{parse_variant_offset, select_variant, LEGACY, RENAMED};

    const LEGACY_FORMAT: &str = include_str!("gpu_sched/fixtures/valve-6.16-drm_sched_job.format");
    const RENAMED_FORMAT: &str =
        include_str!("gpu_sched/fixtures/synthetic-drm_sched_job_queue.format");

    fn events_dir() -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("rigsignal-gpu-sched-{unique}"))
    }

    fn write_pair(
        events_dir: &std::path::Path,
        variant: super::TracepointVariant,
        queue: &str,
        run: &str,
    ) {
        for (event, format) in [(variant.queue_event, queue), (variant.run_event, run)] {
            let event_dir = events_dir.join(event);
            fs::create_dir_all(&event_dir).unwrap();
            fs::write(event_dir.join("format"), format).unwrap();
        }
    }

    #[test]
    fn selects_renamed_pair_when_both_variants_are_present() {
        let events_dir = events_dir();
        write_pair(&events_dir, LEGACY, LEGACY_FORMAT, LEGACY_FORMAT);
        write_pair(&events_dir, RENAMED, RENAMED_FORMAT, RENAMED_FORMAT);
        assert_eq!(select_variant(&events_dir).unwrap().name, "renamed");
        fs::remove_dir_all(events_dir).unwrap();
    }

    #[test]
    fn rejects_mismatched_offsets_between_pair_events() {
        let events_dir = events_dir();
        let run = LEGACY_FORMAT.replacen("offset:32", "offset:40", 1);
        write_pair(&events_dir, LEGACY, LEGACY_FORMAT, &run);
        let error = parse_variant_offset(&events_dir, LEGACY).unwrap_err();
        assert!(error.to_string().contains("offset differs between pair"));
        fs::remove_dir_all(events_dir).unwrap();
    }

    #[test]
    fn parses_scoped_legacy_key() {
        let events_dir = events_dir();
        write_pair(&events_dir, LEGACY, LEGACY_FORMAT, LEGACY_FORMAT);
        assert_eq!(
            parse_variant_offset(&events_dir, LEGACY).unwrap(),
            super::GpuSchedKeyOffsets {
                scope: 8,
                sequence: 32,
            }
        );
        fs::remove_dir_all(events_dir).unwrap();
    }

    #[test]
    fn rate_limits_loss_warnings_without_losing_counts() {
        use std::time::{Duration, Instant};

        let start = Instant::now();
        let mut tracker = super::LossWarningTracker::new();
        assert_eq!(
            tracker.observe(super::LossCounters([0, 0, 3, 0]), start),
            Some(super::LossCounters([0, 0, 3, 0]))
        );
        assert_eq!(
            tracker.observe(
                super::LossCounters([0, 0, 5, 2]),
                start + Duration::from_secs(10)
            ),
            None
        );
        assert_eq!(
            tracker.observe(
                super::LossCounters([0, 0, 5, 2]),
                start + Duration::from_secs(30)
            ),
            Some(super::LossCounters([0, 0, 2, 2]))
        );
    }
}
