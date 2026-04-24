use crate::collectors::Collector;
use anyhow::Result;
use serde_json::Value;

pub struct FrameCollector {
    _game_pid: Option<u32>,
}

impl FrameCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        Self { _game_pid: game_pid }
    }
}

impl Collector for FrameCollector {
    fn dataset(&self) -> &'static str {
        "gamepulse.frame"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        Ok(None)
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self._game_pid = pid;
    }
}
