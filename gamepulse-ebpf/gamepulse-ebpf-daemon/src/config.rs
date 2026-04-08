/// Configuration — reads the shared gamepulse.toml file used by the Python collector.
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
    pub url: String,
    pub api_key: String,
}

#[derive(Debug, Deserialize)]
pub struct EbpfConfig {
    /// Path to the compiled BPF object file (gamepulse-ebpf-probes ELF).
    /// Defaults to the workspace-relative target path after `cargo xtask build-ebpf`.
    #[serde(default = "default_probe_path")]
    pub probe_path: PathBuf,

    /// Which probes to enable. Defaults to all Sprint-1 probes.
    #[serde(default = "default_enabled_probes")]
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
    // Resolve relative to the location of the compiled daemon binary.
    // After `cargo xtask build-ebpf`, the ELF lands at:
    //   <workspace>/target/bpfel-unknown-none/release/gamepulse-ebpf-probes
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    // exe is typically <workspace>/target/release/gamepulse-ebpf
    // Walk up to workspace root and resolve the BPF target path.
    let workspace = exe
        .ancestors()
        .find(|p| p.join("Cargo.toml").exists())
        .unwrap_or_else(|| exe.parent().unwrap_or(std::path::Path::new(".")));
    workspace
        .join("target/bpfel-unknown-none/release/gamepulse-ebpf-probes")
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
    /// Load from the standard gamepulse.toml path.
    /// Search order:
    ///   1. $GAMEPULSE_CONFIG env var
    ///   2. ~/.config/gamepulse/gamepulse.toml
    ///   3. /etc/gamepulse/gamepulse.toml
    pub fn load() -> Result<Self> {
        let path = if let Ok(env_path) = std::env::var("GAMEPULSE_CONFIG") {
            PathBuf::from(env_path)
        } else if let Some(home) = dirs_or_home() {
            home.join(".config/gamepulse/gamepulse.toml")
        } else {
            PathBuf::from("/etc/gamepulse/gamepulse.toml")
        };

        Self::load_from(&path)
    }

    pub fn load_from(path: &PathBuf) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading config file: {}", path.display()))?;
        toml::from_str(&text).with_context(|| format!("parsing config file: {}", path.display()))
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
