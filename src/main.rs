// GamePulse production agent — Phase 6 main loop.
//
// Wires all 8 collectors into a 1-second tick loop, handles game detection,
// writes session.json for the eBPF daemon, ships docs to Elasticsearch, and
// builds a session summary document on shutdown.
//
// Mirrors the structure of collector/gamepulse/cli.py exactly.

#[cfg(all(feature = "ebpf", not(target_os = "linux")))]
compile_error!(
    "the 'ebpf' feature is only supported on Linux (aya/BPF syscalls have no Windows equivalent)"
);

mod collectors;
mod config;
mod host;
mod session;
mod shipper;

use anyhow::Result;
use chrono::Utc;
use clap::Parser;
use collectors::Collector;
use serde_json::{json, Value};
use session::SessionEvent;
use std::path::PathBuf;

// ── CLI ───────────────────────────────────────────────────────────────────────

#[derive(Parser)]
#[command(
    name = "gamepulse-agent",
    version,
    about = "GamePulse Linux telemetry agent"
)]
struct Cli {
    /// Path to config file [default: ~/.config/gamepulse/gamepulse.toml]
    #[arg(short, long, value_name = "PATH")]
    config: Option<PathBuf>,

    /// Run one collection cycle, print output, exit without shipping to ES
    #[arg(long)]
    dry_run: bool,

    /// Short annotation for this session (e.g. "after-driver-update").
    /// Overrides [session].label in the config file.
    #[arg(long, value_name = "TEXT")]
    label: Option<String>,

    // ── Tier 1 settings flags (B.7) ───────────────────────────────────────────
    /// Graphics preset: low | medium | high | ultra | custom | unknown
    #[arg(long, value_name = "VALUE")]
    preset: Option<String>,

    /// Upscaler technology and optional quality preset: tech[:preset]
    /// e.g. dlss:quality  fsr:balanced  xess
    #[arg(long, value_name = "TECH[:PRESET]")]
    upscaler: Option<String>,

    /// Frame generation technology: dlss3 | fsr3 | afmf | lossless-scaling | none
    #[arg(long, value_name = "TECH")]
    frame_gen: Option<String>,

    /// Comma-separated list of active features
    /// e.g. ray_tracing,path_tracing,direct_storage
    #[arg(long, value_name = "FEATURE,...")]
    features: Option<String>,

    /// Output render resolution, e.g. 3440x1440
    #[arg(long, value_name = "WxH")]
    resolution: Option<String>,

    /// VSync mode: off | on | adaptive | fast
    #[arg(long, value_name = "off|on|adaptive|fast")]
    vsync: Option<String>,

    /// Free-text notes for this session, e.g. "engineering sample GPU"
    #[arg(long, value_name = "TEXT")]
    notes: Option<String>,

    // ── Target override (B2.7) ────────────────────────────────────────────────
    /// Skip auto-detection and monitor a specific process ID.
    /// The process must be running when the agent starts.
    #[arg(long, value_name = "PID")]
    target_pid: Option<u32>,

    /// Skip auto-detection; find a process by name (matched against /proc/*/comm
    /// and /proc/*/exe basename, case-insensitive). First match wins.
    #[arg(long, value_name = "NAME")]
    target_name: Option<String>,
}

// ── Utilities ──────────────────────────────────────────────────────────────────

