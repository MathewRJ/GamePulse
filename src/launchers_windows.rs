#![cfg(windows)]

use crate::session::{parse_acf_field, parse_vdf_paths, EpicManifest, Target, TargetSource};
use std::path::{Path, PathBuf};
use sysinfo::{ProcessRefreshKind, RefreshKind, System, UpdateKind};
use winreg::enums::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
use winreg::RegKey;

pub fn scan_for_steam_game_windows() -> Option<Target> {
    let steam = RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(r"Software\Valve\Steam")
        .ok()?;
    let app_id: u32 = steam.get_value("RunningAppID").ok()?;
    if app_id == 0 {
        return None;
    }

    let steam_path: String = steam.get_value("SteamPath").ok()?;
    let libraryfolders = PathBuf::from(steam_path).join(r"steamapps\libraryfolders.vdf");
    let content = std::fs::read_to_string(libraryfolders).ok()?;
    let processes = processes_with_exe();
    let manifest_name = format!("appmanifest_{}.acf", app_id);

    for library in parse_vdf_paths(&content) {
        let steamapps = library.join("steamapps");
        let manifest = steamapps.join(&manifest_name);
        let content = match std::fs::read_to_string(manifest) {
            Ok(content) => content,
            Err(_) => continue,
        };

        let name = parse_acf_field(&content, "name")?.to_string();
        let installdir = parse_acf_field(&content, "installdir")?;
        let game_dir = steamapps.join("common").join(installdir);
        let mut all_pids: Vec<u32> = processes
            .iter()
            .filter(|(_, exe)| path_starts_with_ci(exe, &game_dir))
            .map(|(pid, _)| *pid)
            .collect();

        if all_pids.is_empty() {
            continue;
        }

        all_pids.sort_unstable();
        let pid = all_pids[0];
        return Some(Target::from_steam(
            name,
            app_id,
            pid,
            all_pids,
            crate::dllscan::graphics_api_from_maps(pid),
            None,
            None,
        ));
    }

    None
}

pub fn scan_for_epic_game_windows() -> Option<Target> {
    let manifests_dir = PathBuf::from(std::env::var("ProgramData").ok()?)
        .join(r"Epic\EpicGamesLauncher\Data\Manifests");
    let processes = processes_with_exe();

    for entry in std::fs::read_dir(manifests_dir).ok()? {
        let path = match entry {
            Ok(e) => e.path(),
            Err(_) => continue,
        };
        if path.extension().and_then(|e| e.to_str()) != Some("item") {
            continue;
        }

        // Skip unreadable or malformed manifests — one stale .item file must
        // not disable Epic detection for everything else.
        let content = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(_) => continue,
        };
        let manifest: EpicManifest = match serde_json::from_str(&content) {
            Ok(m) => m,
            Err(_) => continue,
        };
        let launch_executable = match manifest.launch_executable.as_deref() {
            Some(exe) if !exe.trim().is_empty() => exe,
            _ => continue,
        };
        let wanted = PathBuf::from(&manifest.install_location).join(launch_executable);

        if let Some((pid, _)) = processes
            .iter()
            .find(|(_, exe)| paths_equal_ci(exe, &wanted))
        {
            return Some(Target {
                source: TargetSource::EpicGames,
                display_name: manifest.display_name,
                pid: *pid,
                all_pids: vec![*pid],
                steam_app_id: None,
                launcher: Some("Epic Games".to_string()),
                graphics_api: crate::dllscan::graphics_api_from_maps(*pid),
                proton_version: None,
                dxvk_version: None,
            });
        }
    }

    None
}

pub fn scan_for_gog_game_windows() -> Option<Target> {
    let games = RegKey::predef(HKEY_LOCAL_MACHINE)
        .open_subkey(r"SOFTWARE\WOW6432Node\GOG.com\Games")
        .ok()?;
    let processes = processes_with_exe();

    for key_name in games.enum_keys().flatten() {
        let game = match games.open_subkey(key_name) {
            Ok(game) => game,
            Err(_) => continue,
        };
        let display_name: String = match game.get_value("gameName") {
            Ok(name) => name,
            Err(_) => continue,
        };
        let exe = game
            .get_value::<String, _>("exe")
            .ok()
            .map(PathBuf::from)
            .or_else(|| {
                let path: String = game.get_value("path").ok()?;
                let exe_file: String = game.get_value("exeFile").ok()?;
                Some(PathBuf::from(path).join(exe_file))
            })?;

        if let Some((pid, _)) = processes
            .iter()
            .find(|(_, path)| paths_equal_ci(path, &exe))
        {
            return Some(Target {
                source: TargetSource::GogGalaxy,
                display_name,
                pid: *pid,
                all_pids: vec![*pid],
                steam_app_id: None,
                launcher: Some("GOG Galaxy".to_string()),
                graphics_api: crate::dllscan::graphics_api_from_maps(*pid),
                proton_version: None,
                dxvk_version: None,
            });
        }
    }

    None
}

fn processes_with_exe() -> Vec<(u32, PathBuf)> {
    let system = System::new_with_specifics(
        RefreshKind::nothing()
            .with_processes(ProcessRefreshKind::nothing().with_exe(UpdateKind::Always)),
    );
    system
        .processes()
        .iter()
        .filter_map(|(pid, process)| Some((pid.as_u32(), process.exe()?.to_path_buf())))
        .collect()
}

fn paths_equal_ci(left: &Path, right: &Path) -> bool {
    left.to_string_lossy()
        .eq_ignore_ascii_case(&right.to_string_lossy())
}

fn path_starts_with_ci(path: &Path, prefix: &Path) -> bool {
    let path = path.to_string_lossy().to_ascii_lowercase();
    let mut prefix = prefix.to_string_lossy().to_ascii_lowercase();
    if !prefix.ends_with('\\') && !prefix.ends_with('/') {
        prefix.push('\\');
    }
    path.starts_with(&prefix)
}
