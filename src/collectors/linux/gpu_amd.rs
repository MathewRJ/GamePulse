/// AMD GPU collector — mirrors collector/rigsignal/collectors/gpu/amd_linux.py exactly.
///
/// Discovers the best AMD DRM card using the same scoring heuristic as the
/// Python collector. Candidates are scanned from
/// /sys/class/drm/card[0-9] in sorted order; the highest-scoring card wins
/// (first card wins on a tie, matching Python's `>` not `>=` update rule):
///
///   +10  fan1_input exists  — discrete GPU has a fan
///   +5   power1_average exists
///   +2   temp2_input exists  — hotspot sensor present on discrete GPU
///   +1   any hwmon directory exists at all
///
/// Hwmon path is discovered via the card's own device path
/// ({card}/device/hwmon/hwmon*) rather than /sys/class/hwmon, which avoids
/// cross-vendor ambiguity when multiple amdgpu cards are present (e.g. iGPU +
/// discrete). Matches Python _find_hwmon() exactly.
///
/// Output fields (rigsignal.gpu.*):
///   utilisation_pct      f64  — gpu_busy_percent cast to float
///   clock_mhz            i64  — active clock from pp_dpm_sclk (* marker)
///   memory_used_mb       i64  — VRAM used in MiB (bytes // 1_048_576)
///   memory_total_mb      i64  — VRAM total in MiB
///   temperature_c        f64  — edge temp, 1 dp (temp1_input / 1000)
///   hotspot_c            f64  — junction/hotspot, 1 dp (temp2_input / 1000)
///   memory_temperature_c f64  — VRAM temp, 1 dp (temp3_input / 1000)
///   power_w              f64  — GPU power in W, 1 dp (power1_average µW / 1e6)
///   fan_speed_rpm        i64  — fan RPM (fan1_input)
///   fan_pct              f64  — fan %, 1 dp (rpm / fan1_max * 100)
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

// ── sysfs helpers ─────────────────────────────────────────────────────────────

fn read_int(path: &Path) -> Option<i64> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|s| s.trim().parse().ok())
}

fn read_str_file(path: &Path) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

// ── Card / hwmon discovery ─────────────────────────────────────────────────────

/// Return the first (sorted) hwmon directory under {device}/hwmon/.
/// Matches Python: sorted(glob.glob(f"{device_path}/hwmon/hwmon*"))[0]
fn find_hwmon(device: &Path) -> Option<PathBuf> {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(device.join("hwmon"))
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .collect();
    entries.sort();
    entries.into_iter().next()
}

/// Enumerate /sys/class/drm/card[0-9] in sorted order, filter to AMD vendor
/// (0x1002), score each, return the device and hwmon paths of the winner.
///
/// Ties are broken by first-in-sorted-order (matching Python's `if score >
/// best_score` strict-greater update, which keeps the first card on a tie).
fn find_amd_card(drm_path: &Path) -> (Option<PathBuf>, Option<PathBuf>) {
    let mut cards: Vec<PathBuf> = std::fs::read_dir(drm_path)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| {
                    // Match card[0-9] exactly — one digit, no connector suffixes.
                    n.len() == 5
                        && n.starts_with("card")
                        && n[4..].chars().all(|c| c.is_ascii_digit())
                })
                .unwrap_or(false)
        })
        .collect();
    cards.sort();

    let mut best_device: Option<PathBuf> = None;
    let mut best_hwmon: Option<PathBuf> = None;
    let mut best_score: i32 = -1;

    for card in &cards {
        let vendor = match read_str_file(&card.join("device/vendor")) {
            Some(v) => v,
            None => continue,
        };
        if vendor != "0x1002" {
            continue;
        }
        let device = card.join("device");
        let hwmon = find_hwmon(&device);

        let mut score: i32 = 0;
        if let Some(ref hw) = hwmon {
            score += 1;
            if hw.join("fan1_input").exists() {
                score += 10;
            }
            if hw.join("power1_average").exists() {
                score += 5;
            }
            if hw.join("temp2_input").exists() {
                score += 2;
            }
        }

        // Strict greater-than: first card wins on a tie (Python parity).
        if score > best_score {
            best_score = score;
            best_device = Some(device);
            best_hwmon = hwmon;
        }
    }

    (best_device, best_hwmon)
}

// ── Clock parsing ──────────────────────────────────────────────────────────────

