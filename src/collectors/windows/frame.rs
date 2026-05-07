//! PresentMon frame-timing collector — Windows equivalent of
//! `src/collectors/linux/mangohud.rs`.
//!
//! `FrameSource` is the extension point for frame-timing backends.
//! `PresentMonSource` (below) spawns `PresentMon.exe` as a subprocess and
//! parses its CSV — the same pattern as the Linux MangoHud collector
//! (`src/collectors/linux/mangohud.rs`: spawn pw-run-script / read CSV).
//! To swap backends without changing `FrameCollector` or `main.rs`,
//! implement `FrameSource` on a new type and replace the
//! `Box<dyn FrameSource>` in `FrameCollector::new()`. The ETW-direct path
//! (Microsoft PresentMon SDK, github.com/GameTechDev/PresentMon) would
//! eliminate the external binary dependency and is the recommended
//! long-term upgrade.
//!
//! PresentMon 2.x changed several column names from 1.x. Parsing the
//! header to find column indices by name (rather than hardcoding offsets)
//! means only this header-parsing step needs updating if a future version
//! renames columns. If `MsBetweenPresents` is absent from the header, we
//! log a one-time warning and the reader thread exits — `next_sample()`
//! returns `None` permanently for this session. We do not attempt
//! fallback column guessing.
//!
//! Field-path note: this collector emits under `gamepulse.fps.*` (not
//! `gamepulse.frame.*`) to match the Linux collector and the
//! `SessionAccumulators` reader in `src/main.rs`. The dataset name is
//! still `gamepulse.frame`.

use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::thread;

const RING_CAP: usize = 120; // ~2 s at 60 FPS
const CHANNEL_CAP: usize = 256;
const COL_MS_BETWEEN_PRESENTS: &str = "MsBetweenPresents";

// ── FrameSource trait ─────────────────────────────────────────────────────────

pub(crate) trait FrameSource: Send {
    fn next_sample(&mut self) -> Option<FrameSample>;
    fn attach(&mut self, pid: u32);
    fn detach(&mut self);
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct FrameSample {
    /// Smoothed average FPS across the rolling ring (1 dp).
    fps: f64,
    /// Mean frametime in ms over rows received this tick (3 dp).
    frametime_ms: f64,
    /// Most recent frame's instantaneous FPS.
    current_fps: i64,
    /// 1% low FPS computed from the ring.
    low_1pct: i64,
    /// 0.1% low FPS computed from the ring.
    low_01pct: i64,
    /// Variance of frametimes received this tick (3 dp).
    frametime_variance: f64,
    /// Frames in this tick whose frametime exceeded 2× the tick mean.
    stutter_count: i64,
}

// ── PresentMon discovery ──────────────────────────────────────────────────────

fn find_presentmon() -> Option<PathBuf> {
    if let Ok(env_path) = std::env::var("GAMEPULSE_PRESENTMON") {
        let p = PathBuf::from(env_path);
        if p.is_file() {
            return Some(p);
        }
    }

    if let Some(parent) = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|q| q.to_path_buf()))
    {
        let candidate = parent.join("PresentMon.exe");
        if candidate.is_file() {
            return Some(candidate);
        }
    }

    let out = Command::new("where").arg("PresentMon.exe").output().ok()?;
    if !out.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let first = stdout.lines().next()?.trim();
    if first.is_empty() {
        None
    } else {
        Some(PathBuf::from(first))
    }
}

// ── Statistics helper ─────────────────────────────────────────────────────────

/// Percentile from a sorted-ascending slice. Mirrors
/// `src/collectors/linux/mangohud.rs::percentile`.
fn percentile(sorted: &[f64], pct: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let raw = (sorted.len() as f64 * pct / 100.0) as i64 - 1;
    let idx = raw.max(0) as usize;
    sorted[idx.min(sorted.len() - 1)]
}

// ── PresentMonSource ──────────────────────────────────────────────────────────

struct PresentMonSource {
    child: Option<Child>,
    rx: Option<Receiver<f64>>,
    ring: VecDeque<f64>,
    presentmon_path: Option<PathBuf>,
    presentmon_missing_logged: bool,
}

impl PresentMonSource {
    fn new() -> Self {
        let path = find_presentmon();
        Self {
            child: None,
            rx: None,
            ring: VecDeque::with_capacity(RING_CAP),
            presentmon_path: path,
            presentmon_missing_logged: false,
        }
    }
}

impl Drop for PresentMonSource {
    fn drop(&mut self) {
        self.detach();
    }
}

