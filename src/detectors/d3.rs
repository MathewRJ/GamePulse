//! D3 GPU boot diagnosis core.
//!
//! Collection and Clap parsing intentionally live outside this module.  Task D
//! can parse live sysfs/journald data into these typed inputs without changing
//! verdict, persistence, or output semantics.

use super::contract::{
    DetectorContract, Diagnosis, DiagnosisFields, Disposition, NotApplicable, Outcome,
};
use chrono::{DateTime, FixedOffset, Utc};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
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
    pub host: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Operation {
    Diagnose,
    Learn { slot: Option<String> },
    Reset,
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
            let outcome = evaluate(&input, &mut state)?;
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
        if input.current_journal.is_none() {
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
    if let (Some(prior_id), Some(tail)) = (prior.as_deref(), input.prior_tail.as_deref()) {
        let precursor = precursor(tail, baseline);
        if precursor.fires {
            let mut missing = Vec::new();
            if precursor.truncated {
                missing.push(
                    "prior boot tail appears truncated; shutdown completion is unavailable".into(),
                );
            }
            return diagnosis(Disposition::Finding, "precursor-warning", if precursor.truncated { 0.76 } else { 0.88 }, "AMD/amdgpu d3.1 compound precursor thresholds were met on the baseline BDF in the trailing 900-second window.", vec![format!("prior boot: {prior_id}"), format!("baseline BDF {}: SMU-unresponsive={} (minimum {}), reset-attempts={} (minimum {})", baseline.slot, precursor.smu, SMU_UNRESPONSIVE_MIN, precursor.resets, RESET_ATTEMPT_MIN)], "The prior boot evidence is most consistent with an AMD GPU reset/SMU precursor; it does not establish a later boot failure.", vec!["Avoid a warm reboot if the GPU becomes unavailable; capture journal evidence and use a full power drain if needed.".into()], "A later complete prior-boot journal with recovery after every reset would falsify the terminal-failure condition.", missing, "A driver-only recovery event without a power-state latch remains the nearest alternative.", input.host.clone());
        }
    }
    if let Some(pending) = state.pending.clone() {
        if pending.observation_boot_id != input.snapshot.boot_id {
            state.pending = None;
            return diagnosis(Disposition::NonFinding, "recovered", 0.9, "The learned GPU identity is present and a pending bus-absence was recorded on an earlier boot.", vec![format!("current boot: {}", input.snapshot.boot_id), format!("consumed pending {} from boot {}", pending.verdict, pending.observation_boot_id)], "The GPU is present now; this is most consistent with recovery from the earlier observed enumeration absence.", vec!["Keep the baseline and retain future journal evidence if the symptom returns.".into()], "A repeated absence at the learned BDF would falsify the recovery observation.", vec![], "The earlier fault may have been transient rather than a power-state latch.", input.host.clone());
        }
    }
    if prior.is_none() || input.prior_tail.is_none() {
        let mut missing = vec!["usable paired prior Linux boot history unavailable".into()];
        if input.prior_tail.is_none() {
            missing.push("prior boot journal tail unavailable".into());
        }
        return diagnosis(Disposition::NonFinding, "history-unavailable", 0.8, "The learned GPU identity is present, but prior-boot precursor history is unavailable.", vec![format!("baseline identity present at {}", baseline.slot)], "The GPU is present; D3 cannot make a prior-boot precursor claim because history is unavailable.", vec!["Retain journal history across boots for precursor analysis.".into()], "A paired prior boot journal would permit precursor analysis.", missing, "A precursor could have occurred but cannot be assessed from the available history.", input.host.clone());
    }
    diagnosis(Disposition::NonFinding, "ok", 0.92, "The learned GPU identity is present and no d3.1 precursor matched on the paired prior boot.", vec![format!("baseline identity present at {}", baseline.slot)], "The available evidence is most consistent with normal GPU enumeration for this boot.", vec!["No corrective action is indicated; retain journal history for future comparisons.".into()], "An authoritative future snapshot with the identity absent would falsify this observation.", vec![], "A failure outside the collected evidence window remains possible.", input.host.clone())
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
}

fn precursor(tail: &str, baseline: &Baseline) -> Precursor {
    let rows: Vec<(DateTime<FixedOffset>, &str)> = tail
        .lines()
        .filter_map(|line| parse_journal_line(line))
        .collect();
    let Some((end, _)) = rows.last() else {
        return Precursor::default();
    };
    let earliest = *end - chrono::Duration::seconds(PRECURSOR_WINDOW_S);
    let scoped: Vec<_> = rows
        .into_iter()
        .filter(|(time, line)| {
            *time >= earliest && line.contains(&baseline.slot) && line.contains("amdgpu")
        })
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
        fires: smu >= SMU_UNRESPONSIVE_MIN && resets.len() >= RESET_ATTEMPT_MIN && terminal,
        smu,
        resets: resets.len(),
        truncated,
    }
}

fn parse_journal_line(line: &str) -> Option<(DateTime<FixedOffset>, &str)> {
    let (stamp, rest) = line.split_once(' ')?;
    Some((DateTime::parse_from_rfc3339(stamp).ok()?, rest))
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
        } else if looks_like_mac(clean) {
            "[REDACTED_MAC]".into()
        } else if looks_like_uuid(clean) {
            "[REDACTED_UUID]".into()
        } else if clean.to_ascii_lowercase().contains("serial") || clean.starts_with("SN=") {
            "[REDACTED_SERIAL]".into()
        } else if (index == 1 && target_host.is_none_or(|host| clean != host))
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
fn looks_like_mac(value: &str) -> bool {
    let parts: Vec<_> = value.split(':').collect();
    parts.len() == 6
        && parts
            .iter()
            .all(|part| part.len() == 2 && part.chars().all(|c| c.is_ascii_hexdigit()))
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
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&self.lock_path)
            .map_err(|error| format!("cannot open D3 sidecar lock: {error}"))?;
        for _ in 0..50 {
            if file.try_lock_exclusive().is_ok() {
                return Ok(StateLock(file));
            }
            thread::sleep(Duration::from_millis(10));
        }
        Err("D3 state lock contention timed out".into())
    }
    fn load(&self) -> Result<D3State, String> {
        if !self.path.exists() {
            return Ok(D3State::empty());
        }
        let metadata = fs::symlink_metadata(&self.path)
            .map_err(|error| format!("cannot inspect D3 state: {error}"))?;
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
}
