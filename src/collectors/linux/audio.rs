/// Audio collector — mirrors collector/rigsignal/collectors/audio.py exactly.
///
/// Detects the audio backend (PipeWire / PulseAudio / ALSA) at construction
/// time by probing pw-cli and pactl. On each collect() tick, reads settings from
/// the running backend.
///
/// Output fields (rigsignal.audio.*):
///   backend          str  — always present: "pipewire", "pulseaudio", "alsa", "unknown"
///   latency_ms       f64  — configured scheduling latency in ms, 2 dp (PipeWire, when parseable)
///   sink_name        str  — default PipeWire sink name (when parseable)
///   card_profile     str  — default sink device profile (PipeWire, when parseable)
///   channels         i64  — default sink channel count (PipeWire, when parseable)
///   sample_format    str  — default sink sample format (PipeWire, when parseable)
///   sample_rate_hz   i64  — effective PipeWire clock or PulseAudio server rate
///   quantum          i64  — effective PipeWire scheduling quantum (when parseable)
///   driver_latency_ms f64 — default sink actual driver latency, 2 dp (PipeWire, when parseable)
///
/// collect() always returns Some — backend is always included even if stats fail.
use crate::collectors::Collector;
use anyhow::Result;
use serde_json::Value;
use std::io::Read;
use std::time::{Duration, Instant};

// PipeWire settings and pactl sink details are stable between collection ticks.
// Cache both successful reads and failures for 5 s.
const PIPEWIRE_CACHE_TTL: Duration = Duration::from_secs(5);

// ── Subprocess helper ─────────────────────────────────────────────────────────

