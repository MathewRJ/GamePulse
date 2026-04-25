/// Game session lifecycle and target detection (Steam + future launchers).
///
/// Detection: scans /proc/*/environ every 5 s for processes with SteamAppId
/// set. Groups all matching PIDs by app_id; picks a non-helper representative
/// for metadata. Resolves the game name from Steam appmanifest ACF files.
/// B2.3-B2.7 will add Lutris / Heroic / Bottles / user-specified detectors.
///
/// Session.json: written to /tmp/gamepulse/session.json when a target is
/// detected; removed on game exit or agent shutdown. Fields read by the
/// eBPF daemon's SessionInfo struct:
///   {"session_id":"…","game_pid":N,"game_name":"…","game_pids":[…],
///    "target_source":"steam","steam_app_id":N}
/// `steam_app_id` is optional (absent for non-Steam sources). `target_source`
/// is new in B2.2 — daemon ignores it via default serde behaviour.
use anyhow::Result;
use chrono::Utc;
use fs2::FileExt;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::Read;
use std::path::PathBuf;
use std::time::{Duration, Instant};
use uuid::Uuid;

// ── Target (detected game/app) ─────────────────────────────────────────────────

/// Where a `Target` came from. Only `Steam` is constructed today; the other
/// variants are reserved for B2.3-B2.7 (Lutris/Heroic/Bottles/UserSpecified)
/// and B3 (AutoDetected). Per-crate `dead_code = "allow"` keeps them from
/// tripping clippy until their detectors land.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetSource {
    Steam,
    Lutris,
    Heroic,
    Bottles,
    UserSpecified,
    AutoDetected,
}

/// A running game/application that GamePulse is scoped to. Generalises the
/// previous Steam-only `DetectedGame`.
///
/// Runtime invariants (not compiler-checked):
/// - `source == TargetSource::Steam` ⇒ `steam_app_id.is_some()`.
/// - `source != TargetSource::Steam` ⇒ `steam_app_id.is_none()`.
/// - `launcher` mirrors `source` in human-readable form (e.g. "Steam", "Lutris",
///   "Heroic — Epic"). Absent only if the source doesn't have a launcher concept.
/// - `display_name` is non-empty.
#[derive(Debug, Clone)]
pub struct Target {
    pub source: TargetSource,
    pub display_name: String,
    pub pid: u32,
    /// All PIDs that belong to this target — used by the eBPF daemon for TID filtering.
    pub all_pids: Vec<u32>,
    pub steam_app_id: Option<u32>,
    pub launcher: Option<String>,
    pub graphics_api: Option<String>,
    pub proton_version: Option<String>,
    pub dxvk_version: Option<String>,
}

impl Target {
    /// Construct a Steam-sourced `Target`. Used by `scan_for_steam_game`.
    pub fn from_steam(
        name: String,
        steam_app_id: u32,
        pid: u32,
        all_pids: Vec<u32>,
        graphics_api: Option<String>,
        proton_version: Option<String>,
        dxvk_version: Option<String>,
    ) -> Self {
        Target {
            source: TargetSource::Steam,
            display_name: name,
            pid,
            all_pids,
            steam_app_id: Some(steam_app_id),
            launcher: Some("Steam".to_string()),
            graphics_api,
            proton_version,
            dxvk_version,
        }
    }
}

// ── Session events ─────────────────────────────────────────────────────────────

pub enum SessionEvent {
    NoChange,
    GameStarted(Target),
    GameEnded(Target), // the target that just ended
}

// ── Session manager ────────────────────────────────────────────────────────────

