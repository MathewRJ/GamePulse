//! Durable Steam Remote Play connection-log tailer.
//!
//! This is intentionally main-loop owned: unlike a periodic collector it must
//! retain a source offset until Elasticsearch acknowledges the corresponding
//! keyed bulk create.

use anyhow::{Context, Result};
use chrono::{DateTime, Local, LocalResult, NaiveDateTime, TimeZone, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use tracing::{debug, warn};

const MAX_LINE_BYTES: usize = 64 * 1024;
const MAX_BATCH_LINES: usize = 100;
const MAX_BATCH_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TailToken {
    dev: u64,
    ino: u64,
    start: u64,
    end: u64,
}

#[derive(Debug, Clone)]
pub struct EventEnvelope {
    pub document: Value,
    pub id: String,
    pub token: TailToken,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct TailState {
    path: PathBuf,
    dev: u64,
    ino: u64,
    offset: u64,
}

pub struct RemoteConnectionsTailer {
    log_path: PathBuf,
    state_path: PathBuf,
    hostname: String,
    state: Option<TailState>,
    inflight: Option<TailToken>,
    oversize_warned: bool,
}

impl RemoteConnectionsTailer {
    pub fn new(hostname: String) -> Result<Self> {
        let home = env::var_os("HOME").map(PathBuf::from);
        let log_path = home
            .as_ref()
            .map(|home| home.join(".local/share/Steam/logs/remote_connections.txt"))
            .unwrap_or_else(|| PathBuf::from("/var/lib/rigsignal/remote_connections.txt"));
        let state_path = state_path(home.as_deref());
        Self::with_paths(hostname, log_path, state_path)
    }

    fn with_paths(hostname: String, log_path: PathBuf, state_path: PathBuf) -> Result<Self> {
        let state = match fs::read(&state_path) {
            Ok(bytes) => Some(serde_json::from_slice(&bytes).context("parsing stream tail state")?),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                return Err(error).with_context(|| format!("reading {}", state_path.display()))
            }
        };
        Ok(Self {
            log_path,
            state_path,
            hostname,
            state,
            inflight: None,
            oversize_warned: false,
        })
    }

    /// Returns at most one unacknowledged contiguous batch. A later batch is
    /// never exposed until `ack_success` atomically commits this one.
    pub fn poll(&mut self, session: &crate::session::SessionManager) -> Result<Vec<EventEnvelope>> {
        if self.inflight.is_some() {
            return Ok(Vec::new());
        }

        let current = match generation(&self.log_path)? {
            Some(generation) => generation,
            None => {
                debug!(path = %self.log_path.display(), "remote_connections log is not present yet");
                return Ok(Vec::new());
            }
        };

        if self.state.is_none() {
            self.state = Some(TailState {
                path: self.log_path.clone(),
                dev: current.dev,
                ino: current.ino,
                offset: current.len,
            });
            self.persist()?;
            debug!(
                offset = current.len,
                "initialised remote_connections tail at EOF"
            );
            return Ok(Vec::new());
        }

        let mut state = self.state.clone().expect("state initialised above");
        if state.dev == current.dev && state.ino == current.ino && current.len < state.offset {
            warn!(
                old_offset = state.offset,
                new_len = current.len,
                "remote_connections log was truncated; restarting at zero"
            );
            state.offset = 0;
            self.state = Some(state.clone());
            self.persist()?;
        }

        let source = if state.dev == current.dev && state.ino == current.ino {
            Generation {
                path: self.log_path.clone(),
                ..current
            }
        } else if let Some(rotated) = self.find_rotated(state.dev, state.ino)? {
            rotated
        } else {
            warn!(
                saved_dev = state.dev,
                saved_ino = state.ino,
                "remote_connections rotation gap; starting current generation at zero"
            );
            self.state = Some(TailState {
                path: self.log_path.clone(),
                dev: current.dev,
                ino: current.ino,
                offset: 0,
            });
            self.persist()?;
            current.clone()
        };
        let state = self
            .state
            .clone()
            .expect("state set before source selection");

        // The old generation is fully drained. Change generation before reading
        // the new path, but only after its committed offset reached EOF.
        if source.dev != current.dev || source.ino != current.ino {
            if source.len <= state.offset {
                self.state = Some(TailState {
                    path: self.log_path.clone(),
                    dev: current.dev,
                    ino: current.ino,
                    offset: 0,
                });
                self.persist()?;
                return self.poll(session);
            }
        }

        self.read_batch(&source, &state, session)
    }

    pub fn ack_success(&mut self, token: &TailToken) -> Result<()> {
        let Some(inflight) = self.inflight.as_ref() else {
            anyhow::bail!("remote_connections acknowledgement without an inflight batch");
        };
        if inflight != token {
            anyhow::bail!("remote_connections acknowledgement is out of order");
        }
        let state = self.state.as_mut().expect("inflight requires state");
        if state.dev != token.dev || state.ino != token.ino || state.offset != token.start {
            anyhow::bail!(
                "remote_connections acknowledgement does not match checkpoint generation"
            );
        }
        let previous_offset = state.offset;
        state.offset = token.end;
        if let Err(error) = self.persist() {
            self.state
                .as_mut()
                .expect("state exists while acknowledgement is inflight")
                .offset = previous_offset;
            return Err(error);
        }
        self.inflight = None;
        Ok(())
    }

    /// Releases the current batch for retry without changing its durable
    /// checkpoint. The next `poll` will therefore re-read the same byte range.
    pub fn nack(&mut self) {
        self.inflight = None;
    }

    fn read_batch(
        &mut self,
        source: &Generation,
        state: &TailState,
        session: &crate::session::SessionManager,
    ) -> Result<Vec<EventEnvelope>> {
        let mut file = File::open(&source.path)
            .with_context(|| format!("opening {}", source.path.display()))?;
        file.seek(SeekFrom::Start(state.offset))?;
        // Enough for a full regular batch plus one capped complete line. If no
        // newline is present after this amount it remains an incomplete line.
        let mut bytes = vec![0; MAX_BATCH_BYTES + MAX_LINE_BYTES + 1];
        let read = file.read(&mut bytes)?;
        bytes.truncate(read);

        let mut cursor = 0usize;
        let mut lines = 0usize;
        let mut envelopes = Vec::new();
        let mut batch_end = state.offset;
        while cursor < bytes.len() {
            let Some(relative_end) = bytes[cursor..].iter().position(|byte| *byte == b'\n') else {
                break; // incomplete trailing line: do not checkpoint it
            };
            let end = cursor + relative_end;
            let raw = &bytes[cursor..end];
            let next = end + 1;
            if lines == MAX_BATCH_LINES || (next > MAX_BATCH_BYTES && lines > 0) {
                break;
            }
            lines += 1;
            batch_end = state.offset + next as u64;
            if raw.len() > MAX_LINE_BYTES {
                if !self.oversize_warned {
                    warn!(
                        bytes = raw.len(),
                        "remote_connections complete line exceeds 64 KiB; consuming it"
                    );
                    self.oversize_warned = true;
                }
            } else if let Some(document) = parse_document(raw, &self.hostname, session) {
                let id = line_identity(
                    &self.hostname,
                    source.dev,
                    source.ino,
                    state.offset + cursor as u64,
                    raw,
                );
                envelopes.push(EventEnvelope {
                    document,
                    id,
                    token: TailToken {
                        dev: source.dev,
                        ino: source.ino,
                        start: state.offset,
                        end: 0,
                    },
                });
            }
            cursor = next;
        }

        if lines == 0 {
            return Ok(Vec::new());
        }
        let token = TailToken {
            dev: source.dev,
            ino: source.ino,
            start: state.offset,
            end: batch_end,
        };
        for envelope in &mut envelopes {
            envelope.token = token.clone();
        }
        if envelopes.is_empty() {
            // Malformed/oversize lines are consumed source records and do not
            // require a network acknowledgement.
            let state = self.state.as_mut().expect("state exists");
            let previous_offset = state.offset;
            state.offset = batch_end;
            if let Err(error) = self.persist() {
                self.state.as_mut().expect("state exists").offset = previous_offset;
                return Err(error);
            }
        } else {
            self.inflight = Some(token);
        }
        Ok(envelopes)
    }

    fn find_rotated(&self, dev: u64, ino: u64) -> Result<Option<Generation>> {
        let Some(parent) = self.log_path.parent() else {
            return Ok(None);
        };
        let Some(name) = self.log_path.file_name().and_then(|name| name.to_str()) else {
            return Ok(None);
        };
        for entry in
            fs::read_dir(parent).with_context(|| format!("reading {}", parent.display()))?
        {
            let entry = entry?;
            let candidate = entry.path();
            let matches_name = candidate
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|value| value.starts_with(&format!("{name}.")));
            if !matches_name || !entry.file_type()?.is_file() {
                continue;
            }
            if let Some(generation) = generation(&candidate)? {
                if generation.dev == dev && generation.ino == ino {
                    return Ok(Some(generation));
                }
            }
        }
        Ok(None)
    }

    fn persist(&self) -> Result<()> {
        let state = self
            .state
            .as_ref()
            .context("persisting absent stream tail state")?;
        let dir = self
            .state_path
            .parent()
            .context("stream tail state has no parent")?;
        fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
        let tmp = dir.join(format!(".stream-client-tail-{}.tmp", std::process::id()));
        let data = serde_json::to_vec(state)?;
        let mut file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .mode(0o600)
            .open(&tmp)?;
        file.write_all(&data)?;
        file.sync_all()?;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600))?;
        fs::rename(&tmp, &self.state_path)?;
        File::open(dir)?.sync_all()?;
        Ok(())
    }
}

