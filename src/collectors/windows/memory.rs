/// Windows memory collector — uses GlobalMemoryStatusEx + GetProcessMemoryInfo.
/// No PDH needed; all values come from Win32 system calls.
///
/// Output fields (rigsignal.memory.*):
///   total_mb      u64  — total physical RAM in MB
///   used_mb       u64  — used physical RAM in MB (total - available)
///   available_mb  u64  — available physical RAM in MB
///   used_pct      f64  — used / total * 100, rounded to 1 decimal
///   game_rss_mb   u64  — optional working set of game_pid in MB
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};
use windows::Win32::System::ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS};
use windows::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
use windows::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_READ};

const MB: u64 = 1_048_576;

fn game_working_set_mb(pid: u32) -> Option<u64> {
    let handle =
        unsafe { OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, false, pid).ok()? };
    let mut pmc = PROCESS_MEMORY_COUNTERS::default();
    let ok = unsafe {
        GetProcessMemoryInfo(
            handle,
            &mut pmc,
            std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        )
    };
    // SAFETY: CloseHandle is always called, even on failure.
    unsafe {
        let _ = windows::Win32::Foundation::CloseHandle(handle);
    }
    if ok.is_err() {
        return None;
    }
    Some((pmc.WorkingSetSize as u64) / MB)
}

pub struct MemoryCollector {
    game_pid: Option<u32>,
}

impl MemoryCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        MemoryCollector { game_pid }
    }
}

impl Collector for MemoryCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.memory"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self.game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        let mut mem_status = MEMORYSTATUSEX {
            dwLength: std::mem::size_of::<MEMORYSTATUSEX>() as u32,
            ..Default::default()
        };
        unsafe { GlobalMemoryStatusEx(&mut mem_status)? };

        let total_mb = mem_status.ullTotalPhys / MB;
        let available_mb = mem_status.ullAvailPhys / MB;
        let used_mb = total_mb.saturating_sub(available_mb);
        let used_pct = if total_mb > 0 {
            ((used_mb as f64 / total_mb as f64) * 1000.0).round() / 10.0
        } else {
            0.0
        };

        let mut mem = json!({
            "total_mb": total_mb,
            "used_mb": used_mb,
            "available_mb": available_mb,
            "used_pct": used_pct,
        });

        if let Some(pid) = self.game_pid {
            if let Some(rss) = game_working_set_mb(pid) {
                mem.as_object_mut()
                    .unwrap()
                    .insert("game_rss_mb".to_string(), Value::from(rss));
            }
        }

        Ok(Some(json!({ "rigsignal": { "memory": mem } })))
    }
}
