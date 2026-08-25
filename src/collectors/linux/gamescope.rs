/// Dynamic Linux frame collector.
///
/// Gamescope exposes FPS samples through FIFO files while non-Gamescope hosts
/// use MangoHud CSV logs. Keep the source decision here, rather than making a
/// one-time decision during collector construction: Gamescope may start after
/// the agent and stale Gamescope FIFOs are common after a game exits.
use crate::collectors::linux::mangohud::MangoHudCollector;
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader};
use std::os::unix::fs::{FileTypeExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime};

const RESOLVE_INTERVAL: Duration = Duration::from_secs(5);
const DEMOTION_WINDOW: Duration = Duration::from_secs(1);
const REOPEN_BACKOFF_INITIAL: Duration = Duration::from_millis(100);
const REOPEN_BACKOFF_MAX: Duration = Duration::from_secs(1);
const WRITER_RENDEZVOUS_WAIT: Duration = Duration::from_millis(25);
const WOULD_BLOCK_WAIT: Duration = Duration::from_millis(25);
const NO_CANDIDATE_WAIT: Duration = Duration::from_secs(1);

#[derive(Clone, Debug, Eq, PartialEq)]
struct StatsPipeCandidate {
    kind: &'static str,
    path: PathBuf,
}

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

fn runtime_dir() -> PathBuf {
    std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", uid())))
}

fn is_fifo(path: &Path) -> bool {
    path.metadata()
        .map(|metadata| metadata.file_type().is_fifo())
        .unwrap_or(false)
}

/// Discover Gamescope stats FIFOs in preference order.
///
/// `gamescope-stats` is authoritative when present. Remaining candidates are
/// ranked only as a heuristic; the reader demotes an instant-EOF FIFO and moves
/// on, so a stale newest directory cannot pin collection indefinitely.
fn stats_pipe_candidates(run_dir: &Path) -> Vec<StatsPipeCandidate> {
    let mut candidates = Vec::new();
    let mut seen = HashSet::new();
    let symlink = run_dir.join("gamescope-stats");
    if let Ok(target) = std::fs::read_link(&symlink) {
        let target = if target.is_absolute() {
            target
        } else {
            run_dir.join(target)
        };
        let pipe = target.join("stats.pipe");
        if is_fifo(&pipe) {
            seen.insert(pipe.clone());
            candidates.push(StatsPipeCandidate {
                kind: "gamescope-stats symlink",
                path: pipe,
            });
        }
    }

    let mut directory_candidates: Vec<(SystemTime, PathBuf)> = std::fs::read_dir(run_dir)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.ok())
        .filter(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("gamescope.")
        })
        .filter_map(|entry| {
            let pipe = entry.path().join("stats.pipe");
            if !is_fifo(&pipe) || seen.contains(&pipe) {
                return None;
            }
            let modified = pipe
                .metadata()
                .and_then(|metadata| metadata.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            Some((modified, pipe))
        })
        .collect();
    directory_candidates.sort_by(|(a_time, a_path), (b_time, b_path)| {
        b_time.cmp(a_time).then_with(|| a_path.cmp(b_path))
    });
    candidates.extend(
        directory_candidates
            .into_iter()
            .map(|(_, path)| StatsPipeCandidate {
                kind: "gamescope directory",
                path,
            }),
    );
    candidates
}

/// Retained for callers that only need discovery. The dynamic collector uses
/// the complete candidate list so it can demote and rotate stale pipes.
pub fn find_stats_pipe() -> Option<PathBuf> {
    stats_pipe_candidates(&runtime_dir())
        .into_iter()
        .next()
        .map(|candidate| candidate.path)
}

