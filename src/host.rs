/// Host environment enricher — collected once at startup.
///
/// Mirrors collector/gamepulse/enricher/host.py.
/// Returns a serde_json::Value snapshot with:
///   host.os.{type, kernel, name, version, platform}
///   gamepulse.hardware.{cpu, gpu, ram, device}
///
/// Collected once at startup; added to the session start/end document only
/// (not to every per-tick document, matching Python cli.py behaviour).
use serde_json::{json, Map, Value};
use std::io::Read;
use std::time::{Duration, Instant};

// ── Subprocess helper ─────────────────────────────────────────────────────────

fn run_cmd(prog: &str, args: &[&str], timeout_ms: u64) -> Option<String> {
    let mut child = std::process::Command::new(prog)
        .args(args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let mut buf = Vec::new();
                if let Some(mut stdout) = child.stdout.take() {
                    let _ = stdout.read_to_end(&mut buf);
                }
                return Some(String::from_utf8_lossy(&buf).to_string());
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

// ── sysfs helpers ─────────────────────────────────────────────────────────────

fn read_str(path: &str) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn read_int(path: &str) -> Option<i64> {
    read_str(path).and_then(|s| s.parse().ok())
}

// ── Hostname ──────────────────────────────────────────────────────────────────

/// Return the machine hostname.
pub fn hostname() -> String {
    read_str("/proc/sys/kernel/hostname").unwrap_or_else(|| "unknown".to_string())
}

// ── OS release ────────────────────────────────────────────────────────────────

fn os_info() -> Map<String, Value> {
    let mut release: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    if let Ok(content) = std::fs::read_to_string("/etc/os-release") {
        for line in content.lines() {
            if let Some(pos) = line.find('=') {
                let k = line[..pos].trim().to_string();
                let v = line[pos + 1..].trim().trim_matches('"').to_string();
                release.insert(k, v);
            }
        }
    }

    let mut m = Map::new();
    m.insert("type".to_string(), Value::String("linux".to_string()));

    // kernel version from uname
    if let Ok(uname) = std::fs::read_to_string("/proc/sys/kernel/osrelease") {
        m.insert("kernel".to_string(), Value::String(uname.trim().to_string()));
    }

    if let Some(name) = release.get("NAME").or_else(|| release.get("PRETTY_NAME")) {
        m.insert("name".to_string(), Value::String(name.clone()));
    }
    if let Some(version) = release.get("VERSION_ID").or_else(|| release.get("BUILD_ID")) {
        m.insert("version".to_string(), Value::String(version.clone()));
    }
    if let Some(id) = release.get("ID") {
        m.insert("platform".to_string(), Value::String(id.clone()));
    }
    m
}

// ── CPU info ──────────────────────────────────────────────────────────────────

fn cpu_info() -> Map<String, Value> {
    let mut m = Map::new();
    if let Ok(cpuinfo) = std::fs::read_to_string("/proc/cpuinfo") {
        for line in cpuinfo.lines() {
            if line.starts_with("model name") && !m.contains_key("model") {
                if let Some((_, v)) = line.split_once(':') {
                    m.insert("model".to_string(), Value::String(v.trim().to_string()));
                }
            }
            if line.starts_with("cpu cores") && !m.contains_key("cores") {
                if let Some((_, v)) = line.split_once(':') {
                    if let Ok(n) = v.trim().parse::<i64>() {
                        m.insert("cores".to_string(), Value::from(n));
                    }
                }
            }
        }
        let threads = cpuinfo
            .lines()
            .filter(|l| l.starts_with("processor\t"))
            .count() as i64;
        if threads > 0 {
            m.insert("threads".to_string(), Value::from(threads));
        }
    }

    // Boost/base clock from cpufreq
    if let Ok(dir) = std::fs::read_dir("/sys/bus/cpu/devices") {
        let mut max_freqs: Vec<i64> = dir
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let p = e.path().join("cpufreq/cpuinfo_max_freq");
                read_int(p.to_str()?)
            })
            .collect();
        if !max_freqs.is_empty() {
            max_freqs.sort_unstable();
            let max = max_freqs.last().copied().unwrap_or(0) / 1000; // kHz → MHz
            m.insert("boost_clock_mhz".to_string(), Value::from(max));
        }
    }

    m
}

// ── GPU info ──────────────────────────────────────────────────────────────────

