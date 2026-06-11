// Windows launcher scanners. Everything except choose_primary_pid (pure
// selection logic, unit-tested on all platforms) is #[cfg(windows)]-gated.

#[cfg(windows)]
use crate::session::{parse_acf_field, parse_vdf_paths, EpicManifest, Target, TargetSource};
#[cfg(windows)]
use std::path::{Path, PathBuf};
#[cfg(windows)]
use sysinfo::{ProcessRefreshKind, RefreshKind, System, UpdateKind};
#[cfg(windows)]
use winreg::enums::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
#[cfg(windows)]
use winreg::RegKey;

#[cfg(windows)]
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
        let candidates: Vec<(u32, u64)> = processes
            .iter()
            .filter(|(_, exe, _)| path_starts_with_ci(exe, &game_dir))
            .map(|(pid, _, memory)| (*pid, *memory))
            .collect();

        let Some(pid) = choose_primary_pid(&candidates) else {
            continue;
        };
        let mut all_pids: Vec<u32> = candidates.iter().map(|(pid, _)| *pid).collect();
        all_pids.sort_unstable();
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

#[cfg(windows)]
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

        if let Some((pid, _, _)) = processes
            .iter()
            .find(|(_, exe, _)| paths_equal_ci(exe, &wanted))
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

#[cfg(windows)]
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

        if let Some((pid, _, _)) = processes
            .iter()
            .find(|(_, path, _)| paths_equal_ci(path, &exe))
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

/// Pick the main game process from `(pid, memory_bytes)` candidates.
///
/// Helper processes (crash reporters, launchers) share the game install dir
/// but use far less memory than the actual game — e.g. observed live:
/// REDEngineErrorReporter.exe at ~50 MB vs Cyberpunk2077.exe at multiple GB.
/// Picks the candidate with the largest working set; ties break to the
/// lowest pid for determinism.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) fn choose_primary_pid(candidates: &[(u32, u64)]) -> Option<u32> {
    candidates
        .iter()
        .copied()
        .max_by(|(pid_a, mem_a), (pid_b, mem_b)| {
            // Larger memory wins; on equal memory, the LOWER pid must compare
            // as greater so max_by selects it.
            mem_a.cmp(mem_b).then_with(|| pid_b.cmp(pid_a))
        })
        .map(|(pid, _)| pid)
}

/// All processes with a known exe path, as (pid, exe, memory_bytes).
#[cfg(windows)]
fn processes_with_exe() -> Vec<(u32, PathBuf, u64)> {
    let system = System::new_with_specifics(
        RefreshKind::nothing().with_processes(
            ProcessRefreshKind::nothing()
                .with_exe(UpdateKind::Always)
                .with_memory(),
        ),
    );
    system
        .processes()
        .iter()
        .filter_map(|(pid, process)| {
            Some((pid.as_u32(), process.exe()?.to_path_buf(), process.memory()))
        })
        .collect()
}

#[cfg(windows)]
fn paths_equal_ci(left: &Path, right: &Path) -> bool {
    left.to_string_lossy()
        .eq_ignore_ascii_case(&right.to_string_lossy())
}

#[cfg(windows)]
fn path_starts_with_ci(path: &Path, prefix: &Path) -> bool {
    let path = path.to_string_lossy().to_ascii_lowercase();
    let mut prefix = prefix.to_string_lossy().to_ascii_lowercase();
    if !prefix.ends_with('\\') && !prefix.ends_with('/') {
        prefix.push('\\');
    }
    path.starts_with(&prefix)
}

#[cfg(test)]
mod tests {
    use super::choose_primary_pid;

    #[test]
    fn choose_primary_pid_empty_returns_none() {
        assert_eq!(choose_primary_pid(&[]), None);
    }

    #[test]
    fn choose_primary_pid_picks_largest_memory_not_lowest_pid() {
        // Observed live: REDEngineErrorReporter.exe (pid 8032, ~50 MB) had a
        // lower pid than Cyberpunk2077.exe (pid 16720, ~8 GB) — the game must
        // win regardless of pid ordering.
        let candidates = [(8032_u32, 50_000_000_u64), (16720, 8_000_000_000)];
        assert_eq!(choose_primary_pid(&candidates), Some(16720));
    }

    #[test]
    fn choose_primary_pid_equal_memory_ties_break_to_lowest_pid() {
        let candidates = [(300_u32, 1024_u64), (100, 1024), (200, 1024)];
        assert_eq!(choose_primary_pid(&candidates), Some(100));
    }
}