pub struct SessionManager {
    pub session_id: String,
    pub current_game: Option<Target>,
    /// Current label written to every doc. Auto-generated unless the user supplied
    /// a manual override via --label / [session].label in the config.
    ///
    /// Priority at runtime:
    ///   1. Manual label (user override) — set once, never changed.
    ///   2. Auto game label: <slug>-<YYYYMMDD>-<N> — updated on game detection.
    ///   3. Auto idle label: idle-<YYYYMMDD>-<HHMMSS> — set at startup, before any game.
    pub label: Option<String>,
    /// True when the user explicitly set a label — prevents auto-generation from
    /// overwriting it on game detection.
    label_is_manual: bool,
    /// "auto" | "manual" — emitted as gamepulse.session.label_source.
    pub label_source: &'static str,
    /// Per-game per-day ordinal counter. None for manual labels.
    pub sequence_number: Option<u32>,
    /// Pre-resolved Tier 1 settings overlay — merged into session-start and summary docs.
    /// Structure: { "gamepulse": { "settings": { ... } } }. None if nothing configured.
    pub settings_overlay: Option<Value>,
    session_json_path: PathBuf,
    last_scan: Option<Instant>,
    /// Tracks the last time a "no game detected" message was logged, to throttle
    /// INFO noise while still giving visibility into detection health.
    last_no_game_log: Option<Instant>,
}

impl SessionManager {
    pub fn new() -> Self {
        Self::new_with_label_and_settings(None, None)
    }

    pub fn new_with_label(manual_label: Option<String>) -> Self {
        Self::new_with_label_and_settings(manual_label, None)
    }

