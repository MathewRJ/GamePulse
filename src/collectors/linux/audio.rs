/// Audio collector — mirrors collector/gamepulse/collectors/audio.py exactly.
///
/// Detects the audio backend (PipeWire / PulseAudio / ALSA) at construction
/// time by probing pw-cli and pactl. On each collect() tick, reads stats from
/// the running backend.
///
/// Output fields (gamepulse.audio.*):
///   backend          str  — always present: "pipewire", "pulseaudio", "alsa", "unknown"
///   xruns            i64  — delta xruns since last tick (PipeWire, 2nd call+)
///   latency_ms       f64  — quantum/rate latency in ms, 2 dp (PipeWire, when parseable)
///   sample_rate_hz   i64  — server sample rate (PulseAudio)
///
/// collect() always returns Some — backend is always included even if stats fail.
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::Value;
use std::io::Read;
use std::time::{Duration, Instant};

// pw-top -b waits for a PipeWire refresh cycle (~1-2 s) before exiting.
// Calling it every tick blocks the entire collection loop. Cache for 5 s.
const PIPEWIRE_CACHE_TTL: Duration = Duration::from_secs(5);

// ── Subprocess helper ─────────────────────────────────────────────────────────

/// Run a command, wait up to `timeout_ms` milliseconds, return stdout as String.
/// Returns None if the binary is not found, the command fails, or it times out.
fn run_cmd(prog: &str, args: &[&str], timeout_ms: u64) -> Option<String> {
    let mut child = std::process::Command::new(prog)
        .args(args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;

    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let mut buf = Vec::new();
                if let Some(mut stdout) = child.stdout.take() {
                    let _ = stdout.read_to_end(&mut buf);
                }
                return Some(String::from_utf8_lossy(&buf).to_string());
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

// ── String parsing helpers (avoids adding regex crate) ───────────────────────

/// Extract the last integer immediately before `keyword` in `line`.
/// Matches Python: `re.search(r"\s+(\d+)\s+KEYWORD", line)`.
fn number_before(line: &str, keyword: &str) -> Option<i64> {
    let idx = line.find(keyword)?;
    let before = line[..idx].trim_end();
    let num_start = before
        .rfind(|c: char| !c.is_ascii_digit())
        .map(|i| i + 1)
        .unwrap_or(0);
    let s = &before[num_start..];
    if s.is_empty() {
        return None;
    }
    s.parse().ok()
}

/// Extract "N/M" integer pair from `line` (first occurrence).
/// Matches Python: `re.search(r"(\d+)/(\d+)", line)`.
fn quant_rate(line: &str) -> Option<(i64, i64)> {
    let slash = line.find('/')?;
    let before = &line[..slash];
    let after = &line[slash + 1..];
    let n1_start = before
        .rfind(|c: char| !c.is_ascii_digit())
        .map(|i| i + 1)
        .unwrap_or(0);
    let n2_end = after
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(after.len());
    let n1: i64 = before[n1_start..].parse().ok()?;
    let n2: i64 = after[..n2_end].parse().ok()?;
    if n1 > 0 && n2 > 0 {
        Some((n1, n2))
    } else {
        None
    }
}

/// Extract integer before "Hz" in `line`.
/// Matches Python: `re.search(r"(\d+)\s*Hz", line)`.
fn hz_value(line: &str) -> Option<i64> {
    let idx = line.find("Hz")?;
    let before = line[..idx].trim_end();
    let num_start = before
        .rfind(|c: char| !c.is_ascii_digit())
        .map(|i| i + 1)
        .unwrap_or(0);
    let s = &before[num_start..];
    if s.is_empty() {
        return None;
    }
    s.parse().ok()
}

// ── Backend detection ─────────────────────────────────────────────────────────

fn detect_backend() -> String {
    // PipeWire exposes itself as PulseAudio — check PipeWire first.
    if let Some(out) = run_cmd("pw-cli", &["info", "0"], 2000) {
        if out.contains("PipeWire") || out.to_lowercase().contains("pipewire") {
            return "pipewire".to_string();
        }
    }

    if let Some(out) = run_cmd("pactl", &["info"], 2000) {
        if out.contains("PipeWire") {
            return "pipewire".to_string();
        }
        if out.contains("PulseAudio") {
            return "pulseaudio".to_string();
        }
    }

    // Check aplay exists (ALSA available).
    if run_cmd("aplay", &["--version"], 1000).is_some() {
        return "alsa".to_string();
    }

    "unknown".to_string()
}

// ── Per-backend stats ─────────────────────────────────────────────────────────

struct PipewireStats {
    xruns: i64,
    latency_ms: Option<f64>,
}

fn pipewire_stats() -> Option<PipewireStats> {
    let out = run_cmd("pw-top", &["-b"], 3000)?;

    let mut total_xruns: i64 = 0;
    let mut latency_ms: Option<f64> = None;

    for line in out.lines() {
        // pw-top columns: ... ERRORS ... or ... ERR ... — sum all xrun counts
        if let Some(n) = number_before(line, " ERR") {
            total_xruns += n;
        }
        // Latency from quantum/rate (e.g. "1024/48000")
        if latency_ms.is_none() {
            if let Some((quant, rate)) = quant_rate(line) {
                if rate > 0 {
                    latency_ms =
                        Some(((quant as f64 / rate as f64 * 1000.0) * 100.0).round() / 100.0);
                }
            }
        }
    }

    Some(PipewireStats {
        xruns: total_xruns,
        latency_ms,
    })
}

struct PulseaudioStats {
    sample_rate_hz: Option<i64>,
}

fn pulseaudio_stats() -> Option<PulseaudioStats> {
    let out = run_cmd("pactl", &["stat"], 2000)?;
    let mut sample_rate_hz: Option<i64> = None;
    for line in out.lines() {
        if line.contains("Sample Specification") {
            if let Some(hz) = hz_value(line) {
                sample_rate_hz = Some(hz);
            }
        }
    }
    if sample_rate_hz.is_some() {
        Some(PulseaudioStats { sample_rate_hz })
    } else {
        None
    }
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct AudioCollector {
    backend: Option<String>,
    prev_xruns: Option<i64>,
    pw_cache: Option<PipewireStats>,
    pw_cache_at: Option<Instant>,
}

impl AudioCollector {
    pub fn new(_game_pid: Option<u32>) -> Self {
        AudioCollector {
            backend: None,
            prev_xruns: None,
            pw_cache: None,
            pw_cache_at: None,
        }
    }

    fn pipewire_stats_cached(&mut self) -> Option<&PipewireStats> {
        let stale = self
            .pw_cache_at
            .map(|t| t.elapsed() >= PIPEWIRE_CACHE_TTL)
            .unwrap_or(true);
        if stale {
            self.pw_cache = pipewire_stats();
            self.pw_cache_at = Some(Instant::now());
        }
        self.pw_cache.as_ref()
    }
}

impl Collector for AudioCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.audio"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        if self.backend.is_none() {
            self.backend = Some(detect_backend());
        }
        let backend = self.backend.as_deref().unwrap_or("unknown");

        let mut audio = serde_json::Map::new();
        audio.insert("backend".to_string(), Value::from(backend.to_string()));

        if backend == "pipewire" {
            // Copy primitive values out immediately so the borrow on self ends
            // before we mutate self.prev_xruns.
            let pw = self.pipewire_stats_cached().map(|s| (s.xruns, s.latency_ms));
            if let Some((xruns_total, lat)) = pw {
                if let Some(prev) = self.prev_xruns {
                    audio.insert(
                        "xruns".to_string(),
                        Value::from(0i64.max(xruns_total - prev)),
                    );
                }
                self.prev_xruns = Some(xruns_total);
                if let Some(ms) = lat {
                    audio.insert("latency_ms".to_string(), Value::from(ms));
                }
            }
        } else if backend == "pulseaudio" {
            if let Some(stats) = pulseaudio_stats() {
                if let Some(hz) = stats.sample_rate_hz {
                    audio.insert("sample_rate_hz".to_string(), Value::from(hz));
                }
            }
        }

        Ok(Some(serde_json::json!({ "gamepulse": { "audio": audio } })))
    }
}
