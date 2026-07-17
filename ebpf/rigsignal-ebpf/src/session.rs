/// Session file watcher — monitors $XDG_RUNTIME_DIR/rigsignal/session.json
/// (written by the Python collector when a game is detected).
///
/// When the file appears or changes: parse it and update game_pids_map in the
/// BPF kernel via aya.
/// When the file disappears: clear game_pids_map so BPF programs stop filtering.
use anyhow::{Context, Result};
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Deserialize;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

const MAX_GAME_TIDS: usize = 1024;
const MAX_PROC_SCAN_PIDS: usize = 32_768;

/// Contents of the session.json file written by the Python collector.
#[derive(Debug, Clone, Deserialize)]
pub struct SessionInfo {
    pub session_id: String,
    pub game_pid: u32,
    pub game_name: String,
    #[serde(default)]
    pub steam_app_id: Option<u32>,
    /// All PIDs associated with the game (includes wine/Proton subprocesses).
    /// Falls back to [game_pid] if absent (older collector versions).
    #[serde(default)]
    pub game_pids: Vec<u32>,
}

/// Shared session state — updated by the watcher task, read by the aggregator.
#[derive(Debug, Clone, Default)]
pub struct SessionState {
    pub active: bool,
    pub info: Option<SessionInfo>,
    /// All TIDs belonging to the game process tree (game_pid + all threads).
    pub tids: Vec<u32>,
}

/// Canonical path for the session file.
pub fn session_file_path() -> PathBuf {
    if let Ok(xdg_runtime) = std::env::var("XDG_RUNTIME_DIR") {
        PathBuf::from(xdg_runtime).join("rigsignal/session.json")
    } else {
        PathBuf::from("/tmp/rigsignal/session.json")
    }
}

#[derive(Debug, Clone, Copy)]
enum TidSource {
    RecordedPids,
    EnvironScan,
    Union,
}

impl TidSource {
    fn as_str(self) -> &'static str {
        match self {
            Self::RecordedPids => "recorded pids",
            Self::EnvironScan => "environ scan",
            Self::Union => "union",
        }
    }
}

#[derive(Debug)]
struct TidDiscovery {
    tids: Vec<u32>,
    source: TidSource,
}

fn collect_game_tids_from_proc(proc_root: &Path, root_pids: &[u32]) -> Vec<u32> {
    let mut tids = HashSet::new();
    let mut visited_pids = HashSet::new();
    for &pid in root_pids {
        collect_tids_recursive(proc_root, pid, &mut tids, &mut visited_pids, 0);
    }
    let mut tids: Vec<_> = tids.into_iter().collect();
    tids.sort_unstable();
    tids
}

fn collect_tids_recursive(
    proc_root: &Path,
    pid: u32,
    tids: &mut HashSet<u32>,
    visited_pids: &mut HashSet<u32>,
    depth: u8,
) {
    if depth > 4 || tids.len() >= MAX_GAME_TIDS || !visited_pids.insert(pid) {
        return;
    }

    // Add all threads of this process
    let task_dir = proc_root.join(pid.to_string()).join("task");
    let mut thread_ids = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&task_dir) {
        for entry in entries.flatten() {
            if let Ok(name) = entry.file_name().into_string() {
                if let Ok(tid) = name.parse::<u32>() {
                    thread_ids.push(tid);
                }
            }
        }
    }
    thread_ids.sort_unstable();
    thread_ids.dedup();
    for &tid in &thread_ids {
        if tids.len() >= MAX_GAME_TIDS {
            break;
        }
        tids.insert(tid);
    }

    // Wine and Proton can create child processes from worker threads, so read
    // every /proc/<pid>/task/<tid>/children file rather than only the main one.
    for tid in thread_ids {
        if tids.len() >= MAX_GAME_TIDS {
            return;
        }
        let children_path = task_dir.join(tid.to_string()).join("children");
        if let Ok(content) = std::fs::read_to_string(children_path) {
            for child_pid_str in content.split_whitespace() {
                if let Ok(child_pid) = child_pid_str.parse::<u32>() {
                    collect_tids_recursive(proc_root, child_pid, tids, visited_pids, depth + 1);
                }
            }
        }
    }
}