/// RFC3339 UTC timestamp matching Python's datetime.datetime.now(utc).isoformat()
fn utc_now() -> String {
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

/// Recursively deep-merge `overlay` into `base`. Later values win on scalar
/// conflicts; nested objects are merged rather than replaced.
/// Matches Python _merge_docs() in cli.py exactly.
fn deep_merge(mut base: Value, overlay: Value) -> Value {
    match (&mut base, overlay) {
        (Value::Object(base_map), Value::Object(overlay_map)) => {
            for (k, v) in overlay_map {
                match base_map.get_mut(&k) {
                    Some(existing) if existing.is_object() && v.is_object() => {
                        let owned = existing.take();
                        *existing = deep_merge(owned, v);
                    }
                    _ => {
                        base_map.insert(k, v);
                    }
                }
            }
        }
        (base, overlay) => *base = overlay,
    }
    base
}

/// Check liveness of a user-pinned target and synthesise the appropriate SessionEvent.
fn poll_pinned_target(
    pinned: &session::Target,
    current: &mut Option<session::Target>,
) -> session::SessionEvent {
    let alive = std::path::Path::new(&format!("/proc/{}", pinned.pid)).exists();
    match (current.is_some(), alive) {
        (false, true) => {
            *current = Some(pinned.clone());
            session::SessionEvent::GameStarted(pinned.clone())
        }
        (true, false) => {
            let old = current.take().unwrap();
            session::SessionEvent::GameEnded(old)
        }
        _ => session::SessionEvent::NoChange,
    }
}

/// Add data_stream routing fields to a doc so the shipper can derive the index.
fn add_data_stream(doc: &mut Value, dataset: &str) {
    if let Some(obj) = doc.as_object_mut() {
        obj.insert(
            "data_stream".to_string(),
            json!({
                "type": "metrics",
                "dataset": dataset,
                "namespace": "default",
            }),
        );
    }
}

// ── Tier 1 settings overlay builder (B.7) ────────────────────────────────────

/// Build the `{ "gamepulse": { "settings": { … } } }` overlay from merged
/// CLI and config values. Returns `None` if no settings were provided at all
/// (so the key is simply absent from session docs).
#[allow(clippy::too_many_arguments)]
fn build_settings_overlay(
    preset: Option<&str>,
    upscaler_tech: Option<&str>,
    upscaler_preset: Option<&str>,
    frame_gen_tech: Option<&str>,
    features_active: Option<&[String]>,
    resolution: Option<&str>,
    vsync: Option<&str>,
    notes: Option<&str>,
) -> Option<Value> {
    if preset.is_none()
        && upscaler_tech.is_none()
        && frame_gen_tech.is_none()
        && features_active.is_none()
        && resolution.is_none()
        && vsync.is_none()
        && notes.is_none()
    {
        return None;
    }

    let mut s = serde_json::Map::new();

    if let Some(p) = preset {
        s.insert("preset".into(), Value::String(p.to_string()));
    }

    if upscaler_tech.is_some() || upscaler_preset.is_some() {
        let mut u = serde_json::Map::new();
        if let Some(t) = upscaler_tech {
            u.insert("tech".into(), Value::String(t.to_string()));
        }
        if let Some(p) = upscaler_preset {
            u.insert("preset".into(), Value::String(p.to_string()));
        }
        s.insert("upscaler".into(), Value::Object(u));
    }

    if let Some(ft) = frame_gen_tech {
        let mut fg = serde_json::Map::new();
        fg.insert("tech".into(), Value::String(ft.to_string()));
        s.insert("frame_gen".into(), Value::Object(fg));
    }

    if let Some(feats) = features_active {
        let arr: Vec<Value> = feats.iter().map(|f| Value::String(f.clone())).collect();
        s.insert("features_active".into(), Value::Array(arr));
    }

    if resolution.is_some() || vsync.is_some() {
        let mut r = serde_json::Map::new();
        if let Some(res) = resolution {
            r.insert("resolution_output".into(), Value::String(res.to_string()));
        }
        if let Some(vs) = vsync {
            r.insert("vsync".into(), Value::String(vs.to_string()));
        }
        s.insert("render".into(), Value::Object(r));
    }

    if let Some(n) = notes {
        s.insert("notes".into(), Value::String(n.to_string()));
    }

    s.insert("source".into(), Value::String("manual".to_string()));
    s.insert("confidence".into(), Value::String("high".to_string()));

    Some(json!({ "gamepulse": { "settings": Value::Object(s) } }))
}

/// Parse "--upscaler dlss:quality" into (tech, Option<preset>).
fn parse_upscaler(s: &str) -> (String, Option<String>) {
    match s.find(':') {
        Some(pos) => {
            let tech = s[..pos].to_string();
            let preset = s[pos + 1..].to_string();
            (
                tech,
                if preset.is_empty() {
                    None
                } else {
                    Some(preset)
                },
            )
        }
        None => (s.to_string(), None),
    }
}

// ── Session document builders ─────────────────────────────────────────────────

/// Ship a session-start document when the agent comes online.
fn build_session_start_doc(
    session: &session::SessionManager,
    host_snapshot: &Value,
    hostname: &str,
) -> Value {
    let base = session.base_doc(hostname);
    let ts = json!({ "@timestamp": utc_now() });
    let mut doc = deep_merge(deep_merge(ts, base), host_snapshot.clone());
    if let Some(settings) = &session.settings_overlay {
        doc = deep_merge(doc, settings.clone());
    }
    add_data_stream(&mut doc, "gamepulse.session");
    doc
}

/// Ship an updated session document when a target is first detected.
fn build_game_detected_doc(
    session: &session::SessionManager,
    host_snapshot: &Value,
    hostname: &str,
    target: &session::Target,
) -> Value {
    let base = session.base_doc(hostname);
    let ts = json!({ "@timestamp": utc_now() });
    let mut compat = serde_json::Map::new();
    if let Some(v) = &target.proton_version {
        compat.insert("proton_version".to_string(), Value::String(v.clone()));
    }
    if let Some(v) = &target.dxvk_version {
        compat.insert("dxvk_version".to_string(), Value::String(v.clone()));
    }
    let compat_overlay = if compat.is_empty() {
        json!({})
    } else {
        json!({ "gamepulse": { "compatibility": compat } })
    };
    let mut doc = deep_merge(
        deep_merge(deep_merge(ts, base), host_snapshot.clone()),
        compat_overlay,
    );
    if let Some(settings) = &session.settings_overlay {
        doc = deep_merge(doc, settings.clone());
    }
    add_data_stream(&mut doc, "gamepulse.session");
    doc
}

// ── Accumulator helpers ───────────────────────────────────────────────────────

struct SessionAccumulators {
    fps_samples: Vec<f64>,
    frametime_samples: Vec<f64>,
    stutter_total: i64,
    peak_gpu_temp: Option<f64>,
    peak_cpu_temp: Option<f64>,
    peak_gpu_power: Option<f64>,
    gpu_bottleneck_ticks: i64,
    cpu_bottleneck_ticks: i64,
    balanced_ticks: i64,
}

impl SessionAccumulators {
    fn new() -> Self {
        SessionAccumulators {
            fps_samples: Vec::new(),
            frametime_samples: Vec::new(),
            stutter_total: 0,
            peak_gpu_temp: None,
            peak_cpu_temp: None,
            peak_gpu_power: None,
            gpu_bottleneck_ticks: 0,
            cpu_bottleneck_ticks: 0,
            balanced_ticks: 0,
        }
    }

    /// Update accumulators from the tick's doc list.
    fn update(&mut self, docs: &[Value]) {
        let mut tick_gpu_util: Option<f64> = None;
        let mut tick_cpu_util: Option<f64> = None;

        for doc in docs {
            let gp = match doc.get("gamepulse").and_then(|g| g.as_object()) {
                Some(g) => g,
                None => continue,
            };

            if let Some(gpu) = gp.get("gpu").and_then(|g| g.as_object()) {
                tick_gpu_util = gpu.get("utilisation_pct").and_then(|v| v.as_f64());
                if let Some(t) = gpu.get("temperature_c").and_then(|v| v.as_f64()) {
                    self.peak_gpu_temp = Some(self.peak_gpu_temp.map_or(t, |p: f64| p.max(t)));
                }
                if let Some(p) = gpu.get("power_w").and_then(|v| v.as_f64()) {
                    self.peak_gpu_power =
                        Some(self.peak_gpu_power.map_or(p, |prev: f64| prev.max(p)));
                }
            }

            if let Some(cpu) = gp.get("cpu").and_then(|g| g.as_object()) {
                tick_cpu_util = cpu.get("total_utilisation_pct").and_then(|v| v.as_f64());
                if let Some(t) = cpu.get("temperature_c").and_then(|v| v.as_f64()) {
                    self.peak_cpu_temp = Some(self.peak_cpu_temp.map_or(t, |p: f64| p.max(t)));
                }
            }

            if let Some(fps) = gp.get("fps").and_then(|g| g.as_object()) {
                if let Some(avg) = fps.get("avg_1s").and_then(|v| v.as_f64()) {
                    self.fps_samples.push(avg);
                }
                if let Some(sc) = fps.get("stutter_count").and_then(|v| v.as_i64()) {
                    self.stutter_total += sc;
                }
                if let Some(ft) = fps.get("frametime_ms").and_then(|v| v.as_f64()) {
                    self.frametime_samples.push(ft);
                }
            }
        }

        if let (Some(gpu_util), Some(cpu_util)) = (tick_gpu_util, tick_cpu_util) {
            if gpu_util > 90.0 {
                self.gpu_bottleneck_ticks += 1;
            } else if cpu_util > 90.0 {
                self.cpu_bottleneck_ticks += 1;
            } else {
                self.balanced_ticks += 1;
            }
        }
    }

    fn bottleneck_dominant(&self) -> Option<&'static str> {
        let total = self.gpu_bottleneck_ticks + self.cpu_bottleneck_ticks + self.balanced_ticks;
        if total == 0 {
            return None;
        }
        if self.gpu_bottleneck_ticks >= self.cpu_bottleneck_ticks
            && self.gpu_bottleneck_ticks >= self.balanced_ticks
        {
            Some("gpu")
        } else if self.cpu_bottleneck_ticks >= self.balanced_ticks {
            Some("cpu")
        } else {
            Some("balanced")
        }
    }
}

