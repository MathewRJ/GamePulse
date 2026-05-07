/// CPU collector — mirrors collector/gamepulse/collectors/cpu.py exactly.
///
/// Reads /proc/stat (utilisation), sysfs cpufreq (clock), hwmon (temperature),
/// and sysfs boost/governor flags.
///
/// Output fields (gamepulse.cpu.*):
///   total_utilisation_pct  f64   — average across all logical CPUs
///   per_core               [f64] — per-logical-CPU utilisation
///   clock_mhz_avg          u64   — average scaling_cur_freq in MHz (optional)
///   temperature_c          f64   — package/die temperature in °C (optional)
///   governor               str   — cpufreq scaling governor (optional)
///   boost_state            bool  — true if boost/turbo enabled
///   power_w                f64   — RAPL package power in W (optional; absent on AMD)
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use std::path::Path;
use std::time::Instant;

// ── /proc/stat snapshot ──────────────────────────────────────────────────────

struct ProcStatSnapshot {
    _timestamp: Instant,
    /// (idle_jiffies, total_jiffies) per logical CPU, in cpu0, cpu1, … order.
    per_core: Vec<(u64, u64)>,
}

impl ProcStatSnapshot {
    fn read() -> Result<Self> {
        let text = std::fs::read_to_string("/proc/stat")?;
        let mut per_core = Vec::new();
        for line in text.lines() {
            if !line.starts_with("cpu") {
                break; // cpu lines are always at the top
            }
            let mut parts = line.split_ascii_whitespace();
            let name = parts.next().unwrap_or("");
            if name == "cpu" {
                continue; // skip aggregate line; compute from per-core
            }
            // fields: user nice system idle iowait irq softirq steal [guest guest_nice]
            let fields: Vec<u64> = parts.take(8).map(|s| s.parse().unwrap_or(0)).collect();
            if fields.len() < 5 {
                continue;
            }
            let idle = fields[3] + fields[4]; // idle + iowait
            let total: u64 = fields.iter().sum();
            per_core.push((idle, total));
        }
        Ok(ProcStatSnapshot {
            _timestamp: Instant::now(),
            per_core,
        })
    }

    /// Compute per-core utilisation percentages against a previous snapshot.
    /// Returns one f64 per logical CPU, rounded to 1 decimal place.
    fn utilisation(&self, prev: &ProcStatSnapshot) -> Vec<f64> {
        self.per_core
            .iter()
            .zip(prev.per_core.iter())
            .map(|((idle, total), (p_idle, p_total))| {
                let d_total = total.saturating_sub(*p_total);
                let d_idle = idle.saturating_sub(*p_idle);
                if d_total == 0 {
                    0.0
                } else {
                    let pct = 100.0 * (1.0 - d_idle as f64 / d_total as f64);
                    (pct * 10.0).round() / 10.0 // 1 decimal place
                }
            })
            .collect()
    }
}

// ── sysfs helpers ────────────────────────────────────────────────────────────

fn read_int(path: &str) -> Option<u64> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|s| s.trim().parse().ok())
}

fn read_str(path: &str) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|s| s.trim().to_string())
}

fn governor() -> Option<String> {
    read_str("/sys/bus/cpu/devices/cpu0/cpufreq/scaling_governor")
}

fn boost_enabled() -> bool {
    // AMD/generic boost toggle (0 = disabled, 1 = enabled)
    if let Some(v) = read_int("/sys/devices/system/cpu/cpufreq/boost") {
        return v == 1;
    }
    // Intel no_turbo (inverted)
    if let Some(v) = read_int("/sys/devices/system/cpu/intel_pstate/no_turbo") {
        return v == 0;
    }
    true // assume enabled if not detectable
}

fn clock_mhz_avg() -> Option<u64> {
    // Glob /sys/bus/cpu/devices/cpu*/cpufreq/scaling_cur_freq, sorted.
    let base = Path::new("/sys/bus/cpu/devices");
    let mut freqs: Vec<u64> = Vec::new();

    if let Ok(entries) = std::fs::read_dir(base) {
        let mut paths: Vec<_> = entries
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .and_then(|n| n.to_str())
                    .map(|n| n.starts_with("cpu") && n[3..].chars().all(|c| c.is_ascii_digit()))
                    .unwrap_or(false)
            })
            .collect();
        paths.sort();

        for cpu_path in paths {
            let freq_path = cpu_path.join("cpufreq/scaling_cur_freq");
            if let Some(v) = read_int(freq_path.to_str()?) {
                freqs.push(v); // kHz
            }
        }
    }

    if freqs.is_empty() {
        return None;
    }
    let avg_khz: u64 = freqs.iter().sum::<u64>() / freqs.len() as u64;
    Some(avg_khz / 1000) // kHz → MHz, integer (matches Python int())
}

