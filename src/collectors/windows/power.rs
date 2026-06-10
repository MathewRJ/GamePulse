/// Windows power collector — GetSystemPowerStatus.
///
/// Emits battery percentage and AC connection state when available.
/// Returns Ok(None) on desktops with no battery and unknown AC status.
///
/// battery_rate_w (discharge rate in W) requires WMI Win32_Battery
/// EstimatedChargeRemaining + polling, or BatteryInformation via
/// DeviceIoControl. The Linux equivalent is in
/// src/collectors/linux/power.rs battery_rate_w(). Extend here
/// if laptop monitoring becomes a priority.
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use windows::Win32::System::Power::{GetSystemPowerStatus, SYSTEM_POWER_STATUS};

pub struct PowerCollector {
    _game_pid: Option<u32>,
}

impl PowerCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        PowerCollector {
            _game_pid: game_pid,
        }
    }
}

impl Collector for PowerCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.power"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self._game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let mut status = SYSTEM_POWER_STATUS::default();
        unsafe { GetSystemPowerStatus(&mut status)? };

        // ACLineStatus: 0 = battery, 1 = AC, 255 = unknown
        let ac_connected: Option<bool> = match status.ACLineStatus {
            0 => Some(false),
            1 => Some(true),
            _ => None,
        };

        // BatteryLifePercent: 0–100 valid, 255 = unknown/no battery
        let battery_pct: Option<f64> = if status.BatteryLifePercent <= 100 {
            Some(status.BatteryLifePercent as f64)
        } else {
            None
        };

        if ac_connected.is_none() && battery_pct.is_none() {
            return Ok(None);
        }

        let mut power = json!({});
        let obj = power.as_object_mut().unwrap();
        if let Some(ac) = ac_connected {
            obj.insert("ac_connected".to_string(), Value::from(ac));
        }
        if let Some(pct) = battery_pct {
            obj.insert("battery_pct".to_string(), Value::from(pct));
        }

        Ok(Some(json!({ "rigsignal": { "power": power } })))
    }
}