/// Build the session-end summary document. Matches Python cli.py finally block.
fn build_summary_doc(
    session: &session::SessionManager,
    host_snapshot: &Value,
    hostname: &str,
    duration_s: u64,
    acc: &SessionAccumulators,
    last_game: Option<&session::Target>,
) -> Value {
    let interval = 1.0_f64; // 1-second collection interval

    let mut summary = serde_json::Map::new();
    summary.insert("ended".to_string(), Value::Bool(true));
    summary.insert("duration_s".to_string(), Value::from(duration_s));
    summary.insert("stutter_count".to_string(), Value::from(acc.stutter_total));

    if !acc.fps_samples.is_empty() {
        let mut sorted = acc.fps_samples.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let avg_fps = (acc.fps_samples.iter().sum::<f64>() / acc.fps_samples.len() as f64 * 10.0)
            .round()
            / 10.0;
        summary.insert("avg_fps".to_string(), Value::from(avg_fps));
        let n = sorted.len();
        let low_idx = (n as f64 * 0.01) as usize;
        let low_idx = low_idx.saturating_sub(1).min(n - 1);
        summary.insert(
            "low_1pct_fps".to_string(),
            Value::from(sorted[low_idx] as i64),
        );
        let total_frames = (acc.fps_samples.iter().sum::<f64>() * interval).round() as i64;
        summary.insert("total_frames".to_string(), Value::from(total_frames));
    }

    if !acc.frametime_samples.is_empty() {
        let mut ft_sorted = acc.frametime_samples.clone();
        ft_sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = ft_sorted.len();
        let p99_idx = (n as f64 * 0.99) as usize;
        let p99_idx = p99_idx.saturating_sub(1).min(n - 1);
        let p99 = (ft_sorted[p99_idx] * 100.0).round() / 100.0;
        summary.insert("p99_frametime_ms".to_string(), Value::from(p99));
    }

    if let Some(t) = acc.peak_gpu_temp {
        summary.insert("peak_gpu_temp_c".to_string(), Value::from(t));
    }
    if let Some(t) = acc.peak_cpu_temp {
        summary.insert("peak_cpu_temp_c".to_string(), Value::from(t));
    }
    if let Some(p) = acc.peak_gpu_power {
        summary.insert("peak_gpu_power_w".to_string(), Value::from(p));
    }
    if let Some(bn) = acc.bottleneck_dominant() {
        summary.insert(
            "bottleneck_dominant".to_string(),
            Value::String(bn.to_string()),
        );
    }

    let mut base = session.base_doc(hostname);

    // If the target exited before the summary is built, session.current_game is None.
    // Inject the last known target fields so gamepulse.game.* appear in the summary.
    if let (Some(target), None) = (last_game, session.current_game.as_ref()) {
        let overlay = json!({ "gamepulse": { "game": session::target_to_game_doc(target) } });
        base = deep_merge(base, overlay);
    }

    let ts = json!({ "@timestamp": utc_now() });
    let summary_overlay = json!({ "gamepulse": { "summary": summary } });
    let mut doc = deep_merge(
        deep_merge(deep_merge(ts, base), host_snapshot.clone()),
        summary_overlay,
    );
    if let Some(settings) = &session.settings_overlay {
        doc = deep_merge(doc, settings.clone());
    }
    add_data_stream(&mut doc, "gamepulse.session");
    doc
}

