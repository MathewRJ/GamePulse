use anyhow::Result;
use serde_json::Value;

pub mod audio;
pub mod cpu;
pub mod gpu_amd;
pub mod mangohud;
pub mod memory;
pub mod network;
pub mod power;
pub mod storage;

pub trait Collector: Send + 'static {
    /// Returns the data_stream dataset name, e.g. "cpu".
    fn dataset(&self) -> &'static str;

    /// Collect one sample. Returns None when no data is available this tick
    /// (e.g. first tick for delta-based metrics, or metric source absent).
    fn collect(&mut self) -> Result<Option<Value>>;

    fn set_game_pid(&mut self, _pid: Option<u32>) {}
}