fn gpu_info() -> Map<String, Value> {
    let mut m = Map::new();

    // Find first discrete AMD/NVIDIA/Intel card
    let mut cards: Vec<std::path::PathBuf> = std::fs::read_dir("/sys/class/drm")
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.len() == 5 && n.starts_with("card") && n[4..].chars().all(|c| c.is_ascii_digit()))
                .unwrap_or(false)
        })
        .collect();
    cards.sort();

    // Pick the AMD/NVIDIA/Intel card with the most VRAM — prefers dGPU over iGPU when
    // both expose a DRM node (e.g. Ryzen iGPU at card0, RX 9070 XT at card1).
    let mut vendor_id: Option<String> = None;
    let mut best_card: Option<std::path::PathBuf> = None;
    let mut best_vram: i64 = -1;
    for card in &cards {
        let vendor_path = card.join("device/vendor");
        if let Some(v) = read_str(vendor_path.to_str().unwrap_or("")) {
            if matches!(v.as_str(), "0x1002" | "0x10de" | "0x8086") {
                // For NVIDIA/Intel there's typically only one; for AMD pick by VRAM.
                let vram = read_int(card.join("device/mem_info_vram_total").to_str().unwrap_or(""))
                    .unwrap_or(0);
                if best_card.is_none() || vram > best_vram {
                    best_vram = vram;
                    best_card = Some(card.clone());
                    vendor_id = Some(v);
                }
            }
        }
    }
    if let (Some(card), Some(ref v)) = (best_card, &vendor_id) {
        match v.as_str() {
            "0x1002" => {
                m.insert("vendor".to_string(), Value::String("amd".to_string()));
                if best_vram > 0 {
                    m.insert("vram_mb".to_string(), Value::from(best_vram / 1_048_576));
                }
            }
            "0x10de" => {
                m.insert("vendor".to_string(), Value::String("nvidia".to_string()));
            }
            "0x8086" => {
                m.insert("vendor".to_string(), Value::String("intel".to_string()));
            }
            _ => {}
        }
        let _ = card; // used for vendor detection; enrich_* uses vulkaninfo/nvidia-smi
    }

    match vendor_id.as_deref() {
        Some("0x1002") => enrich_amd(&mut m),
        Some("0x10de") => enrich_nvidia(&mut m),
        _ => {}
    }

    m
}

fn enrich_amd(m: &mut Map<String, Value>) {
    // vulkaninfo --summary: deviceName, driverVersion, driverName
    if let Some(out) = run_cmd("vulkaninfo", &["--summary"], 4000) {
        for line in out.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with("deviceName") && !m.contains_key("model") {
                if let Some((_, v)) = trimmed.split_once('=') {
                    m.insert("model".to_string(), Value::String(v.trim().to_string()));
                }
            }
            if trimmed.starts_with("driverVersion") && !m.contains_key("driver_version") {
                if let Some((_, v)) = trimmed.split_once('=') {
                    m.insert("driver_version".to_string(), Value::String(v.trim().to_string()));
                }
            }
            if trimmed.starts_with("driverName") && !m.contains_key("vulkan_driver") {
                if let Some((_, v)) = trimmed.split_once('=') {
                    m.insert(
                        "vulkan_driver".to_string(),
                        Value::String(v.trim().to_lowercase()),
                    );
                }
            }
        }
    }

    // glxinfo -B: Mesa version string (requires DISPLAY; may be absent in service context)
    if let Some(out) = run_cmd("glxinfo", &["-B"], 3000) {
        for line in out.lines() {
            if line.contains("Mesa") {
                if let Some(pos) = line.find("Mesa ") {
                    let rest = &line[pos + 5..];
                    let version: String = rest
                        .chars()
                        .take_while(|&c| c.is_ascii_digit() || c == '.')
                        .collect();
                    if !version.is_empty() {
                        m.insert("mesa_version".to_string(), Value::String(version));
                    }
                    break;
                }
            }
        }
    }

    // For RADV (open-source AMD Vulkan), Mesa version == driver version.
    // Fall back when glxinfo is unavailable (no DISPLAY in service context).
    if !m.contains_key("mesa_version") {
        if let (Some(Value::String(drv)), Some(Value::String(ver))) =
            (m.get("vulkan_driver"), m.get("driver_version"))
        {
            if drv == "radv" {
                m.insert("mesa_version".to_string(), Value::String(ver.clone()));
            }
        }
    }
}

