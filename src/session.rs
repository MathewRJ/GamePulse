/// Game session lifecycle and Steam game detection.
///
/// Mirrors collector/gamepulse/detector/game.py and the session-related
/// logic in collector/gamepulse/cli.py.
///
/// Detection: scans /proc/*/environ every 5 s for processes with SteamAppId
/// set. Groups all matching PIDs by app_id; picks a non-helper representative
/// for metadata. Resolves the game name from Steam appmanifest ACF files.
///
/// Session.json: written to /tmp/gamepulse/session.json when a game is
/// detected; removed on game exit or agent shutdown. Format matches what the
/// eBPF daemon's SessionInfo struct expects:
///   {"session_id":"…","game_pid":N,"game_name":"…","game_pids":[…],"steam_app_id":N}
use anyhow::Result;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};
use uuid::Uuid;

// ── Detected game ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct DetectedGame {
    pub name: String,
    pub steam_app_id: u32,
    pub pid: u32,
    /// All PIDs with this SteamAppId — used by eBPF daemon for TID filter.
    pub all_pids: Vec<u32>,
    pub graphics_api: Option<String>,
    pub proton_version: Option<String>,
    pub dxvk_version: Option<String>,
}

// ── Session events ─────────────────────────────────────────────────────────────

pub enum SessionEvent {
    NoChange,
    GameStarted(DetectedGame),
    GameEnded(DetectedGame), // the game that just ended
}

// ── Session manager ────────────────────────────────────────────────────────────

pub struct SessionManager {
    pub session_id: String,
    pub current_game: Option<DetectedGame>,
    session_json_path: PathBuf,
    last_scan: Option<Instant>,
    /// Tracks the last time a "no game detected" message was logged, to throttle
    /// INFO noise while still giving visibility into detection health.
    last_no_game_log: Option<Instant>,
}

impl SessionManager {
    pub fn new() -> Self {
        SessionManager {
            session_id: Uuid::new_v4().to_string(),
            current_game: None,
            session_json_path: PathBuf::from("/tmp/gamepulse/session.json"),
            last_scan: None,
            last_no_game_log: None,
        }
    }

    /// Poll for game changes. Scans /proc every 5s — cheaper than every tick.
    pub fn poll(&mut self) -> SessionEvent {
        let now = Instant::now();
        if let Some(last) = self.last_scan {
            if now.duration_since(last) < Duration::from_secs(5) {
                return SessionEvent::NoChange;
            }
        }
        self.last_scan = Some(now);

        let detected = scan_for_game();

        match (self.current_game.take(), detected) {
            (None, None) => {
                // Log "no game" at INFO once every 30 s so service logs show detection
                // is running even during long idle periods. This was ranked fix #1 from
                // the 2026-04-14 systemctl bug analysis.
                let log_now = match self.last_no_game_log {
                    None => true,
                    Some(t) => now.duration_since(t) >= Duration::from_secs(30),
                };
                if log_now {
                    tracing::info!("No game detected — scanning /proc every 5 s");
                    self.last_no_game_log = Some(now);
                }
                SessionEvent::NoChange
            }

            (None, Some(game)) => {
                tracing::info!(
                    "Game detected: {} (app_id={}, pid={}, api={:?})",
                    game.name, game.steam_app_id, game.pid,
                    game.graphics_api.as_deref().unwrap_or("unknown")
                );
                if let Err(e) = self.write_session_json(&game) {
                    tracing::warn!("Failed to write session.json: {}", e);
                }
                self.current_game = Some(game.clone());
                self.last_no_game_log = None; // reset so "no game" logs resume if game exits
                SessionEvent::GameStarted(game)
            }

            (Some(old), None) => {
                self.remove_session_json();
                SessionEvent::GameEnded(old)
            }

            (Some(old), Some(new_game)) => {
                if old.pid != new_game.pid || old.steam_app_id != new_game.steam_app_id {
                    // Game changed (or switched) — treat as new start.
                    if let Err(e) = self.write_session_json(&new_game) {
                        tracing::warn!("Failed to write session.json: {}", e);
                    }
                    self.current_game = Some(new_game.clone());
                    SessionEvent::GameStarted(new_game)
                } else {
                    // Same game — silently update all_pids (grows as Proton spins up threads).
                    self.current_game = Some(new_game);
                    SessionEvent::NoChange
                }
            }
        }
    }

    /// Build the base document fields included in every per-tick doc.
    /// Matches Python Session.base_doc() exactly.
    pub fn base_doc(&self, hostname: &str) -> Value {
        let mut gp_session = serde_json::Map::new();
        gp_session.insert("id".to_string(), Value::String(self.session_id.clone()));
        gp_session.insert(
            "agent_version".to_string(),
            Value::String(env!("CARGO_PKG_VERSION").to_string()),
        );
        gp_session.insert("opt_in_public".to_string(), Value::Bool(false));

        let mut gp = serde_json::Map::new();
        gp.insert("session".to_string(), Value::Object(gp_session));

        if let Some(game) = &self.current_game {
            let mut game_doc = serde_json::Map::new();
            game_doc.insert("name".to_string(), Value::String(game.name.clone()));
            game_doc.insert("steam_app_id".to_string(), Value::from(game.steam_app_id));
            if let Some(api) = &game.graphics_api {
                game_doc.insert("graphics_api".to_string(), Value::String(api.clone()));
            }
            gp.insert("game".to_string(), Value::Object(game_doc));
        }

        json!({
            "host": { "name": hostname },
            "gamepulse": gp,
        })
    }

