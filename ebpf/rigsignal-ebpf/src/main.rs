/// rigsignal-ebpf — RigSignal eBPF kernel telemetry daemon.
///
/// Usage:
///   rigsignal-ebpf [--config <path>] [--probe-path <path>]
///
/// Requires: CAP_BPF + CAP_PERFMON (or root). BTF at /sys/kernel/btf/vmlinux.
/// Build BPF programs first: `cargo xtask build-ebpf`
mod aggregator;
mod config;
mod es_model;
mod loader;
mod probes;
mod session;
mod shipper;

use anyhow::{Context, Result};
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use clap::Parser;
use std::path::PathBuf;
use std::time::Duration;
use tokio::signal::unix::{signal, SignalKind};
use tokio::time::interval;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

use aggregator::correlate;
use config::Config;
use es_model::EbpfDocument;
use loader::load_probes;
use probes::bio::BioProbe;
use probes::futex::FutexProbe;
use probes::gpu_fence::GpuFenceProbe;
use probes::gpu_sched::GpuSchedProbe;
use probes::gpu_submit::GpuSubmitProbe;
use probes::irq::IrqProbe;
use probes::mem::MemProbe;
use probes::sched::SchedProbe;
use probes::vfs::VfsProbe;
use session::{session_file_path, spawn_watcher};
use shipper::EsShipper;

/// TSDB derives a metric document ID from its dimensions and @timestamp.  The
/// eBPF stream's probe discriminator is not a TSDB dimension, so snapshots
/// collected in the same millisecond would otherwise collide.  Keep every
/// probe in a stable, sub-second slot within the aggregation tick.
fn metric_timestamp_offset_ms(probe: &str) -> i64 {
    match probe {
        "schedlatency" => 0,
        "bio" => 1,
        "gpu_sched" => 2,
        "mem" => 3,
        "futex" => 4,
        "irq" => 5,
        "vfs" => 6,
        "gpu_fence" => 7,
        "gpu_submit" => 8,
        "stutter_correlation" => 9,
        // All current probes have an explicit slot. Keep an unknown future
        // probe distinct from schedlatency rather than recreating the collision.
        _ => 10,
    }
}

fn assign_metric_timestamps(docs: &mut [EbpfDocument], tick_timestamp: DateTime<Utc>) {
    for doc in docs {
        if let EbpfDocument::Metric(metric) = doc {
            metric.timestamp = tick_timestamp
                + ChronoDuration::milliseconds(metric_timestamp_offset_ms(
                    metric.rigsignal.ebpf.probe,
                ));
        }
    }
}

#[derive(Parser, Debug)]
#[command(
    name = "rigsignal-ebpf",
    about = "RigSignal eBPF kernel telemetry daemon"
)]
struct Cli {
    /// Path to rigsignal.toml (defaults to ~/.config/rigsignal/rigsignal.toml)
    #[arg(long)]
    config: Option<PathBuf>,

    /// Path to compiled BPF ELF (overrides config file setting)
    #[arg(long)]
    probe_path: Option<PathBuf>,

