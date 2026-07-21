//! D3 GPU boot diagnosis core.
//!
//! Collection and Clap parsing intentionally live outside this module.  Task D
//! can parse live sysfs/journald data into these typed inputs without changing
//! verdict, persistence, or output semantics.

use super::contract::{
    self, DetectorContract, Diagnosis, DiagnosisFields, Disposition, NotApplicable, Outcome,
};
use chrono::{DateTime, FixedOffset, NaiveDateTime, Utc};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const CONTRACT: DetectorContract = DetectorContract {
    detector_id: "D3",
    rule_version: "d3.1",
    error_prefix: "D3",
};
pub const SMU_UNRESPONSIVE_MIN: usize = 3;
pub const RESET_ATTEMPT_MIN: usize = 2;
pub const PRECURSOR_WINDOW_S: i64 = 900;
const STATE_SCHEMA_VERSION: u32 = 1;
const READ_LIMIT: u64 = 1024 * 1024;
const OFFLINE_JOURNAL_ABSURD_LIMIT: u64 = 64 * 1024 * 1024;
const JOURNAL_LIMIT: usize = 1024 * 1024;
const JOURNAL_TIMEOUT: Duration = Duration::from_secs(10);
// journalctl's ordering can differ from adjacent realtime stamps by a few
// milliseconds as records from separate transports are committed.  Preserve
// that per-pair jitter allowance, but reject its cumulative backward drift:
// more than one tolerance quantum cannot be treated as harmless jitter and
// makes the window unreliable like a single larger RTC jump.
const TIMING_REGRESSION_TOLERANCE_MS: i64 = 100;
const INVENTORY_WINDOW_GRACE_S: i64 = 3600;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Platform {
    Linux,
    Other,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct PciDevice {
    pub bdf: String,
    pub vendor: String,
    pub device: String,
    pub class: String,
    pub parent_path: String,
    pub upstream_bridge: Option<String>,
}

impl PciDevice {
    fn identity_matches(&self, baseline: &Baseline) -> bool {
        self.vendor == baseline.vendor
            && self.device == baseline.device
            && self.class == baseline.class
    }
    fn is_bridge(&self) -> bool {
        self.class
            .strip_prefix("0x")
            .is_some_and(|c| c.starts_with("06"))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PciSnapshot {
    pub boot_id: String,
    pub devices: Vec<PciDevice>,
}

/// Parse the schema-v1 normalized topology format used by fixtures and later
/// by the live collector.  Its deliberately small grammar rejects partial
/// authoritative input rather than guessing device identity.
pub fn parse_pci_snapshot(source: &str) -> Result<PciSnapshot, String> {
    let boot_marker = "boot_id=";
    let boot_raw = source
        .lines()
        .find_map(|line| {
            line.find(boot_marker)
                .map(|at| &line[at + boot_marker.len()..])
        })
        .and_then(|tail| tail.split_whitespace().next())
        .ok_or_else(|| "PCI snapshot schema v1 lacks boot_id".to_string())?;
    let boot_id = normalize_boot_id(boot_raw)?;
    let mut devices = Vec::new();
    for (line_number, line) in source.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut fields = line.split_whitespace();
        let bdf = fields
            .next()
            .ok_or_else(|| format!("PCI snapshot line {} lacks BDF", line_number + 1))?;
        if !valid_bdf(bdf) {
            return Err(format!(
                "PCI snapshot line {} has invalid BDF",
                line_number + 1
            ));
        }
        let mut path = None;
        let mut vendor = None;
        let mut device = None;
        let mut class = None;
        for field in fields {
            let Some((key, value)) = field.split_once('=') else {
                return Err(format!(
                    "PCI snapshot line {} has malformed field",
                    line_number + 1
                ));
            };
            match key {
                "path" => path = Some(value),
                "vendor" => vendor = Some(value),
                "device" => device = Some(value),
                "class" => class = Some(value),
                _ => {
                    return Err(format!(
                        "PCI snapshot line {} has unknown field {key}",
                        line_number + 1
                    ))
                }
            }
        }
        let (path, vendor, device, class) = (
            path.ok_or_else(|| format!("PCI snapshot line {} lacks path", line_number + 1))?,
            vendor.ok_or_else(|| format!("PCI snapshot line {} lacks vendor", line_number + 1))?,
            device.ok_or_else(|| format!("PCI snapshot line {} lacks device", line_number + 1))?,
            class.ok_or_else(|| format!("PCI snapshot line {} lacks class", line_number + 1))?,
        );
        if !valid_hex_field(vendor, 4) || !valid_hex_field(device, 4) || !valid_hex_field(class, 6)
        {
            return Err(format!(
                "PCI snapshot line {} has invalid PCI identity",
                line_number + 1
            ));
        }
        let chain: Vec<&str> = path.split('/').filter(|part| valid_bdf(part)).collect();
        if chain.is_empty() {
            return Err(format!(
                "PCI snapshot line {} path contains no PCI BDF",
                line_number + 1
            ));
        }
        if chain
            .last()
            .is_none_or(|leaf| !leaf.eq_ignore_ascii_case(bdf))
        {
            return Err(format!(
                "PCI snapshot line {} path leaf does not match row BDF",
                line_number + 1
            ));
        }
        devices.push(PciDevice {
            bdf: bdf.to_ascii_lowercase(),
            vendor: vendor.to_ascii_lowercase(),
            device: device.to_ascii_lowercase(),
            class: class.to_ascii_lowercase(),
            parent_path: path.into(),
            upstream_bridge: chain
                .iter()
                .rev()
                .nth(1)
                .map(|value| (*value).to_ascii_lowercase()),
        });
    }
    if devices.is_empty() {
        return Err("PCI snapshot schema v1 has no devices".into());
    }
    if devices
        .iter()
        .map(|device| &device.bdf)
        .collect::<std::collections::HashSet<_>>()
        .len()
        != devices.len()
    {
        return Err("PCI snapshot contains duplicate BDFs".into());
    }
    let known_bdfs: std::collections::HashSet<&str> =
        devices.iter().map(|device| device.bdf.as_str()).collect();
    if let Some(device) = devices.iter().find(|device| {
        device
            .upstream_bridge
            .as_deref()
            .is_some_and(|bridge| !known_bdfs.contains(bridge))
    }) {
        return Err(format!(
            "PCI snapshot derived upstream BDF for {} is absent from snapshot rows",
            device.bdf
        ));
    }
    Ok(PciSnapshot { boot_id, devices })
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BootInventory {
    pub boot_ids: Vec<String>,
}

pub fn parse_boot_inventory(source: &str) -> Result<BootInventory, String> {
    let mut boot_ids = Vec::new();
    for line in source.lines() {
        for field in line.split_whitespace() {
            if let Ok(id) = normalize_boot_id(field) {
                boot_ids.push(id);
                break;
            }
        }
    }
    if boot_ids.is_empty() {
        return Err("boot inventory contains no canonical boot IDs".into());
    }
    Ok(BootInventory { boot_ids })
}

pub fn normalize_boot_id(value: &str) -> Result<String, String> {
    let compact: String = value
        .chars()
        .filter(|character| *character != '-')
        .collect();
    if compact.len() != 32
        || !compact
            .chars()
            .all(|character| character.is_ascii_hexdigit())
    {
        return Err(format!(
            "invalid boot ID {value:?}; expected 32 hexadecimal digits"
        ));
    }
    Ok(compact.to_ascii_lowercase())
}

#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
pub struct Baseline {
    pub learned_boot_id: String,
    pub learned_at: String,
    pub slot: String,
    pub vendor: String,
    pub device: String,
    pub class: String,
    pub parent_bridge_chain: Vec<String>,
    pub upstream_bridge: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
pub struct PendingFinding {
    pub verdict: String,
    pub observation_boot_id: String,
    pub observed_at: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
pub struct D3State {
    pub schema_version: u32,
    pub baseline: Option<Baseline>,
    pub pending: Option<PendingFinding>,
}

impl D3State {
    fn empty() -> Self {
        Self {
            schema_version: STATE_SCHEMA_VERSION,
            baseline: None,
            pending: None,
        }
    }
}

#[derive(Clone, Debug)]
pub struct D3Input {
    pub platform: Platform,
    pub snapshot: PciSnapshot,
    pub current_boot_id: String,
    pub boot_inventory: Option<BootInventory>,
    pub current_journal: Option<String>,
    pub prior_tail: Option<String>,
    /// Set before combining independently journal-ordered prior sources.  A
    /// merged stream is evidence, not an authority on either source's order.
    pub prior_timing_unreliable: bool,
    pub collection_missing: Vec<String>,
    pub host: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Operation {
    Diagnose,
    Learn { slot: Option<String> },
    Reset,
}

/// Arguments supplied by the Clap adapter. Keeping this separate from
/// `D3Input` makes the core independently replayable and prevents an offline
/// invocation from silently filling a missing fixture with live evidence.
#[derive(Clone, Debug, Default)]
pub struct CliOptions {
    pub offline: bool,
    pub journal_current: Option<PathBuf>,
    pub journal_prior_kernel: Option<PathBuf>,
    pub journal_prior_tail: Option<PathBuf>,
    pub pci_snapshot: Option<PathBuf>,
    pub boot_list: Option<PathBuf>,
    pub current_boot_id: Option<String>,
    pub state_file: Option<PathBuf>,
    pub slot: Option<String>,
    pub json: bool,
    pub host: Option<String>,
    pub learn_baseline: bool,
    pub reset_baseline: bool,
}

pub fn run_cli(options: CliOptions) -> ExitCode {
    contract::emit(CONTRACT, cli_outcome(options.clone()), options.json)
}

fn cli_outcome(options: CliOptions) -> Result<Outcome, String> {
    let supplied_fixture = options.journal_current.is_some()
        || options.journal_prior_kernel.is_some()
        || options.journal_prior_tail.is_some()
        || options.pci_snapshot.is_some()
        || options.boot_list.is_some()
        || options.current_boot_id.is_some();
    if supplied_fixture && !options.offline {
        return Err("offline fixture flags require explicit --offline".into());
    }
    if options.learn_baseline && options.reset_baseline {
        return Err("--learn-baseline and --reset-baseline are mutually exclusive".into());
    }
    if options.reset_baseline && options.slot.is_some() {
        return Err("--reset-baseline cannot be combined with --slot".into());
    }
    let operation = if options.reset_baseline {
        Operation::Reset
    } else if options.learn_baseline {
        Operation::Learn {
            slot: options.slot.clone(),
        }
    } else {
        Operation::Diagnose
    };
    let state_file = options
        .state_file
        .clone()
        .unwrap_or_else(default_state_file);
    if options.offline {
        if options.pci_snapshot.is_none() {
            return Err("offline gpu-boot diagnosis requires --pci-snapshot".into());
        }
        if options.state_file.is_none() {
            return Err("offline gpu-boot diagnosis requires a non-default --state-file".into());
        }
        if options.boot_list.is_none() && options.current_boot_id.is_none() {
            return Err(
                "offline gpu-boot diagnosis requires --boot-list or --current-boot-id".into(),
            );
        }
        let snapshot_text = read_regular_text(options.pci_snapshot.as_deref().expect("checked"))?;
        let snapshot = parse_pci_snapshot(&snapshot_text)?;
        let current_boot_id = options
            .current_boot_id
            .as_deref()
            .map(normalize_boot_id)
            .transpose()?
            .unwrap_or_else(|| snapshot.boot_id.clone());
        let boot_inventory_source = options
            .boot_list
            .as_deref()
            .map(read_regular_text)
            .transpose()?;
        let boot_inventory = boot_inventory_source
            .as_deref()
            .map(parse_boot_inventory)
            .transpose()?;
        let mut collection_missing = Vec::new();
        let current_journal = read_offline_journal_option(
            options.journal_current.as_deref(),
            false,
            "same-boot journal",
            &mut collection_missing,
        )?;
        let prior_kernel = read_offline_journal_option(
            options.journal_prior_kernel.as_deref(),
            true,
            "prior boot kernel journal",
            &mut collection_missing,
        )?;
        let prior_tail = read_offline_journal_option(
            options.journal_prior_tail.as_deref(),
            false,
            "prior boot journal tail",
            &mut collection_missing,
        )?;
        validate_supplied_journal_pairing(
            boot_inventory_source.as_deref(),
            &current_boot_id,
            current_journal.as_deref(),
            prior_kernel.as_deref(),
            prior_tail.as_deref(),
            &mut collection_missing,
        )?;
        let prior_timing_unreliable = prior_kernel
            .as_deref()
            .is_some_and(journal_timestamps_non_monotonic)
            || prior_tail
                .as_deref()
                .is_some_and(journal_timestamps_non_monotonic);
        return run(
            D3Input {
                // Offline fixtures model Linux PCI/journald evidence and must
                // remain replayable on Windows CI hosts.
                platform: Platform::Linux,
                snapshot,
                current_boot_id,
                boot_inventory,
                current_journal,
                // The full end-tail is authoritative for the end timestamp. A
                // supplied kernel excerpt can add precursor lines without
                // changing which entry defines the tail's end.
                prior_tail: combine_prior(prior_kernel, prior_tail),
                prior_timing_unreliable,
                collection_missing,
                host: options.host,
            },
            operation,
            &state_file,
        );
    }
    live_outcome(operation, state_file, options.host)
}

fn combine_prior(kernel: Option<String>, tail: Option<String>) -> Option<String> {
    match (kernel, tail) {
        (_, None) => None,
        (None, Some(tail)) => Some(tail),
        (Some(kernel), Some(tail)) => {
            // Cross-source overlap is deduplicated, but repeated records in
            // either authoritative source remain repeated evidence.
            let kernel_records = journal_records(&kernel);
            let tail_records = journal_records(&tail);
            let mut kernel_counts: HashMap<String, usize> = HashMap::new();
            for record in &kernel_records {
                *kernel_counts.entry(record.text.clone()).or_insert(0usize) += 1;
            }
            let mut tail_seen: HashMap<String, usize> = HashMap::new();
            let mut unique_tail = Vec::new();
            for record in tail_records {
                let seen = tail_seen.entry(record.text.clone()).or_insert(0usize);
                *seen += 1;
                if *seen > kernel_counts.get(&record.text).copied().unwrap_or(0) {
                    unique_tail.push(record);
                }
            }
            Some(stable_merge_journal_records(kernel_records, unique_tail))
        }
    }
}

#[derive(Debug)]
struct JournalRecord {
    timestamp: Option<DateTime<FixedOffset>>,
    text: String,
}

fn journal_records(value: &str) -> Vec<JournalRecord> {
    let mut records = Vec::new();
    for line in value.lines() {
        if let Some((timestamp, _)) = parse_journal_line(line) {
            records.push(JournalRecord {
                timestamp: Some(timestamp),
                text: line.into(),
            });
        } else if line.starts_with(char::is_whitespace) {
            if let Some(record) = records.last_mut() {
                record.text.push('\n');
                record.text.push_str(line);
            } else {
                records.push(JournalRecord {
                    timestamp: None,
                    text: line.into(),
                });
            }
        } else {
            records.push(JournalRecord {
                timestamp: None,
                text: line.into(),
            });
        }
    }
    records
}

fn stable_merge_journal_records(kernel: Vec<JournalRecord>, tail: Vec<JournalRecord>) -> String {
    let mut kernel = kernel.into_iter().peekable();
    let mut tail = tail.into_iter().peekable();
    let mut merged = Vec::new();
    while kernel.peek().is_some() || tail.peek().is_some() {
        let take_kernel = match (kernel.peek(), tail.peek()) {
            (Some(kernel), Some(tail)) => match (kernel.timestamp, tail.timestamp) {
                // Equal timestamps retain kernel-stream order, making this a
                // stable merge while retaining both distinct records.
                (Some(kernel), Some(tail)) => kernel <= tail,
                // Invalid/non-journal records retain the historical source
                // ordering; journal evidence itself is timestamp-merged.
                _ => true,
            },
            (Some(_), None) => true,
            (None, Some(_)) => false,
            (None, None) => unreachable!(),
        };
        let record = if take_kernel {
            kernel.next().expect("peeked")
        } else {
            tail.next().expect("peeked")
        };
        merged.push(record.text);
    }
    merged.join("\n")
}

fn default_state_file() -> PathBuf {
    let root = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))
        .unwrap_or_else(|| PathBuf::from(".local/state"));
    root.join("rigsignal/detectors/d3-gpu-boot.json")
}

fn read_regular_text(path: &Path) -> Result<String, String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect {}: {error}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(format!(
            "{} must be a regular non-symlink file",
            path.display()
        ));
    }
    if metadata.len() > READ_LIMIT {
        return Err(format!("{} exceeds bounded read limit", path.display()));
    }
    fs::read_to_string(path).map_err(|error| format!("cannot read {}: {error}", path.display()))
}

fn read_offline_journal_option(
    path: Option<&Path>,
    retain_start: bool,
    label: &str,
    missing: &mut Vec<String>,
) -> Result<Option<String>, String> {
    let Some(path) = path else {
        return Ok(None);
    };
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect {}: {error}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(format!(
            "{} must be a regular non-symlink file",
            path.display()
        ));
    }
    if metadata.len() > OFFLINE_JOURNAL_ABSURD_LIMIT {
        return Err(format!(
            "{} exceeds offline journal safety limit",
            path.display()
        ));
    }
    let source = fs::read_to_string(path)
        .map_err(|error| format!("cannot read {}: {error}", path.display()))?;
    if journal_has_no_entries(&source) {
        missing.push(format!(
            "{label} unavailable: journalctl reported no entries"
        ));
        return Ok(None);
    }
    let was_truncated = source.len() > JOURNAL_LIMIT;
    let bounded = if retain_start {
        bounded_start(&source)
    } else {
        bounded_end(&source)
    };
    if was_truncated {
        missing.push(format!("{label} was bounded to the D3 journal read limit"));
    }
    Ok(Some(bounded))
}

#[derive(Debug)]
struct InventoryWindow {
    start: NaiveDateTime,
    end: NaiveDateTime,
}

fn validate_supplied_journal_pairing(
    inventory_source: Option<&str>,
    current_boot_id: &str,
    current: Option<&str>,
    prior_kernel: Option<&str>,
    prior_tail: Option<&str>,
    missing: &mut Vec<String>,
) -> Result<(), String> {
    if current.is_none() && prior_kernel.is_none() && prior_tail.is_none() {
        return Ok(());
    }
    let source = inventory_source.ok_or_else(|| {
        "boot identity pairing invalid: supplied journal fixtures require a boot inventory"
            .to_string()
    })?;
    let inventory = parse_boot_inventory(source)?;
    let prior = prior_for(&inventory, current_boot_id);
    let windows = parse_inventory_windows(source)?;
    if let Some(journal) = current {
        validate_journal_window(journal, "same-boot journal", current_boot_id, &windows)?;
    }
    if let Some(journal) = prior_kernel {
        if let Some(prior) = prior.as_deref() {
            validate_journal_window(journal, "prior boot kernel journal", prior, &windows)?;
        } else {
            missing.push(
                "supplied prior boot kernel journal could not be attributed to a paired prior boot"
                    .into(),
            );
        }
    }
    if let Some(journal) = prior_tail {
        if let Some(prior) = prior.as_deref() {
            validate_journal_window(journal, "prior boot journal tail", prior, &windows)?;
        } else {
            missing.push(
                "supplied prior boot journal tail could not be attributed to a paired prior boot"
                    .into(),
            );
        }
    }
    Ok(())
}

fn parse_inventory_windows(source: &str) -> Result<HashMap<String, InventoryWindow>, String> {
    let mut windows = HashMap::new();
    for line in source.lines() {
        let fields: Vec<_> = line.split_whitespace().collect();
        let Some(index) = fields
            .iter()
            .position(|field| normalize_boot_id(field).is_ok())
        else {
            continue;
        };
        // journalctl --list-boots prints weekday, date, time, zone twice.
        let Some(start_fields) = fields.get(index + 1..index + 5) else {
            continue;
        };
        let Some(end_fields) = fields.get(index + 5..index + 9) else {
            continue;
        };
        let start = parse_inventory_time(start_fields)?;
        let end = parse_inventory_time(end_fields)?;
        let id = normalize_boot_id(fields[index])?;
        windows.insert(id, InventoryWindow { start, end });
    }
    Ok(windows)
}

fn parse_inventory_time(fields: &[&str]) -> Result<NaiveDateTime, String> {
    if fields.len() != 4 {
        return Err("boot inventory timestamp is malformed".into());
    }
    NaiveDateTime::parse_from_str(
        &format!("{} {} {}", fields[0], fields[1], fields[2]),
        "%a %Y-%m-%d %H:%M:%S",
    )
    .map_err(|_| "boot inventory timestamp is malformed".into())
}

fn validate_journal_window(
    journal: &str,
    label: &str,
    boot_id: &str,
    windows: &HashMap<String, InventoryWindow>,
) -> Result<(), String> {
    let window = windows.get(boot_id).ok_or_else(|| {
        format!("boot identity pairing invalid: {label} has no attributable boot window")
    })?;
    let rows = fold_journal_records(journal);
    let (Some((first, _)), Some((last, _))) = (rows.first(), rows.last()) else {
        return Err(format!(
            "boot identity pairing invalid: {label} contains no attributable journal timestamps"
        ));
    };
    // The list-boots bounds are display-resolution inventory metadata, while
    // an end-tail can include shutdown records emitted shortly after its last
    // indexed entry. Keep that bounded grace, but reject fixtures clearly
    // belonging to another effective boot.
    let grace = chrono::Duration::seconds(INVENTORY_WINDOW_GRACE_S);
    if first.naive_local() < window.start - grace || last.naive_local() > window.end + grace {
        return Err(format!(
            "boot identity pairing invalid: {label} cannot be paired with the effective boot"
        ));
    }
    Ok(())
}

fn journal_has_no_entries(value: &str) -> bool {
    value.trim() == "-- No entries --"
}

#[cfg(target_os = "linux")]
fn live_outcome(
    operation: Operation,
    state_file: PathBuf,
    host: Option<String>,
) -> Result<Outcome, String> {
    let boot_id_text = read_regular_text(Path::new("/proc/sys/kernel/random/boot_id"))?;
    let current_boot_id = normalize_live_boot_id(&boot_id_text)?;
    let snapshot = collect_sysfs_snapshot(&current_boot_id)?;
    let mut collection_missing = Vec::new();
    let inventory = match journal_command(&["--list-boots"]) {
        Ok(text) => match parse_boot_inventory(&text) {
            Ok(value) => Some(value),
            Err(error) => {
                collection_missing.push(redacted_collection_failure(
                    format!("boot inventory unavailable: {error}"),
                    host.as_deref(),
                ));
                None
            }
        },
        Err(error) => {
            collection_missing.push(redacted_collection_failure(
                format!("boot inventory unavailable: {error}"),
                host.as_deref(),
            ));
            None
        }
    };
    let mut current_journal = None;
    let mut prior_tail = None;
    let mut prior_timing_unreliable = false;
    // Journal failures are deliberately non-fatal: sysfs remains authoritative.
    match journal_command(&["--boot", &current_boot_id, "-k"]) {
        Ok(value) if journal_has_no_entries(&value) => collection_missing
            .push("same-boot kernel journal unavailable: journalctl reported no entries".into()),
        Ok(value) => current_journal = Some(value),
        Err(error) => collection_missing.push(redacted_collection_failure(
            format!("same-boot kernel journal unavailable: {error}"),
            host.as_deref(),
        )),
    }
    if let Some(prior) = inventory
        .as_ref()
        .and_then(|value| prior_for(value, &current_boot_id))
    {
        match (
            journal_command(&["--boot", &prior, "-k"]),
            journal_tail(&prior),
        ) {
            (Ok(kernel), Ok(tail)) => {
                let kernel = if journal_has_no_entries(&kernel) {
                    collection_missing.push(
                        "prior boot kernel journal unavailable: journalctl reported no entries"
                            .into(),
                    );
                    None
                } else {
                    Some(kernel)
                };
                let tail = if journal_has_no_entries(&tail) {
                    collection_missing.push(
                        "prior boot journal tail unavailable: journalctl reported no entries"
                            .into(),
                    );
                    None
                } else {
                    Some(tail)
                };
                prior_timing_unreliable = kernel
                    .as_deref()
                    .is_some_and(journal_timestamps_non_monotonic)
                    || tail
                        .as_deref()
                        .is_some_and(journal_timestamps_non_monotonic);
                prior_tail = combine_prior(kernel, tail);
            }
            (kernel, tail) => collection_missing.push(redacted_collection_failure(
                format!(
                    "prior boot journal unavailable: {}{}",
                    kernel.err().unwrap_or_default(),
                    tail.err()
                        .map(|error| format!(" {error}"))
                        .unwrap_or_default()
                ),
                host.as_deref(),
            )),
        }
    }
    run(
        D3Input {
            platform: Platform::Linux,
            snapshot,
            current_boot_id,
            boot_inventory: inventory,
            current_journal,
            prior_tail,
            prior_timing_unreliable,
            collection_missing,
            host,
        },
        operation,
        &state_file,
    )
}

fn normalize_live_boot_id(value: &str) -> Result<String, String> {
    normalize_boot_id(value.trim())
}

#[cfg(not(target_os = "linux"))]
fn live_outcome(
    _operation: Operation,
    _state_file: PathBuf,
    _host: Option<String>,
) -> Result<Outcome, String> {
    Ok(Outcome::NotApplicable(NotApplicable::new(
        "Live D3 collection is only available on Linux.",
        vec!["platform: non-Linux".into()],
    )))
}

#[cfg(target_os = "linux")]
fn collect_sysfs_snapshot(boot_id: &str) -> Result<PciSnapshot, String> {
    let mut devices = Vec::new();
    for entry in fs::read_dir("/sys/bus/pci/devices")
        .map_err(|error| format!("cannot enumerate PCI sysfs: {error}"))?
    {
        let entry = entry.map_err(|error| format!("cannot read PCI sysfs entry: {error}"))?;
        let bdf = entry.file_name().to_string_lossy().to_ascii_lowercase();
        if !valid_bdf(&bdf) {
            continue;
        }
        let root = entry.path();
        let vendor = read_regular_text(&root.join("vendor"))?
            .trim()
            .to_ascii_lowercase();
        let device = read_regular_text(&root.join("device"))?
            .trim()
            .to_ascii_lowercase();
        let class = read_regular_text(&root.join("class"))?
            .trim()
            .to_ascii_lowercase();
        if !valid_hex_field(&vendor, 4)
            || !valid_hex_field(&device, 4)
            || !valid_hex_field(&class, 6)
        {
            return Err(format!("PCI sysfs identity malformed for {bdf}"));
        }
        let canonical = fs::canonicalize(&root)
            .map_err(|error| format!("cannot resolve PCI parent chain for {bdf}: {error}"))?;
        let parent_path = canonical.to_string_lossy().into_owned();
        let chain: Vec<String> = parent_path
            .split('/')
            .filter(|part| valid_bdf(part))
            .map(str::to_ascii_lowercase)
            .collect();
        devices.push(PciDevice {
            bdf,
            vendor,
            device,
            class,
            upstream_bridge: chain.iter().rev().nth(1).cloned(),
            parent_path,
        });
    }
    if devices.is_empty() {
        return Err("PCI sysfs contains no devices".into());
    }
    Ok(PciSnapshot {
        boot_id: boot_id.into(),
        devices,
    })
}

#[cfg(target_os = "linux")]
fn journal_command(extra: &[&str]) -> Result<String, String> {
    journal_command_bounded(extra, false)
}

#[cfg(target_os = "linux")]
fn journal_command_start(extra: &[&str]) -> Result<String, String> {
    journal_command_bounded(extra, true)
}

#[cfg(target_os = "linux")]
fn journal_command_bounded(extra: &[&str], retain_start: bool) -> Result<String, String> {
    let mut args = vec!["--no-pager", "-o", "short-iso-precise"];
    args.extend_from_slice(extra);
    let mut child = Command::new("journalctl")
        .args(&args)
        .env("LC_ALL", "C")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("journalctl spawn failed: {error}"))?;
    let started = std::time::Instant::now();
    while child
        .try_wait()
        .map_err(|error| format!("journalctl wait failed: {error}"))?
        .is_none()
    {
        if started.elapsed() > JOURNAL_TIMEOUT {
            let _ = child.kill();
            return Err("journalctl timed out".into());
        }
        thread::sleep(Duration::from_millis(20));
    }
    let output = child
        .wait_with_output()
        .map_err(|error| format!("journalctl output failed: {error}"))?;
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        return Err(format!(
            "journalctl failed ({}): {}",
            output.status,
            truncate(&stderr)
        ));
    }
    if !stderr.trim().is_empty() {
        return Err(format!("journalctl wrote stderr: {}", truncate(&stderr)));
    }
    let text = String::from_utf8_lossy(&output.stdout).into_owned();
    if retain_start {
        Ok(bounded_start(&text))
    } else {
        Ok(bounded_end(&text))
    }
}