fn enrich_nvidia(m: &mut Map<String, Value>) {
    let query = "name,memory.total,driver_version";
    if let Some(out) = run_cmd(
        "nvidia-smi",
        &[&format!("--query-gpu={}", query), "--format=csv,noheader,nounits"],
        5000,
    ) {
        let line = out.lines().next().unwrap_or("").trim().to_string();
        let parts: Vec<&str> = line.splitn(3, ',').map(|s| s.trim()).collect();
        if parts.len() >= 3 {
            if !parts[0].is_empty() {
                m.insert("model".to_string(), Value::String(parts[0].to_string()));
            }
            if let Ok(vram) = parts[1].parse::<i64>() {
                m.insert("vram_mb".to_string(), Value::from(vram));
            }
            if !parts[2].is_empty() {
                m.insert("driver_version".to_string(), Value::String(parts[2].to_string()));
            }
        }
        m.insert("vulkan_driver".to_string(), Value::String("nvidia".to_string()));
    }
}

// ── RAM info ──────────────────────────────────────────────────────────────────

fn ram_info() -> Map<String, Value> {
    let mut m = Map::new();
    if let Ok(content) = std::fs::read_to_string("/proc/meminfo") {
        for line in content.lines() {
            if line.starts_with("MemTotal:") {
                let parts: Vec<&str> = line.split_whitespace().collect();
                if let Some(kb) = parts.get(1).and_then(|s| s.parse::<i64>().ok()) {
                    m.insert("total_mb".to_string(), Value::from(kb / 1024));
                }
                break;
            }
        }
    }
    m
}

// ── Device info ───────────────────────────────────────────────────────────────

fn device_info() -> Map<String, Value> {
    let mut m = Map::new();

    if let Some(chassis) = read_str("/sys/class/dmi/id/chassis_type") {
        if let Ok(ct) = chassis.parse::<u32>() {
            let dtype = match ct {
                11 => "handheld",
                8 | 9 | 10 | 14 => "laptop",
                _ => "desktop",
            };
            m.insert("type".to_string(), Value::String(dtype.to_string()));
        }
    }

    if let Some(product) = read_str("/sys/class/dmi/id/product_name") {
        m.insert("model".to_string(), Value::String(product));
    }

    // Power source: check AC/ADP adapters
    let power_dirs: Vec<String> = std::fs::read_dir("/sys/class/power_supply")
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .map(|e| e.path().to_string_lossy().to_string())
        .filter(|p| {
            let name = std::path::Path::new(p)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("");
            name.starts_with("AC") || name.starts_with("ADP")
        })
        .collect();

    for supply in power_dirs {
        if let Some(online) = read_int(&format!("{}/online", supply)) {
            m.insert(
                "power_source".to_string(),
                Value::String(if online != 0 { "ac" } else { "battery" }.to_string()),
            );
            break;
        }
    }

    m
}

// ── Monitor info ──────────────────────────────────────────────────────────────

