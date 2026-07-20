/// Per-game settings profiles — Tier 3 settings capture (D.7).
///
/// A profile is a TOML file that describes known-good settings for a specific
/// game (preset, upscaler, frame-gen, active features, etc.). When the agent
/// detects a game starting, it searches the profile directories and, if a match
/// is found, merges the profile settings into the session settings overlay.
///
/// Merge precedence (highest first):
///   1. CLI flags (source = "manual", confidence = "high")
///   2. [session.settings] in rigsignal.toml  (same)
///   3. Game profile (source = "profile", confidence = "medium")
///
/// Profile TOML format:
///   [game]
///   name = "Cyberpunk 2077"
///   steam_app_id = 1091500        # optional but enables exact matching
///   aliases = ["cp2077", "cp"]    # optional additional name fragments
///
///   [settings]
///   preset = "ultra"
///   upscaler_tech = "dlss"
///   upscaler_preset = "quality"
///   frame_gen_tech = "dlss3"
///   features_active = ["ray_tracing", "path_tracing"]
///   notes = "some note"
///
/// Profile search order (first directory wins for same-named profiles):
///   1. $RIGSIGNAL_PROFILES_DIR env var
///   2. ~/.config/rigsignal/profiles/
///   3. /etc/rigsignal/profiles/
///   4. /usr/share/rigsignal/profiles/
///   5. {exe}/../../../profiles/  (dev build fallback — target/release → repo root)
use crate::session::Target;
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::PathBuf;

// ── Profile structs ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct GameProfile {
    pub game: GameMeta,
    #[serde(default)]
    pub settings: ProfileSettings,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GameMeta {
    /// Canonical display name for logging and dashboard labels.
    pub name: String,
    /// Steam AppID — enables exact matching and is more reliable than name matching.
    #[serde(default)]
    pub steam_app_id: Option<u32>,
    /// Additional lowercase fragments that, if contained in the running game's
    /// display name, count as a match (case-insensitive).
    #[serde(default)]
    pub aliases: Vec<String>,
}

/// Mirrors `SessionSettingsConfig` but is only ever partially populated.
/// Absent fields are not emitted to the session overlay.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct ProfileSettings {
    pub preset: Option<String>,
    pub upscaler_tech: Option<String>,
    pub upscaler_preset: Option<String>,
    pub frame_gen_tech: Option<String>,
    pub features_active: Option<Vec<String>>,
    pub render_resolution_output: Option<String>,
    pub render_vsync: Option<String>,
    pub notes: Option<String>,
}

// ── Public API ────────────────────────────────────────────────────────────────

/// Search all profile directories for a profile that matches `target`.
/// Returns the first match found; Steam AppID (exact) beats name (contains).
pub fn find_profile(target: &Target) -> Option<GameProfile> {
    let all = load_all();

    // Steam AppID: exact match — most reliable identifier.
    if let Some(app_id) = target.steam_app_id {
        if let Some(p) = all.iter().find(|p| p.game.steam_app_id == Some(app_id)) {
            return Some(p.clone());
        }
    }

    // Name/alias: case-insensitive substring match.
    let name_lower = target.display_name.to_lowercase();
    all.into_iter().find(|p| {
        name_lower.contains(&p.game.name.to_lowercase())
            || p.game
                .aliases
                .iter()
                .any(|a| name_lower.contains(&a.to_lowercase()))
    })
}

/// Build a `{ "rigsignal": { "settings": { … } } }` JSON overlay from a profile.
/// Sets `source = "profile"` and `confidence = "medium"`.
/// Returns `serde_json::Value::Null` if the profile has no settings fields.
pub fn to_overlay(profile: &GameProfile) -> Value {
    let s = &profile.settings;
    let mut settings = serde_json::Map::new();

    if let Some(p) = &s.preset {
        settings.insert("preset".into(), json!(p));
    }

    if let Some(tech) = &s.upscaler_tech {
        let mut upscaler = serde_json::Map::new();
        upscaler.insert("tech".into(), json!(tech));
        if let Some(preset) = &s.upscaler_preset {
            upscaler.insert("preset".into(), json!(preset));
        }
        settings.insert("upscaler".into(), Value::Object(upscaler));
    }

    if let Some(fg) = &s.frame_gen_tech {
        settings.insert("frame_gen".into(), json!({ "tech": fg }));
    }

    if let Some(features) = &s.features_active {
        if !features.is_empty() {
            settings.insert("features_active".into(), json!(features));
        }
    }

    let mut render = serde_json::Map::new();
    if let Some(res) = &s.render_resolution_output {
        render.insert("resolution_output".into(), json!(res));
    }
    if let Some(vs) = &s.render_vsync {
        render.insert("vsync".into(), json!(vs));
    }
    if !render.is_empty() {
        settings.insert("render".into(), Value::Object(render));
    }

    if let Some(n) = &s.notes {
        settings.insert("notes".into(), json!(n));
    }

    // Only emit source/confidence when there is at least one real field.
    if settings.is_empty() {
        return Value::Null;
    }

    settings.insert("source".into(), json!("profile"));
    settings.insert("confidence".into(), json!("medium"));

    json!({ "rigsignal": { "settings": Value::Object(settings) } })
}

