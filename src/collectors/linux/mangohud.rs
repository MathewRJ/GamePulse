/// MangoHud frame timing collector — mirrors
/// collector/rigsignal/collectors/frame/mangohud.py exactly.
///
/// Watches the most recently modified CSV in ~/.local/share/MangoHud (or
/// /tmp/MangoHud fallback). Reads new lines incrementally on every tick
/// using a stored file offset. Re-checks for a newer CSV file every 5 s.
///
/// Returns None when no log is present, no new lines since last tick,
/// or no valid fps rows in the new data.
///
/// Output fields (rigsignal.fps.*):
///   current            i64  — fps of last frame in window (int(fps_values[-1]))
///   avg_1s             f64  — mean fps over interval, 1 dp
///   low_1pct           i64  — 1% low fps (sorted bottom percentile)
///   low_01pct          i64  — 0.1% low fps
///   frametime_ms       f64  — mean frametime in ms, 3 dp (when ft data available)
///   frametime_variance f64  — frametime variance, 3 dp (when ft data available)
///   stutter_count      i64  — frames with ft > 2×avg_ft; always present (0 default)
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::Value;
use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::time::{Instant, SystemTime};

const LOG_DIR_PRIMARY: &str = ".local/share/MangoHud";
const LOG_DIR_FALLBACK: &str = "/tmp/MangoHud";
const CHECK_INTERVAL_SECS: f64 = 5.0;
const STALE_SECS: f64 = 10.0; // defined but matches Python _STALE_SECS

// ── Log file discovery ────────────────────────────────────────────────────────

fn find_log_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
    let primary = PathBuf::from(format!("{home}/{LOG_DIR_PRIMARY}"));
    let fallback = PathBuf::from(LOG_DIR_FALLBACK);
    if primary.is_dir() {
        primary
    } else if fallback.is_dir() {
        fallback
    } else {
        primary // may not exist yet; collect() handles gracefully
    }
}

fn latest_log(dir: &PathBuf) -> Option<PathBuf> {
    let entries = std::fs::read_dir(dir).ok()?;
    entries
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        // Never select a special file as a MangoHud log. In particular, a
        // FIFO named *.csv would otherwise make a blocking reader open hang
        // collection indefinitely.
        .filter(|p| {
            p.extension().and_then(|e| e.to_str()) == Some("csv")
                && std::fs::symlink_metadata(p)
                    .map(|metadata| metadata.file_type().is_file())
                    .unwrap_or(false)
        })
        .max_by_key(|p| {
            p.metadata()
                .and_then(|m| m.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH)
        })
}

// ── Statistics ────────────────────────────────────────────────────────────────

