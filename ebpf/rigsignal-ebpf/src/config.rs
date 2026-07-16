/// Configuration — reads the shared rigsignal.toml file used by the Python collector.
///
/// The [ebpf] section is new; all other sections are read-only from the daemon's
/// perspective (we only need elasticsearch.* credentials and endpoint).
use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::PathBuf;

#[derive(Debug, Deserialize)]
pub struct Config {
    pub elasticsearch: ElasticsearchConfig,
    #[serde(default)]
    pub ebpf: EbpfConfig,
}

#[derive(Debug, Deserialize)]
pub struct ElasticsearchConfig {
    pub endpoint: String,
    #[serde(default)]
    pub api_key: Option<String>,
    #[serde(default)]
    pub ca_cert: Option<PathBuf>,
}

#[derive(Debug, Deserialize)]
pub struct EbpfConfig {
    /// Path to the compiled BPF object file (rigsignal-ebpf-probes ELF).
    /// Defaults to the workspace-relative target path after `cargo xtask build-ebpf`.
    #[serde(default = "default_probe_path")]
    pub probe_path: PathBuf,

    /// Which probes to enable. Defaults to all Sprint-1 probes.
    #[serde(default = "default_enabled_probes")]
    #[allow(dead_code)]
    pub enabled_probes: Vec<String>,

    /// Aggregate and ship once per this many seconds.
    #[serde(default = "default_interval_s")]
    pub interval_s: u64,

    /// When true, emit system-wide scheduler/IO baseline metrics even when no
    /// game session is active (at reduced rate: 1 doc per 10 × interval_s).
    #[serde(default)]
    pub background_baseline: bool,
}

fn default_probe_path() -> PathBuf {
    // Walk up from the daemon binary to find the workspace root (the directory
    // that contains rigsignal-ebpf/ as a child).
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    let workspace = exe
        .ancestors()
        .find(|p| p.join("rigsignal-ebpf").exists())
        .unwrap_or_else(|| exe.parent().unwrap_or(std::path::Path::new(".")));

    // Match the BPF probe profile to the daemon profile so `cargo xtask build-ebpf`
    // (debug, default) and `cargo xtask build-ebpf --release` both work without
    // needing --probe-path.
    let profile = if cfg!(debug_assertions) {
        "debug"
    } else {
        "release"
    };
    workspace
        .join("target/bpfel-unknown-none")
        .join(profile)
        .join("rigsignal-ebpf-probes")
}

fn default_enabled_probes() -> Vec<String> {
    vec!["schedlatency".to_string()]
}

fn default_interval_s() -> u64 {
    1
}

impl Default for EbpfConfig {
    fn default() -> Self {
        EbpfConfig {
            probe_path: default_probe_path(),
            enabled_probes: default_enabled_probes(),
            interval_s: default_interval_s(),
            background_baseline: false,
        }
    }
}

impl Config {
    /// Load from the standard rigsignal.toml path.
    /// Search order:
    ///   1. $RIGSIGNAL_CONFIG env var
    ///   2. ~/.config/rigsignal/rigsignal.toml
    ///   3. /etc/rigsignal/rigsignal.toml
    pub fn load() -> Result<Self> {
        let path = if let Ok(env_path) = std::env::var("RIGSIGNAL_CONFIG") {
            PathBuf::from(env_path)
        } else if let Some(home) = dirs_or_home() {
            home.join(".config/rigsignal/rigsignal.toml")
        } else {
            PathBuf::from("/etc/rigsignal/rigsignal.toml")
        };

        Self::load_from(&path)
    }

    /// Apply ES_API_KEY and ES_URL env vars on top of whatever was in the TOML file.
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
        if let Ok(ca_cert) = std::env::var("ES_CA_CERT") {
            if !ca_cert.is_empty() {
                self.elasticsearch.ca_cert = Some(PathBuf::from(ca_cert));
            }
        }
    }

    pub fn load_from(path: &PathBuf) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading config file: {}", path.display()))?;
        let mut config: Self = toml::from_str(&text)
            .with_context(|| format!("parsing config file: {}", path.display()))?;
        config.apply_env_overrides();
        Ok(config)
    }
}

fn dirs_or_home() -> Option<PathBuf> {
    // When running via sudo, HOME is /root but the config lives in the
    // invoking user's home. Prefer SUDO_USER → /home/<user> over HOME.
    if let Ok(sudo_user) = std::env::var("SUDO_USER") {
        if !sudo_user.is_empty() {
            let path = PathBuf::from("/home").join(&sudo_user);
            if path.exists() {
                return Some(path);
            }
        }
    }
    std::env::var("HOME").ok().map(PathBuf::from)
}
