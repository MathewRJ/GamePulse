/// Windows GPU collector — DXGI VRAM + PDH utilisation + WMI temperature.
///
/// # VRAM budget vs hardware total
///
/// VRAM budget reported by DXGI_MEMORY_SEGMENT_GROUP_LOCAL is the OS-managed
/// local (on-GPU) budget. It may be smaller than physical VRAM when other
/// adapters or processes are competing. For total physical VRAM without budget
/// constraints, use DXGI_ADAPTER_DESC1::DedicatedVideoMemory from GetDesc1() —
/// this is the hardware-reported figure, equivalent to what the Linux collector
/// reads from mem_info_vram_total in sysfs. The budget approach is used here
/// because it reflects actual available headroom, which is more useful for
/// gaming telemetry than raw capacity. To switch to hardware total: call
/// adapter.GetDesc1() and read DedicatedVideoMemory instead of
/// QueryVideoMemoryInfo.
///
/// # PDH GPU utilisation counter
///
/// \GPU Engine(*engtype_3D*)\Utilization Percentage requires WDDM 2.0+
/// (Windows 10 1607+, all modern discrete GPUs). On systems where this
/// counter is absent, DXGI VRAM data is still emitted.
/// Vendor SDK upgrade paths:
/// - AMD: ADLX (AMD Device Library eXtension, successor to ADL).
///   Provides GPU utilisation, die temperature, power draw, clock speeds.
///   C++ SDK only as of 2026; no production-quality Rust binding exists.
///   See: https://gpuopen.com/adlx/
/// - NVIDIA: NvAPI. Similar capability set to ADLX.
///   Rust bindings exist (nvapi-rs) but are unmaintained.
///   Both vendor SDKs would replace the PDH utilisation counter and the
///   WMI temperature query with accurate, GPU-specific readings.
///
/// # WMI temperature
///
/// WMI MSAcpi_ThermalZoneTemperature reports ACPI thermal zones which
/// may be GPU die, GPU memory, ambient, or chassis depending on OEM
/// firmware. temp_source='wmi_acpi' in the emitted doc signals this
/// uncertainty. Accurate GPU die temperature requires ADLX (AMD) or
/// NvAPI (NVIDIA). The Linux equivalent is hwmon via amdgpu sysfs at
/// src/collectors/linux/gpu_amd.rs.
use crate::collectors::windows::pdh::{PdhCounter, PdhQuery};
use crate::collectors::windows::wmi;
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use std::time::{Duration, Instant};
use windows::Win32::Graphics::Dxgi::{
    CreateDXGIFactory2, IDXGIAdapter3, IDXGIFactory6, DXGI_CREATE_FACTORY_FLAGS,
    DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, DXGI_MEMORY_SEGMENT_GROUP_LOCAL,
};
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED};

const MB: u64 = 1_048_576;
// HRESULT 0x80010106 — COM already initialised in a different apartment mode
const RPC_E_CHANGED_MODE: i32 = 0x80010106_u32 as i32;

// ── COM + DXGI init ───────────────────────────────────────────────────────────

fn try_init_com() -> bool {
    // CoInitializeEx returns HRESULT directly in windows 0.58 (not Result).
    // HRESULT(0) = S_OK (first init), HRESULT(1) = S_FALSE (already init same apartment).
    // Both need a matching CoUninitialize on drop. RPC_E_CHANGED_MODE means a different
    // apartment already owns COM — our call failed, so no CoUninitialize needed.
    let hr = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
    if hr.0 == 0 || hr.0 == 1 {
        true
    } else {
        if hr.0 != RPC_E_CHANGED_MODE {
            tracing::warn!("GpuCollector CoInitializeEx failed: 0x{:08X}", hr.0 as u32);
        }
        false
    }
}

fn try_init_dxgi() -> Option<(IDXGIFactory6, IDXGIAdapter3)> {
    let factory: IDXGIFactory6 = unsafe { CreateDXGIFactory2(DXGI_CREATE_FACTORY_FLAGS(0)).ok()? };
    let adapter: IDXGIAdapter3 = unsafe {
        factory
            .EnumAdapterByGpuPreference(0, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE)
            .ok()?
    };
    Some((factory, adapter))
}

// ── PDH GPU utilisation ───────────────────────────────────────────────────────

fn try_init_pdh() -> Option<(PdhQuery, PdhCounter)> {
    let mut query = PdhQuery::new().ok()?;
    // Wildcard *engtype_3D* matches only 3D engine contexts.
    let counter = query
        .add_counter(r"\GPU Engine(*engtype_3D*)\Utilization Percentage")
        .ok()?;
    query.collect().ok()?;
    Some((query, counter))
}

// ── WMI GPU temperature ───────────────────────────────────────────────────────