    /// Create a `SessionManager`.
    ///
    /// If `manual_label` is `Some(s)` and non-empty, it is used as-is and never
    /// overridden by auto-generation. Otherwise an `idle-YYYYMMDD-HHMMSS` label
    /// is generated immediately; it will be replaced with a game slug label when
    /// the first game is detected.
    ///
    /// `settings_overlay` is a pre-built JSON value of the form
    /// `{ "gamepulse": { "settings": { ... } } }` derived from CLI flags and
    /// [session.settings] config. Pass `None` if nothing was configured.
    pub fn new_with_label_and_settings(
        manual_label: Option<String>,
        settings_overlay: Option<Value>,
    ) -> Self {
        let (label, label_is_manual, label_source) = match manual_label {
            Some(s) if !s.is_empty() => (Some(s), true, "manual"),
            _ => (Some(auto_label_idle()), false, "auto"),
        };
        SessionManager {
            session_id: Uuid::new_v4().to_string(),
            current_game: None,
            label,
            label_is_manual,
            label_source,
            sequence_number: None,
            settings_overlay,
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

            (None, Some(target)) => {
                // Auto-generate a game slug label unless the user set a manual one.
                if !self.label_is_manual {
                    let slug = slug_from_game_name(&target.display_name);
                    let n = increment_session_counter(&slug);
                    self.sequence_number = Some(n);
                    self.label = Some(auto_label_game_n(&slug, n));
                }
                tracing::info!(
                    "Game detected: {} (source={:?}, app_id={:?}, pid={}, api={:?}, label={:?})",
                    target.display_name,
                    target.source,
                    target.steam_app_id,
                    target.pid,
                    target.graphics_api.as_deref().unwrap_or("unknown"),
                    self.label.as_deref().unwrap_or("")
                );
                if let Err(e) = self.write_session_json(&target) {
                    tracing::warn!("Failed to write session.json: {}", e);
                }
                self.current_game = Some(target.clone());
                self.last_no_game_log = None; // reset so "no game" logs resume if game exits
                SessionEvent::GameStarted(target)
            }

            (Some(old), None) => {
                self.remove_session_json();
                SessionEvent::GameEnded(old)
            }

            (Some(old), Some(new_target)) => {
                if old.pid != new_target.pid || old.steam_app_id != new_target.steam_app_id {
                    // Target changed (or switched) — treat as new start.
                    if let Err(e) = self.write_session_json(&new_target) {
                        tracing::warn!("Failed to write session.json: {}", e);
                    }
                    self.current_game = Some(new_target.clone());
                    SessionEvent::GameStarted(new_target)
                } else {
                    // Same target — silently update all_pids (grows as Proton spins up threads).
                    self.current_game = Some(new_target);
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
        if let Some(label) = &self.label {
            gp_session.insert("label".to_string(), Value::String(label.clone()));
        }
        gp_session.insert(
            "label_source".to_string(),
            Value::String(self.label_source.to_string()),
        );
        if let Some(n) = self.sequence_number {
            gp_session.insert("sequence_number".to_string(), Value::from(n));
        }

        let mut gp = serde_json::Map::new();
        gp.insert("session".to_string(), Value::Object(gp_session));

        if let Some(target) = &self.current_game {
            gp.insert(
                "game".to_string(),
                Value::Object(target_to_game_doc(target)),
            );
        }

        json!({
            "host": { "name": hostname },
            "gamepulse": gp,
        })
    }

    /// Write session.json for the eBPF daemon.
    ///
    /// The daemon's `SessionInfo` reads: session_id, game_pid, game_name,
    /// game_pids (optional, falls back to game_pid), steam_app_id (optional).
    /// `target_source` is new in B2.2 — the daemon silently ignores unknown
    /// fields via default serde behaviour (no `deny_unknown_fields`).
    fn write_session_json(&self, target: &Target) -> Result<()> {
        let dir = self.session_json_path.parent().unwrap();
        std::fs::create_dir_all(dir)?;
        // Set mode 1777 so non-root processes can write session.json later.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o1777));
        }
        let mut doc = serde_json::Map::new();
        doc.insert(
            "session_id".to_string(),
            Value::String(self.session_id.clone()),
        );
        doc.insert("game_pid".to_string(), Value::from(target.pid));
        doc.insert(
            "game_name".to_string(),
            Value::String(target.display_name.clone()),
        );
        doc.insert(
            "game_pids".to_string(),
            Value::Array(target.all_pids.iter().map(|&p| Value::from(p)).collect()),
        );
        doc.insert(
            "target_source".to_string(),
            Value::String(target_source_str(target.source).to_string()),
        );
        if let Some(app_id) = target.steam_app_id {
            doc.insert("steam_app_id".to_string(), Value::from(app_id));
        }
        std::fs::write(
            &self.session_json_path,
            serde_json::to_string(&Value::Object(doc))?,
        )?;
        tracing::debug!(
            "wrote session.json: {} (pids: {:?})",
            self.session_json_path.display(),
            target.all_pids
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

// ── Target helpers ────────────────────────────────────────────────────────────

/// Canonical wire-format string for a `TargetSource`.
/// Must stay in sync with the `gamepulse.game.source` enum in fields.yml.
/// Exhaustive match forces an update here whenever a new variant is added.
fn target_source_str(source: TargetSource) -> &'static str {
    match source {
        TargetSource::Steam => "steam",
        TargetSource::Lutris => "lutris",
        TargetSource::Heroic => "heroic",
        TargetSource::Bottles => "bottles",
        TargetSource::UserSpecified => "user_specified",
        TargetSource::AutoDetected => "auto_detected",
    }
}

/// Build the `gamepulse.game.*` doc map for a target.
/// Shared by `SessionManager::base_doc` (per-tick context) and
/// `build_summary_doc` in main.rs (session-end summary).
pub fn target_to_game_doc(target: &Target) -> serde_json::Map<String, Value> {
    let mut game_doc = serde_json::Map::new();
    game_doc.insert(
        "name".to_string(),
        Value::String(target.display_name.clone()),
    );
    game_doc.insert(
        "source".to_string(),
        Value::String(target_source_str(target.source).to_string()),
    );
    if let Some(app_id) = target.steam_app_id {
        game_doc.insert("steam_app_id".to_string(), Value::from(app_id));
    }
    if let Some(launcher) = &target.launcher {
        game_doc.insert("launcher".to_string(), Value::String(launcher.clone()));
    }
    if let Some(api) = &target.graphics_api {
        game_doc.insert("graphics_api".to_string(), Value::String(api.clone()));
    }
    game_doc
}

// ── Label helpers ──────────────────────────────────────────────────────────────

/// Generate a UTC timestamp string in the format YYYYMMDD-HHMMSS (idle labels only).
fn label_timestamp() -> String {
    Utc::now().format("%Y%m%d-%H%M%S").to_string()
}

/// Slugify a game name: lowercase, spaces→hyphens, strip non-alphanumeric
/// (except hyphens), truncate to 32 chars.
///
/// Examples:
///   "Starfield"                      → "starfield"
///   "Cyberpunk 2077"                  → "cyberpunk-2077"
///   "The Elder Scrolls V: Skyrim"     → "the-elder-scrolls-v-skyrim"
pub(crate) fn slug_from_game_name(name: &str) -> String {
    name.to_lowercase()
        .chars()
        .map(|c| if c == ' ' { '-' } else { c })
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-')
        .take(32)
        .collect()
}

/// Auto-label for when no game is running: "idle-YYYYMMDD-HHMMSS".
fn auto_label_idle() -> String {
    format!("idle-{}", label_timestamp())
}

/// Auto-label once a game is detected: "<slug>-YYYYMMDD-N".
fn auto_label_game_n(slug: &str, n: u32) -> String {
    let date = Utc::now().format("%Y%m%d").to_string();
    format!("{}-{}-{}", slug, date, n)
}

// ── Session counter (B.8) ──────────────────────────────────────────────────────

/// Path to the per-game-per-day session counter file.
fn counter_file_path() -> PathBuf {
    #[cfg(unix)]
    {
        let state_dir = std::env::var("XDG_STATE_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
                PathBuf::from(home).join(".local/state")
            });
        state_dir.join("gamepulse/session-counters.json")
    }
    #[cfg(windows)]
    {
        let local_app_data = std::env::var("LOCALAPPDATA")
            .unwrap_or_else(|_| r"C:\Users\Default\AppData\Local".to_string());
        PathBuf::from(local_app_data).join(r"GamePulse\session-counters.json")
    }
}

/// Increment and persist the session counter for `slug` on today's UTC date.
/// Returns the new counter value (1-based). On any I/O error returns 1 and
/// logs a warning so the agent keeps running.
pub(crate) fn increment_session_counter(slug: &str) -> u32 {
    increment_counter_at(slug, &counter_file_path())
}

fn increment_counter_at(slug: &str, path: &std::path::Path) -> u32 {
    let dir = match path.parent() {
        Some(d) => d,
        None => return 1,
    };

    if !dir.exists() {
        if let Err(e) = std::fs::create_dir_all(dir) {
            tracing::warn!(
                "session counter: cannot create dir {}: {}",
                dir.display(),
                e
            );
            return 1;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
        }
    }

    let mut lock_file = match std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false) // we read before overwriting; atomic rename handles the write
        .open(path)
    {
        Ok(f) => f,
        Err(e) => {
            tracing::warn!("session counter: cannot open {}: {}", path.display(), e);
            return 1;
        }
    };

    if let Err(e) = lock_file.lock_exclusive() {
        tracing::warn!("session counter: cannot lock {}: {}", path.display(), e);
        return 1;
    }

    let mut contents = String::new();
    let _ = lock_file.read_to_string(&mut contents);

    let mut counters: serde_json::Map<String, Value> = if contents.trim().is_empty() {
        serde_json::Map::new()
    } else {
        serde_json::from_str(&contents).unwrap_or_default()
    };

    let today = Utc::now().format("%Y-%m-%d").to_string();

    // Prune entries older than 30 days when _last_pruned differs from today.
    let last_pruned = counters
        .get("_last_pruned")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if last_pruned != today {
        let cutoff = (Utc::now() - chrono::Duration::days(30))
            .format("%Y-%m-%d")
            .to_string();
        counters.retain(|k, _| {
            if k == "_last_pruned" {
                return true;
            }
            // key format: "<slug>:<YYYY-MM-DD>"
            k.split(':')
                .nth(1)
                .map(|d| d >= cutoff.as_str())
                .unwrap_or(true)
        });
        counters.insert("_last_pruned".to_string(), Value::String(today.clone()));
    }

    let key = format!("{}:{}", slug, today);
    let n = counters.get(&key).and_then(|v| v.as_u64()).unwrap_or(0) + 1;
    counters.insert(key, Value::from(n));

    // Atomic write: write to a tmpfile then rename over the target.
    let mut tmp_name = std::ffi::OsString::from(path.file_name().unwrap_or_default());
    tmp_name.push(".tmp");
    let tmp_path = dir.join(tmp_name);

    let serialised =
        serde_json::to_string_pretty(&Value::Object(counters)).unwrap_or_else(|_| "{}".to_string());
    if let Err(e) = std::fs::write(&tmp_path, &serialised) {
        tracing::warn!("session counter: write tmpfile failed: {}", e);
    } else if let Err(e) = std::fs::rename(&tmp_path, path) {
        tracing::warn!("session counter: rename failed: {}", e);
    }

    // lock_file dropped here releases the exclusive lock.
    n as u32
}

// ── Unit tests ─────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_counter_path() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("gp-counter-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("session-counters.json")
    }

