/// Elasticsearch bulk API shipper.
///
/// Matches the Python collector's shipper exactly:
///   - Authorization: ApiKey <key>
///   - Content-Type: application/x-ndjson
///   - Action line: {"create":{"_index":"metrics-rigsignal.<dataset>-default"}}
///   - Index naming: metrics-rigsignal.<dataset>-default
use crate::config::Config;
use anyhow::{Context, Result};
use fs2::FileExt;
use reqwest::Client;
use serde_json::Value;
use std::collections::HashMap;
use std::fs::{File, OpenOptions, ReadDir};
use std::io::{BufRead, BufReader, BufWriter, ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tracing::{debug, info, warn};

/// A 40k-file backlog clears in roughly 20 minutes at the normal 30-second
/// rotation cadence. Keeping this at 1000 also bounds synchronous tick work.
const RETENTION_PRUNE_BATCH: usize = 1_000;
/// Directory entries inspected on each rotation tick. A 40k-file directory completes a
/// full cycle in at most 40 calls without collecting or sorting the whole directory.
const RETENTION_SCAN_BATCH: usize = 1_000;
/// Recovery retains at most this many bytes for the JSON parser at a time. Larger
/// complete lines are malformed; an unterminated final chunk is partial regardless of size.
const RECOVERY_MAX_LINE_BYTES: usize = 1024 * 1024;

pub struct ShipResult {
    pub attempted: usize,
    pub succeeded: usize,
    pub failed: usize,
}

/// A document that may use Elasticsearch's create-time idempotency key.  The id is
/// deliberately transport metadata, never a field added to the source document.
pub struct ShipDocument {
    pub document: Value,
    pub id: Option<String>,
}

pub struct SpoolWriter {
    dir: PathBuf,
    max_file_bytes: u64,
    max_file_age: Duration,
    retention: Duration,
    spools: HashMap<String, DatasetSpool>,
    next_seq: u32,
    lock_file: File,
    retention_scan: Option<ReadDir>,
    #[cfg(test)]
    retention_entries_scanned: usize,
}

struct DatasetSpool {
    active_path: PathBuf,
    writer: BufWriter<File>,
    current_file_bytes: u64,
    current_file_started: Instant,
}

impl SpoolWriter {
    pub fn new(
        dir: impl AsRef<Path>,
        max_file_bytes: u64,
        max_file_age_secs: u64,
        spool_retention_hours: u64,
    ) -> Result<Self> {
        let dir = dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&dir)
            .with_context(|| format!("creating spool directory: {}", dir.display()))?;

        let lock_path = dir.join(".rigsignal-spool.lock");
        let lock_file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(&lock_path)
            .with_context(|| format!("opening spool lockfile: {}", lock_path.display()))?;
        lock_file.try_lock_exclusive().with_context(|| {
            format!(
                "spool directory {} is already locked by another RigSignal agent",
                dir.display()
            )
        })?;

        // DatasetSpool::new truncates active files, so recovery must complete
        // for every dataset before normal operation can create one.
        let mut next_seq = 1;
        recover_stranded_files(&dir, &mut next_seq, || Ok(()))?;

        Ok(Self {
            dir,
            max_file_bytes,
            max_file_age: Duration::from_secs(max_file_age_secs),
            retention: Duration::from_secs(spool_retention_hours.saturating_mul(60 * 60)),
            spools: HashMap::new(),
            next_seq,
            lock_file,
            retention_scan: None,
            #[cfg(test)]
            retention_entries_scanned: 0,
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

    pub fn rotate_stale_files(&mut self) -> Result<()> {
        self.prune_retained_files()?;
        if self.max_file_age.as_secs() == 0 {
            return Ok(());
        }

        let stale_slugs: Vec<String> = self
            .spools
            .iter()
            .filter(|(_, spool)| {
                spool.current_file_bytes > 0
                    && spool.current_file_started.elapsed() >= self.max_file_age
            })
            .map(|(slug, _)| slug.clone())
            .collect();

        for slug in stale_slugs {
            self.rotate(&slug)?;
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
        let spool = self
            .spools
            .remove(slug)
            .expect("dataset spool exists before rotation");
        self.close_publish_and_replace(slug, spool, true)
    }

    /// Flush and close every active writer before publishing. Empty active files
    /// are removed rather than left for a future process to mistake as stranded.
    pub fn finalize_all(&mut self) -> Result<()> {
        let slugs: Vec<String> = self.spools.keys().cloned().collect();
        let mut first_error = None;
        for slug in slugs {
            let spool = self
                .spools
                .remove(&slug)
                .expect("dataset spool exists while finalizing");
            if let Err(error) = self.close_publish_and_replace(&slug, spool, false) {
                if first_error.is_none() {
                    first_error = Some(error);
                }
            }
        }
        if let Some(error) = first_error {
            return Err(error);
        }
        Ok(())
    }

    fn close_publish_and_replace(
        &mut self,
        slug: &str,
        mut spool: DatasetSpool,
        replace_active: bool,
    ) -> Result<()> {
        if let Err(error) = spool.writer.flush() {
            self.spools.insert(slug.to_string(), spool);
            return Err(error).context("flushing spool file before publication");
        }
        let DatasetSpool {
            active_path,
            writer,
            current_file_bytes,
            current_file_started,
        } = spool;
        drop(writer);

        if current_file_bytes == 0 {
            remove_file_if_exists(&active_path)?;
        } else if let Err(error) = self.publish_closed_file(&active_path, slug, "ndjson") {
            // The file is closed but still present. Re-open it for append so a
            // failed rotation never turns a later write into a truncation.
            let restored =
                DatasetSpool::reopen(active_path, current_file_bytes, current_file_started)?;
            self.spools.insert(slug.to_string(), restored);
            return Err(error);
        }

        if replace_active {
            self.spools
                .insert(slug.to_string(), DatasetSpool::new(&self.dir, slug)?);
        }
        Ok(())
    }

    fn publish_closed_file(
        &mut self,
        source: &Path,
        slug: &str,
        extension: &str,
    ) -> Result<PathBuf> {
        let millis = unix_millis()?;
        self.publish_closed_file_at(source, slug, extension, millis)
    }

    fn publish_closed_file_at(
        &mut self,
        source: &Path,
        slug: &str,
        extension: &str,
        millis: u128,
    ) -> Result<PathBuf> {
        loop {
            let target = self.dir.join(format!(
                "rigsignal-{}-{}-{}.{}",
                slug, millis, self.next_seq, extension
            ));
            match OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&target)
            {
                Ok(reservation) => {
                    drop(reservation);
                    if let Err(error) = std::fs::rename(source, &target) {
                        let _ = remove_file_if_exists(&target);
                        return Err(error).with_context(|| {
                            format!(
                                "publishing spool file {} to {}",
                                source.display(),
                                target.display()
                            )
                        });
                    }
                    self.next_seq = self.next_seq.saturating_add(1);
                    return Ok(target);
                }
                Err(error) if error.kind() == ErrorKind::AlreadyExists => {
                    self.next_seq = self.next_seq.saturating_add(1);
                }
                Err(error) => {
                    return Err(error).with_context(|| {
                        format!("reserving spool publication name: {}", target.display())
                    });
                }
            }
        }
    }

    /// Incrementally prune finals and quarantines by mtime. Deep-review finding 2 permits
    /// directory-order deletion instead of a global oldest-first sort: every full scan
    /// cycle sees each entry once, while each synchronous rotation does bounded work.
    fn prune_retained_files(&mut self) -> Result<()> {
        self.prune_retained_files_with_limits(RETENTION_SCAN_BATCH, RETENTION_PRUNE_BATCH)
    }

    fn prune_retained_files_with_limits(
        &mut self,
        scan_batch: usize,
        prune_batch: usize,
    ) -> Result<()> {
        if self.retention.is_zero() {
            return Ok(());
        }
        if self.retention_scan.is_none() {
            self.retention_scan = Some(std::fs::read_dir(&self.dir).with_context(|| {
                format!("scanning spool retention directory: {}", self.dir.display())
            })?);
        }

        let cutoff = SystemTime::now()
            .checked_sub(self.retention)
            .unwrap_or(UNIX_EPOCH);
        let mut deletions = 0;
        let mut exhausted = false;
        for _ in 0..scan_batch {
            let entry = match self
                .retention_scan
                .as_mut()
                .expect("retention scan is initialised")
                .next()
            {
                Some(entry) => entry?,
                None => {
                    exhausted = true;
                    break;
                }
            };
            #[cfg(test)]
            {
                self.retention_entries_scanned += 1;
            }
            let path = entry.path();
            if deletions < prune_batch
                && is_retained_spool_file(&path)
                && entry.metadata()?.modified().unwrap_or(SystemTime::now()) < cutoff
            {
                remove_file_if_exists(&path)
                    .with_context(|| format!("pruning retained spool file: {}", path.display()))?;
                deletions += 1;
            }
        }
        if exhausted {
            self.retention_scan = None;
        }
        Ok(())
    }

    #[cfg(test)]
    fn retention_entries_scanned(&self) -> usize {
        self.retention_entries_scanned
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

    fn reopen(
        active_path: PathBuf,
        current_file_bytes: u64,
        current_file_started: Instant,
    ) -> Result<Self> {
        let file = OpenOptions::new()
            .append(true)
            .open(&active_path)
            .with_context(|| format!("re-opening active spool file: {}", active_path.display()))?;
        Ok(Self {
            active_path,
            writer: BufWriter::new(file),
            current_file_bytes,
            current_file_started,
        })
    }
}

fn recover_stranded_files<F>(dir: &Path, next_seq: &mut u32, mut before_publish: F) -> Result<()>
where
    F: FnMut() -> Result<()>,
{
    for entry in std::fs::read_dir(dir)
        .with_context(|| format!("scanning spool recovery directory: {}", dir.display()))?
    {
        let path = entry?.path();
        if is_recovery_staging_file(&path) {
            remove_file_if_exists(&path).with_context(|| {
                format!(
                    "removing orphaned recovery staging file: {}",
                    path.display()
                )
            })?;
        }
    }

    let entries = std::fs::read_dir(dir)
        .with_context(|| format!("scanning spool recovery directory: {}", dir.display()))?;
    for entry in entries {
        let path = entry?.path();
        let Some(slug) = stranded_slug(&path) else {
            continue;
        };
        recover_stranded_file(dir, &path, &slug, next_seq, &mut before_publish)?;
    }
    Ok(())
}

/// Accepted residual risk (2026-07-18): a SIGKILL after final publication but before
/// source `.tmp` disposal will republish valid records on the next startup. This rare
/// duplicate-final window requires a crash during recovery from a prior crash and is
/// the same class D2 already accepts at the Fleet reader layer.
///
/// Related (hardening review note, 2026-07-18): a crash between the quarantine name
/// reservation and the source rename orphans a zero-byte `.quarantine` placeholder.
/// It never matches the Fleet glob, the source is still reprocessed next startup, and
/// retention prunes the debris within `spool_retention_hours` — accepted as
/// self-healing.
fn recover_stranded_file<F>(
    dir: &Path,
    source: &Path,
    slug: &str,
    next_seq: &mut u32,
    before_publish: &mut F,
) -> Result<()>
where
    F: FnMut() -> Result<()>,
{
    let file = File::open(source)
        .with_context(|| format!("opening stranded spool file: {}", source.display()))?;
    let mut reader = BufReader::new(file);
    let mut line = Vec::new();
    let mut final_stage = None;
    let mut final_writer = None;
    let mut malformed = false;
    let mut partial = false;
    let millis = unix_millis()?;
    while let Some(recovered_line) = read_recovery_line(&mut reader, &mut line)? {
        if !recovered_line.terminated {
            malformed |= recovered_line.oversize;
            partial = true;
            continue;
        }
        if recovered_line.oversize || recovered_line.bytes.is_empty() {
            malformed |= recovered_line.oversize;
            continue;
        }
        if serde_json::from_slice::<Value>(&recovered_line.bytes).is_ok() {
            if final_writer.is_none() {
                let (stage, writer) =
                    create_recovery_stage(dir, slug, millis, *next_seq, "ndjson")?;
                final_stage = Some(stage);
                final_writer = Some(writer);
            }
            let writer = final_writer
                .as_mut()
                .expect("recovery writer exists after staging creation");
            writer
                .write_all(&recovered_line.bytes)
                .context("writing recovered spool record")?;
            writer
                .write_all(b"\n")
                .context("writing recovered spool newline")?;
        } else {
            malformed = true;
        }
    }
    drop(reader);
    if final_stage.is_none() && !malformed && !partial {
        return Ok(());
    }
    if let Some(mut writer) = final_writer {
        writer
            .flush()
            .context("flushing recovery staging file before publication")?;
        drop(writer);
    }
    let quarantine_needed = malformed || partial;
    let mut publisher = RecoveryPublisher { dir, next_seq };
    // Reserve the target before publishing valid data so malformed input can still be
    // moved without a copy. The source itself remains untouched until final publication.
    let quarantine_reservation = if quarantine_needed {
        match publisher.reserve(slug, "quarantine") {
            Ok(reservation) => Some(reservation),
            Err(error) => {
                cleanup_stages(final_stage.as_deref(), None);
                return Err(error);
            }
        }
    } else {
        None
    };

    if let Err(error) = before_publish() {
        cleanup_stages(final_stage.as_deref(), quarantine_reservation.as_deref());
        return Err(error).context("recovering stranded spool file before publication");
    }

    match final_stage
        .as_deref()
        .map(|stage| publisher.publish(stage, slug, "ndjson"))
    {
        Some(Ok(_)) => {}
        Some(Err(error)) => {
            cleanup_stages(final_stage.as_deref(), quarantine_reservation.as_deref());
            return Err(error);
        }
        None => {}
    }
    let quarantine_path = if let Some(target) = quarantine_reservation {
        if let Err(error) = std::fs::rename(source, &target) {
            let _ = remove_file_if_exists(&target);
            return Err(error).with_context(|| {
                format!("quarantining recovered spool input at {}", target.display())
            });
        }
        Some(target)
    } else {
        remove_file_if_exists(source)?;
        None
    };
    if let Some(path) = quarantine_path.as_deref() {
        warn!(
            "recovered malformed or truncated spool file {}; quarantined input at {}",
            source.display(),
            path.display()
        );
    }
    Ok(())
}

struct RecoveryPublisher<'a> {
    dir: &'a Path,
    next_seq: &'a mut u32,
}

impl RecoveryPublisher<'_> {
    fn reserve(&mut self, slug: &str, extension: &str) -> Result<PathBuf> {
        let millis = unix_millis()?;
        loop {
            let target = self.dir.join(format!(
                "rigsignal-{}-{}-{}.{}",
                slug, millis, *self.next_seq, extension
            ));
            match OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&target)
            {
                Ok(reservation) => {
                    drop(reservation);
                    *self.next_seq = self.next_seq.saturating_add(1);
                    return Ok(target);
                }
                Err(error) if error.kind() == ErrorKind::AlreadyExists => {
                    *self.next_seq = self.next_seq.saturating_add(1);
                }
                Err(error) => return Err(error).context("reserving recovered spool filename"),
            }
        }
    }

    fn publish(&mut self, source: &Path, slug: &str, extension: &str) -> Result<PathBuf> {
        let millis = unix_millis()?;
        loop {
            let target = self.dir.join(format!(
                "rigsignal-{}-{}-{}.{}",
                slug, millis, *self.next_seq, extension
            ));
            match OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&target)
            {
                Ok(reservation) => {
                    drop(reservation);
                    if let Err(error) = std::fs::rename(source, &target) {
                        let _ = remove_file_if_exists(&target);
                        return Err(error).with_context(|| {
                            format!("publishing recovered spool file to {}", target.display())
                        });
                    }
                    *self.next_seq = self.next_seq.saturating_add(1);
                    return Ok(target);
                }
                Err(error) if error.kind() == ErrorKind::AlreadyExists => {
                    *self.next_seq = self.next_seq.saturating_add(1);
                }
                Err(error) => return Err(error).context("reserving recovered spool filename"),
            }
        }
    }
}