fn percentile(sorted: &[f64], pct: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((pct / 100.0) * sorted.len() as f64).floor() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

struct ReaderHandle {
    stop: Arc<AtomicBool>,
    join: JoinHandle<()>,
}

/// The sole Linux frame collector. Gamescope collection is asynchronous so a
/// FIFO can never hold up the agent's collection tick; MangoHud remains the
/// synchronous fallback when no Gamescope candidate exists.
pub struct FrameCollector {
    mango: MangoHudCollector,
    runtime_dir: PathBuf,
    app_id: Arc<RwLock<Option<String>>>,
    samples: Arc<Mutex<Vec<f64>>>,
    gamescope_attached: Arc<AtomicBool>,
    reader: Option<ReaderHandle>,
    game_active: bool,
    gamescope_candidate_present: bool,
    last_resolve: Option<Instant>,
    #[cfg(test)]
    reader_attempts: Arc<std::sync::atomic::AtomicUsize>,
}

impl FrameCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        let mut collector = Self::with_runtime_dir(game_pid, runtime_dir());
        if game_pid.is_some() {
            collector.set_game_pid(game_pid);
        }
        collector
    }

    fn with_runtime_dir(game_pid: Option<u32>, runtime_dir: PathBuf) -> Self {
        Self {
            mango: MangoHudCollector::new(game_pid),
            runtime_dir,
            app_id: Arc::new(RwLock::new(None)),
            samples: Arc::new(Mutex::new(Vec::new())),
            gamescope_attached: Arc::new(AtomicBool::new(false)),
            reader: None,
            game_active: false,
            gamescope_candidate_present: false,
            last_resolve: None,
            #[cfg(test)]
            reader_attempts: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        }
    }

    fn reap_reader(&mut self) {
        if self
            .reader
            .as_ref()
            .is_some_and(|reader| reader.join.is_finished())
        {
            let reader = self.reader.take().expect("checked above");
            let _ = reader.join.join();
        }
    }

    fn maybe_resolve_source(&mut self, force: bool) {
        self.reap_reader();
        if !self.game_active {
            self.gamescope_candidate_present = false;
            return;
        }
        let now = Instant::now();
        if !force
            && self
                .last_resolve
                .is_some_and(|last| now.duration_since(last) < RESOLVE_INTERVAL)
        {
            return;
        }
        self.last_resolve = Some(now);
        self.gamescope_candidate_present = !stats_pipe_candidates(&self.runtime_dir).is_empty();
        if self.gamescope_candidate_present && self.reader.is_none() {
            self.start_reader();
        }
    }

    fn start_reader(&mut self) {
        let stop = Arc::new(AtomicBool::new(false));
        let reader_stop = Arc::clone(&stop);
        let runtime_dir = self.runtime_dir.clone();
        let samples = Arc::clone(&self.samples);
        let app_id = Arc::clone(&self.app_id);
        let attached = Arc::clone(&self.gamescope_attached);
        #[cfg(test)]
        let reader_attempts = Arc::clone(&self.reader_attempts);
        // A reader is spawned on a dedicated thread, so retain the caller's
        // tracing dispatch. This keeps its state-transition logs observable by
        // the application's configured subscriber (and by focused tests).
        let dispatch = tracing::dispatcher::get_default(|dispatch| dispatch.clone());
        let join = std::thread::spawn(move || {
            tracing::dispatcher::with_default(&dispatch, || {
                read_gamescope_loop(
                    runtime_dir,
                    samples,
                    app_id,
                    attached,
                    reader_stop,
                    #[cfg(test)]
                    reader_attempts,
                )
            })
        });
        self.reader = Some(ReaderHandle { stop, join });
    }

    fn stop_reader(&mut self) {
        if let Some(reader) = &self.reader {
            reader.stop.store(true, Ordering::Relaxed);
        }
        self.gamescope_attached.store(false, Ordering::Relaxed);
    }

    fn collect_gamescope(&mut self) -> Option<Value> {
        let mut fps_values: Vec<f64> = {
            let mut buf = self.samples.lock().ok()?;
            std::mem::take(&mut *buf)
        };
        if fps_values.is_empty() {
            return None;
        }

        // Keep Gamescope's established statistics and focus matching semantics.
        let avg_fps =
            (fps_values.iter().sum::<f64>() / fps_values.len() as f64 * 10.0).round() / 10.0;
        let current_fps = *fps_values.last().unwrap() as i64;
        let ft_values: Vec<f64> = fps_values
            .iter()
            .filter(|&&fps| fps > 0.0)
            .map(|&fps| 1000.0 / fps)
            .collect();
        fps_values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        let mut fps_map = serde_json::Map::new();
        fps_map.insert("current".to_string(), Value::from(current_fps));
        fps_map.insert("avg_1s".to_string(), Value::from(avg_fps));
        fps_map.insert(
            "low_1pct".to_string(),
            Value::from(percentile(&fps_values, 1.0) as i64),
        );
        fps_map.insert(
            "low_01pct".to_string(),
            Value::from(percentile(&fps_values, 0.1) as i64),
        );
        let stutter_count = if ft_values.is_empty() {
            0
        } else {
            let avg_ft =
                (ft_values.iter().sum::<f64>() / ft_values.len() as f64 * 1000.0).round() / 1000.0;
            fps_map.insert("frametime_ms".to_string(), Value::from(avg_ft));
            ft_values.iter().filter(|&&ft| ft > 2.0 * avg_ft).count() as i64
        };
        fps_map.insert("stutter_count".to_string(), Value::from(stutter_count));
        Some(serde_json::json!({ "rigsignal": { "fps": fps_map } }))
    }
}