    #[test]
    fn counter_increments_per_game_per_day() {
        let path = tmp_counter_path();

        let n1 = increment_counter_at("starfield", &path);
        assert_eq!(n1, 1, "first session should be 1");

        let n2 = increment_counter_at("starfield", &path);
        assert_eq!(n2, 2, "second session same game same day should be 2");

        let n3 = increment_counter_at("cyberpunk-2077", &path);
        assert_eq!(n3, 1, "different game starts at 1");

        let n4 = increment_counter_at("starfield", &path);
        assert_eq!(n4, 3, "starfield continues from 2");
    }

    #[test]
    fn counter_prunes_old_entries() {
        let path = tmp_counter_path();

        // Seed with an old entry and a _last_pruned that's not today.
        let old_date = (Utc::now() - chrono::Duration::days(40))
            .format("%Y-%m-%d")
            .to_string();
        let yesterday = (Utc::now() - chrono::Duration::days(1))
            .format("%Y-%m-%d")
            .to_string();
        let seed = serde_json::json!({
            format!("old-game:{}", old_date): 5,
            format!("recent-game:{}", yesterday): 2,
            "_last_pruned": yesterday,
        });
        std::fs::write(&path, serde_json::to_string(&seed).unwrap()).unwrap();

        increment_counter_at("new-game", &path);

        let contents = std::fs::read_to_string(&path).unwrap();
        let counters: serde_json::Map<String, Value> = serde_json::from_str(&contents).unwrap();

        // Old entry (>30 days) must be gone.
        assert!(
            !counters.contains_key(&format!("old-game:{}", old_date)),
            "40-day-old entry should be pruned"
        );
        // Recent entry within 30 days survives.
        assert!(
            counters.contains_key(&format!("recent-game:{}", yesterday)),
            "yesterday's entry should survive"
        );
    }