/// Run a command, wait up to `timeout_ms` milliseconds, return stdout as String.
/// Returns None if the binary is not found, the command fails, or it times out.
fn run_cmd(prog: &str, args: &[&str], timeout_ms: u64) -> Option<String> {
    let mut child = std::process::Command::new(prog)
        .args(args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;

    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let mut buf = Vec::new();
                if let Some(mut stdout) = child.stdout.take() {
                    let _ = stdout.read_to_end(&mut buf);
                }
                return Some(String::from_utf8_lossy(&buf).to_string());
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}

// ── String parsing helpers (avoids adding regex crate) ───────────────────────

/// Extract integer before "Hz" in `line`.
/// Matches Python: `re.search(r"(\d+)\s*Hz", line)`.
fn hz_value(line: &str) -> Option<i64> {
    let idx = line.find("Hz")?;
    let before = line[..idx].trim_end();
    let num_start = before
        .rfind(|c: char| !c.is_ascii_digit())
        .map(|i| i + 1)
        .unwrap_or(0);
    let s = &before[num_start..];
    if s.is_empty() {
        return None;
    }
    s.parse().ok()
}

// ── Backend detection ─────────────────────────────────────────────────────────

fn detect_backend() -> String {
    // PipeWire exposes itself as PulseAudio — check PipeWire first.
    if let Some(out) = run_cmd("pw-cli", &["info", "0"], 2000) {
        if out.contains("PipeWire") || out.to_lowercase().contains("pipewire") {
            return "pipewire".to_string();
        }
    }

    if let Some(out) = run_cmd("pactl", &["info"], 2000) {
        if out.contains("PipeWire") {
            return "pipewire".to_string();
        }
        if out.contains("PulseAudio") {
            return "pulseaudio".to_string();
        }
    }

    // Check aplay exists (ALSA available).
    if run_cmd("aplay", &["--version"], 1000).is_some() {
        return "alsa".to_string();
    }

    "unknown".to_string()
}

// ── Per-backend stats ─────────────────────────────────────────────────────────

#[derive(Clone, Debug, Default, PartialEq)]
struct PipewireSettings {
    quantum: Option<i64>,
    rate: Option<i64>,
    force_quantum: Option<i64>,
    force_rate: Option<i64>,
}

impl PipewireSettings {
    fn effective_quantum(&self) -> Option<i64> {
        self.force_quantum
            .filter(|value| *value > 0)
            .or_else(|| self.quantum.filter(|value| *value > 0))
    }

    fn effective_rate(&self) -> Option<i64> {
        self.force_rate
            .filter(|value| *value > 0)
            .or_else(|| self.rate.filter(|value| *value > 0))
    }
}

/// Parse the last valid integer update for each clock setting from pw-metadata.
fn parse_pw_metadata_settings(out: &str) -> PipewireSettings {
    let mut settings = PipewireSettings::default();

    for line in out.lines() {
        let Some(update) = line.trim_start().strip_prefix("update:") else {
            continue;
        };
        let mut key = None;
        let mut value = None;
        for field in update.split_whitespace() {
            if let Some(parsed_key) = field
                .strip_prefix("key:'")
                .and_then(|field| field.strip_suffix('\''))
            {
                key = Some(parsed_key);
            } else if let Some(parsed_value) = field
                .strip_prefix("value:'")
                .and_then(|field| field.strip_suffix('\''))
            {
                value = parsed_value.parse::<i64>().ok();
            }
        }

        match (key, value) {
            (Some("clock.quantum"), Some(value)) => settings.quantum = Some(value),
            (Some("clock.rate"), Some(value)) => settings.rate = Some(value),
            (Some("clock.force-quantum"), Some(value)) => settings.force_quantum = Some(value),
            (Some("clock.force-rate"), Some(value)) => settings.force_rate = Some(value),
            _ => {}
        }
    }

    settings
}

fn pipewire_settings() -> Option<PipewireSettings> {
    let out = run_cmd("pw-metadata", &["-n", "settings", "0"], 2000)?;
    Some(parse_pw_metadata_settings(&out))
}

fn configured_latency_ms(quantum: i64, rate: i64) -> f64 {
    ((quantum as f64 / rate as f64 * 1000.0) * 100.0).round() / 100.0
}

#[derive(Clone, Debug, Default, PartialEq)]
struct PipewireSinkInfo {
    sink_name: Option<String>,
    card_profile: Option<String>,
    channels: Option<i64>,
    sample_format: Option<String>,
    sample_rate_hz: Option<i64>,
    driver_latency_ms: Option<f64>,
}

fn pactl_default_sink() -> Option<String> {
    let out = run_cmd("pactl", &["get-default-sink"], 2000)?;
    let sink_name = out.lines().next()?.trim();
    if sink_name.is_empty() {
        None
    } else {
        Some(sink_name.to_string())
    }
}

fn sink_block_name(block: &str) -> Option<&str> {
    block.lines().find_map(|line| {
        line.trim_start()
            .strip_prefix("Name:")
            .map(str::trim)
            .filter(|name| !name.is_empty())
    })
}

fn parse_sample_specification(line: &str) -> Option<(String, i64, i64)> {
    let spec = line
        .trim_start()
        .strip_prefix("Sample Specification:")?
        .trim();
    let mut fields = spec.split_whitespace();
    let format = fields.next()?.to_string();
    let channels = fields.next()?.strip_suffix("ch")?.parse().ok()?;
    let sample_rate_hz = fields.next()?.strip_suffix("Hz")?.parse().ok()?;
    Some((format, channels, sample_rate_hz))
}

fn parse_driver_latency_ms(line: &str) -> Option<f64> {
    let latency = line.trim_start().strip_prefix("Latency:")?.trim();
    let (actual, configured) = latency.split_once(" usec,")?;
    if !configured.trim_start().starts_with("configured ") {
        return None;
    }
    let actual: i64 = actual.trim().parse().ok()?;
    Some(((actual as f64 / 1000.0) * 100.0).round() / 100.0)
}

fn parse_pactl_sink_block(block: &str, sink_name: Option<String>) -> PipewireSinkInfo {
    let mut sink = PipewireSinkInfo {
        sink_name,
        ..Default::default()
    };

    for line in block.lines() {
        let trimmed = line.trim_start();
        if let Some(profile) = trimmed.strip_prefix("device.profile.name =") {
            let profile = profile.trim().trim_matches('"');
            if !profile.is_empty() {
                sink.card_profile = Some(profile.to_string());
            }
        } else if let Some((format, channels, sample_rate_hz)) = parse_sample_specification(line) {
            sink.sample_format = Some(format);
            sink.channels = Some(channels);
            sink.sample_rate_hz = Some(sample_rate_hz);
        } else if let Some(driver_latency_ms) = parse_driver_latency_ms(line) {
            sink.driver_latency_ms = Some(driver_latency_ms);
        }
    }

    sink
}

fn parse_pactl_sinks(out: &str, default_sink: Option<String>) -> Option<PipewireSinkInfo> {
    let mut block_starts = Vec::new();
    let mut offset = 0;
    for line in out.split_inclusive('\n') {
        if line.starts_with("Sink #") {
            block_starts.push(offset);
        }
        offset += line.len();
    }
    let blocks: Vec<&str> = block_starts
        .iter()
        .enumerate()
        .map(|(index, start)| {
            let end = block_starts.get(index + 1).copied().unwrap_or(out.len());
            &out[*start..end]
        })
        .collect();
    let block = match default_sink.as_deref() {
        Some(default_sink) => blocks
            .iter()
            .copied()
            .find(|block| sink_block_name(block) == Some(default_sink))?,
        None => *blocks.first()?,
    };
    Some(parse_pactl_sink_block(block, default_sink))
}

fn pipewire_sink_info() -> Option<PipewireSinkInfo> {
    let default_sink = pactl_default_sink();
    let out = run_cmd("pactl", &["list", "sinks"], 2000)?;
    parse_pactl_sinks(&out, default_sink)
}

struct PulseaudioStats {
    sample_rate_hz: Option<i64>,
}

fn pulseaudio_stats() -> Option<PulseaudioStats> {
    let out = run_cmd("pactl", &["stat"], 2000)?;
    let mut sample_rate_hz: Option<i64> = None;
    for line in out.lines() {
        if line.contains("Sample Specification") {
            if let Some(hz) = hz_value(line) {
                sample_rate_hz = Some(hz);
            }
        }
    }
    if sample_rate_hz.is_some() {
        Some(PulseaudioStats { sample_rate_hz })
    } else {
        None
    }
}

// ── Collector ─────────────────────────────────────────────────────────────────

pub struct AudioCollector {
    backend: Option<String>,
    settings_cache: Option<PipewireSettings>,
    settings_cache_at: Option<Instant>,
    sink_cache: Option<PipewireSinkInfo>,
    sink_cache_at: Option<Instant>,
}

impl AudioCollector {
    pub fn new(_game_pid: Option<u32>) -> Self {
        AudioCollector {
            backend: None,
            settings_cache: None,
            settings_cache_at: None,
            sink_cache: None,
            sink_cache_at: None,
        }
    }

    fn pipewire_settings_cached(&mut self) -> Option<&PipewireSettings> {
        let stale = self
            .settings_cache_at
            .map(|t| t.elapsed() >= PIPEWIRE_CACHE_TTL)
            .unwrap_or(true);
        if stale {
            self.settings_cache = pipewire_settings();
            self.settings_cache_at = Some(Instant::now());
        }
        self.settings_cache.as_ref()
    }

    fn pipewire_sink_info_cached(&mut self) -> Option<&PipewireSinkInfo> {
        let stale = self
            .sink_cache_at
            .map(|t| t.elapsed() >= PIPEWIRE_CACHE_TTL)
            .unwrap_or(true);
        if stale {
            self.sink_cache = pipewire_sink_info();
            self.sink_cache_at = Some(Instant::now());
        }
        self.sink_cache.as_ref()
    }
}

impl Collector for AudioCollector {
    fn dataset(&self) -> &'static str {
        "rigsignal.audio"
    }

    fn collect(&mut self) -> Result<Option<Value>> {
        if self.backend.is_none() {
            self.backend = Some(detect_backend());
        }
        let backend = self.backend.as_deref().unwrap_or("unknown");

        let mut audio = serde_json::Map::new();
        audio.insert("backend".to_string(), Value::from(backend.to_string()));

        if backend == "pipewire" {
            let settings = self
                .pipewire_settings_cached()
                .map(|settings| (settings.effective_quantum(), settings.effective_rate()));
            if let Some((quantum, rate)) = settings {
                if let Some(quantum) = quantum {
                    audio.insert("quantum".to_string(), Value::from(quantum));
                }
                if let Some(rate) = rate {
                    audio.insert("sample_rate_hz".to_string(), Value::from(rate));
                }
                if let (Some(quantum), Some(rate)) = (quantum, rate) {
                    audio.insert(
                        "latency_ms".to_string(),
                        Value::from(configured_latency_ms(quantum, rate)),
                    );
                }
            }

            if let Some(sink) = self.pipewire_sink_info_cached().cloned() {
                if let Some(sink_name) = sink.sink_name {
                    audio.insert("sink_name".to_string(), Value::from(sink_name));
                }
                if let Some(card_profile) = sink.card_profile {
                    audio.insert("card_profile".to_string(), Value::from(card_profile));
                }
                if let Some(channels) = sink.channels {
                    audio.insert("channels".to_string(), Value::from(channels));
                }
                if let Some(sample_format) = sink.sample_format {
                    audio.insert("sample_format".to_string(), Value::from(sample_format));
                }
                if let Some(driver_latency_ms) = sink.driver_latency_ms {
                    audio.insert(
                        "driver_latency_ms".to_string(),
                        Value::from(driver_latency_ms),
                    );
                }
            }
        } else if backend == "pulseaudio" {
            if let Some(stats) = pulseaudio_stats() {
                if let Some(hz) = stats.sample_rate_hz {
                    audio.insert("sample_rate_hz".to_string(), Value::from(hz));
                }
            }
        }

        Ok(Some(serde_json::json!({ "rigsignal": { "audio": audio } })))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SURROUND_SINKS: &str = r#"Sink #42
	State: RUNNING
	Name: alsa_output.pci-0000_01_00.1.hdmi-surround-extra2
	Sample Specification: float32le 6ch 48000Hz
	Latency: 1234 usec, configured 2000 usec
	Properties:
		device.profile.name = "hdmi-surround-extra2"
Sink #43
	State: IDLE
	Name: alsa_output.pci-0000_01_00.1.hdmi-stereo-extra2
	Sample Specification: s16le 2ch 44100Hz
	Latency: 500 usec, configured 1000 usec
	Properties:
		device.profile.name = "hdmi-stereo-extra2"
"#;

    const LIVE_METADATA: &str = "update: id:0 key:'clock.quantum' value:'512' type:''\n\
update: id:0 key:'clock.rate' value:'48000' type:''\n\
update: id:0 key:'clock.force-quantum' value:'0' type:''\n\
update: id:0 key:'clock.force-rate' value:'0' type:''\n";

    #[test]
    fn parses_live_pipewire_clock_settings() {
        let settings = parse_pw_metadata_settings(LIVE_METADATA);

        assert_eq!(settings.quantum, Some(512));
        assert_eq!(settings.rate, Some(48000));
        assert_eq!(settings.force_quantum, Some(0));
        assert_eq!(settings.force_rate, Some(0));
        assert_eq!(settings.effective_quantum(), Some(512));
        assert_eq!(settings.effective_rate(), Some(48000));
    }

    #[test]
    fn nonzero_force_settings_override_base_clock_settings() {
        let settings = parse_pw_metadata_settings(
            "update: id:0 key:'clock.quantum' value:'512' type:''\n\
update: id:0 key:'clock.rate' value:'48000' type:''\n\
update: id:0 key:'clock.force-quantum' value:'1024' type:''\n\
update: id:0 key:'clock.force-rate' value:'96000' type:''\n",
        );

        assert_eq!(settings.effective_quantum(), Some(1024));
        assert_eq!(settings.effective_rate(), Some(96000));
    }

    #[test]
    fn zero_force_settings_fall_back_to_base_clock_settings() {
        let settings = parse_pw_metadata_settings(LIVE_METADATA);

        assert_eq!(settings.effective_quantum(), Some(512));
        assert_eq!(settings.effective_rate(), Some(48000));
    }

    #[test]
    fn malformed_or_missing_settings_are_omitted_and_do_not_override_base() {
        let settings = parse_pw_metadata_settings(
            "update: id:0 key:'clock.quantum' value:'512' type:''\n\
update: id:0 key:'clock.rate' value:'48000' type:''\n\
update: id:0 key:'clock.force-quantum' value:'not-a-number' type:''\n\
update: id:0 key:'clock.force-rate' value:'-1' type:''\n\
update: id:0 key:'clock.unrelated' value:'2048' type:''\n",
        );

        assert_eq!(settings.force_quantum, None);
        assert_eq!(settings.force_rate, Some(-1));
        assert_eq!(settings.effective_quantum(), Some(512));
        assert_eq!(settings.effective_rate(), Some(48000));

        let missing_base = parse_pw_metadata_settings(
            "update: id:0 key:'clock.quantum' value:'0' type:''\n\
update: id:0 key:'clock.force-quantum' value:'broken' type:''\n",
        );
        assert_eq!(missing_base.effective_quantum(), None);
        assert_eq!(missing_base.effective_rate(), None);
    }

    #[test]
    fn calculates_configured_latency_from_effective_clock_settings() {
        assert_eq!(configured_latency_ms(512, 48000), 10.67);
    }

    #[test]
    fn parses_default_sink_fields_from_full_block() {
        let sink = parse_pactl_sinks(
            SURROUND_SINKS,
            Some("alsa_output.pci-0000_01_00.1.hdmi-surround-extra2".to_string()),
        )
        .unwrap();

        assert_eq!(
            sink.sink_name.as_deref(),
            Some("alsa_output.pci-0000_01_00.1.hdmi-surround-extra2")
        );
        assert_eq!(sink.card_profile.as_deref(), Some("hdmi-surround-extra2"));
        assert_eq!(sink.channels, Some(6));
        assert_eq!(sink.sample_format.as_deref(), Some("float32le"));
        assert_eq!(sink.sample_rate_hz, Some(48000));
        assert_eq!(sink.driver_latency_ms, Some(1.23));
    }

    #[test]
    fn profile_flip_uses_matching_default_sink_block() {
        let sink = parse_pactl_sinks(
            SURROUND_SINKS,
            Some("alsa_output.pci-0000_01_00.1.hdmi-stereo-extra2".to_string()),
        )
        .unwrap();

        assert_eq!(sink.card_profile.as_deref(), Some("hdmi-stereo-extra2"));
        assert_eq!(sink.sample_format.as_deref(), Some("s16le"));
        assert_eq!(sink.channels, Some(2));
        assert_eq!(sink.sample_rate_hz, Some(44100));
        assert_eq!(sink.driver_latency_ms, Some(0.5));
    }

    #[test]
    fn falls_back_to_first_sink_without_default_sink() {
        let sink = parse_pactl_sinks(SURROUND_SINKS, None).unwrap();

        assert_eq!(sink.sink_name, None);
        assert_eq!(sink.card_profile.as_deref(), Some("hdmi-surround-extra2"));
    }

    #[test]
    fn omits_malformed_sink_fields() {
        let sink = parse_pactl_sinks(
            r#"Sink #1
	Name: malformed
	Sample Specification: float32le unknown 48000Hz
	Latency: unknown usec, configured 2000 usec
	Properties:
		device.profile.name = ""
"#,
            Some("malformed".to_string()),
        )
        .unwrap();

        assert_eq!(sink.sink_name.as_deref(), Some("malformed"));
        assert_eq!(sink.card_profile, None);
        assert_eq!(sink.channels, None);
        assert_eq!(sink.sample_format, None);
        assert_eq!(sink.sample_rate_hz, None);
        assert_eq!(sink.driver_latency_ms, None);
    }
}