// ── Directory discovery + loading ─────────────────────────────────────────────

/// Return all profile directories to search, in priority order.
///
/// Search order (first found wins for any given game):
///
/// 1. `$RIGSIGNAL_PROFILES_DIR` env var (any platform)
/// 2. Per-user dir — Linux `~/.config/rigsignal/profiles/`,
///    Windows `%APPDATA%\RigSignal\profiles\`
/// 3. System-local — Linux `/etc/rigsignal/profiles/`,
///    Windows `%PROGRAMDATA%\RigSignal\profiles\`
/// 4. System-package — Linux `/usr/share/rigsignal/profiles/`;
///    Windows uses the binary-relative fallback below instead
/// 5. Binary-relative — `<exe>/../profiles/` (MSI/zip install layout) and
///    `<exe>/../../profiles/` (dev: `target/release/...`)
pub fn profile_dirs() -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = Vec::new();

    if let Ok(p) = std::env::var("RIGSIGNAL_PROFILES_DIR") {
        if !p.is_empty() {
            dirs.push(PathBuf::from(p));
        }
    }

    #[cfg(unix)]
    {
        let home = std::env::var("SUDO_USER")
            .ok()
            .filter(|u| !u.is_empty())
            .map(|u| PathBuf::from("/home").join(u))
            .or_else(|| std::env::var("HOME").ok().map(PathBuf::from));
        if let Some(h) = home {
            dirs.push(h.join(".config/rigsignal/profiles"));
        }
        dirs.push(PathBuf::from("/etc/rigsignal/profiles"));
        dirs.push(PathBuf::from("/usr/share/rigsignal/profiles"));
    }

    #[cfg(windows)]
    {
        if let Ok(appdata) = std::env::var("APPDATA") {
            dirs.push(PathBuf::from(appdata).join("RigSignal").join("profiles"));
        }
        if let Ok(programdata) = std::env::var("PROGRAMDATA") {
            dirs.push(
                PathBuf::from(programdata)
                    .join("RigSignal")
                    .join("profiles"),
            );
        }
    }

    // Binary-relative fallbacks (cross-platform). Try one-up
    // (MSI/zip install: `bin/rigsignal-agent.exe` → `../profiles/`) and
    // two-up (dev: `target/release/rigsignal-agent` → `../../profiles/`).
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            let install = exe_dir.join("../profiles");
            if install.exists() {
                dirs.push(install);
            }
            let dev = exe_dir.join("../../profiles");
            if dev.exists() {
                dirs.push(dev);
            }
        }
    }

    dirs
}

