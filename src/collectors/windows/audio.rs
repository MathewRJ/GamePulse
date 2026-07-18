/// Windows audio collector — WASAPI backend marker + xrun scaffold.
///
/// Emits backend: "wasapi". Glitch (xrun) detection is scaffolded but not
/// yet implemented — see GlitchListener below for the upgrade path.
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::{json, Value};

/// Scaffold for WASAPI glitch (xrun) detection.
///
/// WASAPI does not expose a direct glitch counter accessible from
/// outside the audio render thread. Two approaches exist:
///
/// Option A — ETW provider (preferred):
///   Consume the Microsoft-Windows-Audio ETW provider. Glitch events
///   are emitted as ETW events and can be consumed out-of-process.
///   This would be an independent Windows implementation.
///   See: Microsoft-Windows-Audio manifest in Windows SDK evntman.
///
/// Option B — IAudioSessionEvents (complex):
///   Register IAudioSessionControl::RegisterAudioSessionNotification
///   on each active render session. AUDCLNT_E_BUFFER_ERROR in the
///   render thread signals a glitch, but this requires being inside
///   the game's audio render thread — not feasible from an external
///   agent.
///
/// Recommended implementation path: ETW (Option A).
/// When implementing, add an etw.rs module alongside pdh.rs and
/// route xrun counts through the FrameSource / GlitchListener
/// trait pattern used in frame.rs.
///
/// TODO(C.7-xruns): implement ETW-based xrun detection.
struct GlitchListener;

impl GlitchListener {
    fn new() -> Self {
        GlitchListener
    }
    fn pending_xruns(&self) -> u32 {
        0
    }
}

pub struct AudioCollector {
    _game_pid: Option<u32>,
    _glitch_listener: GlitchListener,
}

impl AudioCollector {
    pub fn new(game_pid: Option<u32>) -> Self {
        AudioCollector {
            _game_pid: game_pid,
            _glitch_listener: GlitchListener::new(),
        }
    }
}

impl Collector for AudioCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.audio"
    }

    fn set_game_pid(&mut self, pid: Option<u32>) {
        self._game_pid = pid;
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        Ok(Some(json!({
            "rigsignal": {
                "audio": {
                    "backend": "wasapi",
                }
            }
        })))
    }
}