#[derive(Clone)]
struct Generation {
    path: PathBuf,
    dev: u64,
    ino: u64,
    len: u64,
}

fn generation(path: &Path) -> Result<Option<Generation>> {
    match fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => Ok(Some(Generation {
            path: path.to_path_buf(),
            dev: metadata.dev(),
            ino: metadata.ino(),
            len: metadata.len(),
        })),
        Ok(_) => Ok(None),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("stating {}", path.display())),
    }
}

fn state_path(home: Option<&Path>) -> PathBuf {
    if let Some(state_home) = env::var_os("XDG_STATE_HOME") {
        PathBuf::from(state_home).join("rigsignal/stream-client-tail.json")
    } else if let Some(home) = home {
        home.join(".local/state/rigsignal/stream-client-tail.json")
    } else {
        PathBuf::from("/var/lib/rigsignal/stream-client-tail.json")
    }
}

fn line_identity(host: &str, dev: u64, ino: u64, offset: u64, raw: &[u8]) -> String {
    let mut source = Vec::with_capacity(host.len() + raw.len() + 32);
    source.extend_from_slice(host.as_bytes());
    source.extend_from_slice(&dev.to_ne_bytes());
    source.extend_from_slice(&ino.to_ne_bytes());
    source.extend_from_slice(&offset.to_ne_bytes());
    source.extend_from_slice(raw);
    sha256_hex(&source)
}

