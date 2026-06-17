/// Elasticsearch bulk API shipper.
///
/// Matches the Python collector's shipper exactly:
///   - Authorization: ApiKey <key>
///   - Content-Type: application/x-ndjson
///   - Action line: {"create":{"_index":"metrics-rigsignal.<dataset>-default"}}
///   - Index naming: metrics-rigsignal.<dataset>-default
use crate::config::Config;
use anyhow::{Context, Result};
use reqwest::Client;
use serde_json::Value;
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tracing::{debug, info, warn};

pub struct ShipResult {
    pub attempted: usize,
    pub succeeded: usize,
    pub failed: usize,
}

pub struct SpoolWriter {
    dir: PathBuf,
    max_file_bytes: u64,
    max_file_age: Duration,
    spools: HashMap<String, DatasetSpool>,
    next_seq: u32,
}

struct DatasetSpool {
    active_path: PathBuf,
    writer: BufWriter<File>,
    current_file_bytes: u64,
    current_file_started: Instant,
}

impl SpoolWriter {
    pub fn new(dir: impl AsRef<Path>, max_file_bytes: u64, max_file_age_secs: u64) -> Result<Self> {
        let dir = dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&dir)
            .with_context(|| format!("creating spool directory: {}", dir.display()))?;

        Ok(Self {
            dir,
            max_file_bytes,
            max_file_age: Duration::from_secs(max_file_age_secs),
            spools: HashMap::new(),
            next_seq: 1,
        })
    }

    pub fn write_docs(&mut self, docs: &[Value]) -> Result<()> {
        if docs.is_empty() {
            return Ok(());
        }

        let mut grouped: HashMap<String, Vec<&Value>> = HashMap::new();
        for doc in docs {
            let slug = doc
                .get("data_stream")
                .and_then(|ds| ds.get("dataset"))
                .and_then(|d| d.as_str())
                .map(dataset_slug)
                .unwrap_or_else(|| {
                    warn!("spool doc missing data_stream.dataset; routing to unknown");
                    "unknown".to_string()
                });
            grouped.entry(slug).or_default().push(doc);
        }

        for (slug, docs) in grouped {
            self.ensure_spool(&slug)?;
            for doc in docs {
                let line = serde_json::to_vec(doc).context("serialising spool doc")?;
                {
                    let spool = self
                        .spools
                        .get_mut(&slug)
                        .expect("dataset spool exists after ensure_spool");
                    spool.writer.write_all(&line).context("writing spool doc")?;
                    spool
                        .writer
                        .write_all(b"\n")
                        .context("writing spool newline")?;
                    spool.current_file_bytes += line.len() as u64 + 1;
                }
                self.rotate_if_needed(&slug)?;
            }
            self.spools
                .get_mut(&slug)
                .expect("dataset spool exists after writes")
                .writer
                .flush()
                .context("flushing spool writer")?;
        }
        Ok(())
    }

    fn ensure_spool(&mut self, slug: &str) -> Result<()> {
        if !self.spools.contains_key(slug) {
            self.spools
                .insert(slug.to_string(), DatasetSpool::new(&self.dir, slug)?);
        }
        Ok(())
    }

    fn rotate_if_needed(&mut self, slug: &str) -> Result<()> {
        let spool = self
            .spools
            .get(slug)
            .expect("dataset spool exists before rotation check");
        let size_exceeded =
            self.max_file_bytes > 0 && spool.current_file_bytes > self.max_file_bytes;
        let age_exceeded = self.max_file_age.as_secs() > 0
            && spool.current_file_started.elapsed() >= self.max_file_age;
        if size_exceeded || age_exceeded {
            self.rotate(slug)?;
        }
        Ok(())
    }

    fn rotate(&mut self, slug: &str) -> Result<()> {
        let final_path = {
            let spool = self
                .spools
                .get_mut(slug)
                .expect("dataset spool exists before rotation");
            spool
                .writer
                .flush()
                .context("flushing spool file before rotation")?;
            if spool.current_file_bytes == 0 {
                None
            } else {
                Some((
                    spool.active_path.clone(),
                    self.dir.join(format!(
                        "rigsignal-{}-{}-{}.ndjson",
                        slug,
                        unix_millis()?,
                        self.next_seq
                    )),
                ))
            }
        };

        if let Some((active_path, final_path)) = final_path {
            std::fs::rename(&active_path, &final_path).with_context(|| {
                format!(
                    "rotating spool file {} to {}",
                    active_path.display(),
                    final_path.display()
                )
            })?;
            self.next_seq = self.next_seq.saturating_add(1);
        }

        self.spools
            .insert(slug.to_string(), DatasetSpool::new(&self.dir, slug)?);
        Ok(())
    }
}