impl Drop for FrameCollector {
    fn drop(&mut self) {
        self.stop_reader();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join.join();
        }
    }
}

impl Collector for FrameCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.frame"
    }

    fn set_game_pid(&mut self, game_pid: Option<u32>) {
        self.mango.set_game_pid(game_pid);
        if let Some(pid) = game_pid {
            let env = std::fs::read_to_string(format!("/proc/{pid}/environ")).unwrap_or_default();
            let id = env
                .split('\0')
                .find(|entry| entry.starts_with("SteamAppId="))
                .and_then(|entry| entry.strip_prefix("SteamAppId="))
                .map(str::to_owned);
            if let Ok(mut app_id) = self.app_id.write() {
                *app_id = id;
            }
            self.game_active = true;
            self.maybe_resolve_source(true);
        } else {
            self.game_active = false;
            if let Ok(mut app_id) = self.app_id.write() {
                *app_id = None;
            }
            self.stop_reader();
        }
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        self.maybe_resolve_source(false);
        if self.gamescope_candidate_present || self.gamescope_attached.load(Ordering::Relaxed) {
            Ok(self.collect_gamescope())
        } else {
            self.mango.collect()
        }
    }
}

fn interrupted_or_would_block(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
    )
}

fn sleep_with_stop(stop: &AtomicBool, duration: Duration) -> bool {
    let deadline = Instant::now() + duration;
    while !stop.load(Ordering::Relaxed) {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return true;
        }
        std::thread::sleep(remaining.min(Duration::from_millis(25)));
    }
    false
}