    #[test]
    fn test_lutris_slug_to_title() {
        assert_eq!(
            lutris_slug_to_title("cyberpunk-2077-1683316261"),
            "Cyberpunk 2077"
        );
        assert_eq!(
            lutris_slug_to_title("untitled-goose-game-1683316261"),
            "Untitled Goose Game"
        );
        assert_eq!(
            lutris_slug_to_title("the-elder-scrolls-v-skyrim-1683316261"),
            "The Elder Scrolls V Skyrim"
        );
        assert_eq!(lutris_slug_to_title("mygame"), "Mygame");
    }

    #[test]
    fn slug_from_name_examples() {
        assert_eq!(slug_from_game_name("Starfield"), "starfield");
        assert_eq!(slug_from_game_name("Cyberpunk 2077"), "cyberpunk-2077");
        assert_eq!(
            slug_from_game_name("The Elder Scrolls V: Skyrim"),
            "the-elder-scrolls-v-skyrim"
        );
    }
}

// ── Lutris detection ──────────────────────────────────────────────────────────

#[derive(serde::Deserialize, Default)]
struct LutrisGameConfig {
    #[serde(default)]
    game: LutrisGameSection,
    #[serde(default)]
    wine: serde_yaml::Value,
}

#[derive(serde::Deserialize, Default)]
struct LutrisGameSection {
    exe: Option<String>,
    prefix: Option<String>,
}