fn create_recovery_stage(
    dir: &Path,
    slug: &str,
    millis: u128,
    seq: u32,
    extension: &str,
) -> Result<(PathBuf, BufWriter<File>)> {
    let stage = dir.join(format!(
        ".rigsignal-{}-{}-{}.{}.staging",
        slug, millis, seq, extension
    ));
    let file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&stage)
        .with_context(|| format!("creating recovery staging file: {}", stage.display()))?;
    Ok((stage, BufWriter::new(file)))
}

struct RecoveryLine {
    bytes: Vec<u8>,
    terminated: bool,
    oversize: bool,
}

fn read_recovery_line(
    reader: &mut BufReader<File>,
    line: &mut Vec<u8>,
) -> std::io::Result<Option<RecoveryLine>> {
    line.clear();
    let mut oversize = false;
    loop {
        let (consumed, terminated, bytes_to_append, at_eof) = {
            let buffer = reader.fill_buf()?;
            if buffer.is_empty() {
                (0, false, 0, true)
            } else {
                let newline = buffer.iter().position(|byte| *byte == b'\n');
                let consumed = newline.map_or(buffer.len(), |index| index + 1);
                let content_len = newline.unwrap_or(buffer.len());
                let bytes_to_append = if oversize {
                    0
                } else if line.len().saturating_add(content_len) > RECOVERY_MAX_LINE_BYTES {
                    oversize = true;
                    0
                } else {
                    content_len
                };
                (consumed, newline.is_some(), bytes_to_append, false)
            }
        };
        if at_eof {
            return if line.is_empty() && !oversize {
                Ok(None)
            } else {
                Ok(Some(RecoveryLine {
                    bytes: std::mem::take(line),
                    terminated: false,
                    oversize,
                }))
            };
        }
        if bytes_to_append > 0 {
            let buffer = reader.fill_buf()?;
            line.extend_from_slice(&buffer[..bytes_to_append]);
        }
        reader.consume(consumed);
        if terminated {
            return Ok(Some(RecoveryLine {
                bytes: std::mem::take(line),
                terminated: true,
                oversize,
            }));
        }
    }
}