fn parse_document(
    raw: &[u8],
    hostname: &str,
    session: &crate::session::SessionManager,
) -> Option<Value> {
    parse_document_in_timezone(raw, hostname, session, &Local)
}

fn parse_document_in_timezone<Tz: TimeZone>(
    raw: &[u8],
    hostname: &str,
    session: &crate::session::SessionManager,
    timezone: &Tz,
) -> Option<Value>
where
    Tz::Offset: std::fmt::Display,
{
    let line = String::from_utf8_lossy(raw);
    let line = line.trim_end_matches('\r');
    let parsed = match parse_fields(line) {
        Some(parsed) => parsed,
        None => {
            debug!("unrecognised remote_connections line consumed");
            return None;
        }
    };
    let local = match NaiveDateTime::parse_from_str(parsed.timestamp, "%Y-%m-%d %H:%M:%S") {
        Ok(value) => value,
        Err(error) => {
            warn!(%error, "malformed remote_connections timestamp consumed");
            return None;
        }
    };
    let timestamp: DateTime<Utc> = match timezone.from_local_datetime(&local) {
        LocalResult::Single(value) => value.with_timezone(&Utc),
        LocalResult::Ambiguous(first, second) => {
            warn!(timestamp = %local, "ambiguous remote_connections local timestamp; choosing earlier UTC instant");
            earlier_utc(first, second)
        }
        LocalResult::None => {
            warn!(timestamp = %local, "nonexistent remote_connections local timestamp consumed");
            return None;
        }
    };
    let transition = parsed.transition;
    let event_type = if transition == "connected" {
        json!(["connection", "start"])
    } else {
        json!(["connection", "end"])
    };
    let mut client = serde_json::Map::new();
    client.insert("event".to_string(), Value::String(transition.to_string()));
    if let Some(via) = parsed.via.filter(|via| !via.is_empty()) {
        let transport = match via {
            "direct connection" => "direct",
            "relay connection" => "relay",
            _ => "unknown",
        };
        client.insert(
            "transport".to_string(),
            Value::String(transport.to_string()),
        );
    }
    client.insert(
        "peer".to_string(),
        json!({"id": parsed.peer_id, "name": parsed.peer_name}),
    );
    let mut rigsignal = serde_json::Map::new();
    rigsignal.insert("stream".to_string(), json!({"client": client}));
    if let Some(game) = session.current_game.as_ref() {
        let mut session_doc = serde_json::Map::new();
        session_doc.insert("id".to_string(), Value::String(session.session_id.clone()));
        if let Some(label) = &session.label {
            session_doc.insert("label".to_string(), Value::String(label.clone()));
        }
        session_doc.insert(
            "agent_version".to_string(),
            Value::String(env!("CARGO_PKG_VERSION").to_string()),
        );
        rigsignal.insert("session".to_string(), Value::Object(session_doc));
        rigsignal.insert(
            "game".to_string(),
            Value::Object(crate::session::target_to_game_doc(game)),
        );
    }
    Some(json!({
        "@timestamp": timestamp.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        "data_stream": {"type": "logs", "dataset": "rigsignal.events", "namespace": "default"},
        "host": {"name": hostname},
        "event": {"kind": "event", "category": ["network"], "type": event_type},
        "rigsignal": rigsignal,
    }))
}