fn find_steam_processes(proc_root: &Path, steam_app_id: u32) -> Vec<u32> {
    let expected_game_id = format!("SteamGameId={steam_app_id}");
    let expected_app_id = format!("SteamAppId={steam_app_id}");
    let mut pids = Vec::new();
    let mut scanned = 0;

    let Ok(entries) = std::fs::read_dir(proc_root) else {
        return pids;
    };
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<u32>() else {
            continue;
        };
        if scanned >= MAX_PROC_SCAN_PIDS {
            break;
        }
        scanned += 1;

        let Ok(environ) = std::fs::read(entry.path().join("environ")) else {
            continue;
        };
        if environ.split(|byte| *byte == 0).any(|entry| {
            entry == expected_game_id.as_bytes() || entry == expected_app_id.as_bytes()
        }) {
            pids.push(pid);
        }
    }
    pids.sort_unstable();
    pids
}

fn discover_game_tids_from_proc(proc_root: &Path, info: &SessionInfo) -> TidDiscovery {
    let recorded_tids = collect_game_tids_from_proc(proc_root, &info.game_pids);
    let Some(steam_app_id) = info.steam_app_id else {
        return TidDiscovery {
            tids: recorded_tids,
            source: TidSource::RecordedPids,
        };
    };

    let environ_pids = find_steam_processes(proc_root, steam_app_id);
    if environ_pids.is_empty() {
        return TidDiscovery {
            tids: recorded_tids,
            source: TidSource::RecordedPids,
        };
    }
    if recorded_tids.is_empty() {
        return TidDiscovery {
            tids: collect_game_tids_from_proc(proc_root, &environ_pids),
            source: TidSource::EnvironScan,
        };
    }

    let mut roots = info.game_pids.clone();
    roots.extend(environ_pids);
    TidDiscovery {
        tids: collect_game_tids_from_proc(proc_root, &roots),
        source: TidSource::Union,
    }
}

fn discover_game_tids(info: &SessionInfo) -> TidDiscovery {
    discover_game_tids_from_proc(Path::new("/proc"), info)
}

/// Read and parse the session file, collecting TIDs.
pub fn read_session(path: &Path) -> Result<(SessionInfo, Vec<u32>)> {
    let content = std::fs::read_to_string(path)
        .with_context(|| format!("reading session file: {}", path.display()))?;
    let mut info: SessionInfo =
        serde_json::from_str(&content).with_context(|| "parsing session.json")?;
    // Back-compat: if the collector didn't write game_pids, fall back to game_pid.
    if info.game_pids.is_empty() {
        info.game_pids = vec![info.game_pid];
    }
    let discovery = discover_game_tids(&info);
    info!(
        session_id = %info.session_id,
        game = %info.game_name,
        game_pid = info.game_pid,
        pid_count = info.game_pids.len(),
        seed_source = discovery.source.as_str(),
        tid_count = discovery.tids.len(),
        "session detected"
    );
    Ok((info, discovery.tids))
}

fn active_session_state(path: &Path) -> Result<SessionState> {
    let (info, tids) = read_session(path)?;
    Ok(SessionState {
        active: true,
        tids,
        info: Some(info),
    })
}

fn refreshed_active_session_state(current: &SessionState) -> Option<(SessionState, TidSource)> {
    refreshed_active_session_state_from_proc(current, Path::new("/proc"))
}

fn refreshed_active_session_state_from_proc(
    current: &SessionState,
    proc_root: &Path,
) -> Option<(SessionState, TidSource)> {
    let info = current.info.as_ref()?.clone();
    let discovery = discover_game_tids_from_proc(proc_root, &info);
    if discovery.tids == current.tids {
        return None;
    }
    Some((
        SessionState {
            active: true,
            info: Some(info),
            tids: discovery.tids,
        },
        discovery.source,
    ))
}