    /// Write /tmp/gamepulse/session.json for the eBPF daemon.
    fn write_session_json(&self, game: &DetectedGame) -> Result<()> {
        let dir = self.session_json_path.parent().unwrap();
        std::fs::create_dir_all(dir)?;
        // Set mode 1777 so non-root processes can write session.json later.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o1777));
        }
        let doc = json!({
            "session_id": self.session_id,
            "game_pid": game.pid,
            "game_name": game.name,
            "game_pids": game.all_pids,
            "steam_app_id": game.steam_app_id,
        });
        std::fs::write(&self.session_json_path, serde_json::to_string(&doc)?)?;
        tracing::debug!(
            "wrote session.json: {} (pids: {:?})",
            self.session_json_path.display(),
            game.all_pids
        );
        Ok(())
    }

    /// Remove /tmp/gamepulse/session.json on game exit or agent shutdown.
    pub fn remove_session_json(&self) {
        match std::fs::remove_file(&self.session_json_path) {
            Ok(_) => tracing::debug!("removed session.json"),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => tracing::warn!("Failed to remove session.json: {}", e),
        }
    }
}

// ── /proc/environ parsing ──────────────────────────────────────────────────────

/// Read /proc/{pid}/environ as a null-separated key=value store.
fn read_environ(pid: u32) -> Option<HashMap<String, String>> {
    let bytes = std::fs::read(format!("/proc/{}/environ", pid)).ok()?;
    if bytes.is_empty() {
        return None;
    }
    let mut map = HashMap::new();
    for entry in bytes.split(|&b| b == 0) {
        if let Some(pos) = entry.iter().position(|&b| b == b'=') {
            let key = String::from_utf8_lossy(&entry[..pos]).to_string();
            let val = String::from_utf8_lossy(&entry[pos + 1..]).to_string();
            map.insert(key, val);
        }
    }
    if map.is_empty() { None } else { Some(map) }
}

// ── ACF game name lookup ───────────────────────────────────────────────────────

/// Search Steam library paths for appmanifest_{app_id}.acf and extract the
/// "name" field. Matches Python _game_name_from_appid().
fn game_name_from_appid(app_id: u32) -> Option<String> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());

    let mut roots: Vec<PathBuf> = vec![
        PathBuf::from(format!("{}/.steam/steam/steamapps", home)),
        PathBuf::from(format!("{}/.local/share/Steam/steamapps", home)),
        PathBuf::from(format!(
            "{}/.var/app/com.valvesoftware.Steam/data/Steam/steamapps",
            home
        )),
    ];

    // Also scan libraryfolders.vdf for extra library paths.
    let mut extra: Vec<PathBuf> = Vec::new();
    for root in &roots {
        let vdf = root.join("libraryfolders.vdf");
        if let Ok(content) = std::fs::read_to_string(&vdf) {
            for line in content.lines() {
                let trimmed = line.trim();
                if trimmed.starts_with("\"path\"") {
                    // Format: "path"  "/some/path" — value is in the 4th quote segment
                    if let Some(val) = trimmed.split('"').nth(3) {
                        extra.push(PathBuf::from(val).join("steamapps"));
                    }
                }
            }
        }
    }
    roots.extend(extra);

    for root in &roots {
        let acf = root.join(format!("appmanifest_{}.acf", app_id));
        if let Ok(content) = std::fs::read_to_string(&acf) {
            // Find: "name"  "Game Title Here"
            for line in content.lines() {
                let trimmed = line.trim();
                if trimmed.starts_with("\"name\"") {
                    // Split on quote pairs: key quote, key, close, whitespace, value quote, value, close
                    let parts: Vec<&str> = trimmed.split('"').collect();
                    // parts: ["", "name", "   ", "Game Title", ""]
                    if parts.len() >= 4 {
                        let name = parts[3].trim().to_string();
                        if !name.is_empty() {
                            return Some(name);
                        }
                    }
                }
            }
        }
    }
    None
}

// ── Graphics API detection ─────────────────────────────────────────────────────

