use anyhow::Result;
use serde_json::Value;

pub mod cpu;
pub mod memory;

pub trait Collector: Send {
    /// Returns the data_stream dataset name, e.g. "cpu".
    fn dataset(&self) -> &'static str;

    /// Collect one sample. Returns None when no data is available this tick
    /// (e.g. first tick for delta-based metrics, or metric source absent).
    fn collect(&mut self) -> Result<Option<Value>>;
}