// ── Collector assembly ────────────────────────────────────────────────────────

/// Build the platform-appropriate set of collectors. Types resolve via the
/// cfg-gated `pub use` in collectors/mod.rs — Linux pulls from `linux::*`,
/// Windows from `windows::*` (stubs that return None in B.3).
fn build_collectors(game_pid: Option<u32>) -> Vec<Box<dyn Collector>> {
    vec![
        Box::new(collectors::CpuCollector::new(game_pid)),
        Box::new(collectors::MemoryCollector::new(game_pid)),
        Box::new(collectors::StorageCollector::new(game_pid)),
        Box::new(collectors::NetworkCollector::new(game_pid)),
        Box::new(collectors::PowerCollector::new(game_pid)),
        Box::new(collectors::AudioCollector::new(game_pid)),
        Box::new(collectors::MangoHudCollector::new(game_pid)),
        Box::new(collectors::GpuCollector::new(game_pid)),
    ]
}

// ── Dry-run ───────────────────────────────────────────────────────────────────

async fn dry_run() -> Result<()> {
    tracing::info!("dry-run mode — validating collectors");

    let snapshot = host::collect_snapshot();
    tracing::info!(
        "Host snapshot:\n{}",
        serde_json::to_string_pretty(&snapshot)?
    );

    let mut collectors = build_collectors(None);
    tracing::info!("Loaded {} collectors", collectors.len());

    // Two-tick warmup for delta-based collectors.
    for c in &mut collectors {
        let _ = c.collect();
    }
    std::thread::sleep(std::time::Duration::from_secs(1));

    for c in &mut collectors {
        let dataset = c.dataset();
        match c.collect()? {
            Some(doc) => tracing::info!(
                "{} sample:\n{}",
                dataset,
                serde_json::to_string_pretty(&doc)?
            ),
            None => tracing::info!("{}: no data this tick", dataset),
        }
    }

    tracing::info!(
        "dry-run complete — {} collectors exercised",
        collectors.len()
    );
    Ok(())
}