/// Parse the active clock MHz from pp_dpm_sclk.
///
/// File format (each line is a P-state):
///   0: 500Mhz
///   1: 58Mhz *   ← active clock is marked with *
///   2: 2520Mhz
///
/// Returns the integer MHz of the line containing *.
fn parse_current_clock_mhz(device: &Path) -> Option<i64> {
    let text = std::fs::read_to_string(device.join("pp_dpm_sclk")).ok()?;
    for line in text.lines() {
        if !line.contains('*') {
            continue;
        }
        for part in line.split_whitespace() {
            let lower = part.to_lowercase();
            if lower.ends_with("mhz") {
                let s = &part[..part.len() - 3];
                if let Ok(v) = s.parse::<i64>() {
                    return Some(v);
                }
            }
        }
    }
    None
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct GpuAmdCollector {
    pub game_pid: Option<u32>,
    card_path: Option<PathBuf>, // {card}/device — discovered at init or collect
    hwmon_path: Option<PathBuf>, // {card}/device/hwmon/hwmonN — discovered at init or collect
    drm_path: PathBuf,
    undiscovered_ticks: u64,
}

impl GpuAmdCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        Self::with_drm_path(game_pid, PathBuf::from("/sys/class/drm"))
    }

    fn with_drm_path(game_pid: Option<u32>, drm_path: PathBuf) -> Self {
        let (card_path, hwmon_path) = find_amd_card(&drm_path);
        // Log discovered paths at startup so operators can confirm the heuristic.
        match &card_path {
            Some(p) => tracing::info!("GPU card path: {}", p.display()),
            None => tracing::warn!("GPU: no AMD card found in /sys/class/drm"),
        }
        if let Some(ref h) = hwmon_path {
            tracing::info!("GPU hwmon path: {}", h.display());
        }
        Self {
            game_pid,
            card_path,
            hwmon_path,
            drm_path,
            undiscovered_ticks: 0,
        }
    }

    pub fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }
}

