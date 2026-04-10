/// Elasticsearch bulk API shipper.
///
/// Matches the Python collector's shipper exactly:
///   - Authorization: ApiKey <key>
///   - Content-Type: application/x-ndjson
///   - Action line: {"create":{"_index":"metrics-gamepulse.<dataset>-default"}}
///   - Index naming: metrics-gamepulse.<dataset>-default
use crate::config::Config;
use anyhow::{Context, Result};
use reqwest::Client;
use serde_json::Value;
use tracing::{debug, info, warn};

pub struct ShipResult {
    pub attempted: usize,
    pub succeeded: usize,
    pub failed: usize,
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
        out.push(if chunk.len() > 1 { ALPHABET[((n >> 6) & 0x3f) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { ALPHABET[(n & 0x3f) as usize] as char } else { '=' });
    }
    out
}

/// GET the ES endpoint root. Returns Ok(()) on HTTP 200, Err otherwise.
/// Logs the cluster version string if present.
pub async fn ping(config: &Config) -> Result<()> {
    let client = build_client()?;
    let endpoint = config.elasticsearch.endpoint.trim_end_matches('/');
    let mut req = client.get(endpoint);
    if let Some(auth) = auth_header(config) {
        req = req.header("Authorization", auth);
    }
    let resp = req
        .send()
        .await
        .with_context(|| format!("connecting to Elasticsearch at {}", endpoint))?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("ES ping returned {}: {}", status, &body[..body.len().min(200)]);
    }

    let body: Value = resp.json().await.context("parsing ES ping response")?;
    if let Some(version) = body
        .get("version")
        .and_then(|v| v.get("number"))
        .and_then(|n| n.as_str())
    {
        info!("Elasticsearch reachable — version {}", version);
    } else {
        info!("Elasticsearch reachable");
    }
    Ok(())
}

/// POST docs to /_bulk. Each doc must contain a `data_stream.dataset` field
/// so the correct index can be derived.
///
/// Index name convention (matches Python collector):
///   metrics-gamepulse.<dataset>-default
pub async fn ship(config: &Config, docs: Vec<Value>) -> Result<ShipResult> {
    if docs.is_empty() {
        return Ok(ShipResult { attempted: 0, succeeded: 0, failed: 0 });
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
            .unwrap_or("gamepulse.unknown");
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
        anyhow::bail!("ES bulk returned {}: {}", status, &text[..text.len().min(500)]);
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
    Ok(ShipResult { attempted, succeeded, failed })
}
