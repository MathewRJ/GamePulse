// Entry point for the GamePulse production agent.
// Phase 6: CPU + memory + storage collectors added. Remaining collectors added one per session.

mod collectors;
mod config;
mod shipper;

use anyhow::Result;
use clap::Parser;
use collectors::Collector;
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "gamepulse-agent", version, about = "GamePulse Linux telemetry agent")]
struct Cli {
    /// Path to config file [default: ~/.config/gamepulse/gamepulse.toml]
    #[arg(short, long, value_name = "PATH")]
    config: Option<PathBuf>,

    /// Skip ES connectivity check and exit 0 (runs one CPU sample for validation)
    #[arg(long)]
    dry_run: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    // Initialise tracing — stderr, INFO by default, overridden by GAMEPULSE_LOG.
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("GAMEPULSE_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_writer(std::io::stderr)
        .init();

    let cfg = config::Config::load(cli.config.as_ref())?;

    if cli.dry_run {
        tracing::info!("dry-run mode, skipping ES connectivity check");

        // CPU: first tick returns None (no delta yet), second returns data.
        let mut cpu = collectors::cpu::CpuCollector::new(None);
        let _ = cpu.collect();
        std::thread::sleep(std::time::Duration::from_secs(1));
        match cpu.collect()? {
            Some(doc) => tracing::info!("CPU sample:\n{}", serde_json::to_string_pretty(&doc)?),
            None => tracing::warn!("CPU collector returned None on second tick"),
        }

        // Memory: no delta required — both ticks return data; discard first, print second.
        let mut mem = collectors::memory::MemoryCollector::new(None);
        let _ = mem.collect();
        match mem.collect()? {
            Some(doc) => tracing::info!("Memory sample:\n{}", serde_json::to_string_pretty(&doc)?),
            None => tracing::warn!("Memory collector returned None on second tick"),
        }

        // Storage: delta-based — first tick returns None, second returns data.
        let mut stor = collectors::storage::StorageCollector::new();
        let _ = stor.collect();
        std::thread::sleep(std::time::Duration::from_secs(1));
        match stor.collect()? {
            Some(doc) => tracing::info!("Storage sample:\n{}", serde_json::to_string_pretty(&doc)?),
            None => tracing::warn!("Storage collector returned None on second tick"),
        }

        tracing::info!("GamePulse agent ready — 3 collectors loaded");
        return Ok(());
    }

    shipper::ping(&cfg).await?;
    tracing::info!("GamePulse agent ready — 1 collector loaded");
    Ok(())
}