// ── Main loop ─────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("GAMEPULSE_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_writer(std::io::stderr)
        .init();

    let cfg = config::Config::load(cli.config.as_ref())?;

    if cli.dry_run {
        return dry_run().await;
    }

    // ES connectivity check
    shipper::ping(&cfg).await?;

    // Collect host info once at startup
    tracing::info!("Collecting host environment snapshot…");
    let host_snapshot = host::collect_snapshot();
    let hostname = host::hostname();

    // Instantiate all collectors via platform-appropriate builder.
    let mut collectors = build_collectors(None);

    // Session manager — CLI --label overrides [session].label in config.
    let session_label = cli.label.or_else(|| cfg.session.label.clone());

    // Resolve Tier 1 settings: CLI flags override [session.settings] config.
    let (upscaler_tech, upscaler_preset) = cli.upscaler.as_deref().map(parse_upscaler).unwrap_or((
        cfg.session
            .settings
            .upscaler_tech
            .clone()
            .unwrap_or_default(),
        cfg.session.settings.upscaler_preset.clone(),
    ));
    let upscaler_tech_ref = if upscaler_tech.is_empty() {
        None
    } else {
        Some(upscaler_tech.as_str())
    };

    let preset = cli
        .preset
        .as_deref()
        .or(cfg.session.settings.preset.as_deref());
    let frame_gen_tech =
        cli.frame_gen
            .as_deref()
            .or(cfg.session.settings.frame_gen_tech.as_deref());
    let resolution =
        cli.resolution
            .as_deref()
            .or(cfg.session.settings.render_resolution_output.as_deref());
    let vsync = cli
        .vsync
        .as_deref()
        .or(cfg.session.settings.render_vsync.as_deref());
    let notes = cli
        .notes
        .as_deref()
        .or(cfg.session.settings.notes.as_deref());

    // CLI --features overrides config features_active (comma-separated string vs vec).
    let cli_features: Option<Vec<String>> = cli
        .features
        .as_deref()
        .map(|s| s.split(',').map(|f| f.trim().to_string()).collect());
    let features_active: Option<Vec<String>> =
        cli_features.or_else(|| cfg.session.settings.features_active.clone());

    let settings_overlay = build_settings_overlay(
        preset,
        upscaler_tech_ref,
        upscaler_preset.as_deref(),
        frame_gen_tech,
        features_active.as_deref(),
        resolution,
        vsync,
        notes,
    );

    // Resolve CLI/config target override (B2.7) — takes precedence over auto-detection.
    let pinned_pid = cli.target_pid.or(cfg.session.target_pid);
    let pinned_name = cli
        .target_name
        .as_deref()
        .or(cfg.session.target_name.as_deref())
        .map(|s| s.to_string());
    let pinned_target: Option<session::Target> = if pinned_pid.is_some() || pinned_name.is_some() {
        let t = session::resolve_user_target(pinned_pid, pinned_name.as_deref());
        if t.is_none() {
            tracing::warn!("User-specified target not found — falling back to auto-detection");
        }
        t
    } else {
        None
    };

    let mut session = session::SessionManager::new_with_label_and_settings(
        session_label.clone(),
        settings_overlay,
    );
    tracing::info!(
        "Session {} started{}",
        session.session_id,
        session_label
            .as_deref()
            .map(|l| format!(" (label: {l})"))
            .unwrap_or_default()
    );

    // Ship session-start document
    let start_doc = build_session_start_doc(&session, &host_snapshot, &hostname);
    if let Err(e) = shipper::ship(&cfg, vec![start_doc]).await {
        tracing::warn!("Failed to ship session-start doc: {}", e);
    }

    // Signal handlers — spawned watcher sends on a oneshot so the select! arm
    // is platform-neutral (tokio's select! macro doesn't support #[cfg] on arms).
    let (shutdown_tx, mut shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    #[cfg(unix)]
    tokio::spawn(async move {
        use tokio::signal::unix::SignalKind;
        let mut sigterm =
            tokio::signal::unix::signal(SignalKind::terminate()).expect("SIGTERM handler");
        let mut sigint =
            tokio::signal::unix::signal(SignalKind::interrupt()).expect("SIGINT handler");
        tokio::select! {
            _ = sigterm.recv() => tracing::info!("SIGTERM received — shutting down"),
            _ = sigint.recv() => tracing::info!("SIGINT received — shutting down"),
        }
        let _ = shutdown_tx.send(());
    });
    #[cfg(not(unix))]
    tokio::spawn(async move {
        tokio::signal::ctrl_c().await.ok();
        tracing::info!("Ctrl-C received — shutting down");
        let _ = shutdown_tx.send(());
    });

    // Accumulators for summary doc
    let mut session_start = std::time::Instant::now();
    let mut tick: u64 = 0;
    let mut session_tick: u64 = 0; // resets each time a game exits
    let mut acc = SessionAccumulators::new();
    // Track last seen target so summary doc includes game.name even after the target exits.
    let mut last_known_game: Option<session::Target> = None;

    // 1-second tick interval; skip missed ticks (don't burst-catch-up).
    let mut interval = tokio::time::interval(std::time::Duration::from_secs(1));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    loop {
        tokio::select! {
            _ = interval.tick() => {
                tick += 1;
                session_tick += 1;
                let ts = utc_now();

                // ── Game detection ────────────────────────────────────────────
                match if let Some(ref pinned) = pinned_target {
                    poll_pinned_target(pinned, &mut session.current_game)
                } else {
                    session.poll()
                } {
                    SessionEvent::GameStarted(target) => {
                        tracing::info!(
                            "Game detected: {} (pid {}, all_pids={:?})",
                            target.display_name, target.pid, target.all_pids
                        );
                        for c in &mut collectors {
                            c.set_game_pid(Some(target.pid));
                        }
                        last_known_game = Some(target.clone());
                        let game_doc = build_game_detected_doc(
                            &session, &host_snapshot, &hostname, &target,
                        );
                        if let Err(e) = shipper::ship(&cfg, vec![game_doc]).await {
                            tracing::warn!("Failed to ship game-detected doc: {}", e);
                        }
                    }
                    SessionEvent::GameEnded(target) => {
                        tracing::info!("Game exited: {}", target.display_name);
                        for c in &mut collectors {
                            c.set_game_pid(None);
                        }
                        // Ship summary for the session that just ended.
                        if session_tick > 0 {
                            let duration_s = session_start.elapsed().as_secs();
                            let summary_doc = build_summary_doc(
                                &session,
                                &host_snapshot,
                                &hostname,
                                duration_s,
                                &acc,
                                Some(&target),
                            );
                            tracing::info!(
                                "Shipping session summary on game exit ({}s, {} ticks)",
                                duration_s, session_tick
                            );
                            if let Err(e) = shipper::ship(&cfg, vec![summary_doc]).await {
                                tracing::warn!("Failed to ship summary doc on game exit: {}", e);
                            } else {
                                if let Err(e) = shipper::trigger_transform_sync(&cfg, "gamepulse-game-timeline").await {
                                    tracing::warn!("transform schedule_now failed (non-fatal): {}", e);
                                }
                            }
                        }
                        // Reset per-session state for next game.
                        acc = SessionAccumulators::new();
                        session_start = std::time::Instant::now();
                        session_tick = 0;
                        last_known_game = Some(target); // keep for shutdown-path fallback
                    }
                    SessionEvent::NoChange => {}
                }

                // ── Build base doc for this tick ──────────────────────────────
                let base = deep_merge(
                    json!({ "@timestamp": ts }),
                    session.base_doc(&hostname),
                );

                // ── Collect from all collectors ───────────────────────────────
                let mut tick_docs: Vec<Value> = Vec::with_capacity(collectors.len());
                for c in &mut collectors {
                    let dataset = c.dataset();
                    match c.collect() {
                        Ok(Some(payload)) => {
                            let mut doc = deep_merge(base.clone(), payload);
                            add_data_stream(&mut doc, dataset);
                            tick_docs.push(doc);
                        }
                        Ok(None) => {}
                        Err(e) => tracing::warn!("{} error: {}", dataset, e),
                    }
                }

                // ── Update session accumulators ───────────────────────────────
                acc.update(&tick_docs);

                // ── Ship ──────────────────────────────────────────────────────
                if !tick_docs.is_empty() {
                    let n = tick_docs.len();
                    match shipper::ship(&cfg, tick_docs).await {
                        Ok(r) => {
                            if r.failed > 0 {
                                tracing::warn!("Tick {}: {}/{} docs failed", tick, r.failed, n);
                            } else {
                                tracing::debug!("Tick {}: shipped {} docs", tick, n);
                            }
                        }
                        Err(e) => tracing::warn!("Tick {} bulk error: {}", tick, e),
                    }
                }
            }

            _ = &mut shutdown_rx => {
                break;
            }
        }
    }

    // ── Cleanup ───────────────────────────────────────────────────────────────
    session.remove_session_json();

    if session_tick > 0 {
        let duration_s = session_start.elapsed().as_secs();
        let summary_doc = build_summary_doc(
            &session,
            &host_snapshot,
            &hostname,
            duration_s,
            &acc,
            last_known_game.as_ref(),
        );
        tracing::info!("Shipping session summary ({}s, {} ticks)", duration_s, session_tick);
        if let Err(e) = shipper::ship(&cfg, vec![summary_doc]).await {
            tracing::warn!("Failed to ship summary doc: {}", e);
        } else {
            // Trigger an immediate transform sync so the Games dashboard
            // reflects this session within seconds rather than up to 60 s.
            if let Err(e) = shipper::trigger_transform_sync(&cfg, "gamepulse-game-timeline").await {
                tracing::warn!("transform schedule_now failed (non-fatal): {}", e);
            }
        }
    }

    tracing::info!("GamePulse agent stopped after {} ticks", tick);
    Ok(())
}