#[cfg(target_os = "linux")]
fn journal_tail(boot_id: &str) -> Result<String, String> {
    // --reverse starts at the boot's final entry. We retain an end-oriented
    // window, then restore chronological order for the core parser.
    let reversed = journal_command_start(&["--boot", boot_id, "--reverse", "-n", "2048"])?;
    let mut lines: Vec<&str> = reversed.lines().collect();
    let terminal = lines
        .first()
        .map(|line| line.trim())
        .unwrap_or_default()
        .to_string();
    let last = journal_command(&["--boot", boot_id, "-n", "1"])?;
    let expected = last.lines().last().map(str::trim).unwrap_or_default();
    if terminal != expected {
        return Err("journal tail does not reach this boot's final inventory entry".into());
    }
    lines.reverse();
    Ok(lines.join("\n"))
}

fn bounded_end(value: &str) -> String {
    if value.len() <= JOURNAL_LIMIT {
        return value.into();
    }
    let start = value.len() - JOURNAL_LIMIT;
    let start = preceding_char_boundary(value, start);
    let start = value[start..]
        .find('\n')
        .map(|offset| start + offset + 1)
        .unwrap_or(start);
    value[start..].to_string()
}

fn bounded_start(value: &str) -> String {
    if value.len() <= JOURNAL_LIMIT {
        return value.into();
    }
    let end = preceding_char_boundary(value, JOURNAL_LIMIT);
    let end = value[..end].rfind('\n').unwrap_or(end);
    value[..end].to_string()
}

