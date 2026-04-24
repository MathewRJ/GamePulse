/// Power collector — mirrors collector/gamepulse/collectors/power.py exactly.
///
/// Reads battery state, AC status, AMD TDP cap, and platform power profile from
/// sysfs. All fields are optional — only present when the hardware source exists.
/// Returns None if no power sources are available at all (e.g. VM with no battery
/// and no amdgpu hwmon — this should not happen on target hardware).
///
/// Output fields (gamepulse.power.*):
///   battery_pct      f64  — battery charge percentage (desktop: absent)
///   battery_rate_w   f64  — battery discharge rate in W, 2 dp (desktop: absent)
///   ac_connected     bool — AC adapter online state (desktop: absent)
///   tdp_current_w    f64  — AMD GPU power cap in W, 1 dp (e.g. 330.0 on RX 9070 XT)
///   profile          str  — ACPI platform_profile string (e.g. "balanced")
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};

// ── sysfs helpers ─────────────────────────────────────────────────────────────

fn read_int(path: &str) -> Option<i64> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|s| s.trim().parse().ok())
}

fn read_str_file(path: &str) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

// ── Power supply discovery ─────────────────────────────────────────────────────

fn find_battery() -> Option<String> {
    let dir = std::fs::read_dir("/sys/class/power_supply").ok()?;
    dir.filter_map(|e| e.ok())
        .map(|e| e.path())
        .find(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("BAT"))
                .unwrap_or(false)
        })
        .and_then(|p| p.to_str().map(|s| s.to_string()))
}

fn find_ac() -> Option<String> {
    let dir = std::fs::read_dir("/sys/class/power_supply").ok()?;
    let mut entries: Vec<_> = dir
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("AC") || n.starts_with("ADP"))
                .unwrap_or(false)
        })
        .collect();
    // Stable ordering: AC* before ADP*, matching Python glob concat order.
    entries.sort_by_key(|p| {
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if name.starts_with("AC") { 0u8 } else { 1 }
    });
    entries.into_iter().next().and_then(|p| p.to_str().map(|s| s.to_string()))
}

// ── Battery metrics ───────────────────────────────────────────────────────────

fn battery_pct(bat: &str) -> Option<f64> {
    read_int(&format!("{bat}/capacity")).map(|v| v as f64)
}

fn battery_rate_w(bat: &str) -> Option<f64> {
    // Power now in µW
    if let Some(uw) = read_int(&format!("{bat}/power_now")) {
        return Some(((uw as f64 / 1_000_000.0) * 100.0).round() / 100.0);
    }
    // Current µA × voltage µV → W
    let ua = read_int(&format!("{bat}/current_now"))?;
    let uv = read_int(&format!("{bat}/voltage_now"))?;
    Some(((ua as f64 * uv as f64 / 1e12) * 100.0).round() / 100.0)
}

fn ac_connected(ac: &str) -> Option<bool> {
    read_int(&format!("{ac}/online")).map(|v| v != 0)
}

// ── AMD TDP cap ───────────────────────────────────────────────────────────────

fn amd_tdp_w() -> Option<f64> {
    let dir = std::fs::read_dir("/sys/class/hwmon").ok()?;
    let mut hwmons: Vec<_> = dir
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .collect();
    hwmons.sort();

    for hwmon in hwmons {
        let name_path = hwmon.join("name");
        let name = read_str_file(name_path.to_str()?)?;
        if name != "amdgpu" {
            continue;
        }
        let cap_path = hwmon.join("power1_cap");
        if let Some(cap) = read_int(cap_path.to_str()?) {
            return Some(((cap as f64 / 1_000_000.0) * 10.0).round() / 10.0);
        }
    }
    None
}

// ── Platform profile ──────────────────────────────────────────────────────────

fn power_profile() -> Option<String> {
    read_str_file("/sys/firmware/acpi/platform_profile")
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct PowerCollector {
    battery_path: Option<String>,
    ac_path: Option<String>,
}

impl PowerCollector {
    pub fn new(_game_pid: Option<u32>) -> Self {
        PowerCollector {
            battery_path: find_battery(),
            ac_path: find_ac(),
        }
    }
}

impl Collector for PowerCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.power"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let mut power = serde_json::Map::new();

        if let Some(bat) = &self.battery_path {
            if let Some(pct) = battery_pct(bat) {
                power.insert("battery_pct".to_string(), Value::from(pct));
            }
            if let Some(rate) = battery_rate_w(bat) {
                power.insert("battery_rate_w".to_string(), Value::from(rate));
            }
        }

        if let Some(ac) = &self.ac_path {
            if let Some(online) = ac_connected(ac) {
                power.insert("ac_connected".to_string(), Value::from(online));
            }
        }

        if let Some(tdp) = amd_tdp_w() {
            power.insert("tdp_current_w".to_string(), Value::from(tdp));
        }

        if let Some(profile) = power_profile() {
            power.insert("profile".to_string(), Value::from(profile));
        }

        if power.is_empty() {
            return Ok(None);
        }

        Ok(Some(json!({ "gamepulse": { "power": power } })))
    }
}
