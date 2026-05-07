use anyhow::Result;
use serde_json::Value;

pub trait Collector: Send + 'static {
    /// Returns the data_stream dataset name, e.g. "cpu".
    fn dataset(&self) -> &'static str;

    /// Collect one sample. Returns None when no data is available this tick
    /// (e.g. first tick for delta-based metrics, or metric source absent).
    fn collect(&mut self) -> Result<Option<Value>>;

    fn set_game_pid(&mut self, _pid: Option<u32>) {}
}

#[cfg(target_os = "linux")]
pub mod linux;

#[cfg(target_os = "linux")]
pub use linux::*;

#[cfg(target_os = "windows")]
pub mod windows;

#[cfg(target_os = "windows")]
pub use windows::*;