fn preceding_char_boundary(value: &str, mut index: usize) -> usize {
    while index > 0 && !value.is_char_boundary(index) {
        index -= 1;
    }
    index
}

#[cfg(target_os = "linux")]
fn truncate(value: &str) -> String {
    value.chars().take(512).collect()
}

/// The stateful boundary used by the future CLI.  It locks a stable sidecar,
/// validates inputs before choosing a verdict, commits a transition, then
/// returns the ready-to-emit contract outcome.
pub fn run(input: D3Input, operation: Operation, state_file: &Path) -> Result<Outcome, String> {
    if input.platform == Platform::Other {
        return Ok(Outcome::NotApplicable(NotApplicable::new(
            "D3 GPU boot diagnosis is only applicable to Linux PCI and journald evidence.",
            vec!["platform: non-Linux".into()],
        )));
    }
    preflight(&input)?;
    let store = StateStore::new(state_file);
    let _lock = store.lock()?;
    let mut state = store.load()?;
    match operation {
        Operation::Reset => {
            state.baseline = None;
            state.pending = None;
            store.save(&state)?;
            Ok(success(
                "baseline-reset",
                "D3 baseline and any pending finding were removed.",
                input.host,
            ))
        }
        Operation::Learn { slot } => {
            if state.baseline.is_some() {
                return Err(
                    "learn refused: an existing baseline must be reset before learning".into(),
                );
            }
            let slot = slot
                .ok_or_else(|| "learn refused: explicit --slot is required".to_string())?
                .to_ascii_lowercase();
            let device = input
                .snapshot
                .devices
                .iter()
                .find(|device| device.bdf == slot)
                .ok_or_else(|| {
                    "learn refused: slot is not present in authoritative PCI snapshot".to_string()
                })?;
            let duplicates = input
                .snapshot
                .devices
                .iter()
                .filter(|candidate| {
                    candidate.vendor == device.vendor
                        && candidate.device == device.device
                        && candidate.class == device.class
                })
                .count();
            if duplicates > 1 {
                return Err(
                    "learn refused: selected identity is ambiguous in the PCI snapshot".into(),
                );
            }
            state.baseline = Some(baseline_from(device, &input.snapshot.boot_id));
            state.pending = None;
            store.save(&state)?;
            Ok(success(
                "baseline-learned",
                &format!(
                    "D3 baseline learned for {} [{}:{}] at boot {}.",
                    device.bdf, device.vendor, device.device, input.snapshot.boot_id
                ),
                input.host,
            ))
        }
        Operation::Diagnose => {
            if let Some(baseline) = state.baseline.as_ref() {
                let baseline_present =
                    input.snapshot.devices.iter().any(|device| {
                        device.bdf == baseline.slot && device.identity_matches(baseline)
                    });
                if baseline_present
                    && input.current_journal.as_deref().is_some_and(|journal| {
                        link_down_for(journal, baseline.upstream_bridge.as_deref())
                    })
                {
                    return Err("fixture consistency invalid: healthy baseline PCI snapshot contradicts same-boot absent-GPU journal evidence".into());
                }
            }
            let mut outcome = evaluate(&input, &mut state)?;
            if let Outcome::Diagnosis(diagnosis) = &mut outcome {
                diagnosis.missing_evidence.extend(
                    input
                        .collection_missing
                        .iter()
                        .map(|item| redact_collection_missing(item, input.host.as_deref())),
                );
            }
            store.save(&state)?;
            Ok(outcome)
        }
    }
}

fn preflight(input: &D3Input) -> Result<(), String> {
    let current = normalize_boot_id(&input.current_boot_id)?;
    if current != input.snapshot.boot_id {
        return Err(
            "boot identity pairing invalid: current boot ID does not match PCI snapshot".into(),
        );
    }
    if let Some(inventory) = &input.boot_inventory {
        if !inventory.boot_ids.iter().any(|id| id == &current) {
            return Err(
                "boot identity pairing invalid: current boot is absent from inventory".into(),
            );
        }
    }
    Ok(())
}

fn canonical_boot_id(value: &str) -> bool {
    normalize_boot_id(value).is_ok_and(|normalized| normalized == value)
}