/// Return value at the given percentile (0–100) of a sorted ascending slice.
/// Matches Python: sorted_v[max(0, int(len * pct / 100.0) - 1)]
fn percentile(sorted: &[f64], pct: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let raw = (sorted.len() as f64 * pct / 100.0) as i64 - 1;
    let idx = raw.max(0) as usize;
    sorted[idx.min(sorted.len() - 1)]
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct MangoHudCollector {
    pub game_pid: Option<u32>,
    log_dir: PathBuf,
    log_path: Option<PathBuf>,
    file_pos: u64,
    fps_col: Option<usize>,
    ft_col: Option<usize>,
    header_done: bool,
    last_check: Option<Instant>,
    last_mtime: Option<SystemTime>,
}

impl MangoHudCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        Self::with_log_dir(game_pid, find_log_dir())
    }

    pub(crate) fn with_log_dir(game_pid: Option<u32>, log_dir: PathBuf) -> Self {
        MangoHudCollector {
            game_pid,
            log_dir,
            log_path: None,
            file_pos: 0,
            fps_col: None,
            ft_col: None,
            header_done: false,
            last_check: None,
            last_mtime: None,
        }
    }

    pub fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }

    /// Check every 5 s for a newer CSV log file; reset state when switching.
    fn maybe_switch_log(&mut self) {
        let now = Instant::now();
        if let Some(last) = self.last_check {
            if now.duration_since(last).as_secs_f64() < CHECK_INTERVAL_SECS {
                return;
            }
        }
        self.last_check = Some(now);

        let latest = latest_log(&self.log_dir);
        if latest != self.log_path {
            self.log_path = latest;
            self.file_pos = 0;
            self.fps_col = None;
            self.ft_col = None;
            self.header_done = false;
            self.last_mtime = None;
        }
    }

    /// Read new CSV rows since the last file offset. Returns raw string rows.
    fn read_new_rows(&mut self) -> Vec<Vec<String>> {
        let path = match &self.log_path {
            Some(p) if p.exists() => p.clone(),
            _ => return vec![],
        };

        // Stale check: same mtime and nothing new to read.
        match std::fs::metadata(&path) {
            Err(_) => return vec![],
            Ok(meta) => {
                let mtime = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
                if Some(mtime) == self.last_mtime && self.file_pos >= meta.len() {
                    return vec![];
                }
                self.last_mtime = Some(mtime);
            }
        }

        // The selection check above excludes special files, but the path can
        // still be replaced between discovery and open. O_NONBLOCK keeps that
        // race harmless if it becomes a FIFO.
        let chunk = match OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&path)
        {
            Err(_) => return vec![],
            Ok(mut f) => {
                if f.seek(SeekFrom::Start(self.file_pos)).is_err() {
                    return vec![];
                }
                let mut buf = String::new();
                let _ = f.read_to_string(&mut buf);
                // Advance stored offset by bytes read (seek back to get position).
                if let Ok(pos) = f.seek(SeekFrom::Current(0)) {
                    self.file_pos = pos;
                }
                buf
            }
        };

        let mut rows: Vec<Vec<String>> = Vec::new();

        for line in chunk.lines() {
            // Parse CSV row (MangoHud uses plain commas, no quoting needed).
            let row: Vec<String> = line.split(',').map(|s| s.trim().to_string()).collect();
            if row.is_empty() || row.iter().all(|s| s.is_empty()) {
                continue;
            }

            if !self.header_done {
                // MangoHud 3-line preamble:
                //   Line 1: os,cpu,gpu,...    ← system info header
                //   Line 2: CachyOS,...       ← system info values
                //   Line 3: fps,frametime,... ← data column header (first col = "fps")
                if row[0].to_lowercase() == "fps" {
                    let header: Vec<String> = row.iter().map(|s| s.to_lowercase()).collect();
                    self.fps_col = header.iter().position(|s| s == "fps");
                    self.ft_col = header.iter().position(|s| s == "frametime");
                    self.header_done = true;
                }
                continue; // skip all preamble lines including the header row
            }

            rows.push(row);
        }

        rows
    }
}