impl Collector for GpuAmdCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.gpu"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        if self.card_path.is_none() {
            let (card_path, hwmon_path) = find_amd_card(&self.drm_path);
            if let Some(card_path) = card_path {
                tracing::info!(
                    "GPU found after {} undiscovered collection ticks: {}",
                    self.undiscovered_ticks,
                    card_path.display()
                );
                self.card_path = Some(card_path);
                self.hwmon_path = hwmon_path;
            } else {
                self.undiscovered_ticks += 1;
                return Ok(None);
            }
        }

        let device = self
            .card_path
            .as_ref()
            .expect("AMD card path must be set after discovery")
            .clone();

        if self.hwmon_path.is_none() {
            if let Some(hwmon_path) = find_hwmon(&device) {
                tracing::info!(
                    "GPU hwmon found after card discovery: {}",
                    hwmon_path.display()
                );
                self.hwmon_path = Some(hwmon_path);
            }
        }

        let mut gpu = serde_json::Map::new();

        // Utilisation
        if let Some(util) = read_int(&device.join("gpu_busy_percent")) {
            gpu.insert("utilisation_pct".to_string(), Value::from(util as f64));
        }

        // Clock — active P-state from pp_dpm_sclk
        if let Some(clk) = parse_current_clock_mhz(&device) {
            gpu.insert("clock_mhz".to_string(), Value::from(clk));
        }

        // VRAM (bytes → MiB via integer division, matching Python //)
        if let Some(used) = read_int(&device.join("mem_info_vram_used")) {
            gpu.insert("memory_used_mb".to_string(), Value::from(used / 1_048_576));
        }
        if let Some(total) = read_int(&device.join("mem_info_vram_total")) {
            gpu.insert(
                "memory_total_mb".to_string(),
                Value::from(total / 1_048_576),
            );
        }

        if let Some(hw) = &self.hwmon_path {
            // Temperatures: AMD maps temp1=edge, temp2=junction/hotspot, temp3=mem
            // millidegrees → °C, 1 dp (matches Python round(x/1000.0, 1))
            if let Some(v) = read_int(&hw.join("temp1_input")) {
                gpu.insert(
                    "temperature_c".to_string(),
                    Value::from((v as f64 / 1000.0 * 10.0).round() / 10.0),
                );
                gpu.insert("temp_source".to_string(), Value::from("hwmon"));
            }
            if let Some(v) = read_int(&hw.join("temp2_input")) {
                gpu.insert(
                    "hotspot_c".to_string(),
                    Value::from((v as f64 / 1000.0 * 10.0).round() / 10.0),
                );
            }
            if let Some(v) = read_int(&hw.join("temp3_input")) {
                gpu.insert(
                    "memory_temperature_c".to_string(),
                    Value::from((v as f64 / 1000.0 * 10.0).round() / 10.0),
                );
            }

            // Power: µW → W, 1 dp
            if let Some(v) = read_int(&hw.join("power1_average")) {
                gpu.insert(
                    "power_w".to_string(),
                    Value::from((v as f64 / 1_000_000.0 * 10.0).round() / 10.0),
                );
            }

            // Fan: RPM always emitted when present; fan_pct only when fan1_max > 0
            let fan_rpm = read_int(&hw.join("fan1_input"));
            if let Some(rpm) = fan_rpm {
                gpu.insert("fan_speed_rpm".to_string(), Value::from(rpm));
                if let Some(max_rpm) = read_int(&hw.join("fan1_max")) {
                    if max_rpm > 0 {
                        let pct = (rpm as f64 / max_rpm as f64 * 100.0 * 10.0).round() / 10.0;
                        gpu.insert("fan_pct".to_string(), Value::from(pct));
                    }
                }
            }
        }

        if gpu.is_empty() {
            return Ok(None);
        }

        Ok(Some(json!({ "rigsignal": { "gpu": gpu } })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT_TEMP_DIR: AtomicUsize = AtomicUsize::new(0);

    struct TestDirectory {
        path: PathBuf,
    }

    impl TestDirectory {
        fn new() -> Self {
            let id = NEXT_TEMP_DIR.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "rigsignal-gpu-amd-test-{}-{id}",
                std::process::id()
            ));
            std::fs::create_dir_all(&path).expect("test DRM directory should be created");
            Self { path }
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }

    fn add_amd_card(drm_path: &Path, utilisation_pct: i64) {
        let device = drm_path.join("card0/device");
        std::fs::create_dir_all(&device).expect("test AMD device directory should be created");
        std::fs::write(device.join("vendor"), "0x1002\n")
            .expect("test AMD vendor should be written");
        std::fs::write(
            device.join("gpu_busy_percent"),
            format!("{utilisation_pct}\n"),
        )
        .expect("test GPU utilisation should be written");
    }

    fn add_hwmon(device: &Path) {
        let hwmon = device.join("hwmon/hwmon0");
        std::fs::create_dir_all(&hwmon).expect("test hwmon directory should be created");
        std::fs::write(hwmon.join("temp1_input"), "63500\n")
            .expect("test GPU temperature should be written");
        std::fs::write(hwmon.join("power1_average"), "123456789\n")
            .expect("test GPU power should be written");
    }

    #[test]
    fn late_discovery_emits_documents_from_the_discovery_tick_onward() {
        let drm = TestDirectory::new();
        let mut collector = GpuAmdCollector::with_drm_path(None, drm.path.clone());

        assert_eq!(collector.collect().unwrap(), None);

        add_amd_card(&drm.path, 73);

        let doc = collector
            .collect()
            .expect("late-discovery collection should succeed")
            .expect("late-discovered AMD card should emit a document");
        assert_eq!(doc["rigsignal"]["gpu"]["utilisation_pct"], 73.0);

        let next_doc = collector
            .collect()
            .expect("subsequent collection should succeed")
            .expect("late-discovered AMD card should keep emitting documents");
        assert_eq!(next_doc["rigsignal"]["gpu"]["utilisation_pct"], 73.0);
    }

    #[test]
    fn persistent_absence_emits_nothing_without_panicking() {
        let drm = TestDirectory::new();
        let mut collector = GpuAmdCollector::with_drm_path(None, drm.path.clone());

        assert_eq!(collector.collect().unwrap(), None);
        assert_eq!(collector.collect().unwrap(), None);
    }

    #[test]
    fn card_present_at_init_emits_the_existing_metrics() {
        let drm = TestDirectory::new();
        add_amd_card(&drm.path, 42);
        let mut collector = GpuAmdCollector::with_drm_path(None, drm.path.clone());

        std::fs::remove_file(drm.path.join("card0/device/vendor"))
            .expect("AMD vendor discovery marker should be removed");

        let doc = collector
            .collect()
            .expect("collection should succeed")
            .expect("AMD card present at init should emit a document");
        assert_eq!(doc["rigsignal"]["gpu"]["utilisation_pct"], 42.0);
    }

    #[test]
    fn late_hwmon_discovery_emits_hwmon_metrics_from_the_discovery_tick_onward() {
        let drm = TestDirectory::new();
        add_amd_card(&drm.path, 42);
        let device = drm.path.join("card0/device");
        let mut collector = GpuAmdCollector::with_drm_path(None, drm.path.clone());

        let initial_doc = collector
            .collect()
            .expect("collection without hwmon should succeed")
            .expect("device-level AMD metrics should emit without hwmon");
        assert_eq!(initial_doc["rigsignal"]["gpu"]["utilisation_pct"], 42.0);
        assert!(initial_doc["rigsignal"]["gpu"]
            .get("temperature_c")
            .is_none());

        add_hwmon(&device);

        let late_hwmon_doc = collector
            .collect()
            .expect("collection after hwmon discovery should succeed")
            .expect("AMD metrics should emit after hwmon discovery");
        assert_eq!(late_hwmon_doc["rigsignal"]["gpu"]["temperature_c"], 63.5);
        assert_eq!(late_hwmon_doc["rigsignal"]["gpu"]["power_w"], 123.5);
    }
}
