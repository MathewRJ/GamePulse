/// Elasticsearch bulk API shipper.
///
/// Batches eBPF documents and POSTs them to the ES bulk endpoint.
/// Uses the same API key and endpoint as the Python collector.
use crate::es_model::EbpfDocument;
use anyhow::{Context, Result};
use reqwest::Client;
use tracing::{debug, error, warn};

pub struct EsShipper {
    client: Client,
    endpoint: String,
    api_key: String,
    /// Pending documents awaiting the next flush.
    pending: Vec<EbpfDocument>,
    /// How many docs to accumulate before forcing a flush.
    #[allow(dead_code)]
    batch_size: usize,
}

impl EsShipper {
    pub fn new(endpoint: &str, api_key: &str) -> Result<Self> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .context("building HTTP client")?;

        let endpoint = format!("{}/_bulk", endpoint.trim_end_matches('/'));

        Ok(EsShipper {
            client,
            endpoint,
            api_key: api_key.to_string(),
            pending: Vec::new(),
            batch_size: 100,
        })
    }

    /// Queue a document for the next flush.
    #[allow(dead_code)]
    pub fn queue(&mut self, doc: EbpfDocument) {
        self.pending.push(doc);
    }

    /// Queue multiple documents.
    pub fn queue_all(&mut self, docs: Vec<EbpfDocument>) {
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
            let preview: String = text.chars().take(500).collect();
            error!("ES bulk API returned {}: {}", status, preview);
            // Docs are dropped on error — eBPF telemetry is best-effort.
            return Ok(());
        }

        // Check for per-item errors in the bulk response.
        let resp_body: serde_json::Value = resp.json().await.context("parsing bulk response")?;
        if resp_body
            .get("errors")
            .and_then(|e| e.as_bool())
            .unwrap_or(false)
        {
            if let Some(items) = resp_body.get("items").and_then(|i| i.as_array()) {
                let (conflict_count, real_error_count, first_reason) = items.iter().fold(
                    (0usize, 0usize, None::<&str>),
                    |(conflicts, errors, reason), item| {
                        let Some(op) = item.get("create") else {
                            return (conflicts, errors, reason);
                        };
                        let Some(err) = op.get("error") else {
                            return (conflicts, errors, reason);
                        };
                        let is_conflict = err
                            .get("type")
                            .and_then(|t| t.as_str())
                            == Some("version_conflict_engine_exception");
                        if is_conflict {
                            (conflicts + 1, errors, reason)
                        } else {
                            let r = reason.or_else(|| {
                                err.get("reason").and_then(|r| r.as_str())
                            });
                            (conflicts, errors + 1, r)
                        }
                    },
                );
                if conflict_count > 0 {
                    debug!(
                        "{}/{} docs skipped — duplicate TSDB id (version conflict)",
                        conflict_count, count
                    );
                }
                if real_error_count > 0 {
                    if let Some(reason) = first_reason {
                        warn!(
                            "{}/{} docs had bulk errors; first reason: {}",
                            real_error_count, count, reason
                        );
                    } else {
                        warn!("{}/{} docs had bulk errors", real_error_count, count);
                    }
                }
            }
        }

        Ok(())
    }
}

fn build_bulk_body(docs: &[EbpfDocument]) -> Result<String> {
    let mut body = String::with_capacity(docs.len() * 256);
    for doc in docs {
        let action = format!(r#"{{"create":{{"_index":"{}"}}}}"#, doc.index());
        body.push_str(&action);
        body.push('\n');
        let doc_json = match doc {
            EbpfDocument::Metric(metric) => serde_json::to_string(metric),
            EbpfDocument::Thread(thread) => serde_json::to_string(thread),
        }
        .context("serialising doc")?;
        body.push_str(&doc_json);
        body.push('\n');
    }
    Ok(body)
}
