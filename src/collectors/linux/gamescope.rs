/// Gamescope frame timing collector.
///
/// On SteamOS (Gaming Mode and Desktop Mode), Gamescope always overrides
/// MANGOHUD_CONFIGFILE with its own shim, making MangoHud CSV unavailable.
/// The stats pipe at /run/user/<uid>/gamescope.<id>/stats.pipe is the only
/// reliable FPS source for Steam games on SteamOS.
///
/// The pipe delivers alternating lines:
///   fps=<float>
///   focus=<steam_app_id | "steam">
///
/// A background thread reads continuously and accumulates samples where
/// focus == current game app ID. collect() drains and computes stats.
///
/// Output fields (rigsignal.frame.*):
///   current    i64  — fps of the most recent sample in the tick window
///   avg_1s     f64  — mean fps over the tick interval, 1 dp
///   low_1pct   i64  — 1% low fps (sorted ascending percentile)
///   low_01pct  i64  — 0.1% low fps
///
/// Returns None when the game has no focus or collected no samples this tick.
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::Value;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};

// ── Pipe discovery ────────────────────────────────────────────────────────────

fn uid() -> u32 {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| {
            s.lines()
                .find(|l| l.starts_with("Uid:"))
                .and_then(|l| l.split_whitespace().nth(1))
                .and_then(|u| u.parse().ok())
        })
        .unwrap_or(1000)
}

pub fn find_stats_pipe() -> Option<PathBuf> {
    let run_dir = format!("/run/user/{}", uid());
    for entry in std::fs::read_dir(&run_dir).ok()?.flatten() {
        if entry
            .file_name()
            .to_string_lossy()
            .starts_with("gamescope.")
        {
            let pipe = entry.path().join("stats.pipe");
            if pipe.exists() {
                return Some(pipe);
            }
        }
    }
    None
}

// ── Stats helpers ─────────────────────────────────────────────────────────────

fn percentile(sorted: &[f64], pct: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((pct / 100.0) * sorted.len() as f64).floor() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct GamescopeFrameCollector {
    /// Current game's Steam App ID string (e.g. "1703340"), shared with reader thread.
    app_id: Arc<RwLock<Option<String>>>,
    /// FPS samples accumulated by the reader thread, drained each tick.
    samples: Arc<Mutex<Vec<f64>>>,
    stop: Arc<AtomicBool>,
    thread_started: bool,
}

impl GamescopeFrameCollector {
    pub fn new(_game_pid: Option<u32>) -> Self {
        GamescopeFrameCollector {
            app_id: Arc::new(RwLock::new(None)),
            samples: Arc::new(Mutex::new(Vec::new())),
            stop: Arc::new(AtomicBool::new(false)),
            thread_started: false,
        }
    }

    fn start_reader(&mut self, pipe_path: PathBuf) {
        let samples = Arc::clone(&self.samples);
        let app_id = Arc::clone(&self.app_id);
        let stop = Arc::clone(&self.stop);

        std::thread::spawn(move || {
            let file = match std::fs::File::open(&pipe_path) {
                Ok(f) => f,
                Err(_) => return,
            };
            let reader = BufReader::new(file);
            let mut pending_fps: Option<f64> = None;

            for line in reader.lines() {
                if stop.load(Ordering::Relaxed) {
                    break;
                }
                let line = match line {
                    Ok(l) => l,
                    Err(_) => break,
                };
                if let Some(v) = line.strip_prefix("fps=") {
                    pending_fps = v.trim().parse().ok();
                } else if let Some(focus) = line.strip_prefix("focus=") {
                    if let Some(fps) = pending_fps.take() {
                        let focus = focus.trim();
                        let matches = app_id
                            .read()
                            .ok()
                            .and_then(|g| g.as_deref().map(|id| id == focus))
                            .unwrap_or(false);
                        if matches && fps >= 1.0 {
                            if let Ok(mut buf) = samples.lock() {
                                buf.push(fps);
                            }
                        }
                    }
                }
            }
        });
    }
}

impl Drop for GamescopeFrameCollector {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

impl Collector for GamescopeFrameCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.frame"
    }

    fn set_game_pid(&mut self, game_pid: Option<u32>) {
        if let Some(pid) = game_pid {
            // Read the Steam App ID from the game's environment.
            let env = std::fs::read_to_string(format!("/proc/{}/environ", pid))
                .unwrap_or_default();
            let id = env
                .split('\0')
                .find(|s| s.starts_with("SteamAppId="))
                .and_then(|s| s.strip_prefix("SteamAppId="))
                .map(|s| s.to_string());
            *self.app_id.write().unwrap() = id;

            // Start the reader thread on first game detection.
            if !self.thread_started {
                if let Some(pipe) = find_stats_pipe() {
                    self.thread_started = true;
                    self.start_reader(pipe);
                }
            }
        } else {
            *self.app_id.write().unwrap() = None;
        }
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let mut fps_values: Vec<f64> = {
            let mut buf = self.samples.lock().unwrap();
            std::mem::take(&mut *buf)
        };

        if fps_values.is_empty() {
            return Ok(None);
        }

        let avg_fps =
            (fps_values.iter().sum::<f64>() / fps_values.len() as f64 * 10.0).round() / 10.0;
        let current_fps = *fps_values.last().unwrap() as i64;

        fps_values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let low_1pct = percentile(&fps_values, 1.0) as i64;
        let low_01pct = percentile(&fps_values, 0.1) as i64;

        Ok(Some(serde_json::json!({
            "rigsignal": {
                "fps": {
                    "current": current_fps,
                    "avg_1s": avg_fps,
                    "low_1pct": low_1pct,
                    "low_01pct": low_01pct,
                }
            }
        })))
    }
}