impl DatasetSpool {
    fn new(dir: &Path, slug: &str) -> Result<Self> {
        let active_path = dir.join(format!("rigsignal-{}.ndjson.tmp", slug));
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&active_path)
            .with_context(|| format!("opening active spool file: {}", active_path.display()))?;

        Ok(Self {
            active_path,
            writer: BufWriter::new(file),
            current_file_bytes: 0,
            current_file_started: Instant::now(),
        })
    }
}

fn dataset_slug(dataset: &str) -> String {
    dataset.rsplit('.').next().unwrap_or(dataset).to_string()
}

fn unix_millis() -> Result<u128> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before Unix epoch")?
        .as_millis())
}

fn build_client() -> Result<Client> {
    Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .context("building HTTP client")
}

fn auth_header(config: &Config) -> Option<String> {
    let es = &config.elasticsearch;
    if let Some(key) = &es.api_key {
        Some(format!("ApiKey {}", key))
    } else if let (Some(user), Some(pass)) = (&es.username, &es.password) {
        // Base64-encode "user:pass" for Basic auth.
        // Using the alphabet directly to avoid adding the base64 crate.
        let creds = format!("{}:{}", user, pass);
        let encoded = encode_base64(creds.as_bytes());
        Some(format!("Basic {}", encoded))
    } else {
        None
    }
}

fn encode_base64(input: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((input.len() + 2) / 3 * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(n >> 18) as usize] as char);
        out.push(ALPHABET[((n >> 12) & 0x3f) as usize] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[((n >> 6) & 0x3f) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[(n & 0x3f) as usize] as char
        } else {
            '='
        });
    }
    out
}

/// GET /_cluster/health to verify connectivity. Returns Ok(()) on HTTP 2xx/4xx, Err on
/// network failure. Uses the same endpoint as `rigsignal setup` so the API key
/// privileges required are identical (cluster:monitor/health, not cluster:monitor/main).
pub async fn ping(config: &Config) -> Result<()> {
    let client = build_client()?;
    let endpoint = config.elasticsearch.endpoint.trim_end_matches('/');
    let url = format!("{}/_cluster/health", endpoint);
    let mut req = client.get(&url);
    if let Some(auth) = auth_header(config) {
        req = req.header("Authorization", auth);
    }
    let resp = req
        .send()
        .await
        .with_context(|| format!("connecting to Elasticsearch at {}", endpoint))?;

    let status = resp.status();
    // 401 = wrong key (endpoint alive). 403 = key exists but missing privilege.
    // Only treat network-level failures as fatal; auth issues surface at bulk time.
    if status.is_server_error() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!(
            "ES ping returned {}: {}",
            status,
            &body[..body.len().min(200)]
        );
    }

    if status.is_success() {
        let body: Value = resp.json().await.unwrap_or_default();
        let cluster_status = body
            .get("status")
            .and_then(|s| s.as_str())
            .unwrap_or("unknown");
        info!(
            "Elasticsearch reachable — cluster status: {}",
            cluster_status
        );
    } else {
        info!("Elasticsearch reachable (HTTP {})", status);
    }
    Ok(())
}

/// POST docs to /_bulk. Each doc must contain a `data_stream.dataset` field
/// so the correct index can be derived.
///
/// Index name convention (matches Python collector):
///   metrics-rigsignal.<dataset>-default
pub async fn ship(config: &Config, docs: Vec<Value>) -> Result<ShipResult> {
    if docs.is_empty() {
        return Ok(ShipResult {
            attempted: 0,
            succeeded: 0,
            failed: 0,
        });
    }

    let attempted = docs.len();
    let client = build_client()?;
    let endpoint = format!(
        "{}/_bulk",
        config.elasticsearch.endpoint.trim_end_matches('/')
    );

    let mut body = String::with_capacity(attempted * 256);
    for doc in &docs {
        let dataset = doc
            .get("data_stream")
            .and_then(|ds| ds.get("dataset"))
            .and_then(|d| d.as_str())
            .unwrap_or("rigsignal.unknown");
        let index = format!("metrics-{}-default", dataset);
        let action = serde_json::json!({"create": {"_index": index}});
        body.push_str(&serde_json::to_string(&action).context("serialising action line")?);
        body.push('\n');
        body.push_str(&serde_json::to_string(doc).context("serialising doc")?);
        body.push('\n');
    }

    let mut req = client
        .post(&endpoint)
        .header("Content-Type", "application/x-ndjson")
        .body(body);
    if let Some(auth) = auth_header(config) {
        req = req.header("Authorization", auth);
    }

    let resp = req.send().await.context("sending bulk request")?;
    let status = resp.status();
    if !status.is_success() {
        let text = resp.text().await.unwrap_or_default();
        anyhow::bail!(
            "ES bulk returned {}: {}",
            status,
            &text[..text.len().min(500)]
        );
    }

    let resp_body: Value = resp.json().await.context("parsing bulk response")?;
    let has_errors = resp_body
        .get("errors")
        .and_then(|e| e.as_bool())
        .unwrap_or(false);

    let mut failed = 0usize;
    if has_errors {
        if let Some(items) = resp_body.get("items").and_then(|i| i.as_array()) {
            for item in items {
                for action in ["create", "index"] {
                    if let Some(err) = item.get(action).and_then(|a| a.get("error")) {
                        warn!("bulk item error: {}", err);
                        failed += 1;
                        break;
                    }
                }
            }
        }
    }

    let succeeded = attempted - failed;
    debug!("shipped {}/{} docs", succeeded, attempted);
    Ok(ShipResult {
        attempted,
        succeeded,
        failed,
    })
}