    /// Log level filter (e.g. info, debug, rigsignal_ebpf_daemon=debug)
    #[arg(long, default_value = "info")]
    log: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    // Logging
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(&cli.log)),
        )
        .init();

    // Config
    let config = if let Some(path) = cli.config {
        Config::load_from(&path)?
    } else {
        Config::load()?
    };

    let probe_path = cli
        .probe_path
        .unwrap_or_else(|| config.ebpf.probe_path.clone());

    info!("RigSignal eBPF daemon starting");
    info!("BPF probe path: {}", probe_path.display());
    info!("ES endpoint: {}", config.elasticsearch.endpoint);

    // Host metadata (filled once at startup)
    let host_name = std::fs::read_to_string("/etc/hostname")
        .unwrap_or_else(|_| "unknown".to_string())
        .trim()
        .to_string();
    let kernel_version = std::fs::read_to_string("/proc/sys/kernel/osrelease")
        .unwrap_or_else(|_| "unknown".to_string())
        .trim()
        .to_string();

    // Build probe candidates
    let candidates: Vec<Box<dyn probes::Probe>> = vec![
        Box::new(SchedProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(BioProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(GpuSchedProbe::new(
            host_name.clone(),
            kernel_version.clone(),
        )),
        Box::new(MemProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(FutexProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(IrqProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(VfsProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(GpuFenceProbe::new(
            host_name.clone(),
            kernel_version.clone(),
        )),
        Box::new(GpuSubmitProbe::new(
            host_name.clone(),
            kernel_version.clone(),
        )),
    ];

    // Load probes (does capability + BTF checks, attaches tracepoints)
    let mut loaded = load_probes(&probe_path, candidates).context("failed to load BPF probes")?;

    if loaded.loaded_count == 0 {
        error!("no probes loaded — nothing to do, exiting");
        std::process::exit(1);
    }

    // Session watcher
    let session_path = session_file_path();
    let (session_state, mut session_rx) =
        spawn_watcher(session_path).context("spawning session watcher")?;

    // Seed PID map from current session state
    {
        let state = session_state.lock().unwrap();
        if state.active {
            if let Err(e) = SchedProbe::update_pids(&mut loaded.ebpf, &state.tids) {
                warn!("could not seed GAME_PIDS: {e}");
            }
        }
    }

    // ES shipper
    let api_key = config.elasticsearch.api_key.as_deref().ok_or_else(|| {
        anyhow::anyhow!("No API key: set ES_API_KEY env var or api_key in rigsignal.toml")
    })?;
    let mut shipper = EsShipper::new(
        &config.elasticsearch.endpoint,
        api_key,
        config.elasticsearch.ca_cert.as_deref(),
    )
    .context("creating ES shipper")?;

    // Aggregation interval
    let interval_duration = Duration::from_secs(config.ebpf.interval_s);
    let mut tick = interval(interval_duration);

    // Signal handlers
    let mut sigint = signal(SignalKind::interrupt()).context("SIGINT handler")?;
    let mut sigterm = signal(SignalKind::terminate()).context("SIGTERM handler")?;

    info!(
        "daemon running — {}/{} probes active",
        loaded.loaded_count,
        loaded.loaded_count + loaded.skipped_count
    );

    loop {
        tokio::select! {
            // Aggregation tick
            _ = tick.tick() => {
                // All snapshots below describe this aggregation tick. Do not let
                // per-probe Utc::now() calls collapse to the same TSDB millisecond.
                let tick_timestamp = Utc::now();
                let session_id = {
                    let s = session_state.lock().unwrap();
                    if s.active {
                        s.info.as_ref().map(|i| i.session_id.clone())
                    } else {
                        None
                    }
                };

                let sid = match session_id {
                    Some(id) => id,
                    None => {
                        // No active session — skip unless background_baseline enabled
                        if !config.ebpf.background_baseline {
                            continue;
                        }
                        "no-session".to_string()
                    }
                };

                let mut tick_docs = Vec::new();
                for probe in &mut loaded.probes {
                    match probe.collect(&sid) {
                        Ok(docs) => tick_docs.extend(docs),
                        Err(e) => warn!("probe '{}' collect error: {e}", probe.name()),
                    }
                }

                // Stutter correlation: emit a cross-probe doc if ≥2 probes spiked.
                let metric_docs: Vec<_> = tick_docs.iter().filter_map(|doc| doc.as_metric()).collect();
                if let Some(corr_doc) = correlate(&metric_docs, &host_name, &kernel_version, &sid) {
                    tick_docs.push(corr_doc.into());
                }

                assign_metric_timestamps(&mut tick_docs, tick_timestamp);

                shipper.queue_all(tick_docs);

                if let Err(e) = shipper.flush().await {
                    warn!("ES flush error: {e}");
                }
            }

            // Session state change — update GAME_PIDS in BPF
            Some(new_state) = session_rx.recv() => {
                if new_state.active {
                    info!(
                        session_id = new_state.info.as_ref().map(|i| i.session_id.as_str()).unwrap_or(""),
                        tid_count = new_state.tids.len(),
                        "session started — updating PID filter"
                    );
                } else {
                    info!("session ended — clearing PID filter");
                }
                if let Err(e) = SchedProbe::update_pids(&mut loaded.ebpf, &new_state.tids) {
                    warn!("could not update GAME_PIDS: {e}");
                }
            }

            _ = sigint.recv() => {
                info!("received SIGINT — shutting down");
                break;
            }

            _ = sigterm.recv() => {
                info!("received SIGTERM — shutting down");
                break;
            }
        }
    }

    // Flush remaining docs before exit
    if let Err(e) = shipper.flush().await {
        warn!("final flush error: {e}");
    }

    // Detach probes
    for probe in &mut loaded.probes {
        if let Err(e) = probe.detach() {
            warn!("error detaching probe '{}': {e}", probe.name());
        }
    }

    info!("rigsignal-ebpf stopped");
    Ok(())
}

#[cfg(test)]
mod tests {
    use chrono::TimeZone;

    use super::{assign_metric_timestamps, Utc};
    use crate::{
        aggregator::{
            GpuAggregator, RawGpuSchedEvent, RawSchedEvent, SchedAggregator, EVENT_SWITCH,
        },
        es_model::EbpfDocument,
    };

    #[test]
    fn every_gpu_window_gets_a_tsdb_timestamp_distinct_from_schedlatency() {
        let mut sched = SchedAggregator::new("host".to_string(), "kernel".to_string());
        let mut gpu = GpuAggregator::new("host".to_string(), "kernel".to_string());
        let start = Utc.with_ymd_and_hms(2026, 7, 17, 20, 33, 30).unwrap();

        for second in 0..5 {
            sched.push(RawSchedEvent {
                event_type: EVENT_SWITCH,
                _pad: [0; 3],
                tid: 1,
                wait_ns: 7_000,
                prev_cpu: 0,
                next_cpu: 0,
                comm: [0; 16],
            });
            for _ in 0..1_000 {
                gpu.push(RawGpuSchedEvent {
                    latency_ns: 7_000,
                    _pad: 0,
                });
            }

            // Runtime invokes every probe in one aggregation tick, so these
            // documents can initially share the tick's millisecond in TSDB.
            let mut docs = sched.flush("session");
            docs.push(gpu.flush("session").unwrap().into());
            let tick_timestamp = start + chrono::Duration::seconds(second);
            for doc in &mut docs {
                if let EbpfDocument::Metric(metric) = doc {
                    // This mirrors the pre-fix per-probe Utc::now() calls after
                    // Elasticsearch rounds both calls to the same millisecond.
                    metric.timestamp = tick_timestamp;
                }
            }

            let initial_timestamps: Vec<_> = docs
                .iter()
                .filter_map(|doc| match doc {
                    EbpfDocument::Metric(metric) => Some(metric.timestamp),
                    EbpfDocument::Thread(_) => None,
                })
                .collect();
            assert!(initial_timestamps.windows(2).all(|pair| pair[0] == pair[1]));

            assign_metric_timestamps(&mut docs, tick_timestamp);

            let sched_timestamp = docs
                .iter()
                .find_map(|doc| match doc {
                    EbpfDocument::Metric(metric)
                        if metric.rigsignal.ebpf.probe == "schedlatency" =>
                    {
                        Some(metric.timestamp)
                    }
                    _ => None,
                })
                .unwrap();
            let gpu_doc = docs
                .iter()
                .find_map(|doc| match doc {
                    EbpfDocument::Metric(metric) if metric.rigsignal.ebpf.probe == "gpu_sched" => {
                        Some(metric)
                    }
                    _ => None,
                })
                .unwrap();

            assert_ne!(gpu_doc.timestamp, sched_timestamp);
            assert_eq!(
                gpu_doc.timestamp,
                tick_timestamp + chrono::Duration::milliseconds(2)
            );
            assert_eq!(
                gpu_doc
                    .rigsignal
                    .ebpf
                    .gpu_sched
                    .as_ref()
                    .unwrap()
                    .event_count,
                1_000
            );
        }
    }
}