fn read_gamescope_loop(
    runtime_dir: PathBuf,
    samples: Arc<Mutex<Vec<f64>>>,
    app_id: Arc<RwLock<Option<String>>>,
    attached: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    #[cfg(test)] attempts: Arc<std::sync::atomic::AtomicUsize>,
) {
    let mut demoted_until: HashMap<PathBuf, Instant> = HashMap::new();
    // A candidate remains failed until it delivers a line. This makes failure
    // logs transitions, instead of emitting one line for every retry.
    let mut failed_candidates: HashSet<PathBuf> = HashSet::new();
    let mut backoff = REOPEN_BACKOFF_INITIAL;
    let mut last_attached_path: Option<PathBuf> = None;

    while !stop.load(Ordering::Relaxed) {
        let now = Instant::now();
        demoted_until.retain(|_, until| *until > now);
        #[cfg(test)]
        attempts.fetch_add(1, Ordering::Relaxed);
        let candidates = stats_pipe_candidates(&runtime_dir);
        let candidate = candidates
            .into_iter()
            .find(|candidate| !demoted_until.contains_key(&candidate.path));
        let Some(candidate) = candidate else {
            if !sleep_with_stop(&stop, NO_CANDIDATE_WAIT) {
                break;
            }
            continue;
        };

        // O_NONBLOCK is essential: opening a reader on a writer-less FIFO must
        // succeed immediately, so all retry and rotation logic remains reachable.
        let file = match OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&candidate.path)
        {
            Ok(file) => file,
            Err(error) => {
                if failed_candidates.insert(candidate.path.clone()) {
                    tracing::debug!(path = %candidate.path.display(), %error, "Gamescope stats pipe open failed; retrying");
                }
                if !sleep_with_stop(&stop, backoff) {
                    break;
                }
                backoff = (backoff * 2).min(REOPEN_BACKOFF_MAX);
                continue;
            }
        };

        let mut reader = BufReader::new(file);
        let mut pending_fps: Option<f64> = None;
        // Give a writer blocked in open(2) a chance to rendezvous with this
        // nonblocking reader before treating its first EOF as writer-less.
        if !sleep_with_stop(&stop, WRITER_RENDEZVOUS_WAIT) {
            return;
        }
        loop {
            if stop.load(Ordering::Relaxed) {
                return;
            }
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    let was_attached = attached.swap(false, Ordering::Relaxed);
                    let first_failure = failed_candidates.insert(candidate.path.clone());
                    if was_attached {
                        tracing::info!(path = %candidate.path.display(), "Gamescope frame source detached; reopening after EOF");
                    } else if first_failure {
                        tracing::debug!(path = %candidate.path.display(), "Gamescope frame source demoted; reopening after EOF");
                    }
                    demoted_until.insert(candidate.path.clone(), Instant::now() + DEMOTION_WINDOW);
                    if !sleep_with_stop(&stop, backoff) {
                        return;
                    }
                    backoff = (backoff * 2).min(REOPEN_BACKOFF_MAX);
                    break;
                }
                Ok(_) => {
                    let recovered = failed_candidates.remove(&candidate.path);
                    if !attached.swap(true, Ordering::Relaxed) {
                        let transition = last_attached_path
                            .as_ref()
                            .is_some_and(|path| path != &candidate.path);
                        tracing::info!(
                            source_kind = candidate.kind,
                            path = %candidate.path.display(),
                            transition,
                            recovered,
                            "Gamescope frame source attached"
                        );
                    }
                    last_attached_path = Some(candidate.path.clone());
                    backoff = REOPEN_BACKOFF_INITIAL;
                    if let Some(value) = line.strip_prefix("fps=") {
                        pending_fps = value.trim().parse().ok();
                    } else if let Some(focus) = line.strip_prefix("focus=") {
                        if let Some(fps) = pending_fps.take() {
                            let focus = focus.trim();
                            let matches = app_id
                                .read()
                                .ok()
                                .and_then(|current| current.as_deref().map(|id| id == focus))
                                .unwrap_or(false);
                            if matches && fps >= 1.0 {
                                if let Ok(mut buffer) = samples.lock() {
                                    buffer.push(fps);
                                }
                            }
                        }
                    }
                }
                Err(error) if interrupted_or_would_block(&error) => {
                    if !sleep_with_stop(&stop, WOULD_BLOCK_WAIT) {
                        return;
                    }
                }
                Err(error) => {
                    let was_attached = attached.swap(false, Ordering::Relaxed);
                    let first_failure = failed_candidates.insert(candidate.path.clone());
                    if was_attached {
                        tracing::info!(path = %candidate.path.display(), %error, "Gamescope frame source detached; reopening after read error");
                    } else if first_failure {
                        tracing::debug!(path = %candidate.path.display(), %error, "Gamescope stats pipe read failed; retrying");
                    }
                    if !sleep_with_stop(&stop, backoff) {
                        return;
                    }
                    backoff = (backoff * 2).min(REOPEN_BACKOFF_MAX);
                    break;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt;
    use std::fs::{create_dir, File};
    use std::io::Write;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::symlink;
    use std::sync::atomic::AtomicUsize;
    use tracing::{field::Field, field::Visit, Event, Subscriber};
    use tracing_subscriber::{layer::Context, prelude::*, Layer};

    #[derive(Clone)]
    struct EventCapture(Arc<Mutex<Vec<String>>>);

    struct MessageVisitor<'a>(&'a Mutex<Vec<String>>);

    impl Visit for MessageVisitor<'_> {
        fn record_debug(&mut self, field: &Field, value: &dyn fmt::Debug) {
            if field.name() == "message" {
                self.0.lock().unwrap().push(format!("{value:?}"));
            }
        }
    }

    impl<S: Subscriber> Layer<S> for EventCapture {
        fn on_event(&self, event: &Event<'_>, _context: Context<'_, S>) {
            event.record(&mut MessageVisitor(&self.0));
        }
    }

    fn temp_run_dir() -> PathBuf {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let nonce = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "rigsignal-gamescope-{}-{nonce}-{id}",
            std::process::id()
        ));
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    fn make_fifo(path: &Path) {
        let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
    }

    fn activate(collector: &mut FrameCollector, app_id: &str) {
        collector.game_active = true;
        *collector.app_id.write().unwrap() = Some(app_id.to_owned());
        collector.maybe_resolve_source(true);
    }

    fn write_pipe(path: PathBuf, contents: &'static str) -> JoinHandle<()> {
        std::thread::spawn(move || {
            let mut writer = OpenOptions::new().write(true).open(path).unwrap();
            writer.write_all(contents.as_bytes()).unwrap();
        })
    }

    fn write_pipe_and_hold(
        path: PathBuf,
        contents: &'static str,
    ) -> (std::sync::mpsc::Sender<()>, JoinHandle<()>) {
        let (release, wait_for_release) = std::sync::mpsc::channel();
        let writer = std::thread::spawn(move || {
            let mut writer = OpenOptions::new().write(true).open(path).unwrap();
            writer.write_all(contents.as_bytes()).unwrap();
            wait_for_release.recv().unwrap();
        });
        (release, writer)
    }

    fn wait_for_sample(collector: &mut FrameCollector) -> Value {
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if let Some(document) = collector.collect().unwrap() {
                return document;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        panic!("timed out waiting for Gamescope sample")
    }

    #[test]
    fn t1_decoy_first_is_demoted_and_live_pipe_produces_samples() {
        let run_dir = temp_run_dir();
        let live_dir = run_dir.join("gamescope.live");
        let decoy_dir = run_dir.join("gamescope.decoy");
        create_dir(&live_dir).unwrap();
        make_fifo(&live_dir.join("stats.pipe"));
        std::thread::sleep(Duration::from_millis(20));
        create_dir(&decoy_dir).unwrap();
        make_fifo(&decoy_dir.join("stats.pipe"));
        let candidates = stats_pipe_candidates(&run_dir);
        assert_eq!(candidates[0].path, decoy_dir.join("stats.pipe"));

        let mut collector = FrameCollector::with_runtime_dir(None, run_dir);
        activate(&mut collector, "42");
        let writer = write_pipe(live_dir.join("stats.pipe"), "fps=60\nfocus=42\n");
        let document = wait_for_sample(&mut collector);
        writer.join().unwrap();
        assert_eq!(document["rigsignal"]["fps"]["current"], 60);
    }

    #[test]
    fn t2_symlink_target_is_preferred_over_directory_ranking() {
        let run_dir = temp_run_dir();
        let preferred = run_dir.join("preferred");
        let other = run_dir.join("gamescope.other");
        create_dir(&preferred).unwrap();
        create_dir(&other).unwrap();
        make_fifo(&preferred.join("stats.pipe"));
        make_fifo(&other.join("stats.pipe"));
        symlink(&preferred, run_dir.join("gamescope-stats")).unwrap();

        let candidates = stats_pipe_candidates(&run_dir);
        assert_eq!(candidates[0].kind, "gamescope-stats symlink");
        assert_eq!(candidates[0].path, preferred.join("stats.pipe"));
    }

    #[test]
    fn t3_eof_reattaches_when_writer_returns() {
        let run_dir = temp_run_dir();
        let dir = run_dir.join("gamescope.live");
        create_dir(&dir).unwrap();
        let pipe = dir.join("stats.pipe");
        make_fifo(&pipe);
        let mut collector = FrameCollector::with_runtime_dir(None, run_dir);
        activate(&mut collector, "42");
        let (release_first, first_writer) = write_pipe_and_hold(pipe.clone(), "fps=60\nfocus=42\n");
        assert_eq!(
            wait_for_sample(&mut collector)["rigsignal"]["fps"]["current"],
            60
        );
        release_first.send(()).unwrap();
        first_writer.join().unwrap();
        let (release_second, second_writer) = write_pipe_and_hold(pipe, "fps=50\nfocus=42\n");
        assert_eq!(
            wait_for_sample(&mut collector)["rigsignal"]["fps"]["current"],
            50
        );
        release_second.send(()).unwrap();
        second_writer.join().unwrap();
    }

    #[test]
    fn t4_would_block_does_not_kill_the_reader() {
        let run_dir = temp_run_dir();
        let dir = run_dir.join("gamescope.live");
        create_dir(&dir).unwrap();
        let pipe = dir.join("stats.pipe");
        make_fifo(&pipe);
        let mut collector = FrameCollector::with_runtime_dir(None, run_dir);
        activate(&mut collector, "42");
        let writer = std::thread::spawn({
            let pipe = pipe.clone();
            move || {
                let mut writer = OpenOptions::new().write(true).open(pipe).unwrap();
                writer.write_all(b"fps=60\nfocus=42\n").unwrap();
                std::thread::sleep(Duration::from_millis(100));
                writer.write_all(b"fps=55\nfocus=42\n").unwrap();
            }
        });
        let first = wait_for_sample(&mut collector);
        assert_eq!(first["rigsignal"]["fps"]["current"], 60);
        writer.join().unwrap();
        assert_eq!(
            wait_for_sample(&mut collector)["rigsignal"]["fps"]["current"],
            55
        );
    }

    #[test]
    fn t5_no_gamescope_delegates_to_mangohud() {
        let run_dir = temp_run_dir();
        let log_dir = temp_run_dir();
        let log = log_dir.join("mangohud.csv");
        File::create(&log)
            .unwrap()
            .write_all(b"os,cpu\nLinux,test\nfps,frametime\n60,16.0\n")
            .unwrap();
        let mut collector = FrameCollector::with_runtime_dir(None, run_dir);
        collector.mango = MangoHudCollector::with_log_dir(None, log_dir);
        collector.game_active = true;
        assert_eq!(
            collector.collect().unwrap().unwrap()["rigsignal"]["fps"]["current"],
            60
        );
    }

    #[test]
    fn t6_writerless_open_never_logs_attached() {
        let run_dir = temp_run_dir();
        let dir = run_dir.join("gamescope.decoy");
        create_dir(&dir).unwrap();
        make_fifo(&dir.join("stats.pipe"));
        let messages = Arc::new(Mutex::new(Vec::new()));
        let subscriber = tracing_subscriber::registry().with(EventCapture(Arc::clone(&messages)));
        tracing::subscriber::with_default(subscriber, || {
            let mut collector = FrameCollector::with_runtime_dir(None, run_dir);
            activate(&mut collector, "42");
            std::thread::sleep(Duration::from_millis(300));
        });
        assert!(
            !messages
                .lock()
                .unwrap()
                .iter()
                .any(|message| message.contains("Gamescope frame source attached")),
            "a writer-less FIFO must not emit an attached transition before a successful read"
        );
    }

    #[test]
    fn t7_writerless_candidate_is_bounded_and_rotates_to_live() {
        let run_dir = temp_run_dir();
        let dead_dir = run_dir.join("gamescope.dead");
        create_dir(&dead_dir).unwrap();
        make_fifo(&dead_dir.join("stats.pipe"));
        let mut collector = FrameCollector::with_runtime_dir(None, run_dir);
        activate(&mut collector, "42");
        let started = Instant::now();
        // One full demotion window proves a writer-less FIFO is reopened rather
        // than parking in open(2). Count actual discovery attempts over the
        // elapsed interval so an EOF retry hot loop cannot satisfy this test.
        std::thread::sleep(DEMOTION_WINDOW + Duration::from_millis(300));
        let elapsed = started.elapsed();
        let attempts = collector.reader_attempts.load(Ordering::Relaxed);
        let max_attempts =
            (elapsed.as_millis() / REOPEN_BACKOFF_INITIAL.as_millis()).saturating_add(3) as usize;
        assert!(
            attempts >= 2,
            "writer-less FIFO should be retried after demotion"
        );
        assert!(
            attempts <= max_attempts,
            "{attempts} discovery attempts in {elapsed:?} exceeds the bounded retry rate ({max_attempts})"
        );
        assert!(collector
            .reader
            .as_ref()
            .is_some_and(|reader| !reader.join.is_finished()));

        let live_dir = collector.runtime_dir.join("gamescope.live");
        create_dir(&live_dir).unwrap();
        make_fifo(&live_dir.join("stats.pipe"));
        let (release, writer) =
            write_pipe_and_hold(live_dir.join("stats.pipe"), "fps=48\nfocus=42\n");
        assert_eq!(
            wait_for_sample(&mut collector)["rigsignal"]["fps"]["current"],
            48
        );
        release.send(()).unwrap();
        writer.join().unwrap();
    }

    #[test]
    fn collect_emits_sample_derived_frametime_and_stutter_count() {
        let mut collector = FrameCollector::with_runtime_dir(None, temp_run_dir());
        *collector.samples.lock().unwrap() = vec![60.0, 60.0, 10.0];
        let document = collector.collect_gamescope().unwrap();
        let fps = &document["rigsignal"]["fps"];
        assert_eq!(fps["frametime_ms"].as_f64(), Some(44.444));
        assert_eq!(fps["stutter_count"].as_i64(), Some(1));
        assert_eq!(fps["current"].as_i64(), Some(10));
        assert_eq!(fps["avg_1s"].as_f64(), Some(43.3));
        assert_eq!(fps["low_1pct"].as_i64(), Some(10));
        assert_eq!(fps["low_01pct"].as_i64(), Some(10));
    }

    #[test]
    fn collect_ignores_non_positive_fps_when_deriving_frametime() {
        let mut collector = FrameCollector::with_runtime_dir(None, temp_run_dir());
        *collector.samples.lock().unwrap() = vec![60.0, 0.0, -10.0];
        let document = collector.collect_gamescope().unwrap();
        let fps = &document["rigsignal"]["fps"];
        assert_eq!(fps["frametime_ms"].as_f64(), Some(16.667));
        assert_eq!(fps["stutter_count"].as_i64(), Some(0));
    }
}