/// Request an immediate transform sync via POST /_transform/{id}/_schedule_now.
///
/// Called after shipping the session summary document so the Games dashboard
/// updates within seconds rather than waiting up to 60 s for the next scheduled
/// sync. Failures are logged at WARN level and never propagate to the caller —
/// this is a best-effort optimisation, not part of the critical shipping path.
pub async fn trigger_transform_sync(config: &Config, transform_id: &str) -> Result<()> {
    let client = build_client()?;
    let endpoint = format!(
        "{}/_transform/{}/_schedule_now",
        config.elasticsearch.endpoint.trim_end_matches('/'),
        transform_id,
    );
    let mut req = client.post(&endpoint);
    if let Some(auth) = auth_header(config) {
        req = req.header("Authorization", auth);
    }
    let resp = req.send().await.context("sending transform schedule_now")?;
    let status = resp.status();
    if status.is_success() {
        debug!("transform '{}' schedule_now accepted", transform_id);
    } else {
        let body = resp.text().await.unwrap_or_default();
        warn!(
            "transform '{}' schedule_now returned {}: {}",
            transform_id,
            status,
            &body[..body.len().min(200)]
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;

    fn temp_spool_dir(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "rigsignal-{}-{}-{}",
            name,
            std::process::id(),
            unix_millis().expect("system clock should be after Unix epoch")
        ))
    }

    #[test]
    fn dataset_slug_uses_last_dot_segment() {
        assert_eq!(dataset_slug("rigsignal.frame"), "frame");
        assert_eq!(dataset_slug("rigsignal.ebpf_thread"), "ebpf_thread");
    }

    #[test]
    fn write_docs_creates_per_dataset_spool_files() -> Result<()> {
        let dir = temp_spool_dir("mixed-dataset-spool");
        let mut writer = SpoolWriter::new(&dir, 0, 0)?;
        let docs = vec![
            json!({
                "data_stream": { "dataset": "rigsignal.frame" },
                "rigsignal": { "frame": { "fps": 60.0 } }
            }),
            json!({
                "data_stream": { "dataset": "rigsignal.ebpf_thread" },
                "rigsignal": { "ebpf_thread": { "pid": 1234 } }
            }),
            json!({
                "data_stream": { "dataset": "rigsignal.frame" },
                "rigsignal": { "frame": { "fps": 59.5 } }
            }),
            json!({
                "rigsignal": { "unknown": true }
            }),
        ];

        writer.write_docs(&docs)?;

        let frame_path = dir.join("rigsignal-frame.ndjson.tmp");
        let ebpf_path = dir.join("rigsignal-ebpf_thread.ndjson.tmp");
        let unknown_path = dir.join("rigsignal-unknown.ndjson.tmp");
        assert!(frame_path.exists());
        assert!(ebpf_path.exists());
        assert!(unknown_path.exists());

        let frame_lines = fs::read_to_string(&frame_path)?;
        let ebpf_lines = fs::read_to_string(&ebpf_path)?;
        let unknown_lines = fs::read_to_string(&unknown_path)?;
        assert_eq!(frame_lines.lines().count(), 2);
        assert_eq!(ebpf_lines.lines().count(), 1);
        assert_eq!(unknown_lines.lines().count(), 1);
        assert!(frame_lines.contains("\"fps\":60.0"));
        assert!(frame_lines.contains("\"fps\":59.5"));
        assert!(ebpf_lines.contains("\"pid\":1234"));
        assert!(unknown_lines.contains("\"unknown\":true"));

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }
}