impl FrameSource for PresentMonSource {
    fn attach(&mut self, pid: u32) {
        // Always tear down a prior instance first.
        self.detach();

        let path = match &self.presentmon_path {
            Some(p) => p.clone(),
            None => {
                if !self.presentmon_missing_logged {
                    tracing::warn!(
                        "PresentMon.exe not found. Frame timing unavailable. \
                         Set GAMEPULSE_PRESENTMON=/path/to/PresentMon.exe or \
                         place PresentMon.exe alongside the agent binary."
                    );
                    self.presentmon_missing_logged = true;
                }
                return;
            }
        };

        let mut cmd = Command::new(&path);
        cmd.arg("--process_id")
            .arg(pid.to_string())
            .arg("--output_stdout")
            .arg("--stop_existing_session")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("PresentMon spawn failed: {}", e);
                return;
            }
        };

        let stdout = match child.stdout.take() {
            Some(s) => s,
            None => {
                tracing::warn!("PresentMon stdout unavailable");
                let _ = child.kill();
                let _ = child.wait();
                return;
            }
        };

        let (tx, rx) = mpsc::sync_channel::<f64>(CHANNEL_CAP);

        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);

            // Header line: locate MsBetweenPresents column by name.
            let mut header = String::new();
            match reader.read_line(&mut header) {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
            let cols: Vec<&str> = header.trim_end().split(',').collect();
            let col_idx = match cols
                .iter()
                .position(|c| c.trim() == COL_MS_BETWEEN_PRESENTS)
            {
                Some(i) => i,
                None => {
                    tracing::warn!(
                        "PresentMon CSV missing '{}' column — frame timing disabled for this session. \
                         Header: {}",
                        COL_MS_BETWEEN_PRESENTS,
                        header.trim_end()
                    );
                    return;
                }
            };

            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) => return, // EOF — child exited
                    Ok(_) => {}
                    Err(_) => return,
                }
                let trimmed = line.trim_end();
                if trimmed.is_empty() {
                    continue;
                }
                let fields: Vec<&str> = trimmed.split(',').collect();
                let raw = match fields.get(col_idx) {
                    Some(s) => s.trim(),
                    None => continue,
                };
                let val: f64 = match raw.parse() {
                    // Sanity-cap implausible values (loading-screen freezes,
                    // garbage). 1000 ms = 1 FPS lower bound, > 0 upper for FPS.
                    Ok(v) if v > 0.0 && v <= 1000.0 => v,
                    _ => continue,
                };
                if tx.send(val).is_err() {
                    return; // receiver dropped
                }
            }
        });

        self.child = Some(child);
        self.rx = Some(rx);
    }

    fn detach(&mut self) {
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
        // Dropping rx signals the reader thread to exit on next send.
        self.rx = None;
        self.ring.clear();
    }

    fn next_sample(&mut self) -> Option<FrameSample> {
        let rx = self.rx.as_ref()?;

        // Drain everything available without blocking; track new arrivals
        // for tick-scoped stats (stutter, variance, mean frametime).
        let mut new_this_tick: Vec<f64> = Vec::new();
        let mut disconnected = false;
        loop {
            match rx.try_recv() {
                Ok(ft) => {
                    new_this_tick.push(ft);
                    if self.ring.len() == RING_CAP {
                        self.ring.pop_front();
                    }
                    self.ring.push_back(ft);
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    disconnected = true;
                    break;
                }
            }
        }
        if disconnected {
            self.rx = None;
        }

        if new_this_tick.is_empty() || self.ring.is_empty() {
            return None;
        }

        // Smoothed FPS over the ring (1 dp).
        let mean_ft_ring: f64 = self.ring.iter().sum::<f64>() / self.ring.len() as f64;
        let fps = (1000.0 / mean_ft_ring * 10.0).round() / 10.0;

        // Per-tick mean frametime + variance (mirrors Linux collector).
        let n_new = new_this_tick.len() as f64;
        let mean_ft_tick: f64 = new_this_tick.iter().sum::<f64>() / n_new;
        let frametime_ms = (mean_ft_tick * 1000.0).round() / 1000.0;
        let variance: f64 = new_this_tick
            .iter()
            .map(|x| (x - mean_ft_tick).powi(2))
            .sum::<f64>()
            / n_new;
        let frametime_variance = (variance * 1000.0).round() / 1000.0;

        // Stutter: frames this tick where ft exceeds 2× tick mean.
        let stutter_count = new_this_tick
            .iter()
            .filter(|&&ft| ft > 2.0 * mean_ft_tick)
            .count() as i64;

        // Most recent instantaneous FPS.
        let last_ft = *self.ring.back().unwrap();
        let current_fps = (1000.0 / last_ft).round() as i64;

        // Low percentiles: sort ring as FPS ascending.
        let mut fps_sorted: Vec<f64> = self.ring.iter().map(|ft| 1000.0 / ft).collect();
        fps_sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let low_1pct = percentile(&fps_sorted, 1.0).round() as i64;
        let low_01pct = percentile(&fps_sorted, 0.1).round() as i64;

        Some(FrameSample {
            fps,
            frametime_ms,
            current_fps,
            low_1pct,
            low_01pct,
            frametime_variance,
            stutter_count,
        })
    }
}

// ── FrameCollector ────────────────────────────────────────────────────────────

pub struct FrameCollector {
    source: Box<dyn FrameSource>,
    game_pid: Option<u32>,
}

impl FrameCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        let source: Box<dyn FrameSource> = Box::new(PresentMonSource::new());
        let mut fc = FrameCollector { source, game_pid };
        if let Some(pid) = game_pid {
            fc.source.attach(pid);
        }
        fc
    }
}

impl Collector for FrameCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.frame"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        match self.source.next_sample() {
            None => Ok(None),
            Some(s) => Ok(Some(json!({
                "gamepulse": {
                    "fps": {
                        "avg_1s": s.fps,
                        "current": s.current_fps,
                        "low_1pct": s.low_1pct,
                        "low_01pct": s.low_01pct,
                        "frametime_ms": s.frametime_ms,
                        "frametime_variance": s.frametime_variance,
                        "stutter_count": s.stutter_count,
                    }
                }
            }))),
        }
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        if pid == self.game_pid {
            return;
        }
        match (self.game_pid, pid) {
            (_, Some(new)) => self.source.attach(new),
            (Some(_), None) => self.source.detach(),
            (None, None) => {}
        }
        self.game_pid = pid;
    }
}