fn cleanup_stages(first: Option<&Path>, second: Option<&Path>) {
    for stage in [first, second].into_iter().flatten() {
        let _ = remove_file_if_exists(stage);
    }
}

fn stranded_slug(path: &Path) -> Option<String> {
    path.file_name()
        .and_then(|name| name.to_str())?
        .strip_prefix("rigsignal-")?
        .strip_suffix(".ndjson.tmp")
        .map(str::to_string)
}

fn is_recovery_staging_file(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".staging"))
}

fn is_retained_spool_file(path: &Path) -> bool {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    name.starts_with("rigsignal-") && (name.ends_with(".ndjson") || name.ends_with(".quarantine"))
}

fn remove_file_if_exists(path: &Path) -> std::io::Result<()> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
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

fn build_client(config: &Config) -> Result<Client> {
    let mut builder = Client::builder().timeout(std::time::Duration::from_secs(30));
    if let Some(path) = &config.elasticsearch.ca_cert {
        let pem = std::fs::read(path)
            .with_context(|| format!("reading Elasticsearch CA cert: {}", path.display()))?;
        let cert =
            reqwest::Certificate::from_pem(&pem).context("parsing Elasticsearch CA cert PEM")?;
        builder = builder.add_root_certificate(cert);
    }
    builder.build().context("building HTTP client")
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
    let client = build_client(config)?;
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

/// POST ordinary (unkeyed) docs to /_bulk.
pub async fn ship(config: &Config, docs: Vec<Value>) -> Result<ShipResult> {
    ship_documents(
        config,
        docs.into_iter()
            .map(|document| ShipDocument { document, id: None })
            .collect(),
    )
    .await
}

/// POST documents to /_bulk, optionally using an internal create `_id`.
/// Documents route to their managed data stream, which supplies its default
/// pipeline; no explicit bulk pipeline parameter is ever attached.
pub async fn ship_documents(config: &Config, docs: Vec<ShipDocument>) -> Result<ShipResult> {
    if docs.is_empty() {
        return Ok(ShipResult {
            attempted: 0,
            succeeded: 0,
            failed: 0,
        });
    }

    let attempted = docs.len();
    let client = build_client(config)?;
    let endpoint = format!(
        "{}/_bulk",
        config.elasticsearch.endpoint.trim_end_matches('/')
    );

    let mut body = String::with_capacity(attempted * 256);
    for doc in &docs {
        let action = bulk_action(&doc.document, doc.id.as_deref())?;
        body.push_str(&serde_json::to_string(&action).context("serialising action line")?);
        body.push('\n');
        body.push_str(&serde_json::to_string(&doc.document).context("serialising doc")?);
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
    if has_errors || docs.iter().any(|doc| doc.id.is_some()) {
        if let Some(items) = resp_body.get("items").and_then(|i| i.as_array()) {
            for (position, item) in items.iter().enumerate() {
                let create = item.get("create").or_else(|| item.get("index"));
                let keyed = docs.get(position).and_then(|doc| doc.id.as_ref());
                let success = if keyed.is_some() {
                    bulk_item_success(keyed, create)
                } else {
                    !has_errors || bulk_item_success(keyed, create)
                };
                if !success {
                    let status = create
                        .and_then(|action| action.get("status"))
                        .and_then(Value::as_u64);
                    if let Some(err) = create.and_then(|action| action.get("error")) {
                        warn!("bulk item error: {}", err);
                    } else {
                        warn!("bulk item returned unexpected status {:?}", status);
                    }
                    failed += 1;
                }
            }
        } else {
            failed = attempted;
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

fn bulk_action(document: &Value, id: Option<&str>) -> Result<Value> {
    let data_stream = document
        .get("data_stream")
        .and_then(Value::as_object)
        .context("bulk document missing data_stream")?;
    let stream_type = data_stream
        .get("type")
        .and_then(Value::as_str)
        .context("bulk document missing data_stream.type")?;
    if stream_type != "metrics" && stream_type != "logs" {
        anyhow::bail!(
            "unsupported data_stream.type {:?}; expected metrics or logs",
            stream_type
        );
    }
    let dataset = data_stream
        .get("dataset")
        .and_then(Value::as_str)
        .context("bulk document missing data_stream.dataset")?;
    let mut create = serde_json::Map::new();
    create.insert(
        "_index".to_string(),
        Value::String(format!("{}-{}-default", stream_type, dataset)),
    );
    if let Some(id) = id {
        create.insert("_id".to_string(), Value::String(id.to_string()));
    }
    Ok(Value::Object(serde_json::Map::from_iter([(
        "create".to_string(),
        Value::Object(create),
    )])))
}

/// Interprets an injected/mock bulk item the same way the live response path
/// does. A keyed create may be acknowledged by either a fresh 201 or the
/// specific idempotency conflict; regular metric behaviour remains error-based.
fn bulk_item_success(id: Option<&String>, item: Option<&Value>) -> bool {
    let status = item
        .and_then(|action| action.get("status"))
        .and_then(Value::as_u64);
    if id.is_some() && status == Some(201) {
        return true;
    }
    if id.is_some()
        && status == Some(409)
        && item
            .and_then(|action| action.get("error"))
            .and_then(|error| error.get("type"))
            .and_then(Value::as_str)
            == Some("version_conflict_engine_exception")
    {
        return true;
    }
    id.is_none() && item.and_then(|action| action.get("error")).is_none()
}

/// Request an immediate transform sync via POST /_transform/{id}/_schedule_now.
///
/// Called after shipping the session summary document so the Games dashboard
/// updates within seconds rather than waiting up to 60 s for the next scheduled
/// sync. Failures are logged at WARN level and never propagate to the caller —
/// this is a best-effort optimisation, not part of the critical shipping path.
pub async fn trigger_transform_sync(config: &Config, transform_id: &str) -> Result<()> {
    let client = build_client(config)?;
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
    use std::time::Duration;

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
    fn bulk_action_routes_logs_and_accepts_an_optional_id() -> Result<()> {
        let action = bulk_action(
            &json!({"data_stream": {"type": "logs", "dataset": "rigsignal.events"}}),
            Some("source-record-id"),
        )?;
        assert_eq!(action["create"]["_index"], "logs-rigsignal.events-default");
        assert_eq!(action["create"]["_id"], "source-record-id");
        let metrics = bulk_action(
            &json!({"data_stream": {"type": "metrics", "dataset": "rigsignal.cpu"}}),
            None,
        )?;
        assert_eq!(metrics["create"]["_index"], "metrics-rigsignal.cpu-default");
        assert!(metrics["create"].get("_id").is_none());
        assert!(bulk_action(
            &json!({"data_stream": {"type": "traces", "dataset": "rigsignal.events"}}),
            None,
        )
        .is_err());
        Ok(())
    }

    #[test]
    fn keyed_bulk_acknowledges_201_and_idempotent_409_only() {
        let source_id = "source-record-id".to_string();
        let id = Some(&source_id);
        assert!(bulk_item_success(id, Some(&json!({"status": 201}))));
        assert!(bulk_item_success(
            id,
            Some(&json!({
                "status": 409,
                "error": {"type": "version_conflict_engine_exception"}
            }))
        ));
        assert!(!bulk_item_success(
            id,
            Some(&json!({
                "status": 409,
                "error": {"type": "mapper_parsing_exception"}
            }))
        ));
        assert!(!bulk_item_success(id, Some(&json!({"status": 500}))));
    }

    #[test]
    fn write_docs_creates_per_dataset_spool_files() -> Result<()> {
        let dir = temp_spool_dir("mixed-dataset-spool");
        let mut writer = SpoolWriter::new(&dir, 0, 0, 72)?;
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

    #[test]
    fn rotate_stale_files_rotates_pending_file_without_new_writes() -> Result<()> {
        let dir = temp_spool_dir("stale-spool");
        let mut writer = SpoolWriter::new(&dir, 0, 1, 72)?;
        let docs = vec![json!({
            "data_stream": { "dataset": "rigsignal.frame" },
            "rigsignal": { "frame": { "fps": 60.0 } }
        })];

        writer.write_docs(&docs)?;
        let active_path = dir.join("rigsignal-frame.ndjson.tmp");
        assert!(active_path.exists());

        std::thread::sleep(Duration::from_millis(1100));
        writer.rotate_stale_files()?;

        let final_files = final_spool_files(&dir)?;
        assert_eq!(final_files.len(), 1);
        assert!(active_path.exists());
        assert_eq!(fs::read_to_string(&active_path)?, "");

        let final_lines = fs::read_to_string(&final_files[0])?;
        assert_eq!(final_lines.lines().count(), 1);
        assert!(final_lines.contains("\"fps\":60.0"));

        std::thread::sleep(Duration::from_millis(1100));
        writer.rotate_stale_files()?;
        assert_eq!(final_spool_files(&dir)?.len(), 1);

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn finalize_all_publishes_each_dataset_once_and_removes_active_files() -> Result<()> {
        let dir = temp_spool_dir("shutdown-finalize");
        let mut writer = SpoolWriter::new(&dir, 0, 0, 72)?;
        writer.write_docs(&[
            test_doc("rigsignal.cpu", "cpu-marker"),
            test_doc("rigsignal.gpu", "gpu-marker"),
            test_doc("rigsignal.memory", "memory-marker"),
        ])?;

        writer.finalize_all()?;
        let finals = published_spool_files(&dir)?;
        assert_eq!(finals.len(), 3);
        for marker in ["cpu-marker", "gpu-marker", "memory-marker"] {
            assert_eq!(count_marker(&finals, marker)?, 1);
        }
        for slug in ["cpu", "gpu", "memory"] {
            assert!(!dir.join(format!("rigsignal-{slug}.ndjson.tmp")).exists());
            assert!(finals.iter().any(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| {
                        name.starts_with(&format!("rigsignal-{slug}-")) && name.ends_with(".ndjson")
                    })
            }));
        }

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn startup_recovery_publishes_valid_lines_and_quarantines_bad_input() -> Result<()> {
        let dir = temp_spool_dir("recovery-malformed-truncated");
        fs::create_dir_all(&dir)?;
        let source = dir.join("rigsignal-frame.ndjson.tmp");
        let mut original =
            b"{\"marker\":\"valid-one\"}\nnot-json\n{\"marker\":\"valid-two\"}\n".to_vec();
        original.extend(std::iter::repeat_n(b'x', RECOVERY_MAX_LINE_BYTES + 1));
        original.extend_from_slice(b"\n{\"marker\":\"partial");
        fs::write(&source, &original)?;

        let writer = SpoolWriter::new(&dir, 0, 0, 72)?;
        let finals = published_spool_files(&dir)?;
        assert_eq!(count_marker(&finals, "valid-one")?, 1);
        assert_eq!(count_marker(&finals, "valid-two")?, 1);
        assert!(!source.exists());
        let quarantines: Vec<PathBuf> = fs::read_dir(&dir)?
            .filter_map(|entry| entry.ok().map(|entry| entry.path()))
            .filter(|path| path.extension().is_some_and(|ext| ext == "quarantine"))
            .collect();
        assert_eq!(quarantines.len(), 1);
        assert_eq!(fs::read(&quarantines[0])?, original);
        let quarantined = fs::read_to_string(&quarantines[0])?;
        assert!(quarantined.contains("not-json"));
        assert!(quarantined.contains("partial"));
        assert!(!fs::read_dir(&dir)?.any(|entry| {
            entry
                .ok()
                .is_some_and(|entry| entry.path().extension().is_some_and(|ext| ext == "staging"))
        }));

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn startup_recovery_removes_orphaned_staging_before_republishing_tmp() -> Result<()> {
        let dir = temp_spool_dir("recovery-orphaned-staging");
        fs::create_dir_all(&dir)?;
        let staging = dir.join(".rigsignal-frame-1-1.ndjson.staging");
        let source = dir.join("rigsignal-frame.ndjson.tmp");
        fs::write(&staging, b"orphaned staging")?;
        fs::write(&source, b"{\"marker\":\"recover-once\"}\n")?;

        let writer = SpoolWriter::new(&dir, 0, 0, 72)?;
        let finals = published_spool_files(&dir)?;
        assert!(!staging.exists());
        assert_eq!(count_marker(&finals, "recover-once")?, 1);
        assert!(!source.exists());

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn startup_recovery_removes_fully_valid_source_without_quarantine() -> Result<()> {
        let dir = temp_spool_dir("recovery-valid");
        fs::create_dir_all(&dir)?;
        let source = dir.join("rigsignal-frame.ndjson.tmp");
        fs::write(
            &source,
            b"{\"marker\":\"valid-one\"}\n{\"marker\":\"valid-two\"}\n",
        )?;

        let writer = SpoolWriter::new(&dir, 0, 0, 72)?;
        let finals = published_spool_files(&dir)?;
        assert_eq!(count_marker(&finals, "valid-one")?, 1);
        assert_eq!(count_marker(&finals, "valid-two")?, 1);
        assert!(!source.exists());
        assert!(!fs::read_dir(&dir)?.any(|entry| {
            entry.ok().is_some_and(|entry| {
                entry
                    .path()
                    .extension()
                    .is_some_and(|extension| extension == "quarantine")
            })
        }));

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn failed_recovery_keeps_source_tmp_and_publishes_nothing() -> Result<()> {
        let dir = temp_spool_dir("recovery-disk-full");
        fs::create_dir_all(&dir)?;
        let source = dir.join("rigsignal-frame.ndjson.tmp");
        fs::write(&source, b"{\"marker\":\"retain-me\"}\n")?;
        let mut next_seq = 1;

        let error =
            recover_stranded_files(&dir, &mut next_seq, || anyhow::bail!("simulated disk full"))
                .expect_err("simulated publication failure should fail recovery");
        assert!(format!("{error:#}").contains("simulated disk full"));
        assert!(source.exists());
        assert!(published_spool_files(&dir)?.is_empty());

        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn publication_collision_bumps_sequence_without_overwriting() -> Result<()> {
        let dir = temp_spool_dir("publication-collision");
        let mut writer = SpoolWriter::new(&dir, 0, 0, 72)?;
        let existing = dir.join("rigsignal-frame-42-1.ndjson");
        fs::write(&existing, "existing")?;
        let source = dir.join("closed-source.tmp");
        fs::write(&source, "new")?;

        let published = writer.publish_closed_file_at(&source, "frame", "ndjson", 42)?;
        assert_eq!(published, dir.join("rigsignal-frame-42-2.ndjson"));
        assert_eq!(fs::read_to_string(&existing)?, "existing");
        assert_eq!(fs::read_to_string(&published)?, "new");

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn size_rotation_publishes_batch_before_the_next_write() -> Result<()> {
        let dir = temp_spool_dir("size-rotation");
        let mut writer = SpoolWriter::new(&dir, 1, 0, 72)?;
        writer.write_docs(&[test_doc("rigsignal.frame", "size-marker")])?;

        let finals = final_spool_files(&dir)?;
        assert_eq!(finals.len(), 1);
        assert_eq!(count_marker(&finals, "size-marker")?, 1);
        assert_eq!(
            fs::read_to_string(dir.join("rigsignal-frame.ndjson.tmp"))?,
            ""
        );

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn stale_summary_rotation_is_harmless_when_finalize_follows() -> Result<()> {
        let dir = temp_spool_dir("stale-summary-finalize");
        let mut writer = SpoolWriter::new(&dir, 0, 60, 72)?;
        writer.write_docs(&[test_doc("rigsignal.session", "start-marker")])?;
        writer
            .spools
            .get_mut("session")
            .expect("session spool should exist")
            .current_file_started = Instant::now() - Duration::from_secs(61);

        // The summary write itself rotates the stale session active file. The
        // shutdown finalizer must then be a harmless no-op for that dataset.
        writer.write_docs(&[test_doc("rigsignal.session", "summary-marker")])?;
        writer.finalize_all()?;
        let finals = published_spool_files(&dir)?;
        assert_eq!(finals.len(), 1);
        assert_eq!(count_marker(&finals, "start-marker")?, 1);
        assert_eq!(count_marker(&finals, "summary-marker")?, 1);
        assert!(!dir.join("rigsignal-session.ndjson.tmp").exists());

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn retention_scan_and_deletions_are_bounded_per_call() -> Result<()> {
        let dir = temp_spool_dir("retention-bounded");
        let mut writer = SpoolWriter::new(&dir, 0, 0, 1)?;
        let retained: Vec<PathBuf> = (0..4)
            .map(|index| dir.join(format!("rigsignal-frame-{index}.ndjson")))
            .collect();
        for path in &retained {
            fs::write(path, "old")?;
            set_file_age(path, Duration::from_secs(2 * 60 * 60))?;
        }

        let scanned_before = writer.retention_entries_scanned();
        writer.prune_retained_files_with_limits(2, 1)?;
        assert!(writer.retention_entries_scanned() - scanned_before <= 2);

        writer.prune_retained_files_with_limits(100, 1)?;
        assert!(retained.iter().filter(|path| path.exists()).count() >= 2);

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn retention_rechecks_newly_eligible_files_on_the_next_cycle() -> Result<()> {
        let dir = temp_spool_dir("retention-next-cycle");
        let mut writer = SpoolWriter::new(&dir, 0, 0, 1)?;
        let candidate = dir.join("rigsignal-frame-new.ndjson");
        fs::write(&candidate, "new")?;

        for _ in 0..10 {
            writer.prune_retained_files_with_limits(1, 1)?;
            if writer.retention_scan.is_none() {
                break;
            }
        }
        assert!(candidate.exists());
        assert!(writer.retention_scan.is_none());

        set_file_age(&candidate, Duration::from_secs(2 * 60 * 60))?;
        for _ in 0..10 {
            writer.prune_retained_files_with_limits(1, 1)?;
            if !candidate.exists() {
                break;
            }
        }
        assert!(!candidate.exists());

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    #[test]
    fn second_writer_for_a_spool_directory_fails_fast() -> Result<()> {
        let dir = temp_spool_dir("single-writer-lock");
        let writer = SpoolWriter::new(&dir, 0, 0, 72)?;
        let error = match SpoolWriter::new(&dir, 0, 0, 72) {
            Ok(_) => anyhow::bail!("second writer unexpectedly acquired the spool lock"),
            Err(error) => error,
        };
        assert!(error.to_string().contains("already locked"));

        drop(writer);
        fs::remove_dir_all(&dir)?;
        Ok(())
    }

    fn test_doc(dataset: &str, marker: &str) -> Value {
        json!({
            "data_stream": { "dataset": dataset },
            "marker": marker,
        })
    }

    fn set_file_age(path: &Path, age: Duration) -> Result<()> {
        let modified = SystemTime::now()
            .checked_sub(age)
            .context("test age should be representable")?;
        let file = OpenOptions::new().write(true).open(path)?;
        file.set_times(std::fs::FileTimes::new().set_modified(modified))?;
        Ok(())
    }

    fn published_spool_files(dir: &Path) -> Result<Vec<PathBuf>> {
        let mut paths = Vec::new();
        for entry in fs::read_dir(dir)? {
            let path = entry?.path();
            let name = path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("");
            if name.starts_with("rigsignal-") && name.ends_with(".ndjson") {
                paths.push(path);
            }
        }
        paths.sort();
        Ok(paths)
    }

    fn count_marker(paths: &[PathBuf], marker: &str) -> Result<usize> {
        Ok(paths
            .iter()
            .map(fs::read_to_string)
            .collect::<std::io::Result<Vec<_>>>()?
            .iter()
            .map(|contents| contents.matches(marker).count())
            .sum())
    }

    fn final_spool_files(dir: &Path) -> Result<Vec<PathBuf>> {
        let mut paths = Vec::new();
        for entry in fs::read_dir(dir)? {
            let path = entry?.path();
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if name.starts_with("rigsignal-frame-") && name.ends_with(".ndjson") {
                paths.push(path);
            }
        }
        paths.sort();
        Ok(paths)
    }
}
