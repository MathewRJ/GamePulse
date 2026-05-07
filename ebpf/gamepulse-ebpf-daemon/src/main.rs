/// gamepulse-ebpf — GamePulse eBPF kernel telemetry daemon.
///
/// Usage:
///   gamepulse-ebpf [--config <path>] [--probe-path <path>]
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
use clap::Parser;
use std::path::PathBuf;
use std::time::Duration;
use tokio::signal::unix::{signal, SignalKind};
use tokio::time::interval;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

use aggregator::correlate;
use config::Config;
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

#[derive(Parser, Debug)]
#[command(name = "gamepulse-ebpf", about = "GamePulse eBPF kernel telemetry daemon")]
struct Cli {
    /// Path to gamepulse.toml (defaults to ~/.config/gamepulse/gamepulse.toml)
    #[arg(long)]
    config: Option<PathBuf>,

    /// Path to compiled BPF ELF (overrides config file setting)
    #[arg(long)]
    probe_path: Option<PathBuf>,

    /// Log level filter (e.g. info, debug, gamepulse_ebpf_daemon=debug)
    #[arg(long, default_value = "info")]
    log: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    // Logging
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new(&cli.log)),
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

    info!("GamePulse eBPF daemon starting");
    info!("BPF probe path: {}", probe_path.display());
    info!(
        "ES endpoint: {}",
        config.elasticsearch.endpoint
    );

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
        Box::new(GpuSchedProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(MemProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(FutexProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(IrqProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(VfsProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(GpuFenceProbe::new(host_name.clone(), kernel_version.clone())),
        Box::new(GpuSubmitProbe::new(host_name.clone(), kernel_version.clone())),
    ];

    // Load probes (does capability + BTF checks, attaches tracepoints)
    let mut loaded = load_probes(&probe_path, candidates)
        .context("failed to load BPF probes")?;

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
    let api_key = config.elasticsearch.api_key
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("No API key: set ES_API_KEY env var or api_key in gamepulse.toml"))?;
    let mut shipper = EsShipper::new(
        &config.elasticsearch.endpoint,
        api_key,
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
                if let Some(corr_doc) = correlate(&tick_docs, &host_name, &kernel_version, &sid) {
                    tick_docs.push(corr_doc);
                }

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

    info!("gamepulse-ebpf stopped");
    Ok(())
}
