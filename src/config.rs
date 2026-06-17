/// Configuration — reads the shared rigsignal.toml file.
///
/// Mirrors the Python collector's config.py exactly so both agents
/// read the same file without conflict.
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Config {
    pub elasticsearch: ElasticsearchConfig,
    #[serde(default)]
    pub output: OutputConfig,
    #[serde(default)]
    pub collection: CollectionConfig,
    #[serde(default)]
    pub privacy: PrivacyConfig,
    #[serde(default)]
    pub session: SessionConfig,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum OutputMode {
    #[default]
    Elasticsearch,
    Spool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct OutputConfig {
    #[serde(default)]
    pub mode: OutputMode,
    #[serde(default = "default_spool_dir")]
    pub spool_dir: PathBuf,
    #[serde(default = "default_max_file_bytes")]
    pub max_file_bytes: u64,
    #[serde(default = "default_max_file_age_secs")]
    pub max_file_age_secs: u64,
}

fn default_spool_dir() -> PathBuf {
    if let Some(home) = home_dir() {
        home.join(".local/state/rigsignal/spool")
    } else {
        PathBuf::from(".local/state/rigsignal/spool")
    }
}

fn default_max_file_bytes() -> u64 {
    10_485_760
}

fn default_max_file_age_secs() -> u64 {
    300
}

impl Default for OutputConfig {
    fn default() -> Self {
        Self {
            mode: OutputMode::Elasticsearch,
            spool_dir: default_spool_dir(),
            max_file_bytes: default_max_file_bytes(),
            max_file_age_secs: default_max_file_age_secs(),
        }
    }
}

/// Optional per-session metadata the user can set in rigsignal.toml.
///
/// [session]
/// label = "after-driver-update"
///
/// [session.settings]
/// preset = "ultra"
/// upscaler_tech = "dlss"
#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct SessionConfig {
    /// A short human-readable annotation for this session (e.g. "proton-9-test").
    /// Written to every doc as rigsignal.session.label for easy dashboard filtering.
    pub label: Option<String>,
    #[serde(default)]
    pub settings: SessionSettingsConfig,
    pub target_pid: Option<u32>,
    pub target_name: Option<String>,
}

/// Tier 1 manual settings capture — populated from [session.settings] in the config
/// and/or CLI flags. All fields optional; unset fields are omitted from session docs.
#[derive(Debug, Clone, Deserialize, Serialize, Default)]
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

#[derive(Debug, Clone, Deserialize, Serialize)]
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
    "rigsignal".to_string()
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

#[derive(Debug, Clone, Deserialize, Serialize)]
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

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
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
    ///   2. $RIGSIGNAL_CONFIG env var
    ///
    /// Linux fallback chain (when neither of the above is set):
    ///   3. ~/.config/rigsignal/rigsignal.toml
    ///   4. /etc/rigsignal/rigsignal.toml
    ///
    /// Windows fallback chain (when neither of the above is set):
    ///   3. %APPDATA%\RigSignal\rigsignal.toml          (per-user)
    ///   4. %PROGRAMDATA%\RigSignal\rigsignal.toml      (system-wide)
    pub fn load(path: Option<&PathBuf>) -> Result<Self> {
        let candidates: Vec<PathBuf> = if let Some(p) = path {
            vec![p.clone()]
        } else if let Ok(env_path) = std::env::var("RIGSIGNAL_CONFIG") {
            vec![PathBuf::from(env_path)]
        } else {
            let mut v = Vec::new();
            #[cfg(windows)]
            {
                if let Ok(appdata) = std::env::var("APPDATA") {
                    v.push(
                        PathBuf::from(appdata)
                            .join("RigSignal")
                            .join("rigsignal.toml"),
                    );
                }
                if let Ok(programdata) = std::env::var("PROGRAMDATA") {
                    v.push(
                        PathBuf::from(programdata)
                            .join("RigSignal")
                            .join("rigsignal.toml"),
                    );
                }
            }
            #[cfg(not(windows))]
            {
                if let Some(home) = home_dir() {
                    v.push(home.join(".config/rigsignal/rigsignal.toml"));
                }
                v.push(PathBuf::from("/etc/rigsignal/rigsignal.toml"));
            }
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

    /// Return a clone suitable for display: api_key / username / password are
    /// replaced with "<redacted>" so the output is safe to print or log.
    pub fn redacted_for_display(&self) -> Self {
        let mut out = self.clone();
        let es = &mut out.elasticsearch;
        if es.api_key.is_some() {
            es.api_key = Some("<redacted>".to_string());
        }
        if es.username.is_some() {
            es.username = Some("<redacted>".to_string());
        }
        if es.password.is_some() {
            es.password = Some("<redacted>".to_string());
        }
        out
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_defaults_output_when_absent() {
        let cfg: Config = toml::from_str(
            r#"
            [elasticsearch]
            endpoint = "http://localhost:9200"
            "#,
        )
        .unwrap();

        assert_eq!(cfg.output.mode, OutputMode::Elasticsearch);
        assert!(cfg
            .output
            .spool_dir
            .ends_with(".local/state/rigsignal/spool"));
        assert_eq!(cfg.output.max_file_bytes, 10_485_760);
        assert_eq!(cfg.output.max_file_age_secs, 300);
    }

    #[test]
    fn config_deserializes_spool_output() {
        let cfg: Config = toml::from_str(
            r#"
            [elasticsearch]
            endpoint = "http://localhost:9200"

            [output]
            mode = "spool"
            spool_dir = "/tmp/rigsignal-spool"
            max_file_bytes = 1024
            max_file_age_secs = 30
            "#,
        )
        .unwrap();

        assert_eq!(cfg.output.mode, OutputMode::Spool);
        assert_eq!(cfg.output.spool_dir, PathBuf::from("/tmp/rigsignal-spool"));
        assert_eq!(cfg.output.max_file_bytes, 1024);
        assert_eq!(cfg.output.max_file_age_secs, 30);
    }
}
