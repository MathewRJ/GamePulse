/// Session file watcher — monitors $XDG_RUNTIME_DIR/gamepulse/session.json
/// (written by the Python collector when a game is detected).
///
/// When the file appears or changes: parse it and update game_pids_map in the
/// BPF kernel via aya.
/// When the file disappears: clear game_pids_map so BPF programs stop filtering.
use anyhow::{Context, Result};
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{info, warn};

/// Contents of the session.json file written by the Python collector.
#[derive(Debug, Clone, Deserialize)]
pub struct SessionInfo {
    pub session_id: String,
    pub game_pid: u32,
    pub game_name: String,
    #[serde(default)]
    #[allow(dead_code)]
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
        PathBuf::from(xdg_runtime).join("gamepulse/session.json")
    } else {
        PathBuf::from("/tmp/gamepulse/session.json")
    }
}

/// Walk /proc/<pid>/task/ and /proc/<pid>/children recursively (bounded depth)
/// to collect all TIDs in the process trees rooted at each of `root_pids`.
pub fn collect_game_tids(root_pids: &[u32]) -> Vec<u32> {
    let mut tids = Vec::new();
    for &pid in root_pids {
        collect_tids_recursive(pid, &mut tids, 0);
    }
    tids
}

fn collect_tids_recursive(pid: u32, tids: &mut Vec<u32>, depth: u8) {
    if depth > 4 || tids.len() >= 256 {
        return;
    }

    // Add all threads of this process
    let task_dir = format!("/proc/{}/task", pid);
    if let Ok(entries) = std::fs::read_dir(&task_dir) {
        for entry in entries.flatten() {
            if let Ok(name) = entry.file_name().into_string() {
                if let Ok(tid) = name.parse::<u32>() {
                    if !tids.contains(&tid) {
                        tids.push(tid);
                    }
                }
            }
        }
    }

    // Recurse into child processes
    let children_path = format!("/proc/{}/task/{}/children", pid, pid);
    if let Ok(content) = std::fs::read_to_string(&children_path) {
        for child_pid_str in content.split_whitespace() {
            if let Ok(child_pid) = child_pid_str.parse::<u32>() {
                collect_tids_recursive(child_pid, tids, depth + 1);
            }
        }
    }
}

/// Read and parse the session file, collecting TIDs.
pub fn read_session(path: &Path) -> Result<(SessionInfo, Vec<u32>)> {
    let content = std::fs::read_to_string(path)
        .with_context(|| format!("reading session file: {}", path.display()))?;
    let mut info: SessionInfo = serde_json::from_str(&content)
        .with_context(|| "parsing session.json")?;
    // Back-compat: if the collector didn't write game_pids, fall back to game_pid.
    if info.game_pids.is_empty() {
        info.game_pids = vec![info.game_pid];
    }
    let tids = collect_game_tids(&info.game_pids);
    info!(
        session_id = %info.session_id,
        game = %info.game_name,
        game_pid = info.game_pid,
        pid_count = info.game_pids.len(),
        tid_count = tids.len(),
        "session detected"
    );
    Ok((info, tids))
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
        match read_session(&session_path) {
            Ok((info, tids)) => SessionState {
                active: true,
                tids: tids.clone(),
                info: Some(info),
            },
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
        let _ = std::fs::set_permissions(
            &watch_dir,
            std::fs::Permissions::from_mode(0o1777),
        );
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
                    // while watcher wasn't ready). Only act if we don't already have
                    // an active session — avoids re-populating GAME_PIDS every 30 s.
                    let already_active = state_clone.lock().unwrap().active;
                    if !already_active && session_path.exists() {
                        if let Ok((info, tids)) = read_session(&session_path) {
                            let new_state = SessionState {
                                active: true,
                                tids,
                                info: Some(info),
                            };
                            *state_clone.lock().unwrap() = new_state.clone();
                            let _ = tx.try_send(new_state);
                        }
                    }
                    continue;
                }
                Err(_) => break,
            };

            // Only react to events on our specific file
            let affects_session = event
                .paths
                .iter()
                .any(|p| p == &session_path);
            if !affects_session {
                continue;
            }

            let new_state = match event.kind {
                EventKind::Create(_) | EventKind::Modify(_) => {
                    match read_session(&session_path) {
                        Ok((info, tids)) => SessionState {
                            active: true,
                            tids,
                            info: Some(info),
                        },
                        Err(e) => {
                            warn!("error reading session file: {e}");
                            continue;
                        }
                    }
                }
                EventKind::Remove(_) => {
                    info!("session file removed — game ended");
                    SessionState::default()
                }
                _ => continue,
            };

            *state_clone.lock().unwrap() = new_state.clone();
            let _ = tx.try_send(new_state);
        }
    });

    Ok((state, rx))
}