/// Strip trailing `-<timestamp>` suffix from a Lutris filename stem, then
/// convert the remaining slug to Title Case.
fn lutris_slug_to_title(stem: &str) -> String {
    let parts: Vec<&str> = stem.split('-').collect();
    let slug = if let Some(last) = parts.last() {
        if last.len() >= 10 && last.chars().all(|c| c.is_ascii_digit()) {
            &stem[..stem.len() - last.len() - 1]
        } else {
            stem
        }
    } else {
        stem
    };

    slug.split('-')
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                None => String::new(),
                Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Scan `~/.local/share/lutris/games/*.yml` and cross-reference running
/// processes to detect a Lutris-managed game. Returns the first match.
pub(crate) fn scan_for_lutris_game() -> Option<Target> {
    let home = std::env::var("HOME").ok()?;
    let games_dir = PathBuf::from(&home).join(".local/share/lutris/games");

    let dir_iter = match std::fs::read_dir(&games_dir) {
        Ok(it) => it,
        Err(_) => return None,
    };

    // Parse all *.yml configs up front.
    let mut configs: Vec<(String, LutrisGameConfig)> = Vec::new();
    for entry in dir_iter.filter_map(|e| e.ok()) {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("yml") {
            continue;
        }
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string();
        let display_name = lutris_slug_to_title(&stem);

        let content = match std::fs::read_to_string(&path) {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("Lutris: failed to read {:?}: {}", path, e);
                continue;
            }
        };
        let config: LutrisGameConfig = match serde_yaml::from_str(&content) {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("Lutris: failed to parse {:?}: {}", path, e);
                continue;
            }
        };
        configs.push((display_name, config));
    }

    if configs.is_empty() {
        return None;
    }

    // Build exe → PIDs map from /proc.
    let pids: Vec<u32> = std::fs::read_dir("/proc")
        .ok()?
        .filter_map(|e| e.ok())
        .filter_map(|e| e.file_name().to_str()?.parse::<u32>().ok())
        .collect();

    let mut exe_map: HashMap<PathBuf, Vec<u32>> = HashMap::new();
    for &pid in &pids {
        if let Ok(exe) = std::fs::read_link(format!("/proc/{}/exe", pid)) {
            exe_map.entry(exe).or_default().push(pid);
        }
    }

    // Match configs against running processes.
    for (display_name, config) in &configs {
        let is_wine = !config.wine.is_null();
        let launcher = Some(if is_wine {
            "Lutris \u{2014} Wine".to_string()
        } else {
            "Lutris \u{2014} Native".to_string()
        });

        let mut matched_pids: Vec<u32> = Vec::new();

        // Native exe match.
        if let Some(exe_str) = &config.game.exe {
            if let Ok(canonical) = std::fs::canonicalize(exe_str) {
                if let Some(p) = exe_map.get(&canonical) {
                    matched_pids.extend_from_slice(p);
                }
            }
        }

        // Wine prefix match (secondary).
        if matched_pids.is_empty() {
            if let Some(prefix) = &config.game.prefix {
                for &pid in &pids {
                    if let Some(env) = read_environ(pid) {
                        if env.get("WINEPREFIX").map(String::as_str) == Some(prefix.as_str()) {
                            matched_pids.push(pid);
                        }
                    }
                }
            }
        }

        if !matched_pids.is_empty() {
            let pid = matched_pids[0];
            return Some(Target {
                source: TargetSource::Lutris,
                display_name: display_name.clone(),
                pid,
                all_pids: matched_pids,
                steam_app_id: None,
                launcher,
                graphics_api: None,
                proton_version: None,
                dxvk_version: None,
            });
        }
    }

    None
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
    if map.is_empty() {
        None
    } else {
        Some(map)
    }
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

    let dll_overrides = env
        .get("WINEDLLOVERRIDES")
        .map(|s| s.to_lowercase())
        .unwrap_or_default();
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
    if !version.is_empty() {
        Some(version)
    } else {
        None
    }
}

// ── Target scanner (dispatcher + per-source helpers) ──────────────────────────

/// Try every detection source in order; return the first match.
/// B2.1 only knows Steam. Future WPs slot launcher-specific scanners into this
/// chain without restructuring callers.
pub fn scan_for_game() -> Option<Target> {
    scan_for_steam_game().or_else(scan_for_lutris_game)
    // B2.4 will add: .or_else(scan_for_heroic_game)
    // B2.5 will add: .or_else(scan_for_bottles_game)
    // B2.7 will add: .or_else(scan_for_user_specified_target)
}

/// Scan /proc for a running Steam game. Returns the best candidate or None.
/// Matches Python GameDetector._scan() exactly.
pub(crate) fn scan_for_steam_game() -> Option<Target> {
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
    let all_pids = all_pids_by_appid
        .get(&app_id)
        .cloned()
        .unwrap_or_else(|| vec![pid]);

    Some(Target::from_steam(
        name,
        app_id,
        pid,
        all_pids,
        graphics_api,
        proton_version,
        dxvk_version,
    ))
}