fn temperature_c() -> Option<f64> {
    // Search hwmon devices for k10temp (AMD) or coretemp (Intel).
    let hwmon_base = Path::new("/sys/class/hwmon");
    let mut entries: Vec<_> = std::fs::read_dir(hwmon_base)
        .ok()?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .collect();
    entries.sort();

    for hwmon in entries {
        let name = read_str(hwmon.join("name").to_str()?)?;
        if name != "k10temp" && name != "coretemp" {
            continue;
        }

        // Look for a temp*_label matching Tdie / Tctl / Package.
        if let Ok(label_entries) = std::fs::read_dir(&hwmon) {
            let mut labels: Vec<_> = label_entries
                .filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| {
                    p.file_name()
                        .and_then(|n| n.to_str())
                        .map(|n| n.starts_with("temp") && n.ends_with("_label"))
                        .unwrap_or(false)
                })
                .collect();
            labels.sort();

            for label_path in labels {
                let label = read_str(label_path.to_str()?).unwrap_or_default();
                if label.contains("Tdie") || label.contains("Package") || label.contains("Tctl") {
                    let input_path = label_path.to_string_lossy().replace("_label", "_input");
                    if let Some(v) = read_int(&input_path) {
                        return Some((v as f64 / 1000.0 * 10.0).round() / 10.0);
                    }
                }
            }
        }

        // Fallback: temp1_input
        if let Some(v) = read_int(hwmon.join("temp1_input").to_str()?) {
            return Some((v as f64 / 1000.0 * 10.0).round() / 10.0);
        }
    }
    None
}

fn power_w() -> Option<f64> {
    // Intel RAPL only — AMD energy driver is not reliable enough to use here.
    // Returns None on AMD (matches Python behaviour).
    let rapl_path = "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj";
    if !Path::new(rapl_path).exists() {
        return None;
    }
    let e1: u64 = read_int(rapl_path)?;
    std::thread::sleep(std::time::Duration::from_millis(50));
    let e2: u64 = read_int(rapl_path)?;
    let watts = (e2.saturating_sub(e1)) as f64 / 0.05 / 1_000_000.0;
    Some((watts * 10.0).round() / 10.0)
}

// ── Collector ────────────────────────────────────────────────────────────────

pub struct CpuCollector {
    prev: Option<ProcStatSnapshot>,
    game_pid: Option<u32>,
}

impl CpuCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        CpuCollector {
            prev: None,
            game_pid,
        }
    }

    pub fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }
}

impl Collector for CpuCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.cpu"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let snap = ProcStatSnapshot::read()?;

        let prev = match self.prev.take() {
            None => {
                self.prev = Some(snap);
                return Ok(None); // need two snapshots for a delta
            }
            Some(p) => p,
        };

        let per_core = snap.utilisation(&prev);
        self.prev = Some(snap);

        let total = if per_core.is_empty() {
            0.0
        } else {
            let sum: f64 = per_core.iter().sum();
            (sum / per_core.len() as f64 * 10.0).round() / 10.0
        };

        let mut cpu = json!({
            "total_utilisation_pct": total,
            "per_core": per_core,
            "boost_state": boost_enabled(),
        });

        let obj = cpu.as_object_mut().unwrap();
        if let Some(clk) = clock_mhz_avg() {
            obj.insert("clock_mhz_avg".to_string(), Value::from(clk));
        }
        if let Some(temp) = temperature_c() {
            obj.insert("temperature_c".to_string(), Value::from(temp));
        }
        if let Some(pwr) = power_w() {
            obj.insert("power_w".to_string(), Value::from(pwr));
        }
        if let Some(gov) = governor() {
            obj.insert("governor".to_string(), Value::from(gov));
        }

        Ok(Some(json!({ "gamepulse": { "cpu": cpu } })))
    }
}