/// Select the best GPU temperature from all thermal zones.
/// Prefers zones whose name contains "GPU", "VRAM", "diode", or "VGA"
/// (case-insensitive). Falls back to the highest temperature reading.
fn select_gpu_temp(zones: &[(String, f64)]) -> Option<f64> {
    if zones.is_empty() {
        return None;
    }
    let gpu_keywords = ["gpu", "vram", "diode", "vga"];
    for (name, temp) in zones {
        let lower = name.to_lowercase();
        if gpu_keywords.iter().any(|kw| lower.contains(kw)) {
            return Some(*temp);
        }
    }
    // Fallback: hottest zone (most likely GPU under load on gaming hardware).
    zones.iter().map(|(_, t)| *t).reduce(f64::max)
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct GpuCollector {
    game_pid: Option<u32>,
    pdh_query: Option<PdhQuery>,
    pdh_counter: Option<PdhCounter>,
    pdh_available: bool,
    dxgi_factory: Option<IDXGIFactory6>,
    dxgi_adapter: Option<IDXGIAdapter3>,
    com_owner: bool,
    wmi_cache: Option<(f64, Instant)>,
}

impl GpuCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        let com_owner = try_init_com();

        let (dxgi_factory, dxgi_adapter) = match try_init_dxgi() {
            Some(pair) => (Some(pair.0), Some(pair.1)),
            None => {
                tracing::warn!("GpuCollector DXGI init failed — VRAM will not be emitted");
                (None, None)
            }
        };

        let (pdh_query, pdh_counter, pdh_available) = match try_init_pdh() {
            Some((q, c)) => (Some(q), Some(c), true),
            None => {
                tracing::warn!(
                    "GpuCollector PDH GPU counter unavailable — \
                     utilisation_pct will not be emitted (WDDM 2.0+ required)"
                );
                (None, None, false)
            }
        };

        GpuCollector {
            game_pid,
            pdh_query,
            pdh_counter,
            pdh_available,
            dxgi_factory,
            dxgi_adapter,
            com_owner,
            wmi_cache: None,
        }
    }

    fn gpu_temp_cached(&mut self) -> Option<f64> {
        let now = Instant::now();
        let stale = self
            .wmi_cache
            .map(|(_, t)| now.duration_since(t) >= Duration::from_secs(5))
            .unwrap_or(true);

        if stale {
            let zones = wmi::query_thermal_zones();
            let temp = select_gpu_temp(&zones);
            self.wmi_cache = Some((temp.unwrap_or(f64::NAN), now));
        }

        self.wmi_cache
            .and_then(|(t, _)| if t.is_nan() { None } else { Some(t) })
    }
}

impl Drop for GpuCollector {
    fn drop(&mut self) {
        if self.com_owner {
            unsafe { CoUninitialize() };
        }
    }
}

impl Collector for GpuCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.gpu"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let mut gpu = json!({});
        let obj = gpu.as_object_mut().unwrap();

        // ── PDH utilisation ──────────────────────────────────────────────────
        if self.pdh_available {
            if let Some(query) = self.pdh_query.as_mut() {
                if query.collect().is_ok() {
                    if let Some(counter) = &self.pdh_counter {
                        let pairs = query.counter_values_array(counter).unwrap_or_default();
                        let util: f64 = pairs
                            .iter()
                            .filter(|(name, _)| name.contains("engtype_3D"))
                            .map(|(_, v)| *v)
                            .fold(f64::NEG_INFINITY, f64::max);
                        if util.is_finite() && util >= 0.0 {
                            obj.insert(
                                "utilisation_pct".to_string(),
                                Value::from((util * 10.0).round() / 10.0),
                            );
                        }
                    }
                }
            }
        }

        // ── DXGI VRAM ────────────────────────────────────────────────────────
        if let Some(adapter) = &self.dxgi_adapter {
            let mut info = windows::Win32::Graphics::Dxgi::DXGI_QUERY_VIDEO_MEMORY_INFO::default();
            if unsafe {
                adapter.QueryVideoMemoryInfo(0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &mut info)
            }
            .is_ok()
            {
                obj.insert(
                    "memory_used_mb".to_string(),
                    Value::from(info.CurrentUsage / MB),
                );
                obj.insert("memory_total_mb".to_string(), Value::from(info.Budget / MB));
            }
        }

        // ── WMI temperature ──────────────────────────────────────────────────
        if let Some(temp) = self.gpu_temp_cached() {
            obj.insert("temperature_c".to_string(), Value::from(temp));
            obj.insert("temp_source".to_string(), Value::from("wmi_acpi"));
        }

        if obj.is_empty() {
            return Ok(None);
        }

        Ok(Some(json!({ "gamepulse": { "gpu": gpu } })))
    }
}
