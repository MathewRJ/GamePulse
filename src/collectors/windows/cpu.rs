use crate::collectors::Collector;
use anyhow::Result;
use serde_json::Value;

pub struct CpuCollector {
    _game_pid: Option<u32>,
}

impl CpuCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        Self {
            _game_pid: game_pid,
        }
    }
}

impl Collector for CpuCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.cpu"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        Ok(None)
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self._game_pid = pid;
    }
}