impl Collector for MangoHudCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.frame"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        self.maybe_switch_log();
        let rows = self.read_new_rows();

        let fps_col = match self.fps_col {
            Some(c) => c,
            None => return Ok(None),
        };
        if rows.is_empty() {
            return Ok(None);
        }

        let mut fps_values: Vec<f64> = Vec::new();
        let mut ft_values: Vec<f64> = Vec::new();

        for row in &rows {
            let fps_v = match row.get(fps_col).and_then(|s| s.parse::<f64>().ok()) {
                Some(v) => v,
                None => continue,
            };
            // Exclude sub-1fps frames (hard freezes, loading transitions).
            if fps_v < 1.0 {
                continue;
            }
            fps_values.push(fps_v);

            // Cap frametime at 200ms — loading screen pauses corrupt the histogram.
            if let Some(ft_col) = self.ft_col {
                if let Some(ft_v) = row.get(ft_col).and_then(|s| s.parse::<f64>().ok()) {
                    if ft_v > 0.0 && ft_v <= 200.0 {
                        ft_values.push(ft_v);
                    }
                }
            }
        }

        if fps_values.is_empty() {
            return Ok(None);
        }

        let avg_fps =
            (fps_values.iter().sum::<f64>() / fps_values.len() as f64 * 10.0).round() / 10.0;
        let current_fps = *fps_values.last().unwrap() as i64;

        let mut fps_sorted = fps_values.clone();
        fps_sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let low_1pct = percentile(&fps_sorted, 1.0) as i64;
        let low_01pct = percentile(&fps_sorted, 0.1) as i64;

        let mut fps_map = serde_json::Map::new();
        fps_map.insert("current".to_string(), Value::from(current_fps));
        fps_map.insert("avg_1s".to_string(), Value::from(avg_fps));
        fps_map.insert("low_1pct".to_string(), Value::from(low_1pct));
        fps_map.insert("low_01pct".to_string(), Value::from(low_01pct));

        let stutter_count: i64;
        if !ft_values.is_empty() {
            let avg_ft =
                (ft_values.iter().sum::<f64>() / ft_values.len() as f64 * 1000.0).round() / 1000.0;
            let variance = ft_values.iter().map(|&x| (x - avg_ft).powi(2)).sum::<f64>()
                / ft_values.len() as f64;
            let variance = (variance * 1000.0).round() / 1000.0;
            stutter_count = ft_values.iter().filter(|&&ft| ft > 2.0 * avg_ft).count() as i64;
            fps_map.insert("frametime_ms".to_string(), Value::from(avg_ft));
            fps_map.insert("frametime_variance".to_string(), Value::from(variance));
        } else {
            stutter_count = 0;
        }
        fps_map.insert("stutter_count".to_string(), Value::from(stutter_count));

        Ok(Some(serde_json::json!({ "rigsignal": { "fps": fps_map } })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::ffi::OsStrExt;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;
    use std::time::Duration;

    fn make_fifo(path: &std::path::Path) {
        let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
    }

    fn temp_log_dir() -> PathBuf {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let nonce = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "rigsignal-mangohud-{}-{nonce}-{id}",
            std::process::id()
        ));
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn fifo_named_csv_is_skipped_without_parking_collection() {
        let dir = temp_log_dir();
        let log = dir.join("mangohud.csv");
        std::fs::File::create(&log)
            .unwrap()
            .write_all(b"os,cpu\nLinux,test\nfps,frametime\n60,16.0\n")
            .unwrap();
        // Ensure the FIFO would win the old mtime-only selection.
        std::thread::sleep(Duration::from_millis(20));
        make_fifo(&dir.join("newest.csv"));

        let (done, result) = mpsc::channel();
        let log_dir = dir.clone();
        std::thread::spawn(move || {
            let mut collector = MangoHudCollector::with_log_dir(None, log_dir);
            done.send(collector.collect()).unwrap();
        });

        let document = result
            .recv_timeout(Duration::from_millis(500))
            .expect("a FIFO named *.csv must not park MangoHud collection")
            .unwrap()
            .expect("the regular CSV should still be selected");
        assert_eq!(document["rigsignal"]["fps"]["current"], 60);
        assert_eq!(latest_log(&dir), Some(log.clone()));

        // Exercise the discovery-to-open race too: selection saw a regular
        // CSV, then the path was replaced by a FIFO before the collector
        // opened it. This must also return under the timeout.
        let mut collector = MangoHudCollector::with_log_dir(None, dir);
        collector.maybe_switch_log();
        std::fs::remove_file(&log).unwrap();
        make_fifo(&log);
        let (done, result) = mpsc::channel();
        std::thread::spawn(move || done.send(collector.read_new_rows()).unwrap());
        assert!(result
            .recv_timeout(Duration::from_millis(500))
            .expect("a selected CSV replaced by a FIFO must not park MangoHud open")
            .is_empty());
    }
}