fn valid_timestamp(value: &str) -> bool {
    DateTime::parse_from_rfc3339(value).is_ok()
}

fn canonical_bdf(value: &str) -> bool {
    valid_bdf(value) && value == value.to_ascii_lowercase()
}

fn canonical_hex_field(value: &str, width: usize) -> bool {
    valid_hex_field(value, width) && value == value.to_ascii_lowercase()
}

fn validate_state(state: &D3State) -> Result<(), String> {
    let Some(baseline) = state.baseline.as_ref() else {
        if state.pending.is_some() {
            return Err("D3 state corrupt: pending finding has no baseline".into());
        }
        return Ok(());
    };
    if !canonical_boot_id(&baseline.learned_boot_id)
        || !valid_timestamp(&baseline.learned_at)
        || !canonical_bdf(&baseline.slot)
        || !canonical_hex_field(&baseline.vendor, 4)
        || !canonical_hex_field(&baseline.device, 4)
        || !canonical_hex_field(&baseline.class, 6)
        || baseline
            .parent_bridge_chain
            .iter()
            .any(|bdf| !canonical_bdf(bdf))
        || baseline
            .upstream_bridge
            .as_deref()
            .is_some_and(|bdf| !canonical_bdf(bdf))
    {
        return Err("D3 state corrupt: baseline contains non-canonical fields".into());
    }
    if let Some(pending) = &state.pending {
        if pending.verdict != "bus-absent"
            || !canonical_boot_id(&pending.observation_boot_id)
            || !valid_timestamp(&pending.observed_at)
        {
            return Err("D3 state corrupt: invalid pending finding".into());
        }
    }
    Ok(())
}

fn evaluate(input: &D3Input, state: &mut D3State) -> Result<Outcome, String> {
    let Some(baseline) = state.baseline.as_ref() else {
        return diagnosis(
            Disposition::NonFinding,
            "baseline-required",
            1.0,
            "No explicit D3 baseline has been learned.",
            vec!["authoritative PCI snapshot parsed".into()],
            "An explicit GPU slot baseline is required before D3 can compare boot enumeration.",
            vec!["Re-run with an explicit slot using the learn-baseline operation.".into()],
            "A learned baseline with a healthy matching GPU would falsify this setup-only result.",
            vec![],
            "No hardware condition has been assessed.",
            input.host.clone(),
        );
    };
    let matching: Vec<&PciDevice> = input
        .snapshot
        .devices
        .iter()
        .filter(|device| device.identity_matches(baseline))
        .collect();
    let at_slot = input
        .snapshot
        .devices
        .iter()
        .find(|device| device.bdf == baseline.slot);
    if matching.len() > 1
        || matching
            .first()
            .is_some_and(|device| device.bdf != baseline.slot)
        || (matching.is_empty() && at_slot.is_some_and(|device| !device.is_bridge()))
    {
        let detail = if matching.len() > 1 {
            "expected identity is ambiguous at multiple BDFs"
        } else if matching
            .first()
            .is_some_and(|device| device.bdf != baseline.slot)
        {
            "expected identity uniquely relocated to another BDF"
        } else {
            "a different non-bridge endpoint occupies the expected BDF"
        };
        state.pending = None;
        return diagnosis(Disposition::Finding, "hardware-changed", 0.95, "Authoritative PCI identity differs from the learned slot baseline.", vec![format!("PCI identity observation: {detail}"), format!("baseline slot: {} [{}:{}]", baseline.slot, baseline.vendor, baseline.device)], "The current PCI topology is most consistent with an intentional or physical hardware/topology change, not a GPU power-state latch.", vec!["Verify the installed device and re-learn the baseline if this change is intentional.".into()], "The expected identity at the learned BDF on a later snapshot would falsify the topology-change interpretation.", vec!["Current PCI snapshot only; it does not establish why the topology changed.".into()], "A transient enumeration problem remains possible but is less consistent with the observed identity change.", input.host.clone());
    }
    if matching.is_empty() && at_slot.is_none_or(|device| device.is_bridge()) {
        let corroborated = input
            .current_journal
            .as_deref()
            .is_some_and(|journal| link_down_for(journal, baseline.upstream_bridge.as_deref()));
        let mut missing = Vec::new();
        if input
            .current_journal
            .as_deref()
            .is_none_or(journal_has_no_entries)
        {
            missing.push("same-boot journal evidence unavailable".into());
        }
        let confidence = if corroborated { 0.95 } else { 0.72 };
        let evidence = if corroborated {
            vec![
                format!(
                    "expected identity [{}:{}] absent from complete PCI snapshot",
                    baseline.vendor, baseline.device
                ),
                format!("expected BDF {} is empty or bridge-class", baseline.slot),
                format!(
                    "same-boot upstream {} reports link-down/empty-slot",
                    baseline
                        .upstream_bridge
                        .as_deref()
                        .unwrap_or("bridge chain")
                ),
            ]
        } else {
            vec![
                format!(
                    "expected identity [{}:{}] absent from complete PCI snapshot",
                    baseline.vendor, baseline.device
                ),
                format!("expected BDF {} is empty or bridge-class", baseline.slot),
            ]
        };
        state.pending = Some(PendingFinding {
            verdict: "bus-absent".into(),
            observation_boot_id: input.snapshot.boot_id.clone(),
            observed_at: now(),
        });
        return diagnosis(Disposition::Finding, "bus-absent", confidence, if corroborated { "Authoritative absence is corroborated by same-boot upstream link evidence." } else { "Authoritative PCI absence is observed; same-boot link corroboration is unavailable." }, evidence, "The observed absence is most consistent with a GPU enumeration failure; the underlying cause remains undetermined without stronger corroboration.", vec!["Shut down fully and perform a PSU power-drain before retrying; inspect slot seating if the issue recurs.".into()], "The expected GPU identity present at the learned BDF on a fresh boot would falsify the absence finding.", missing, "A loose card, failed device, or deliberate hardware removal is an alternative to a power-state latch.", input.host.clone());
    }
    let prior = input
        .boot_inventory
        .as_ref()
        .and_then(|inventory| prior_for(inventory, &input.snapshot.boot_id));
    let mut precursor_missing = Vec::new();
    if let (Some(prior_id), Some(tail)) = (prior.as_deref(), input.prior_tail.as_deref()) {
        if !journal_has_no_entries(tail) {
            let mut precursor = precursor(tail, baseline);
            precursor.timing_unreliable |= input.prior_timing_unreliable;
            precursor.fires &= !precursor.timing_unreliable;
            if precursor.timing_unreliable {
                precursor_missing.push(
                "prior boot timestamps are non-monotonic in the evaluated window; timing evidence is unreliable"
                    .into(),
            );
            }
            if precursor.suppressed {
                precursor_missing.push(
                "prior boot journal contains printk suppressed-record markers; precursor confidence is degraded"
                    .into(),
            );
            }
            if precursor.fires {
                let mut missing = precursor_missing;
                if precursor.truncated {
                    missing.push(
                        "prior boot tail appears truncated; shutdown completion is unavailable"
                            .into(),
                    );
                }
                let confidence = if precursor.truncated || precursor.suppressed {
                    0.76
                } else {
                    0.88
                };
                return diagnosis(Disposition::Finding, "precursor-warning", confidence, "AMD/amdgpu d3.1 compound precursor thresholds were met on the baseline BDF in the trailing 900-second window.", vec![format!("prior boot: {prior_id}"), format!("baseline BDF {}: SMU-unresponsive={} (minimum {}), reset-attempts={} (minimum {})", baseline.slot, precursor.smu, SMU_UNRESPONSIVE_MIN, precursor.resets, RESET_ATTEMPT_MIN)], "The prior boot evidence is most consistent with an AMD GPU reset/SMU precursor; it does not establish a later boot failure.", vec!["Avoid a warm reboot if the GPU becomes unavailable; capture journal evidence and use a full power drain if needed.".into()], "A later complete prior-boot journal with recovery after every reset would falsify the terminal-failure condition.", missing, "A driver-only recovery event without a power-state latch remains the nearest alternative.", input.host.clone());
            }
        }
    }
    if let Some(pending) = state.pending.clone() {
        if pending.observation_boot_id != input.snapshot.boot_id {
            state.pending = None;
            return diagnosis(Disposition::NonFinding, "recovered", 0.9, "The learned GPU identity is present and a pending bus-absence was recorded on an earlier boot.", vec![format!("current boot: {}", input.snapshot.boot_id), format!("consumed pending {} from boot {}", pending.verdict, pending.observation_boot_id)], "The GPU is present now; this is most consistent with recovery from the earlier observed enumeration absence.", vec!["Keep the baseline and retain future journal evidence if the symptom returns.".into()], "A repeated absence at the learned BDF would falsify the recovery observation.", vec![], "The earlier fault may have been transient rather than a power-state latch.", input.host.clone());
        }
    }
    if prior.is_none()
        || input.prior_tail.is_none()
        || input
            .prior_tail
            .as_deref()
            .is_some_and(journal_has_no_entries)
    {
        let mut missing = vec!["usable paired prior Linux boot history unavailable".into()];
        if input.prior_tail.is_none()
            || input
                .prior_tail
                .as_deref()
                .is_some_and(journal_has_no_entries)
        {
            missing.push("prior boot journal tail unavailable".into());
        }
        return diagnosis(Disposition::NonFinding, "history-unavailable", 0.8, "The learned GPU identity is present, but prior-boot precursor history is unavailable.", vec![format!("baseline identity present at {}", baseline.slot)], "The GPU is present; D3 cannot make a prior-boot precursor claim because history is unavailable.", vec!["Retain journal history across boots for precursor analysis.".into()], "A paired prior boot journal would permit precursor analysis.", missing, "A precursor could have occurred but cannot be assessed from the available history.", input.host.clone());
    }
    let confidence = if precursor_missing.is_empty() {
        0.92
    } else {
        0.76
    };
    diagnosis(Disposition::NonFinding, "ok", confidence, "The learned GPU identity is present and no d3.1 precursor matched on the paired prior boot.", vec![format!("baseline identity present at {}", baseline.slot)], "The available evidence is most consistent with normal GPU enumeration for this boot.", vec!["No corrective action is indicated; retain journal history for future comparisons.".into()], "An authoritative future snapshot with the identity absent would falsify this observation.", precursor_missing, "A failure outside the collected evidence window remains possible.", input.host.clone())
}

#[allow(clippy::too_many_arguments)] // Converts immediately into the shared named field API below.
fn diagnosis(
    disposition: Disposition,
    verdict: &str,
    confidence: f64,
    confidence_basis: &str,
    evidence: Vec<String>,
    plain: &str,
    fixes: Vec<String>,
    falsifier: &str,
    missing: Vec<String>,
    alternative: &str,
    host: Option<String>,
) -> Result<Outcome, String> {
    Ok(Outcome::Diagnosis(Box::new(Diagnosis::build(CONTRACT, DiagnosisFields { disposition, verdict: verdict.into(), confidence, confidence_basis: confidence_basis.into(), evidence, plain_language: plain.into(), suggested_fixes: fixes, falsifier: falsifier.into(), supported_scope: vec!["Explicit PCI slot baseline, authoritative PCI snapshot, and paired Linux boot evidence only.".into()], missing_evidence: missing, nearest_alternative: alternative.into(), host })?)))
}

fn success(verdict: &str, message: &str, host: Option<String>) -> Outcome {
    diagnosis(
        Disposition::NonFinding,
        verdict,
        1.0,
        "Requested state operation completed atomically under the D3 sidecar lock.",
        vec![message.into()],
        message,
        vec!["No further action is required.".into()],
        "A subsequent state read would falsify this confirmation if it differs.",
        vec![],
        "No diagnostic inference was made for this state operation.",
        host,
    )
    .expect("constant success contract is valid")
}