/// Detect graphics API from process environment. Matches Python _detect_graphics_api().
fn detect_graphics_api(env: &HashMap<String, String>) -> (Option<String>, bool) {
    let uses_proton =
        env.contains_key("PROTON_VERSION") || env.contains_key("STEAM_COMPAT_DATA_PATH");

    let dll_overrides = env.get("WINEDLLOVERRIDES").map(|s| s.to_lowercase()).unwrap_or_default();
    if dll_overrides.contains("vkd3d") || env.contains_key("VKD3D_CONFIG") {
        return (Some("dx12_via_vkd3d".to_string()), uses_proton);
    }
    if dll_overrides.contains("dxvk") || env.contains_key("DXVK_CONFIG_FILE") {
        return (Some("dx11_via_dxvk".to_string()), uses_proton);
    }
    if env.contains_key("VULKAN_DEVICE_INDEX") || env.contains_key("VK_ICD_FILENAMES") {
        return (Some("vulkan".to_string()), uses_proton);
    }
    if uses_proton {
        return (Some("dx_via_proton".to_string()), uses_proton);
    }
    (None, false)
}

fn proton_version_from_env(env: &HashMap<String, String>) -> Option<String> {
    if let Some(v) = env.get("PROTON_VERSION") {
        return Some(v.clone());
    }
    let compat_path = env.get("STEAM_COMPAT_TOOL_PATHS")?;
    for part in compat_path.split(':') {
        let vf = std::path::Path::new(part).join("version");
        if let Ok(v) = std::fs::read_to_string(&vf) {
            let v = v.trim().to_string();
            if !v.is_empty() {
                return Some(v);
            }
        }
    }
    None
}

fn dxvk_version_from_env(env: &HashMap<String, String>) -> Option<String> {
    let log_path = env.get("DXVK_LOG_PATH")?;
    let log_file = std::path::Path::new(log_path).join("dxvk.log");
    let content = std::fs::read_to_string(&log_file).ok()?;
    let first_line = content.lines().next()?;
    // Look for vN.N[.N] in the first log line
    let mut in_v = false;
    let mut version = String::new();
    for ch in first_line.chars() {
        if ch == 'v' && !in_v {
            in_v = true;
            version.clear();
        } else if in_v && (ch.is_ascii_digit() || ch == '.') {
            version.push(ch);
        } else if in_v {
            if !version.is_empty() {
                return Some(version);
            }
            in_v = false;
        }
    }
    if !version.is_empty() { Some(version) } else { None }
}

// ── Main game scanner ──────────────────────────────────────────────────────────

/// Scan /proc for a running Steam game. Returns the best candidate or None.
/// Matches Python GameDetector._scan() exactly.
fn scan_for_game() -> Option<DetectedGame> {
    let pids: Vec<u32> = std::fs::read_dir("/proc")
        .ok()?
        .filter_map(|e| e.ok())
        .filter_map(|e| e.file_name().to_str()?.parse::<u32>().ok())
        .collect();

    let mut representative: Option<(u32, HashMap<String, String>)> = None;
    let mut all_pids_by_appid: HashMap<u32, Vec<u32>> = HashMap::new();

    for &pid in &pids {
        let env = match read_environ(pid) {
            Some(e) => e,
            None => continue,
        };

        let app_id_str = env.get("SteamAppId").or_else(|| env.get("STEAM_APP_ID"));
        let app_id: u32 = match app_id_str.and_then(|s| s.trim().parse().ok()) {
            Some(id) if id != 0 => id,
            _ => continue,
        };

        all_pids_by_appid.entry(app_id).or_default().push(pid);

        // Pick first non-helper process as the representative for metadata.
        // Wine/Proton subprocesses are in all_pids for eBPF but bad sources of metadata.
        if representative.is_none() {
            let exe_path = std::fs::read_link(format!("/proc/{}/exe", pid))
                .ok()
                .and_then(|p| Some(p.to_string_lossy().to_lowercase()))
                .unwrap_or_default();
            let skip = ["proton", "wine", "steam", "reaper"];
            if !skip.iter().any(|w| exe_path.contains(w)) {
                representative = Some((pid, env));
            }
        }
    }

    if all_pids_by_appid.is_empty() {
        return None;
    }

    // Resolve representative → (pid, env, app_id)
    let (pid, env, app_id) = if let Some((rep_pid, rep_env)) = representative {
        let app_id = rep_env
            .get("SteamAppId")
            .or_else(|| rep_env.get("STEAM_APP_ID"))
            .and_then(|s| s.trim().parse().ok())
            .unwrap_or(0u32);
        (rep_pid, rep_env, app_id)
    } else {
        // All were helpers — use first pid of first app_id.
        let app_id = *all_pids_by_appid.keys().next()?;
        let first_pid = all_pids_by_appid[&app_id][0];
        let env = read_environ(first_pid).unwrap_or_default();
        (first_pid, env, app_id)
    };

    let name = game_name_from_appid(app_id).unwrap_or_else(|| format!("App {}", app_id));
    let (graphics_api, _uses_proton) = detect_graphics_api(&env);
    let proton_version = proton_version_from_env(&env);
    let dxvk_version = dxvk_version_from_env(&env);
    let all_pids = all_pids_by_appid.get(&app_id).cloned().unwrap_or_else(|| vec![pid]);

    Some(DetectedGame {
        name,
        steam_app_id: app_id,
        pid,
        all_pids,
        graphics_api,
        proton_version,
        dxvk_version,
    })
}