/// Spawn the session watcher task.
///
/// Returns a shared `Arc<Mutex<SessionState>>` that other tasks can read,
/// plus a channel receiver that fires whenever the session state changes
/// (so the loader can update game_pids_map without polling).
pub fn spawn_watcher(
    session_path: PathBuf,
) -> Result<(Arc<Mutex<SessionState>>, mpsc::Receiver<SessionState>)> {
    let state = Arc::new(Mutex::new(SessionState::default()));
    let (tx, rx) = mpsc::channel::<SessionState>(16);

    // Seed with current state if session file already exists
    let initial_state = if session_path.exists() {
        match active_session_state(&session_path) {
            Ok(state) => state,
            Err(e) => {
                warn!("could not read existing session file: {e}");
                SessionState::default()
            }
        }
    } else {
        SessionState::default()
    };

    {
        let mut s = state.lock().unwrap();
        *s = initial_state.clone();
    }
    // Only send initial state if a session was already active when the daemon
    // started. An inactive initial state would trigger a spurious "session ended"
    // log in the main loop on every startup.
    if initial_state.active {
        let _ = tx.try_send(initial_state);
    }

    let state_clone = Arc::clone(&state);
    let watch_dir = session_path
        .parent()
        .unwrap_or(std::path::Path::new("/tmp"))
        .to_path_buf();

    // Create the watch directory if it doesn't exist (daemon might start before
    // the Python collector has run for the first time).
    // Mode 1777 (world-writable + sticky) so the unprivileged collector can
    // write session.json into a directory created by root.
    let _ = std::fs::create_dir_all(&watch_dir);
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&watch_dir, std::fs::Permissions::from_mode(0o1777));
    }

    std::thread::spawn(move || {
        let (notify_tx, notify_rx) = std::sync::mpsc::channel();

        let mut watcher = match RecommendedWatcher::new(
            move |res: Result<Event, notify::Error>| {
                if let Ok(event) = res {
                    let _ = notify_tx.send(event);
                }
            },
            notify::Config::default().with_poll_interval(Duration::from_secs(2)),
        ) {
            Ok(w) => w,
            Err(e) => {
                warn!("could not create filesystem watcher: {e}; session tracking disabled");
                return;
            }
        };

        if let Err(e) = watcher.watch(&watch_dir, RecursiveMode::NonRecursive) {
            warn!("could not watch {}: {e}", watch_dir.display());
            return;
        }

        info!("watching {} for session changes", watch_dir.display());

        loop {
            let event = match notify_rx.recv_timeout(Duration::from_secs(30)) {
                Ok(e) => e,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    // Safety-net: catch missed inotify events (e.g. session started
                    // while watcher wasn't ready), and refresh active games because
                    // their process trees can change after the initial seed.
                    let current = state_clone.lock().unwrap().clone();
                    if current.active {
                        if let Some((new_state, source)) = refreshed_active_session_state(&current)
                        {
                            info!(
                                session_id = new_state
                                    .info
                                    .as_ref()
                                    .map(|i| i.session_id.as_str())
                                    .unwrap_or(""),
                                seed_source = source.as_str(),
                                previous_tid_count = current.tids.len(),
                                tid_count = new_state.tids.len(),
                                "session TID refresh changed — updating PID filter"
                            );
                            *state_clone.lock().unwrap() = new_state.clone();
                            let _ = tx.try_send(new_state);
                        } else {
                            debug!(
                                session_id = current
                                    .info
                                    .as_ref()
                                    .map(|i| i.session_id.as_str())
                                    .unwrap_or(""),
                                tid_count = current.tids.len(),
                                "session TID refresh unchanged"
                            );
                        }
                    } else if session_path.exists() {
                        if let Ok(new_state) = active_session_state(&session_path) {
                            *state_clone.lock().unwrap() = new_state.clone();
                            let _ = tx.try_send(new_state);
                        }
                    }
                    continue;
                }
                Err(_) => break,
            };

            // Only react to events on our specific file
            let affects_session = event.paths.iter().any(|p| p == &session_path);
            if !affects_session {
                continue;
            }

            let new_state = match event.kind {
                EventKind::Create(_) | EventKind::Modify(_) => {
                    match active_session_state(&session_path) {
                        Ok(state) => state,
                        Err(e) => {
                            warn!("error reading session file: {e}");
                            continue;
                        }
                    }
                }
                EventKind::Remove(_) => {
                    if session_path.exists() {
                        match active_session_state(&session_path) {
                            Ok(state) => state,
                            Err(e) => {
                                warn!("error reading replaced session file: {e}");
                                continue;
                            }
                        }
                    } else {
                        info!("session file removed — game ended");
                        SessionState::default()
                    }
                }
                _ => continue,
            };

            *state_clone.lock().unwrap() = new_state.clone();
            let _ = tx.try_send(new_state);
        }
    });

    Ok((state, rx))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_session_path(test_name: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir()
            .join(format!(
                "rigsignal-ebpf-{test_name}-{}-{nanos}",
                std::process::id()
            ))
            .join("session.json")
    }

    fn write_session_file(path: &Path, session_id: &str, game_pid: u32) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let doc = json!({
            "session_id": session_id,
            "game_pid": game_pid,
            "game_name": "test-game",
            "game_pids": [game_pid],
        });
        std::fs::write(path, serde_json::to_string(&doc).unwrap()).unwrap();
    }

    fn temp_proc_root(test_name: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "rigsignal-ebpf-proc-{test_name}-{}-{nanos}",
            std::process::id()
        ))
    }

    fn create_process(proc_root: &Path, pid: u32, tids: &[u32]) {
        for tid in tids {
            std::fs::create_dir_all(
                proc_root
                    .join(pid.to_string())
                    .join("task")
                    .join(tid.to_string()),
            )
            .unwrap();
        }
    }

    fn write_children(proc_root: &Path, pid: u32, tid: u32, children: &str) {
        std::fs::write(
            proc_root
                .join(pid.to_string())
                .join("task")
                .join(tid.to_string())
                .join("children"),
            children,
        )
        .unwrap();
    }

    fn test_session_info(game_pid: u32, steam_app_id: Option<u32>) -> SessionInfo {
        SessionInfo {
            session_id: "test-session".to_string(),
            game_pid,
            game_name: "test-game".to_string(),
            steam_app_id,
            game_pids: vec![game_pid],
        }
    }

    #[test]
    fn dead_recorded_pid_uses_matching_environ_process() {
        let proc_root = temp_proc_root("environ-discovery");
        create_process(&proc_root, 4242, &[4242, 4243]);
        std::fs::write(
            proc_root.join("4242/environ"),
            b"PATH=/usr/bin\0SteamGameId=12345\0",
        )
        .unwrap();

        let discovery =
            discover_game_tids_from_proc(&proc_root, &test_session_info(9999, Some(12345)));

        assert!(matches!(discovery.source, TidSource::EnvironScan));
        assert_eq!(discovery.tids, vec![4242, 4243]);
        let _ = std::fs::remove_dir_all(proc_root);
    }

    #[test]
    fn walks_children_of_every_thread() {
        let proc_root = temp_proc_root("per-thread-children");
        create_process(&proc_root, 100, &[100, 101]);
        create_process(&proc_root, 200, &[200, 201]);
        write_children(&proc_root, 100, 101, "200");

        assert_eq!(
            collect_game_tids_from_proc(&proc_root, &[100]),
            vec![100, 101, 200, 201]
        );
        let _ = std::fs::remove_dir_all(proc_root);
    }

    #[test]
    fn collects_up_to_game_pid_map_capacity() {
        let proc_root = temp_proc_root("tid-cap");
        let tids: Vec<u32> = (1..=1_100).collect();
        create_process(&proc_root, 300, &tids);

        let collected = collect_game_tids_from_proc(&proc_root, &[300]);

        assert_eq!(collected.len(), MAX_GAME_TIDS);
        assert_eq!(collected[0], 1);
        assert_eq!(collected[MAX_GAME_TIDS - 1], MAX_GAME_TIDS as u32);
        let _ = std::fs::remove_dir_all(proc_root);
    }

    #[test]
    fn refresh_detects_added_tid() {
        let proc_root = temp_proc_root("refresh");
        create_process(&proc_root, 500, &[500]);
        let info = test_session_info(500, None);
        let current = SessionState {
            active: true,
            info: Some(info.clone()),
            tids: collect_game_tids_from_proc(&proc_root, &info.game_pids),
        };
        create_process(&proc_root, 500, &[500, 501]);

        let (refreshed, _) = refreshed_active_session_state_from_proc(&current, &proc_root)
            .expect("added TID should trigger a refresh");
        assert_eq!(refreshed.tids, vec![500, 501]);
        let _ = std::fs::remove_dir_all(proc_root);
    }

    async fn recv_session_id(
        rx: &mut mpsc::Receiver<SessionState>,
        expected_session_id: &str,
    ) -> SessionState {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
        loop {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            assert!(
                !remaining.is_zero(),
                "timed out waiting for session {expected_session_id}"
            );
            match tokio::time::timeout(remaining, rx.recv()).await {
                Ok(Some(state))
                    if state.info.as_ref().map(|i| i.session_id.as_str())
                        == Some(expected_session_id) =>
                {
                    return state;
                }
                Ok(Some(_)) => continue,
                Ok(None) => panic!("watcher channel closed"),
                Err(_) => panic!("timed out waiting for session {expected_session_id}"),
            }
        }
    }

    async fn write_until_observed(
        path: &Path,
        rx: &mut mpsc::Receiver<SessionState>,
        session_id: &str,
        game_pid: u32,
    ) -> SessionState {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
        loop {
            write_session_file(path, session_id, game_pid);
            match tokio::time::timeout(Duration::from_millis(100), rx.recv()).await {
                Ok(Some(state))
                    if state.info.as_ref().map(|i| i.session_id.as_str()) == Some(session_id) =>
                {
                    return state;
                }
                Ok(Some(_)) | Err(_) => {
                    assert!(
                        tokio::time::Instant::now() < deadline,
                        "timed out waiting for session {session_id}"
                    );
                    tokio::time::sleep(Duration::from_millis(25)).await;
                }
                Ok(None) => panic!("watcher channel closed"),
            }
        }
    }

    #[tokio::test]
    async fn watcher_observes_in_place_session_update() {
        let session_path = temp_session_path("in-place");
        write_session_file(&session_path, "initial", 1111);

        let (_state, mut rx) = spawn_watcher(session_path.clone()).unwrap();
        let initial = recv_session_id(&mut rx, "initial").await;
        assert!(initial.active);

        let updated = write_until_observed(&session_path, &mut rx, "updated", 2222).await;
        assert!(updated.active);
        assert_eq!(updated.info.unwrap().game_pid, 2222);

        let _ = std::fs::remove_dir_all(session_path.parent().unwrap());
    }

    #[tokio::test]
    async fn watcher_observes_session_after_delete_and_recreate() {
        let session_path = temp_session_path("delete-recreate");
        write_session_file(&session_path, "initial", 1111);

        let (_state, mut rx) = spawn_watcher(session_path.clone()).unwrap();
        let _ = recv_session_id(&mut rx, "initial").await;
        let _ = write_until_observed(&session_path, &mut rx, "ready", 2222).await;

        std::fs::remove_file(&session_path).unwrap();
        let replaced = write_until_observed(&session_path, &mut rx, "recreated", 3333).await;

        assert!(replaced.active);
        assert_eq!(replaced.info.unwrap().game_pid, 3333);

        let _ = std::fs::remove_dir_all(session_path.parent().unwrap());
    }
}