fn baseline_from(device: &PciDevice, boot_id: &str) -> Baseline {
    let mut chain: Vec<String> = device
        .parent_path
        .split('/')
        .filter(|part| valid_bdf(part))
        .map(str::to_ascii_lowercase)
        .collect();
    chain.pop();
    Baseline {
        learned_boot_id: boot_id.into(),
        learned_at: now(),
        slot: device.bdf.clone(),
        vendor: device.vendor.clone(),
        device: device.device.clone(),
        class: device.class.clone(),
        parent_bridge_chain: chain,
        upstream_bridge: device.upstream_bridge.clone(),
    }
}

fn prior_for(inventory: &BootInventory, current: &str) -> Option<String> {
    inventory
        .boot_ids
        .iter()
        .position(|id| id == current)
        .and_then(|index| index.checked_sub(1))
        .and_then(|index| inventory.boot_ids.get(index))
        .cloned()
}

#[derive(Default)]
struct Precursor {
    fires: bool,
    smu: usize,
    resets: usize,
    truncated: bool,
    timing_unreliable: bool,
    suppressed: bool,
}

fn precursor(tail: &str, baseline: &Baseline) -> Precursor {
    let rows = fold_journal_records(tail);
    let Some((end, _)) = rows.last() else {
        return Precursor::default();
    };
    let earliest = *end - chrono::Duration::seconds(PRECURSOR_WINDOW_S);
    let window: Vec<_> = rows
        .into_iter()
        .filter(|(time, _)| *time >= earliest)
        .collect();
    let timing_unreliable = timestamps_regress(&window);
    let scoped: Vec<_> = window
        .iter()
        .filter(|(_, line)| kernel_amdgpu_record(line, &baseline.slot))
        .collect();
    let smu = scoped
        .iter()
        .filter(|(_, line)| line.contains("SMU: response:0xFFFFFFFF"))
        .count();
    let resets: Vec<usize> = scoped
        .iter()
        .enumerate()
        .filter_map(|(index, (_, line))| line.contains("GPU reset begin").then_some(index))
        .collect();
    let recovered_after_each = resets.iter().all(|reset| {
        scoped.iter().skip(*reset + 1).any(|(_, line)| {
            line.contains("recovered through reset") || line.contains("GPU recovered")
        })
    });
    let last_smu_at_end = scoped
        .last()
        .is_some_and(|(_, line)| line.contains("SMU: response:0xFFFFFFFF"));
    let terminal = !resets.is_empty() && !recovered_after_each || last_smu_at_end;
    let truncated = !tail.contains("Reached target Shutdown")
        && !tail.contains("Powering Off")
        && !tail.contains("reboot: Power down");
    Precursor {
        fires: !timing_unreliable
            && smu >= SMU_UNRESPONSIVE_MIN
            && resets.len() >= RESET_ATTEMPT_MIN
            && terminal,
        smu,
        resets: resets.len(),
        truncated,
        timing_unreliable,
        suppressed: tail.contains("printk:") && tail.contains("messages suppressed"),
    }
}

fn journal_timestamps_non_monotonic(journal: &str) -> bool {
    timestamps_regress(&fold_journal_records(journal))
}

fn timestamps_regress(rows: &[(DateTime<FixedOffset>, String)]) -> bool {
    let tolerance = chrono::Duration::milliseconds(TIMING_REGRESSION_TOLERANCE_MS);
    let mut cumulative_regression = chrono::Duration::zero();
    for pair in rows.windows(2) {
        let regression = pair[0].0 - pair[1].0;
        if regression <= chrono::Duration::zero() {
            continue;
        }
        cumulative_regression += regression;
        if regression > tolerance || cumulative_regression > tolerance {
            return true;
        }
    }
    false
}

fn parse_journal_line(line: &str) -> Option<(DateTime<FixedOffset>, &str)> {
    let (stamp, rest) = line.split_once(' ')?;
    Some((DateTime::parse_from_rfc3339(stamp).ok()?, rest))
}

fn fold_journal_records(value: &str) -> Vec<(DateTime<FixedOffset>, String)> {
    let mut records: Vec<(DateTime<FixedOffset>, String)> = Vec::new();
    for line in value.lines() {
        if let Some((stamp, rest)) = parse_journal_line(line) {
            records.push((stamp, rest.into()));
        } else if line.starts_with(char::is_whitespace) {
            if let Some((_, record)) = records.last_mut() {
                record.push('\n');
                record.push_str(line.trim_start());
            }
        }
    }
    records
}

fn kernel_amdgpu_record(record: &str, bdf: &str) -> bool {
    let Some((_, transport_and_message)) = record.split_once(' ') else {
        return false;
    };
    let Some(message) = transport_and_message.strip_prefix("kernel:") else {
        return false;
    };
    let prefix = format!("amdgpu {bdf}:");
    message.trim_start().starts_with(&prefix)
}
fn link_down_for(journal: &str, upstream: Option<&str>) -> bool {
    journal.lines().any(|line| {
        (line.contains("Link Down")
            || line.contains("Card not present")
            || line.contains("Link Active not set"))
            && upstream.is_none_or(|bridge| line.contains(bridge))
    })
}
fn valid_bdf(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 12
        && bytes[4] == b':'
        && bytes[7] == b':'
        && bytes[10] == b'.'
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 4 | 7 | 10) || byte.is_ascii_hexdigit())
}
fn valid_hex_field(value: &str, width: usize) -> bool {
    value.len() == width + 2
        && value.starts_with("0x")
        && value[2..]
            .chars()
            .all(|character| character.is_ascii_hexdigit())
}
fn now() -> String {
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

/// Privacy guard for the rare evidence excerpt retained by collection code.
pub fn redact_excerpt(value: &str, target_host: Option<&str>) -> String {
    let no_controls: String = value
        .chars()
        .filter(|character| !character.is_control())
        .collect();
    let mut words: Vec<String> = Vec::new();
    for (index, word) in no_controls.split_whitespace().enumerate() {
        let clean = word
            .trim_matches(|character: char| matches!(character, ',' | ';' | '(' | ')' | '[' | ']'));
        let redacted = if clean.contains("@") {
            "[REDACTED_USER]".into()
        } else if looks_like_private_path(clean) {
            "[REDACTED_PATH]".into()
        } else if looks_like_mac(clean) {
            "[REDACTED_MAC]".into()
        } else if looks_like_ipv6(clean) {
            "[REDACTED_IPV6]".into()
        } else if looks_like_uuid(clean) {
            "[REDACTED_UUID]".into()
        } else if clean.to_ascii_lowercase().contains("serial")
            || clean.starts_with("SN=")
            || has_long_hex_run(clean)
        {
            "[REDACTED_SERIAL]".into()
        } else if (index == 1
            && looks_like_bare_hostname(clean)
            && target_host.is_none_or(|host| clean != host))
            || clean.contains("hostname=")
            || (clean.contains('.')
                && !clean.contains(':')
                && target_host.is_none_or(|host| clean != host))
        {
            "[REDACTED_HOST]".into()
        } else {
            word.into()
        };
        words.push(redacted);
    }
    words.join(" ").chars().take(1024).collect()
}
fn redacted_collection_failure(value: String, target_host: Option<&str>) -> String {
    redact_collection_missing(&value, target_host)
}

fn redact_collection_missing(value: &str, target_host: Option<&str>) -> String {
    // The prefix comes from this detector. Only journalctl's error text is an
    // untrusted excerpt, so redacting the whole sentence must not erase words
    // such as "boot", "kernel", or "inventory".
    let Some((structure, excerpt)) = value.split_once(": ") else {
        return redact_excerpt(value, target_host);
    };
    format!("{structure}: {}", redact_excerpt(excerpt, target_host))
}
fn looks_like_bare_hostname(value: &str) -> bool {
    value.contains('-') || value.chars().any(char::is_uppercase)
}
fn looks_like_mac(value: &str) -> bool {
    let parts: Vec<_> = value.split(':').collect();
    parts.len() == 6
        && parts
            .iter()
            .all(|part| part.len() == 2 && part.chars().all(|c| c.is_ascii_hexdigit()))
}
fn looks_like_ipv6(value: &str) -> bool {
    let clean = value.trim_matches(|c| matches!(c, '[' | ']' | ',' | ';' | '(' | ')'));
    clean.matches(':').count() >= 2
        && clean
            .chars()
            .all(|c| c.is_ascii_hexdigit() || c == ':' || c == '.')
}
fn looks_like_private_path(value: &str) -> bool {
    value.starts_with("/home/")
        || value.starts_with("/Users/")
        || value.contains("/home/")
        || value.contains("/Users/")
}
fn has_long_hex_run(value: &str) -> bool {
    let mut run = 0;
    for character in value.chars() {
        if character.is_ascii_hexdigit() {
            run += 1;
            if run >= 16 {
                return true;
            }
        } else {
            run = 0;
        }
    }
    false
}
fn looks_like_uuid(value: &str) -> bool {
    normalize_boot_id(value).is_ok()
}

struct StateStore {
    path: PathBuf,
    lock_path: PathBuf,
}
struct StateLock(File);
impl StateStore {
    fn new(path: &Path) -> Self {
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        Self {
            path: path.into(),
            lock_path: parent.join("d3-gpu-boot.lock"),
        }
    }
    fn lock(&self) -> Result<StateLock, String> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("cannot create D3 state directory: {error}"))?;
            restrict_permissions(parent, 0o700)?;
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&self.lock_path)
            .map_err(|error| format!("cannot open D3 sidecar lock: {error}"))?;
        restrict_permissions(&self.lock_path, 0o600)?;
        for _ in 0..50 {
            if file.try_lock_exclusive().is_ok() {
                return Ok(StateLock(file));
            }
            thread::sleep(Duration::from_millis(10));
        }
        Err("D3 state lock contention timed out".into())
    }
    fn load(&self) -> Result<D3State, String> {
        let metadata = match fs::symlink_metadata(&self.path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(D3State::empty())
            }
            Err(error) => return Err(format!("cannot inspect D3 state: {error}")),
        };
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err("D3 state file must be a regular non-symlink file".into());
        }
        if metadata.len() > READ_LIMIT {
            return Err("D3 state file exceeds bounded read limit".into());
        }
        let mut source = String::new();
        File::open(&self.path)
            .map_err(|error| format!("cannot open D3 state: {error}"))?
            .read_to_string(&mut source)
            .map_err(|error| format!("cannot read D3 state: {error}"))?;
        let state: D3State = serde_json::from_str(&source)
            .map_err(|error| format!("cannot parse D3 state: {error}"))?;
        if state.schema_version != STATE_SCHEMA_VERSION {
            return Err(format!(
                "unknown D3 state schema version {}",
                state.schema_version
            ));
        }
        validate_state(&state)?;
        Ok(state)
    }
    fn save(&self, state: &D3State) -> Result<(), String> {
        let parent = self.path.parent().unwrap_or_else(|| Path::new("."));
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| error.to_string())?
            .as_nanos();
        let temporary = parent.join(format!(".d3-gpu-boot-{}-{nonce}.tmp", std::process::id()));
        let encoded = serde_json::to_vec(state)
            .map_err(|error| format!("cannot serialize D3 state: {error}"))?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|error| format!("cannot create atomic D3 state temporary: {error}"))?;
        restrict_permissions(&temporary, 0o600)?;
        file.write_all(&encoded)
            .and_then(|_| file.sync_all())
            .map_err(|error| {
                let _ = fs::remove_file(&temporary);
                format!("cannot write atomic D3 state: {error}")
            })?;
        fs::rename(&temporary, &self.path).map_err(|error| {
            let _ = fs::remove_file(&temporary);
            format!("cannot replace D3 state atomically: {error}")
        })?;
        Ok(())
    }
}