struct ParsedFields<'a> {
    timestamp: &'a str,
    peer_id: &'a str,
    peer_name: &'a str,
    transition: &'a str,
    via: Option<&'a str>,
}

fn parse_fields(line: &str) -> Option<ParsedFields<'_>> {
    let timestamp_end = line.find("] Client ")?;
    let timestamp = line.get(1..timestamp_end)?;
    let rest = line.get(timestamp_end + "] Client ".len()..)?;
    let peer_start = rest.find(" (")?;
    let peer_id = rest.get(..peer_start)?;
    if peer_id.is_empty() || !peer_id.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let rest = rest.get(peer_start + 2..)?;
    let peer_end = rest.find(") ")?;
    let peer_name = rest.get(..peer_end)?;
    let rest = rest.get(peer_end + 2..)?;
    let (transition, via) = match rest.split_once(" via ") {
        Some((transition, via)) if !via.is_empty() => (transition, Some(via)),
        Some(_) => return None,
        // Steam appends a reason after a colon on disconnects (live-observed:
        // "disconnected: disconnecting all"); strip it, it is not a transport.
        None => {
            let transition = rest.split_once(':').map(|(t, _)| t).unwrap_or(rest);
            (transition.trim_end(), None)
        }
    };
    if !matches!(transition, "connected" | "disconnected") {
        return None;
    }
    Some(ParsedFields {
        timestamp,
        peer_id,
        peer_name,
        transition,
        via,
    })
}

