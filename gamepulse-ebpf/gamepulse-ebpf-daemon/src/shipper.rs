/// Elasticsearch bulk API shipper.
///
/// Batches EbpfMetricDoc documents and POSTs them to the ES bulk endpoint.
/// Uses the same API key and endpoint as the Python collector.
use crate::es_model::EbpfMetricDoc;
use anyhow::{Context, Result};
use reqwest::Client;
use tracing::{debug, error, warn};

pub struct EsShipper {
    client: Client,
    endpoint: String,
    api_key: String,
    /// Pending documents awaiting the next flush.
    pending: Vec<EbpfMetricDoc>,
    /// How many docs to accumulate before forcing a flush.
    batch_size: usize,
}

impl EsShipper {
    pub fn new(endpoint: &str, api_key: &str) -> Result<Self> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .context("building HTTP client")?;

        // The ES bulk endpoint for the eBPF data stream.
        let endpoint = format!("{}/metrics-gamepulse.ebpf-default/_bulk", endpoint.trim_end_matches('/'));

        Ok(EsShipper {
            client,
            endpoint,
            api_key: api_key.to_string(),
            pending: Vec::new(),
            batch_size: 100,
        })
    }

    /// Queue a document for the next flush.
    pub fn queue(&mut self, doc: EbpfMetricDoc) {
        self.pending.push(doc);
    }

    /// Queue multiple documents.
    pub fn queue_all(&mut self, docs: Vec<EbpfMetricDoc>) {
        self.pending.extend(docs);
    }

    /// Flush all pending documents to Elasticsearch.
    /// No-op if nothing is queued.
    pub async fn flush(&mut self) -> Result<()> {
        if self.pending.is_empty() {
            return Ok(());
        }

        let docs = std::mem::take(&mut self.pending);
        let count = docs.len();
        debug!("flushing {} eBPF docs to ES", count);

        let body = build_bulk_body(&docs)?;

        let resp = self
            .client
            .post(&self.endpoint)
            .header("Content-Type", "application/x-ndjson")
            .header("Authorization", format!("ApiKey {}", self.api_key))
            .body(body)
            .send()
            .await
            .context("sending bulk request")?;

        let status = resp.status();
        if !status.is_success() {
            let text = resp.text().await.unwrap_or_default();
            error!("ES bulk API returned {}: {}", status, &text[..text.len().min(500)]);
            // Docs are dropped on error — eBPF telemetry is best-effort.
            return Ok(());
        }

        // Check for per-item errors in the bulk response.
        let resp_body: serde_json::Value = resp.json().await.context("parsing bulk response")?;
        if resp_body.get("errors").and_then(|e| e.as_bool()).unwrap_or(false) {
            if let Some(items) = resp_body.get("items").and_then(|i| i.as_array()) {
                let error_count = items
                    .iter()
                    .filter(|item| {
                        item.get("index")
                            .and_then(|idx| idx.get("error"))
                            .is_some()
                    })
                    .count();
                warn!("{}/{} docs had bulk errors", error_count, count);
            }
        }

        Ok(())
    }
}

fn build_bulk_body(docs: &[EbpfMetricDoc]) -> Result<String> {
    let mut body = String::with_capacity(docs.len() * 256);
    // Action line — create in the data stream (no explicit _id, ES generates one)
    let action = r#"{"create":{}}"#;
    for doc in docs {
        body.push_str(action);
        body.push('\n');
        let doc_json = serde_json::to_string(doc).context("serialising doc")?;
        body.push_str(&doc_json);
        body.push('\n');
    }
    Ok(body)
}
