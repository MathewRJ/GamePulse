/// Configuration — reads the shared gamepulse.toml file.
///
/// Mirrors the Python collector's config.py exactly so both agents
/// read the same file without conflict.
use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::PathBuf;

#[derive(Debug, Deserialize)]
pub struct Config {
    pub elasticsearch: ElasticsearchConfig,
    #[serde(default)]
    pub collection: CollectionConfig,
    #[serde(default)]
    pub privacy: PrivacyConfig,
    #[serde(default)]
    pub session: SessionConfig,
}

/// Optional per-session metadata the user can set in gamepulse.toml.
///
/// [session]
/// label = "after-driver-update"
///
/// [session.settings]
/// preset = "ultra"
/// upscaler_tech = "dlss"
#[derive(Debug, Deserialize, Default)]
pub struct SessionConfig {
    /// A short human-readable annotation for this session (e.g. "proton-9-test").
    /// Written to every doc as gamepulse.session.label for easy dashboard filtering.
    pub label: Option<String>,
    #[serde(default)]
    pub settings: SessionSettingsConfig,
    pub target_pid: Option<u32>,
    pub target_name: Option<String>,
}

/// Tier 1 manual settings capture — populated from [session.settings] in the config
/// and/or CLI flags. All fields optional; unset fields are omitted from session docs.
#[derive(Debug, Deserialize, Default)]
pub struct SessionSettingsConfig {
    pub preset: Option<String>,
    pub upscaler_tech: Option<String>,
    pub upscaler_preset: Option<String>,
    pub frame_gen_tech: Option<String>,
    pub features_active: Option<Vec<String>>,
    pub render_resolution_output: Option<String>,
    pub render_vsync: Option<String>,
    pub notes: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ElasticsearchConfig {
    pub endpoint: String,
    pub api_key: Option<String>,
    pub username: Option<String>,
    pub password: Option<String>,
    #[serde(default = "default_index_prefix")]
    pub index_prefix: String,
    #[serde(default = "default_flush_interval_secs")]
    pub flush_interval_secs: u64,
    #[serde(default = "default_batch_size")]
    pub batch_size: usize,
}

fn default_index_prefix() -> String {
    "gamepulse".to_string()
}
fn default_flush_interval_secs() -> u64 {
    5
}
fn default_batch_size() -> usize {
    100
}

impl Default for ElasticsearchConfig {
    fn default() -> Self {
        Self {
            endpoint: "http://localhost:9200".to_string(),
            api_key: None,
            username: None,
            password: None,
            index_prefix: default_index_prefix(),
            flush_interval_secs: default_flush_interval_secs(),
            batch_size: default_batch_size(),
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct CollectionConfig {
    #[serde(default = "default_interval_ms")]
    pub interval_ms: u64,
    #[serde(default = "default_true")]
    pub cpu: bool,
    #[serde(default = "default_true")]
    pub memory: bool,
    #[serde(default = "default_true")]
    pub gpu: bool,
    #[serde(default = "default_true")]
    pub storage: bool,
    #[serde(default = "default_true")]
    pub network: bool,
    #[serde(default)]
    pub ebpf: bool,
    #[serde(default = "default_true")]
    pub frame_timing: bool,
    #[serde(default = "default_true")]
    pub game_detection: bool,
    #[serde(default = "default_sample_interval")]
    pub sample_interval_secs: u64,
}

fn default_interval_ms() -> u64 {
    1000
}
fn default_true() -> bool {
    true
}
fn default_sample_interval() -> u64 {
    1
}

impl Default for CollectionConfig {
    fn default() -> Self {
        Self {
            interval_ms: default_interval_ms(),
            cpu: true,
            memory: true,
            gpu: true,
            storage: true,
            network: true,
            ebpf: false,
            frame_timing: true,
            game_detection: true,
            sample_interval_secs: default_sample_interval(),
        }
    }
}

#[derive(Debug, Deserialize, Default)]
pub struct PrivacyConfig {
    #[serde(default)]
    pub opt_in_public: bool,
    #[serde(default)]
    pub share_ebpf: bool,
    #[serde(default)]
    pub share_network: bool,
}

impl Config {
    /// Load config from an explicit path or the first found default location.
    ///
    /// Search order:
    ///   1. `path` argument (from --config CLI flag)
    ///   2. $GAMEPULSE_CONFIG env var
    ///   3. ~/.config/gamepulse/gamepulse.toml
    ///   4. /etc/gamepulse/gamepulse.toml
    pub fn load(path: Option<&PathBuf>) -> Result<Self> {
        let candidates: Vec<PathBuf> = if let Some(p) = path {
            vec![p.clone()]
        } else if let Ok(env_path) = std::env::var("GAMEPULSE_CONFIG") {
            vec![PathBuf::from(env_path)]
        } else {
            let mut v = Vec::new();
            if let Some(home) = home_dir() {
                v.push(home.join(".config/gamepulse/gamepulse.toml"));
            }
            v.push(PathBuf::from("/etc/gamepulse/gamepulse.toml"));
            v
        };

        for candidate in &candidates {
            if candidate.exists() {
                let mut config = Self::load_from(candidate)?;
                config.apply_env_overrides();
                return Ok(config);
            }
        }
        anyhow::bail!(
            "no config file found; searched: {}",
            candidates
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join(", ")
        )
    }

    /// Apply ES_API_KEY and ES_URL env vars on top of whatever was in the TOML file.
    /// Env vars win — allows key rotation without editing config files.
    fn apply_env_overrides(&mut self) {
        if let Ok(key) = std::env::var("ES_API_KEY") {
            if !key.is_empty() {
                self.elasticsearch.api_key = Some(key);
            }
        }
        if let Ok(url) = std::env::var("ES_URL") {
            if !url.is_empty() {
                self.elasticsearch.endpoint = url;
            }
        }
    }

    fn load_from(path: &PathBuf) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading config file: {}", path.display()))?;
        toml::from_str(&text).with_context(|| format!("parsing config file: {}", path.display()))
    }
}

fn home_dir() -> Option<PathBuf> {
    // When running via sudo, HOME is /root but config lives in the invoking
    // user's home. Prefer SUDO_USER → /home/<user> over HOME.
    if let Ok(sudo_user) = std::env::var("SUDO_USER") {
        if !sudo_user.is_empty() {
            let p = PathBuf::from("/home").join(&sudo_user);
            if p.exists() {
                return Some(p);
            }
        }
    }
    std::env::var("HOME").ok().map(PathBuf::from)
}