fn earlier_utc<Tz: TimeZone>(first: DateTime<Tz>, second: DateTime<Tz>) -> DateTime<Utc> {
    let first = first.with_timezone(&Utc);
    let second = second.with_timezone(&Utc);
    if first <= second {
        first
    } else {
        second
    }
}

/// Small self-contained SHA-256 implementation so the agent's durable identity
/// path does not add a network-fetched dependency to this packaged binary.
fn sha256_hex(input: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut bytes = input.to_vec();
    let bit_len = (bytes.len() as u64).wrapping_mul(8);
    bytes.push(0x80);
    while bytes.len() % 64 != 56 {
        bytes.push(0);
    }
    bytes.extend_from_slice(&bit_len.to_be_bytes());
    let mut h = [
        0x6a09e667_u32,
        0xbb67ae85,
        0x3c6ef372,
        0xa54ff53a,
        0x510e527f,
        0x9b05688c,
        0x1f83d9ab,
        0x5be0cd19,
    ];
    for chunk in bytes.chunks_exact(64) {
        let mut w = [0_u32; 64];
        for (i, word) in w[..16].iter_mut().enumerate() {
            *word = u32::from_be_bytes(chunk[i * 4..i * 4 + 4].try_into().unwrap());
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }
    h.iter().map(|word| format!("{word:08x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::FixedOffset;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "rigsignal-tail-{name}-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }
    fn session() -> crate::session::SessionManager {
        crate::session::SessionManager::new()
    }
    fn line() -> &'static [u8] {
        b"[2026-07-17 09:09:44] Client 10364467328988576325 (GamingPC) connected via direct connection"
    }

    #[test]
    fn parser_accepts_live_disconnect_reason_suffix() {
        // Exact line observed live on StreamClient 2026-07-18: Steam appends a
        // colon-separated reason and no `via` phrase on disconnect.
        let doc = parse_document_in_timezone(
            b"[2026-07-18 09:08:11] Client 10364467328988576325 (GamingPC) disconnected: disconnecting all",
            "StreamClient",
            &session(),
            &Utc,
        )
        .unwrap();
        assert_eq!(doc["event"]["type"], json!(["connection", "end"]));
        assert_eq!(doc["rigsignal"]["stream"]["client"]["event"], "disconnected");
        assert!(doc["rigsignal"]["stream"]["client"].get("transport").is_none());
    }

    #[test]
    fn parser_matches_contract_shape() {
        let doc = parse_document_in_timezone(line(), "StreamClient", &session(), &Utc).unwrap();
        assert_eq!(doc["@timestamp"], "2026-07-17T09:09:44.000Z");
        assert_eq!(doc["event"]["category"], json!(["network"]));
        assert_eq!(doc["event"]["type"], json!(["connection", "start"]));
        assert_eq!(doc["rigsignal"]["stream"]["client"]["transport"], "direct");
        assert!(doc["rigsignal"].get("session").is_none());
        assert!(doc["rigsignal"].get("game").is_none());
    }

    #[test]
    fn parser_disconnect_transport_and_missing_via() {
        let s = session();
        let relay = parse_document_in_timezone(
            b"[2026-07-17 09:09:44] Client 1 (Peer) disconnected via relay connection",
            "h",
            &s,
            &Utc,
        )
        .unwrap();
        assert_eq!(relay["event"]["type"], json!(["connection", "end"]));
        assert_eq!(relay["rigsignal"]["stream"]["client"]["transport"], "relay");
        let no_via = parse_document_in_timezone(
            b"[2026-07-17 09:09:44] Client 1 (Peer) connected",
            "h",
            &s,
            &Utc,
        )
        .unwrap();
        assert!(no_via["rigsignal"]["stream"]["client"]
            .get("transport")
            .is_none());
    }

    #[test]
    fn ambiguous_dst_uses_earlier_utc_instant() {
        let early = FixedOffset::east_opt(2 * 3600)
            .unwrap()
            .from_local_datetime(
                &NaiveDateTime::parse_from_str("2026-10-25 02:30:00", "%Y-%m-%d %H:%M:%S").unwrap(),
            )
            .single()
            .unwrap();
        let late = FixedOffset::east_opt(3600)
            .unwrap()
            .from_local_datetime(
                &NaiveDateTime::parse_from_str("2026-10-25 02:30:00", "%Y-%m-%d %H:%M:%S").unwrap(),
            )
            .single()
            .unwrap();
        let chosen = earlier_utc(early, late);
        assert_eq!(
            chosen.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            "2026-10-25T00:30:00.000Z"
        );
    }

    #[test]
    fn checkpoint_only_advances_after_ack_and_replay_id_is_stable() -> Result<()> {
        let root = temp("ack");
        fs::create_dir_all(&root)?;
        let log = root.join("remote_connections.txt");
        fs::write(&log, [line(), b"\n"].concat())?;
        let state = root.join("state.json");
        let mut tailer =
            RemoteConnectionsTailer::with_paths("h".into(), log.clone(), state.clone())?;
        assert!(tailer.poll(&session())?.is_empty()); // first run starts at EOF
        fs::write(&log, [line(), b"\n", line(), b"\n"].concat())?;
        let events = tailer.poll(&session())?;
        let id = events[0].id.clone();
        let token = events[0].token.clone();
        assert!(!state_contents(&state).contains("offset\":0"));
        drop(tailer);
        let mut replay = RemoteConnectionsTailer::with_paths("h".into(), log, state.clone())?;
        let replay_events = replay.poll(&session())?;
        assert_eq!(replay_events[0].id, id);
        replay.ack_success(&replay_events[0].token)?;
        assert!(state_contents(&state).contains("offset\":"));
        let _ = token;
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn nack_replays_the_same_batch_on_the_same_instance() -> Result<()> {
        let root = temp("nack");
        fs::create_dir_all(&root)?;
        let log = root.join("remote_connections.txt");
        fs::write(&log, b"")?;
        let state = root.join("state.json");
        let mut tailer = RemoteConnectionsTailer::with_paths("h".into(), log.clone(), state)?;
        tailer.poll(&session())?; // first run starts at EOF
        fs::write(&log, [line(), b"\n"].concat())?;

        let first = tailer.poll(&session())?;
        assert_eq!(first.len(), 1);
        let token = first[0].token.clone();
        tailer.nack(); // simulate a failed bulk request

        let retry = tailer.poll(&session())?;
        assert_eq!(retry.len(), 1);
        assert_eq!(retry[0].id, first[0].id);
        assert_eq!(retry[0].token, token);
        assert_eq!(retry[0].document, first[0].document);
        tailer.ack_success(&retry[0].token)?;
        assert_eq!(
            tailer.state.as_ref().unwrap().offset,
            fs::metadata(&log)?.len()
        );
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn partial_line_is_held_until_terminated_and_201_or_409_ack_advances() -> Result<()> {
        let root = temp("partial");
        fs::create_dir_all(&root)?;
        let log = root.join("remote_connections.txt");
        fs::write(&log, b"")?;
        let state = root.join("state.json");
        let mut tailer = RemoteConnectionsTailer::with_paths("h".into(), log.clone(), state)?;
        tailer.poll(&session())?;
        fs::write(&log, line())?;
        assert!(tailer.poll(&session())?.is_empty());
        let mut appended = fs::read(&log)?;
        appended.push(b'\n');
        fs::write(&log, appended)?;
        let events = tailer.poll(&session())?;
        assert_eq!(events.len(), 1);
        let token = events[0].token.clone();
        // This is the tail-side effect used after either mock bulk result:
        // keyed 201 or keyed version-conflict 409.
        tailer.ack_success(&token)?;
        assert_eq!(
            tailer.state.as_ref().unwrap().offset,
            fs::metadata(&log)?.len()
        );
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn truncation_restarts_from_zero_and_oversize_complete_line_advances() -> Result<()> {
        let root = temp("truncate");
        fs::create_dir_all(&root)?;
        let log = root.join("remote_connections.txt");
        let mut initial = vec![b'x'; 100];
        initial.push(b'\n');
        fs::write(&log, initial)?;
        let state = root.join("state.json");
        let mut tailer = RemoteConnectionsTailer::with_paths("h".into(), log.clone(), state)?;
        tailer.poll(&session())?;
        fs::write(&log, [line(), b"\n"].concat())?;
        let events = tailer.poll(&session())?;
        assert_eq!(events.len(), 1);
        tailer.ack_success(&events[0].token)?;
        let oversize = vec![b'x'; MAX_LINE_BYTES + 1];
        fs::write(&log, [&oversize[..], b"\n"].concat())?;
        assert!(tailer.poll(&session())?.is_empty());
        assert_eq!(
            tailer.state.as_ref().unwrap().offset,
            fs::metadata(&log)?.len()
        );
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn rotated_old_generation_is_drained_before_new_generation() -> Result<()> {
        let root = temp("rotate");
        fs::create_dir_all(&root)?;
        let log = root.join("remote_connections.txt");
        fs::write(&log, b"initial\n")?;
        let state = root.join("state.json");
        let mut tailer = RemoteConnectionsTailer::with_paths("h".into(), log.clone(), state)?;
        tailer.poll(&session())?;
        let mut old = fs::read(&log)?;
        old.extend_from_slice(line());
        old.push(b'\n');
        fs::write(&log, old)?;
        let rotated = root.join("remote_connections.txt.1");
        fs::rename(&log, &rotated)?;
        fs::write(&log, [line(), b"\n"].concat())?;
        let old_events = tailer.poll(&session())?;
        assert_eq!(old_events.len(), 1);
        tailer.ack_success(&old_events[0].token)?;
        let new_events = tailer.poll(&session())?;
        assert_eq!(new_events.len(), 1);
        tailer.ack_success(&new_events[0].token)?;
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn restarted_tailer_drains_saved_rotated_generation_before_current() -> Result<()> {
        let root = temp("restart-rotate");
        fs::create_dir_all(&root)?;
        let log = root.join("remote_connections.txt");
        fs::write(&log, b"initial\n")?;
        let state = root.join("state.json");
        let mut tailer =
            RemoteConnectionsTailer::with_paths("h".into(), log.clone(), state.clone())?;
        tailer.poll(&session())?;
        let mut old = fs::read(&log)?;
        old.extend_from_slice(line());
        old.push(b'\n');
        fs::write(&log, old)?;
        let rotated = root.join("remote_connections.txt.1");
        fs::rename(&log, &rotated)?;
        fs::write(&log, [line(), b"\n"].concat())?;
        drop(tailer);

        let mut restarted = RemoteConnectionsTailer::with_paths("h".into(), log, state)?;
        let old_events = restarted.poll(&session())?;
        assert_eq!(old_events.len(), 1);
        let old_id = old_events[0].id.clone();
        restarted.ack_success(&old_events[0].token)?;
        let new_events = restarted.poll(&session())?;
        assert_eq!(new_events.len(), 1);
        assert_ne!(new_events[0].id, old_id);
        restarted.ack_success(&new_events[0].token)?;
        fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn sha256_identity_matches_known_digest() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    fn state_contents(path: &Path) -> String {
        fs::read_to_string(path).unwrap()
    }
}