/// Parse `xrandr --verbose` output and return one JSON object per connected
/// monitor. Fields collected per monitor:
///   name, resolution_h, resolution_v, refresh_rate_hz, is_primary,
///   vrr_capable, hdr_capable, current_vrr_enabled
fn collect_monitors() -> Vec<Value> {
    let out = match run_cmd("xrandr", &["--verbose"], 4000) {
        Some(s) => s,
        None => return Vec::new(),
    };

    let mut monitors: Vec<Value> = Vec::new();
    let mut current: Option<Map<String, Value>> = None;
    // True after we see the *current mode line; cleared after we parse its v: clock.
    let mut after_current = false;

    for line in out.lines() {
        let tokens: Vec<&str> = line.split_whitespace().collect();

        // ── Monitor header ────────────────────────────────────────────────────
        // Format: "<name> connected [primary] [WxH+X+Y] ..."
        //      or "<name> disconnected ..."
        if tokens.len() >= 2 && tokens[1] == "connected" {
            // Flush previous monitor.
            if let Some(m) = current.take() {
                monitors.push(Value::Object(m));
            }
            after_current = false;

            let mut m = Map::new();
            m.insert("name".to_string(), Value::String(tokens[0].to_string()));
            m.insert(
                "is_primary".to_string(),
                Value::Bool(tokens.contains(&"primary")),
            );
            // Default booleans — overwritten if properties are found below.
            m.insert("vrr_capable".to_string(), Value::Bool(false));
            m.insert("hdr_capable".to_string(), Value::Bool(false));
            m.insert("current_vrr_enabled".to_string(), Value::Bool(false));

            // Geometry token: "WxH+X+Y" e.g. "3440x1440+0+0"
            for tok in &tokens[2..] {
                if tok.contains('x') && tok.contains('+') {
                    let geom = tok.split('+').next().unwrap_or("");
                    let parts: Vec<&str> = geom.split('x').collect();
                    if parts.len() == 2 {
                        if let (Ok(w), Ok(h)) =
                            (parts[0].parse::<i64>(), parts[1].parse::<i64>())
                        {
                            m.insert("resolution_h".to_string(), Value::from(w));
                            m.insert("resolution_v".to_string(), Value::from(h));
                        }
                    }
                    break;
                }
            }

            current = Some(m);
            continue;
        }

        let m = match current.as_mut() {
            Some(m) => m,
            None => continue,
        };

        let trimmed = line.trim();

        // ── Active mode line ──────────────────────────────────────────────────
        // "  3440x1440 (0x41) 889.750MHz -HSync +VSync *current +preferred"
        if trimmed.contains("*current") {
            after_current = true;
            continue;
        }

        // ── Refresh rate (v: timing line after *current) ───────────────────
        // "  v: height 1440 start 1443 end 1453 total 1545 clock 119.98Hz"
        if after_current && trimmed.starts_with("v:") {
            if let Some(pos) = trimmed.find("clock ") {
                let rest = &trimmed[pos + 6..];
                let hz: String = rest
                    .chars()
                    .take_while(|&c| c.is_ascii_digit() || c == '.')
                    .collect();
                if let Ok(f) = hz.parse::<f64>() {
                    m.insert("refresh_rate_hz".to_string(), json!(f));
                }
            }
            after_current = false;
            continue;
        }

        // ── VRR / HDR properties ──────────────────────────────────────────
        let lower = trimmed.to_lowercase();

        // vrr_capable: 1
        if lower.starts_with("vrr_capable:") && lower.contains(": 1") {
            m.insert("vrr_capable".to_string(), Value::Bool(true));
        }
        // FreeSync or G-Sync Compatible properties
        if (lower.contains("freesync") || lower.contains("gsync"))
            && lower.ends_with(": 1")
        {
            m.insert("vrr_capable".to_string(), Value::Bool(true));
        }
        // Variable Refresh Rate currently active
        if lower.starts_with("variable refresh rate") && lower.contains(": 1") {
            m.insert("current_vrr_enabled".to_string(), Value::Bool(true));
        }
        // HDR: max bpc > 8 signals HDR panel
        if lower.starts_with("max bpc:") {
            if let Some(pos) = trimmed.rfind(':') {
                if let Ok(bpc) = trimmed[pos + 1..].trim().parse::<u32>() {
                    if bpc > 8 {
                        m.insert("hdr_capable".to_string(), Value::Bool(true));
                    }
                }
            }
        }
        // Colorspace BT.2020 or HDR10 metadata implies HDR panel
        if lower.contains("colorspace") && (lower.contains("bt2020") || lower.contains("hdr")) {
            m.insert("hdr_capable".to_string(), Value::Bool(true));
        }
    }

    // Flush the last monitor.
    if let Some(m) = current.take() {
        monitors.push(Value::Object(m));
    }

    monitors
}

// ── Public entry point ─────────────────────────────────────────────────────────

/// Build the host enrichment snapshot. Called once at startup.
/// Returns a serde_json::Value merging host.os.* and gamepulse.hardware.*.
pub fn collect_snapshot() -> Value {
    let os = os_info();
    let cpu = cpu_info();
    let gpu = gpu_info();
    let ram = ram_info();
    let device = device_info();
    let monitors = collect_monitors();

    let mut hardware = serde_json::Map::new();
    if !cpu.is_empty() {
        hardware.insert("cpu".to_string(), Value::Object(cpu));
    }
    if !gpu.is_empty() {
        hardware.insert("gpu".to_string(), Value::Object(gpu));
    }
    if !ram.is_empty() {
        hardware.insert("ram".to_string(), Value::Object(ram));
    }
    if !device.is_empty() {
        hardware.insert("device".to_string(), Value::Object(device));
    }
    if !monitors.is_empty() {
        hardware.insert("monitors".to_string(), Value::Array(monitors));
    }

    let mut doc = json!({ "host": { "os": os } });
    if !hardware.is_empty() {
        doc.as_object_mut().unwrap().insert(
            "gamepulse".to_string(),
            json!({ "hardware": hardware }),
        );
    }
    doc
}