#[cfg(unix)]
fn restrict_permissions(path: &Path, mode: u32) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(|error| {
        format!(
            "cannot restrict permissions for {}: {error}",
            path.display()
        )
    })
}

#[cfg(not(unix))]
fn restrict_permissions(_path: &Path, _mode: u32) -> Result<(), String> {
    Ok(())
}

impl Drop for StateLock {
    fn drop(&mut self) {
        let _ = self.0.unlock();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    const ABSENT: &str = include_str!("../../fixtures/d3/synthetic/absent-gpu-pci-topology.txt");
    const RECOVERY: &str =
        include_str!("../../fixtures/d3/synthetic/recovery-healthy-pci-topology.txt");
    const INVENTORY: &str = include_str!("../../fixtures/d3/synthetic/recovery-boot-inventory.txt");
    const HEALTHY: &str =
        include_str!("../../fixtures/d3/synthetic/precursor-current-healthy-pci-topology.txt");
    const PRECURSOR_INVENTORY: &str =
        include_str!("../../fixtures/d3/synthetic/precursor-boot-inventory.txt");
    fn path() -> PathBuf {
        std::env::temp_dir()
            .join(format!(
                "rigsignal-d3-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::SeqCst)
            ))
            .join("state.json")
    }
    fn input(snapshot: &str, inventory: Option<&str>, tail: Option<&str>) -> D3Input {
        let snapshot = parse_pci_snapshot(snapshot).unwrap();
        D3Input {
            platform: Platform::Linux,
            current_boot_id: snapshot.boot_id.clone(),
            snapshot,
            boot_inventory: inventory.map(|value| parse_boot_inventory(value).unwrap()),
            current_journal: None,
            prior_tail: tail.map(str::to_owned),
            prior_timing_unreliable: false,
            collection_missing: vec![],
            host: None,
        }
    }
    fn verdict(outcome: Outcome) -> String {
        match outcome {
            Outcome::Diagnosis(d) => d.verdict,
            Outcome::NotApplicable(_) => "not-applicable".into(),
        }
    }
    fn diagnosis(outcome: Outcome) -> Box<Diagnosis> {
        match outcome {
            Outcome::Diagnosis(diagnosis) => diagnosis,
            Outcome::NotApplicable(_) => panic!("expected D3 diagnosis"),
        }
    }
    fn learn(state: &Path, source: &str, inventory: Option<&str>) {
        assert_eq!(
            verdict(
                run(
                    input(source, inventory, None),
                    Operation::Learn {
                        slot: Some("0000:03:00.0".into())
                    },
                    state
                )
                .unwrap()
            ),
            "baseline-learned"
        );
    }
    #[test]
    fn boot_ids_normalize_and_schema_rejects_bad_input() {
        assert_eq!(
            normalize_boot_id("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA").unwrap(),
            "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
        );
        assert!(parse_pci_snapshot(include_str!(
            "../../fixtures/d3/synthetic/malformed-pci-topology.txt"
        ))
        .is_err());
    }
    #[test]
    fn combine_prior_deduplicates_overlap_and_preserves_disjoint_order() {
        assert_eq!(
            combine_prior(
                Some("kernel-a\nkernel-b".into()),
                Some("kernel-b\ntail-c".into())
            ),
            Some("kernel-a\nkernel-b\ntail-c".into())
        );
        assert_eq!(
            combine_prior(Some("kernel-a".into()), Some("tail-b".into())),
            Some("kernel-a\ntail-b".into())
        );
    }
    #[test]
    fn combine_prior_handles_empty_inputs() {
        assert_eq!(
            combine_prior(Some(String::new()), Some(String::new())),
            Some(String::new())
        );
        assert_eq!(
            combine_prior(None, Some("tail".into())),
            Some("tail".into())
        );
        assert_eq!(combine_prior(Some("kernel".into()), None), None);
    }

    #[test]
    fn capture_a_kernel_and_tail_merge_without_fabricating_non_monotonic_time() {
        let merged = combine_prior(
            Some(
                include_str!("../../fixtures/d3/real/capture-a/journal-previous-1-kernel.log")
                    .into(),
            ),
            Some(
                include_str!("../../fixtures/d3/real/capture-a/journal-previous-1-tail.log").into(),
            ),
        )
        .unwrap();
        let snapshot = parse_pci_snapshot(include_str!(
            "../../fixtures/d3/real/capture-a/pci-topology.txt"
        ))
        .unwrap();
        let baseline = baseline_from(
            snapshot
                .devices
                .iter()
                .find(|device| device.bdf == "0000:03:00.0")
                .unwrap(),
            &snapshot.boot_id,
        );
        assert!(!precursor(&merged, &baseline).timing_unreliable);
    }

    #[test]
    fn timestamp_merge_keeps_a_precursor_when_tail_starts_before_kernel_overlap() {
        let kernel = "2026-07-21T23:58:00+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:01+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:02+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin\n2026-07-21T23:58:03+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:04+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin";
        let tail = format!("2026-07-21T23:57:59+00:00 host app[1]: userspace record before overlap\n{kernel}\n2026-07-21T23:59:00+00:00 host systemd[1]: Reached target Shutdown.");
        let merged = combine_prior(Some(kernel.into()), Some(tail)).unwrap();
        let snapshot = parse_pci_snapshot(RECOVERY).unwrap();
        let baseline = baseline_from(
            snapshot
                .devices
                .iter()
                .find(|device| device.bdf == "0000:03:00.0")
                .unwrap(),
            &snapshot.boot_id,
        );
        let precursor = precursor(&merged, &baseline);
        assert!(precursor.fires);
        assert!(!precursor.timing_unreliable);
    }
    #[test]
    fn absent_recovery_once_and_bridge_routes_to_absent() {
        let state = path();
        learn(&state, RECOVERY, Some(INVENTORY));
        let mut absent = input(ABSENT, Some(INVENTORY), None);
        absent.current_journal =
            Some(include_str!("../../fixtures/d3/synthetic/absent-gpu-current-journal.log").into());
        assert_eq!(
            verdict(run(absent, Operation::Diagnose, &state).unwrap()),
            "bus-absent"
        );
        assert_eq!(
            verdict(
                run(
                    input(RECOVERY, Some(INVENTORY), None),
                    Operation::Diagnose,
                    &state
                )
                .unwrap()
            ),
            "recovered"
        );
        assert_eq!(
            verdict(
                run(
                    input(
                        RECOVERY,
                        Some(INVENTORY),
                        Some(include_str!(
                            "../../fixtures/d3/synthetic/clean-tail-prior-tail.log"
                        )),
                    ),
                    Operation::Diagnose,
                    &state
                )
                .unwrap()
            ),
            "ok"
        );
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn precedence_hardware_cases_clear_pending() {
        let state = path();
        learn(&state, RECOVERY, Some(INVENTORY));
        for source in [
            include_str!("../../fixtures/d3/synthetic/different-device-at-bdf-pci-topology.txt"),
            include_str!("../../fixtures/d3/synthetic/unique-relocation-pci-topology.txt"),
            include_str!("../../fixtures/d3/synthetic/duplicate-id-ambiguity-pci-topology.txt"),
        ] {
            assert_eq!(
                verdict(
                    run(
                        input(ABSENT, Some(INVENTORY), None),
                        Operation::Diagnose,
                        &state
                    )
                    .unwrap()
                ),
                "bus-absent"
            );
            assert!(StateStore::new(&state).load().unwrap().pending.is_some());
            assert_eq!(
                verdict(run(input(source, None, None), Operation::Diagnose, &state).unwrap()),
                "hardware-changed"
            );
            assert_eq!(StateStore::new(&state).load().unwrap().pending, None);
        }
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn precursor_fixture_matrix_and_history() {
        let state = path();
        learn(&state, HEALTHY, Some(PRECURSOR_INVENTORY));
        for (tail, expected) in [
            (
                include_str!("../../fixtures/d3/synthetic/precursor-prior-tail.log"),
                "precursor-warning",
            ),
            (
                include_str!("../../fixtures/d3/synthetic/threshold-minus-one-prior-tail.log"),
                "ok",
            ),
            (
                include_str!("../../fixtures/d3/synthetic/cross-slot-prior-tail.log"),
                "ok",
            ),
            (
                include_str!("../../fixtures/d3/synthetic/outside-window-prior-tail.log"),
                "ok",
            ),
            (
                include_str!("../../fixtures/d3/synthetic/clean-tail-prior-tail.log"),
                "ok",
            ),
        ] {
            assert_eq!(
                verdict(
                    run(
                        input(HEALTHY, Some(PRECURSOR_INVENTORY), Some(tail)),
                        Operation::Diagnose,
                        &state
                    )
                    .unwrap()
                ),
                expected
            );
        }
        let clean = diagnosis(
            run(
                input(
                    HEALTHY,
                    Some(PRECURSOR_INVENTORY),
                    Some(include_str!(
                        "../../fixtures/d3/synthetic/precursor-prior-tail.log"
                    )),
                ),
                Operation::Diagnose,
                &state,
            )
            .unwrap(),
        );
        assert_eq!(clean.confidence, 0.88);
        let truncated = diagnosis(
            run(
                input(
                    HEALTHY,
                    Some(PRECURSOR_INVENTORY),
                    Some(include_str!(
                        "../../fixtures/d3/synthetic/truncated-tail-prior-tail.log"
                    )),
                ),
                Operation::Diagnose,
                &state,
            )
            .unwrap(),
        );
        assert_eq!(truncated.verdict, "precursor-warning");
        assert_eq!(truncated.confidence, 0.76);
        assert!(truncated.confidence < clean.confidence);
        assert!(truncated
            .missing_evidence
            .iter()
            .any(|item| item.contains("tail appears truncated")));
        assert_eq!(
            verdict(run(input(HEALTHY, None, None), Operation::Diagnose, &state).unwrap()),
            "history-unavailable"
        );
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn baseline_operations_state_failures_and_lock() {
        let state = path();
        assert_eq!(
            verdict(
                run(
                    input(RECOVERY, Some(INVENTORY), None),
                    Operation::Diagnose,
                    &state
                )
                .unwrap()
            ),
            "baseline-required"
        );
        assert!(run(
            input(
                include_str!("../../fixtures/d3/synthetic/multi-gpu-learn-pci-topology.txt"),
                None,
                None
            ),
            Operation::Learn { slot: None },
            &state
        )
        .is_err());
        assert_eq!(
            verdict(
                run(
                    input(
                        include_str!(
                            "../../fixtures/d3/synthetic/multi-gpu-learn-pci-topology.txt"
                        ),
                        None,
                        None
                    ),
                    Operation::Learn {
                        slot: Some("0000:03:00.0".into())
                    },
                    &state
                )
                .unwrap()
            ),
            "baseline-learned"
        );
        assert!(run(
            input(RECOVERY, Some(INVENTORY), None),
            Operation::Learn {
                slot: Some("0000:03:00.0".into())
            },
            &state
        )
        .is_err());
        fs::write(&state, "not json").unwrap();
        assert!(run(
            input(RECOVERY, Some(INVENTORY), None),
            Operation::Diagnose,
            &state
        )
        .is_err());
        fs::write(
            &state,
            r#"{"schema_version":99,"baseline":null,"pending":null}"#,
        )
        .unwrap();
        assert!(run(
            input(RECOVERY, Some(INVENTORY), None),
            Operation::Diagnose,
            &state
        )
        .is_err());
        fs::remove_file(&state).unwrap();
        let store = StateStore::new(&state);
        let lock = store.lock().unwrap();
        assert!(store.lock().is_err());
        drop(lock);
        assert_eq!(
            verdict(
                run(
                    input(RECOVERY, Some(INVENTORY), None),
                    Operation::Reset,
                    &state
                )
                .unwrap()
            ),
            "baseline-reset"
        );
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn contradiction_non_linux_and_redaction() {
        let state = path();
        learn(&state, RECOVERY, Some(INVENTORY));
        let mut contradictory = input(RECOVERY, Some(INVENTORY), None);
        contradictory.current_journal =
            Some(include_str!("../../fixtures/d3/synthetic/absent-gpu-current-journal.log").into());
        assert!(run(contradictory, Operation::Diagnose, &state).is_err());
        let mut other = input(RECOVERY, None, None);
        other.platform = Platform::Other;
        assert_eq!(
            verdict(run(other, Operation::Diagnose, &state).unwrap()),
            "not-applicable"
        );
        let redacted = redact_excerpt("2026-07-21T23:59:00+00:00 HOST-SYNTH alice@private.example mac 01:23:45:67:89:ab SN=SERIAL-123 11111111-1111-4111-8111-111111111111\u{1b}", Some("target"));
        for sensitive in [
            "alice",
            "HOST-SYNTH",
            "private.example",
            "01:23:45:67:89:ab",
            "SERIAL-123",
            "11111111-1111-4111-8111-111111111111",
        ] {
            assert!(!redacted.contains(sensitive), "unredacted {sensitive}");
        }
        assert!(redacted.contains("[REDACTED_USER]"));
        assert!(redacted.contains("[REDACTED_HOST]"));
        assert!(redacted.contains("[REDACTED_MAC]"));
        assert!(redacted.contains("[REDACTED_SERIAL]"));
        assert!(redacted.contains("[REDACTED_UUID]"));
        assert!(!redacted.chars().any(char::is_control));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn multi_gpu_existing_explicit_baseline_is_ok_and_preserves_pending() {
        let state = path();
        let topology = include_str!("../../fixtures/d3/synthetic/multi-gpu-learn-pci-topology.txt");
        let snapshot = parse_pci_snapshot(topology).unwrap();
        let inventory = BootInventory {
            boot_ids: vec![
                "44444444444444448444444444444444".into(),
                snapshot.boot_id.clone(),
            ],
        };
        assert_eq!(
            verdict(
                run(
                    input(topology, None, None),
                    Operation::Learn {
                        slot: Some("0000:03:00.0".into())
                    },
                    &state,
                )
                .unwrap()
            ),
            "baseline-learned"
        );
        let pending = PendingFinding {
            verdict: "bus-absent".into(),
            observation_boot_id: snapshot.boot_id.clone(),
            observed_at: "2026-07-21T00:00:00Z".into(),
        };
        let store = StateStore::new(&state);
        let mut saved = store.load().unwrap();
        saved.pending = Some(pending.clone());
        store.save(&saved).unwrap();
        assert_eq!(
            verdict(
                run(
                    input(
                        topology,
                        Some(&format!(
                            "0 {}\n0 {}",
                            inventory.boot_ids[0], inventory.boot_ids[1]
                        )),
                        Some(include_str!(
                            "../../fixtures/d3/synthetic/clean-tail-prior-tail.log"
                        )),
                    ),
                    Operation::Diagnose,
                    &state,
                )
                .unwrap()
            ),
            "ok"
        );
        assert_eq!(store.load().unwrap().pending, Some(pending));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn frozen_real_and_legacy_rows_are_replayable() {
        for (topology, inventory, slot, tail) in [
            (
                include_str!("../../fixtures/d3/real/capture-a/pci-topology.txt"),
                include_str!("../../fixtures/d3/real/capture-a/boot-inventory.txt"),
                "0000:03:00.0",
                include_str!("../../fixtures/d3/synthetic/clean-tail-prior-tail.log"),
            ),
            (
                include_str!("../../fixtures/d3/real/capture-b/pci-topology.txt"),
                include_str!("../../fixtures/d3/real/capture-b/boot-inventory.txt"),
                "0000:09:00.0",
                include_str!("../../fixtures/d3/synthetic/clean-tail-prior-tail.log"),
            ),
        ] {
            let state = path();
            assert_eq!(
                verdict(
                    run(
                        input(topology, Some(inventory), None),
                        Operation::Learn {
                            slot: Some(slot.into())
                        },
                        &state,
                    )
                    .unwrap()
                ),
                "baseline-learned"
            );
            assert_eq!(
                verdict(
                    run(
                        input(topology, Some(inventory), Some(tail)),
                        Operation::Diagnose,
                        &state
                    )
                    .unwrap()
                ),
                "ok"
            );
            let _ = fs::remove_dir_all(state.parent().unwrap());
        }
        let legacy_slot: serde_json::Value = serde_json::from_str(include_str!(
            "../../fixtures/d3/real/legacy/healthy-slot.json"
        ))
        .unwrap();
        let legacy_inventory = parse_boot_inventory(include_str!(
            "../../fixtures/d3/real/legacy/boot-inventory.txt"
        ))
        .unwrap();
        let legacy_kernel = include_str!("../../fixtures/d3/real/legacy/good-boot-kernel.log");
        assert!(legacy_kernel.lines().any(|line| line.contains(" kernel: ")));
        let legacy_boot = legacy_inventory.boot_ids.last().unwrap();
        let legacy_bdf = legacy_slot["slot"].as_str().unwrap();
        let legacy_vendor = legacy_slot["vendor"].as_str().unwrap();
        let legacy_device = legacy_slot["device"].as_str().unwrap();
        let legacy_class = legacy_slot["class"].as_str().unwrap();
        let legacy_snapshot = parse_pci_snapshot(&format!(
            "# converted authoritative healthy snapshot from healthy-slot.json\n# boot_id={legacy_boot}\n0000:00:03.1 path=pci0000:00/0000:00:03.1 vendor=0x1022 device=0x14db class=0x060400\n{legacy_bdf} path=pci0000:00/0000:00:03.1/{legacy_bdf} vendor={legacy_vendor} device={legacy_device} class={legacy_class}\n"
        ))
        .unwrap();
        let state = path();
        let legacy_input = D3Input {
            platform: Platform::Linux,
            current_boot_id: legacy_snapshot.boot_id.clone(),
            snapshot: legacy_snapshot,
            boot_inventory: Some(legacy_inventory),
            current_journal: Some(legacy_kernel.into()),
            prior_tail: None,
            prior_timing_unreliable: false,
            collection_missing: vec![],
            host: None,
        };
        assert_eq!(
            verdict(
                run(
                    legacy_input.clone(),
                    Operation::Learn {
                        slot: Some(legacy_bdf.into())
                    },
                    &state
                )
                .unwrap()
            ),
            "baseline-learned"
        );
        assert_eq!(
            verdict(run(legacy_input, Operation::Diagnose, &state).unwrap()),
            "history-unavailable"
        );
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn pending_transition_table_preserves_warning_and_writes_no_temps() {
        let state = path();
        learn(&state, RECOVERY, Some(INVENTORY));
        assert_eq!(
            verdict(
                run(
                    input(ABSENT, Some(INVENTORY), None),
                    Operation::Diagnose,
                    &state
                )
                .unwrap()
            ),
            "bus-absent"
        );
        let before = StateStore::new(&state).load().unwrap().pending;
        assert_eq!(
            verdict(
                run(
                    input(
                        HEALTHY,
                        Some(PRECURSOR_INVENTORY),
                        Some(include_str!(
                            "../../fixtures/d3/synthetic/precursor-prior-tail.log"
                        ))
                    ),
                    Operation::Diagnose,
                    &state
                )
                .unwrap()
            ),
            "precursor-warning"
        );
        assert_eq!(StateStore::new(&state).load().unwrap().pending, before);
        let temporary_count = fs::read_dir(state.parent().unwrap())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
            .count();
        assert_eq!(temporary_count, 0);
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn bus_absence_confidence_and_missing_evidence_follow_corroboration() {
        let state = path();
        learn(&state, RECOVERY, Some(INVENTORY));
        let mut corroborated_input = input(ABSENT, Some(INVENTORY), None);
        corroborated_input.current_journal =
            Some(include_str!("../../fixtures/d3/synthetic/absent-gpu-current-journal.log").into());
        let corroborated = diagnosis(run(corroborated_input, Operation::Diagnose, &state).unwrap());
        assert_eq!(corroborated.verdict, "bus-absent");
        assert_eq!(corroborated.confidence, 0.95);
        assert!(corroborated.missing_evidence.is_empty());
        let omitted = diagnosis(
            run(
                input(ABSENT, Some(INVENTORY), None),
                Operation::Diagnose,
                &state,
            )
            .unwrap(),
        );
        assert_eq!(omitted.verdict, "bus-absent");
        assert_eq!(omitted.confidence, 0.72);
        assert!(omitted.confidence < corroborated.confidence);
        assert!(omitted
            .missing_evidence
            .iter()
            .any(|item| item == "same-boot journal evidence unavailable"));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
    #[test]
    fn relocation_and_ambiguity_fixtures_resolve_their_upstream_bridge() {
        for source in [
            include_str!("../../fixtures/d3/synthetic/unique-relocation-pci-topology.txt"),
            include_str!("../../fixtures/d3/synthetic/duplicate-id-ambiguity-pci-topology.txt"),
        ] {
            let snapshot = parse_pci_snapshot(source).unwrap();
            assert_eq!(
                snapshot
                    .devices
                    .iter()
                    .find(|device| device.bdf == "0000:04:00.0")
                    .unwrap()
                    .upstream_bridge
                    .as_deref(),
                Some("0000:00:04.0")
            );
        }
    }
    #[test]
    fn preflight_and_contract_regressions_are_explicit() {
        let state = path();
        let mut bad_pair = input(RECOVERY, Some(INVENTORY), None);
        bad_pair.current_boot_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc".into();
        assert!(run(bad_pair, Operation::Diagnose, &state).is_err());
        assert!(parse_boot_inventory(include_str!(
            "../../fixtures/d3/synthetic/unpairable-boot-inventory.txt"
        ))
        .is_ok());
        let unmatched = input(
            RECOVERY,
            Some(include_str!(
                "../../fixtures/d3/synthetic/unpairable-boot-inventory.txt"
            )),
            None,
        );
        assert!(run(unmatched, Operation::Diagnose, &state).is_err());
        learn(&state, HEALTHY, Some(PRECURSOR_INVENTORY));
        let outcome = run(
            input(
                HEALTHY,
                Some(PRECURSOR_INVENTORY),
                Some(include_str!(
                    "../../fixtures/d3/synthetic/threshold-minus-one-prior-tail.log"
                )),
            ),
            Operation::Diagnose,
            &state,
        )
        .unwrap();
        let json = serde_json::to_value(outcome).unwrap();
        for key in [
            "rule_version",
            "confidence_basis",
            "falsifier",
            "supported_scope",
            "missing_evidence",
            "nearest_alternative",
        ] {
            assert!(json.get(key).is_some(), "missing {key}");
        }
        // The thresholds are independently enforced; four SMU lines cannot be
        // pooled with zero reset attempts to make a warning.
        let pooled = "2026-07-21T23:58:00+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:01+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:02+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:03+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:59:30+00:00 host systemd[1]: Reached target Shutdown.";
        assert_eq!(
            verdict(
                run(
                    input(HEALTHY, Some(PRECURSOR_INVENTORY), Some(pooled)),
                    Operation::Diagnose,
                    &state
                )
                .unwrap()
            ),
            "ok"
        );
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }

    #[test]
    fn live_boot_id_newline_is_trimmed_before_normalization() {
        assert_eq!(
            normalize_live_boot_id("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA\n").unwrap(),
            "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
        );
    }

    #[cfg(unix)]
    #[test]
    fn dangling_state_symlink_is_refused_without_replacement() {
        use std::os::unix::fs::symlink;
        let state = path();
        fs::create_dir_all(state.parent().unwrap()).unwrap();
        symlink(state.parent().unwrap().join("missing-state.json"), &state).unwrap();
        assert!(run(
            input(RECOVERY, Some(INVENTORY), None),
            Operation::Diagnose,
            &state
        )
        .is_err());
        assert!(fs::symlink_metadata(&state)
            .unwrap()
            .file_type()
            .is_symlink());
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }

    #[test]
    fn corrupt_pending_hardware_changed_json_is_refused_on_load() {
        let state = path();
        fs::create_dir_all(state.parent().unwrap()).unwrap();
        fs::write(&state, r#"{"schema_version":1,"baseline":{"learned_boot_id":"bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb","learned_at":"2026-07-21T00:00:00Z","slot":"0000:03:00.0","vendor":"0x1002","device":"0x7550","class":"0x030000","parent_bridge_chain":["0000:00:03.1"],"upstream_bridge":"0000:00:03.1"},"pending":{"verdict":"hardware-changed","observation_boot_id":"aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa","observed_at":"2026-07-21T00:01:00Z"}}"#).unwrap();
        let error = run(
            input(RECOVERY, Some(INVENTORY), None),
            Operation::Diagnose,
            &state,
        )
        .unwrap_err();
        assert!(error.contains("invalid pending finding"));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }

    #[test]
    fn no_entries_journals_are_unavailable_not_clean_evidence() {
        let state = path();
        learn(&state, RECOVERY, Some(INVENTORY));
        let no_entries = "-- No entries --";
        let history = diagnosis(
            run(
                input(RECOVERY, Some(INVENTORY), Some(no_entries)),
                Operation::Diagnose,
                &state,
            )
            .unwrap(),
        );
        assert_eq!(history.verdict, "history-unavailable");
        assert!(!history.missing_evidence.is_empty());
        let mut absent = input(ABSENT, Some(INVENTORY), None);
        absent.current_journal = Some(no_entries.into());
        let absence = diagnosis(run(absent, Operation::Diagnose, &state).unwrap());
        assert_eq!(absence.verdict, "bus-absent");
        assert!(!absence.missing_evidence.is_empty());
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }

    #[test]
    fn indented_journal_continuations_are_folded_into_kernel_records() {
        let tail = "2026-07-21T23:58:00+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu:\n    SMU: response:0xFFFFFFFF\n2026-07-21T23:58:01+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu:\n    SMU: response:0xFFFFFFFF\n2026-07-21T23:58:02+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin\n2026-07-21T23:58:03+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu:\n    SMU: response:0xFFFFFFFF\n2026-07-21T23:58:04+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin";
        let snapshot = parse_pci_snapshot(RECOVERY).unwrap();
        let baseline = baseline_from(
            snapshot
                .devices
                .iter()
                .find(|device| device.bdf == "0000:03:00.0")
                .unwrap(),
            "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
        );
        let precursor = precursor(tail, &baseline);
        assert_eq!(precursor.smu, 3);
        assert!(precursor.fires);
    }

    #[test]
    fn backwards_timestamps_make_window_evidence_unreliable() {
        let state = path();
        learn(&state, HEALTHY, Some(PRECURSOR_INVENTORY));
        let tail = "2026-07-21T23:58:02+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:01+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:03+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin\n2026-07-21T23:58:04+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:05+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin";
        let outcome = diagnosis(
            run(
                input(HEALTHY, Some(PRECURSOR_INVENTORY), Some(tail)),
                Operation::Diagnose,
                &state,
            )
            .unwrap(),
        );
        assert_eq!(outcome.verdict, "ok");
        assert!(outcome
            .missing_evidence
            .iter()
            .any(|item| item.contains("non-monotonic")));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }

    #[test]
    fn accumulated_small_timestamp_regressions_make_window_evidence_unreliable() {
        let state = path();
        learn(&state, HEALTHY, Some(PRECURSOR_INVENTORY));
        let mut lines = vec![
            "2026-07-21T23:58:00+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF".into(),
            "2026-07-21T23:58:01+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF".into(),
            "2026-07-21T23:58:02+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin".into(),
            "2026-07-21T23:58:03+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF".into(),
            "2026-07-21T23:58:04+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin".into(),
        ];
        let start = DateTime::parse_from_rfc3339("2026-07-21T23:58:04+00:00").unwrap();
        for step in 1..=9_202 {
            let timestamp = start - chrono::Duration::milliseconds(99 * step);
            lines.push(format!(
                "{} host app[1]: cross-transport jitter record",
                timestamp.to_rfc3339()
            ));
        }
        lines.push("2026-07-21T23:59:00+00:00 host systemd[1]: Reached target Shutdown.".into());
        let outcome = diagnosis(
            run(
                input(HEALTHY, Some(PRECURSOR_INVENTORY), Some(&lines.join("\n"))),
                Operation::Diagnose,
                &state,
            )
            .unwrap(),
        );
        assert_eq!(outcome.verdict, "ok");
        assert!(outcome
            .missing_evidence
            .iter()
            .any(|item| item.contains("non-monotonic")));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }

    #[test]
    fn small_timestamp_regressions_within_cumulative_tolerance_remain_reliable() {
        let tail = "2026-07-21T23:58:00+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:57:59.975+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:57:59.950+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin\n2026-07-21T23:57:59.925+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:01+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin\n2026-07-21T23:59:00+00:00 host systemd[1]: Reached target Shutdown.";
        let snapshot = parse_pci_snapshot(RECOVERY).unwrap();
        let baseline = baseline_from(
            snapshot
                .devices
                .iter()
                .find(|device| device.bdf == "0000:03:00.0")
                .unwrap(),
            &snapshot.boot_id,
        );
        let precursor = precursor(tail, &baseline);
        assert!(!precursor.timing_unreliable);
        assert!(precursor.fires);
    }

    #[test]
    fn multiset_merge_preserves_repeats_and_deduplicates_cross_source_overlap() {
        let smu = "2026-07-21T23:58:00+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: SMU: response:0xFFFFFFFF";
        let reset =
            "2026-07-21T23:58:01+00:00 host kernel: amdgpu 0000:03:00.0: amdgpu: GPU reset begin";
        let kernel = format!("{smu}\n{smu}\n{smu}\n{reset}\n{reset}");
        let tail = format!(
            "{kernel}\n2026-07-21T23:59:00+00:00 host systemd[1]: Reached target Shutdown."
        );
        let merged = combine_prior(Some(kernel), Some(tail)).unwrap();
        let snapshot = parse_pci_snapshot(RECOVERY).unwrap();
        let baseline = baseline_from(
            snapshot
                .devices
                .iter()
                .find(|device| device.bdf == "0000:03:00.0")
                .unwrap(),
            "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
        );
        assert!(
            precursor(&merged, &baseline).fires,
            "within-source repeats must fire"
        );
        assert_eq!(
            combine_prior(Some("SMU\nSMU\nSMU".into()), Some("SMU".into())),
            Some("SMU\nSMU\nSMU".into())
        );
        assert_eq!(
            combine_prior(Some("SMU\nSMU".into()), Some("SMU\nSMU".into())),
            Some("SMU\nSMU".into())
        );
        assert_eq!(
            combine_prior(Some("SMU".into()), Some("SMU\nSMU".into())),
            Some("SMU\nSMU".into())
        );
    }

    #[test]
    fn quoted_amdgpu_tokens_from_non_kernel_transport_do_not_match() {
        let quoted = "2026-07-21T23:58:00+00:00 host helper[42]: contains amdgpu 0000:03:00.0: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:01+00:00 host helper[42]: contains amdgpu 0000:03:00.0: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:02+00:00 host helper[42]: contains amdgpu 0000:03:00.0: GPU reset begin\n2026-07-21T23:58:03+00:00 host helper[42]: contains amdgpu 0000:03:00.0: SMU: response:0xFFFFFFFF\n2026-07-21T23:58:04+00:00 host helper[42]: contains amdgpu 0000:03:00.0: GPU reset begin";
        let snapshot = parse_pci_snapshot(RECOVERY).unwrap();
        let baseline = baseline_from(
            snapshot
                .devices
                .iter()
                .find(|device| device.bdf == "0000:03:00.0")
                .unwrap(),
            "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
        );
        assert!(!precursor(quoted, &baseline).fires);
    }

    #[test]
    fn malformed_topology_leaf_and_missing_upstream_are_refused() {
        let leaf_mismatch = "# boot_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb\n0000:03:00.0 path=pci0000:00/0000:00:03.1/0000:04:00.0 vendor=0x1002 device=0x7550 class=0x030000";
        assert!(parse_pci_snapshot(leaf_mismatch)
            .unwrap_err()
            .contains("path leaf"));
        let missing_upstream = "# boot_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb\n0000:03:00.0 path=pci0000:00/0000:00:03.1/0000:03:00.0 vendor=0x1002 device=0x7550 class=0x030000";
        assert!(parse_pci_snapshot(missing_upstream)
            .unwrap_err()
            .contains("upstream BDF"));
    }

    #[test]
    fn collection_failures_are_redacted_before_output() {
        let state = path();
        learn(&state, RECOVERY, Some(INVENTORY));
        let mut evidence = input(
            RECOVERY,
            Some(INVENTORY),
            Some(include_str!(
                "../../fixtures/d3/synthetic/clean-tail-prior-tail.log"
            )),
        );
        evidence.collection_missing.push(redacted_collection_failure("prior boot journal unavailable: journalctl stderr from hostname=host.example alice@private.example [2001:db8:dead:beef::1] serial=abcDEF1234567890FACE".into(), None));
        let outcome = diagnosis(run(evidence, Operation::Diagnose, &state).unwrap());
        let joined = outcome.missing_evidence.join(" ");
        for sensitive in ["alice", "host.example", "2001:db8", "abcDEF1234567890FACE"] {
            assert!(!joined.contains(sensitive));
        }
        assert!(joined.contains("[REDACTED_USER]"));
        assert!(joined.contains("[REDACTED_HOST]"));
        assert!(joined.contains("[REDACTED_IPV6]"));
        assert!(joined.contains("[REDACTED_SERIAL]"));
        assert!(joined.contains("prior boot journal unavailable"));
        assert!(joined.contains("journalctl stderr from"));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }

    #[test]
    fn suppressed_printk_records_degrade_precursor_confidence() {
        let state = path();
        learn(&state, HEALTHY, Some(PRECURSOR_INVENTORY));
        let tail = format!(
            "{}\n2026-07-21T23:59:31+00:00 host kernel: printk: 12 messages suppressed",
            include_str!("../../fixtures/d3/synthetic/precursor-prior-tail.log")
        );
        let outcome = diagnosis(
            run(
                input(HEALTHY, Some(PRECURSOR_INVENTORY), Some(&tail)),
                Operation::Diagnose,
                &state,
            )
            .unwrap(),
        );
        assert_eq!(outcome.verdict, "precursor-warning");
        assert_eq!(outcome.confidence, 0.76);
        assert!(outcome
            .missing_evidence
            .iter()
            .any(|item| item.contains("suppressed")));
        let _ = fs::remove_dir_all(state.parent().unwrap());
    }
}