fn load_all() -> Vec<GameProfile> {
    let mut profiles: Vec<GameProfile> = Vec::new();
    for dir in profile_dirs() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("toml") {
                continue;
            }
            match std::fs::read_to_string(&path)
                .map_err(|e| e.to_string())
                .and_then(|s| toml::from_str::<GameProfile>(&s).map_err(|e| e.to_string()))
            {
                Ok(p) => profiles.push(p),
                Err(e) => tracing::warn!("skipping invalid profile {:?}: {}", path, e),
            }
        }
    }
    profiles
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_profile(name: &str, app_id: Option<u32>, aliases: &[&str]) -> GameProfile {
        GameProfile {
            game: GameMeta {
                name: name.to_string(),
                steam_app_id: app_id,
                aliases: aliases.iter().map(|s| s.to_string()).collect(),
            },
            settings: ProfileSettings::default(),
        }
    }

    fn make_target(name: &str, app_id: Option<u32>) -> Target {
        Target {
            source: crate::session::TargetSource::Steam,
            display_name: name.to_string(),
            pid: 1,
            all_pids: vec![1],
            steam_app_id: app_id,
            launcher: None,
            graphics_api: None,
            proton_version: None,
            dxvk_version: None,
        }
    }

    #[test]
    fn test_profile_match_by_steam_id() {
        let profiles = [
            make_profile("Cyberpunk 2077", Some(1091500), &["cp2077"]),
            make_profile("Starfield", Some(1716740), &[]),
        ];

        let target = make_target("some-renamed-game", Some(1091500));
        let name_lower = target.display_name.to_lowercase();

        // AppID match wins over name/alias mismatch
        let found = profiles
            .iter()
            .find(|p| p.game.steam_app_id == target.steam_app_id);
        assert_eq!(found.unwrap().game.name, "Cyberpunk 2077");

        // No AppID match falls through to name
        let target2 = make_target("Starfield Enhanced Edition", None);
        let name2 = target2.display_name.to_lowercase();
        let found2 = profiles.iter().find(|p| {
            name2.contains(&p.game.name.to_lowercase())
                || p.game
                    .aliases
                    .iter()
                    .any(|a| name2.contains(&a.to_lowercase()))
        });
        assert_eq!(found2.unwrap().game.name, "Starfield");

        // Alias match
        let target3 = make_target("cp2077 benchmark tool", None);
        let name3 = target3.display_name.to_lowercase();
        let found3 = profiles.iter().find(|p| {
            name3.contains(&p.game.name.to_lowercase())
                || p.game
                    .aliases
                    .iter()
                    .any(|a| name3.contains(&a.to_lowercase()))
        });
        assert_eq!(found3.unwrap().game.name, "Cyberpunk 2077");

        // No match
        let target4 = make_target("unknown game", None);
        let name4 = target4.display_name.to_lowercase();
        let found4 = profiles.iter().find(|p| {
            name4.contains(&p.game.name.to_lowercase())
                || p.game
                    .aliases
                    .iter()
                    .any(|a| name4.contains(&a.to_lowercase()))
        });
        assert!(found4.is_none());

        let _ = name_lower; // suppress unused warning
    }

    #[test]
    fn test_profile_overlay_has_source_and_confidence() {
        let profile = GameProfile {
            game: GameMeta {
                name: "Test Game".into(),
                steam_app_id: None,
                aliases: vec![],
            },
            settings: ProfileSettings {
                preset: Some("ultra".into()),
                frame_gen_tech: Some("dlss3".into()),
                notes: Some("test note".into()),
                ..Default::default()
            },
        };
        let ov = to_overlay(&profile);
        let settings = &ov["rigsignal"]["settings"];
        assert_eq!(settings["source"], "profile");
        assert_eq!(settings["confidence"], "medium");
        assert_eq!(settings["preset"], "ultra");
        assert_eq!(settings["frame_gen"]["tech"], "dlss3");
        assert!(settings["frame_gen"].is_object());
    }

    #[test]
    fn test_profile_dirs_env_override_listed_first() {
        // Use a deliberately unique sentinel so we don't disturb real env state.
        let sentinel = std::env::temp_dir().join("rigsignal-profile-test-XYZ");
        // SAFETY: tests run in the same process — set_var is safe in single-thread cargo
        // test, and this test does not rely on concurrency.
        unsafe {
            std::env::set_var("RIGSIGNAL_PROFILES_DIR", &sentinel);
        }
        let dirs = profile_dirs();
        unsafe {
            std::env::remove_var("RIGSIGNAL_PROFILES_DIR");
        }
        assert_eq!(dirs.first(), Some(&sentinel));
    }

    #[test]
    #[cfg(windows)]
    fn test_profile_dirs_includes_windows_paths() {
        // With APPDATA set (always set on Windows runners), profile_dirs should
        // include a path under APPDATA\RigSignal\profiles.
        let dirs = profile_dirs();
        let appdata = std::env::var("APPDATA").expect("APPDATA must be set on Windows");
        let expected = std::path::PathBuf::from(&appdata)
            .join("RigSignal")
            .join("profiles");
        assert!(
            dirs.iter().any(|d| d == &expected),
            "expected {expected:?} in {dirs:?}"
        );
    }

    #[test]
    fn test_profile_overlay_null_when_empty() {
        let profile = GameProfile {
            game: GameMeta {
                name: "Empty".into(),
                steam_app_id: None,
                aliases: vec![],
            },
            settings: ProfileSettings::default(),
        };
        assert!(to_overlay(&profile).is_null());
    }

    #[test]
    fn test_starter_profiles_parse() {
        let starfield = include_str!("../profiles/starfield.toml");
        let cp = include_str!("../profiles/cyberpunk-2077.toml");
        let bg3 = include_str!("../profiles/baldurs-gate-3.toml");

        let p1: GameProfile = toml::from_str(starfield).expect("starfield.toml parses");
        let p2: GameProfile = toml::from_str(cp).expect("cyberpunk-2077.toml parses");
        let p3: GameProfile = toml::from_str(bg3).expect("baldurs-gate-3.toml parses");

        assert_eq!(p1.game.steam_app_id, Some(1716740));
        assert_eq!(p2.game.steam_app_id, Some(1091500));
        assert_eq!(p3.game.steam_app_id, Some(1086940));
    }
}
