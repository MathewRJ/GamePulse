//! Shared WMI thermal-zone helper.
//!
//! Runs `Get-WmiObject MSAcpi_ThermalZoneTemperature` via PowerShell and
//! returns all zones as (instance_name, celsius) pairs.
//!
//! Callers are responsible for caching — the subprocess is slow (~200 ms)
//! so each collector debounces with its own 5-second cache.

/// Returns all ACPI thermal zones as (instance_name, celsius) pairs.
/// Values outside 10–105 °C are discarded. Returns an empty Vec on any
/// subprocess or parse failure.
pub fn query_thermal_zones() -> Vec<(String, f64)> {
    let output = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi | \
             ForEach-Object { \"$($_.InstanceName)`t$($_.CurrentTemperature)\" }",
        ])
        .output();

    let output = match output {
        Ok(o) if o.status.success() || !o.stdout.is_empty() => o,
        _ => return Vec::new(),
    };

    let text = String::from_utf8_lossy(&output.stdout);
    let mut zones = Vec::new();

    for line in text.lines() {
        let mut parts = line.splitn(2, '\t');
        let name = match parts.next() {
            Some(n) => n.trim().to_string(),
            None => continue,
        };
        let raw: f64 = match parts.next().and_then(|s| s.trim().parse().ok()) {
            Some(v) => v,
            None => continue,
        };
        // WMI reports temperature in tenths of Kelvin.
        let celsius = (raw / 10.0) - 273.15;
        if !(10.0..=105.0).contains(&celsius) {
            continue;
        }
        zones.push((name, (celsius * 10.0).round() / 10.0));
    }

    zones
}
