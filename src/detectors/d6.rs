//! D6: Gamescope display mode-override detector.
//!
//! The decision logic is deliberately independent of the filesystem so captured
//! DRM state can be replayed exactly as it was collected.
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::Path;
use std::process::ExitCode;

const AREA_RATIO_MAX: f64 = 0.5;
const ASPECT_TOLERANCE: f64 = 0.05;

#[derive(Clone, Debug, PartialEq)]
struct ModeOverride {
    line_number: usize,
    raw_line: String,
    key: String,
    width: u32,
    height: u32,
    hz: f64,
}

impl ModeOverride {
    fn resolution(&self) -> (u32, u32) {
        (self.width, self.height)
    }
    fn resolution_text(&self) -> String {
        format!("{}x{}", self.width, self.height)
    }
}

#[derive(Debug, Deserialize, Serialize)]
struct DrmState {
    #[serde(default)]
    connectors: Vec<Connector>,
    #[serde(default)]
    gamescope_control: GamescopeControl,
    #[serde(default)]
    collection_notes: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Connector {
    name: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    edid_bytes: u64,
    #[serde(default)]
    modes: Vec<String>,
    #[serde(default)]
    active_mode: Option<String>,
    #[serde(default)]
    enabled: Option<String>,
    #[serde(default)]
    dpms: Option<String>,
}

#[derive(Debug, Default, Deserialize, Serialize)]
struct GamescopeControl {
    #[serde(default)]
    connector_name: String,
    #[serde(default)]
    display_make: String,
    #[serde(default)]
    display_model: String,
    #[serde(default)]
    valid_refresh_rates: Vec<f64>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Diagnosis {
    #[serde(rename = "@timestamp")]
    pub timestamp: String,
    pub detector_id: String,
    pub rule_version: String,
    pub verdict: String,
    pub confidence: f64,
    pub confidence_basis: String,
    pub evidence: Vec<String>,
    pub plain_language: String,
    pub suggested_fixes: Vec<String>,
    pub falsifier: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub host: Option<String>,
}

impl Diagnosis {
    #[allow(clippy::too_many_arguments)]
    fn new(
        verdict: &str,
        confidence: f64,
        confidence_basis: impl Into<String>,
        evidence: Vec<String>,
        plain_language: impl Into<String>,
        suggested_fixes: Vec<String>,
        falsifier: impl Into<String>,
        host: Option<String>,
    ) -> Result<Self, String> {
        let result = Self {
            timestamp: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            detector_id: "D6".into(),
            rule_version: "d6.1".into(),
            verdict: verdict.into(),
            confidence,
            confidence_basis: confidence_basis.into(),
            evidence,
            plain_language: plain_language.into(),
            suggested_fixes,
            falsifier: falsifier.into(),
            host,
        };
        result.validate()?;
        Ok(result)
    }

    fn validate(&self) -> Result<(), String> {
        if !(0.0..=1.0).contains(&self.confidence) {
            return Err("D6 diagnosis confidence must be in [0,1]".into());
        }
        if self.evidence.is_empty()
            || self.plain_language.is_empty()
            || self.falsifier.is_empty()
            || self.confidence_basis.is_empty()
        {
            return Err("D6 diagnosis contract requires evidence, plain language, confidence basis, and falsifier".into());
        }
        if self.verdict != "ok" && self.suggested_fixes.is_empty() {
            return Err("D6 real findings require suggested fixes".into());
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct NotApplicable {
    pub outcome: &'static str,
    pub verdict: &'static str,
    pub explanation: String,
    pub evidence: Vec<String>,
}

impl NotApplicable {
    fn new(explanation: impl Into<String>, evidence: Vec<String>) -> Self {
        Self {
            outcome: "not-applicable",
            verdict: "not-applicable",
            explanation: explanation.into(),
            evidence,
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum Outcome {
    Diagnosis(Diagnosis),
    NotApplicable(NotApplicable),
}

fn parse_resolution(value: &str) -> Option<(u32, u32)> {
    let start = value.find(|c: char| c.is_ascii_digit())?;
    let value = &value[start..];
    let (width, rest) = value.split_once('x')?;
    let height: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    Some((width.parse().ok()?, height.parse().ok()?))
}

fn orientation_normalize(resolution: (u32, u32)) -> (u32, u32) {
    (
        resolution.0.max(resolution.1),
        resolution.0.min(resolution.1),
    )
}
fn area(resolution: (u32, u32)) -> f64 {
    f64::from(resolution.0) * f64::from(resolution.1)
}
fn aspect(resolution: (u32, u32)) -> f64 {
    f64::from(resolution.0) / f64::from(resolution.1)
}
fn connector_short_name(connector: &Connector) -> &str {
    connector
        .name
        .split_once('-')
        .and_then(|(card, rest)| {
            if card.starts_with("card") && card[4..].chars().all(|c| c.is_ascii_digit()) {
                Some(rest)
            } else {
                None
            }
        })
        .unwrap_or(&connector.name)
}
fn is_connected_external(connector: &Connector) -> bool {
    connector.status == "connected"
        && connector.edid_bytes > 0
        && !connector_short_name(connector).starts_with("eDP")
}
fn mode_set(connector: &Connector) -> HashSet<(u32, u32)> {
    connector
        .modes
        .iter()
        .filter_map(|mode| parse_resolution(mode))
        .collect()
}
fn preferred_resolution(connector: &Connector) -> Option<(u32, u32)> {
    connector
        .modes
        .first()
        .and_then(|mode| parse_resolution(mode))
}
fn internal_native_resolutions(state: &DrmState) -> HashSet<(u32, u32)> {
    state
        .connectors
        .iter()
        .filter(|connector| connector_short_name(connector).starts_with("eDP"))
        .filter_map(|connector| {
            connector
                .modes
                .first()
                .and_then(|mode| parse_resolution(mode))
        })
        .map(orientation_normalize)
        .collect()
}

fn parse_modes_cfg(contents: &str) -> Result<(Vec<ModeOverride>, Vec<String>), String> {
    let mut entries = Vec::new();
    let mut evidence = Vec::new();
    let mut nonblank = 0;
    for (index, raw) in contents.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        nonblank += 1;
        let parsed = line.rsplit_once(':').and_then(|(key, mode)| {
            let (resolution, hz_text) = mode.split_once('@')?;
            let (width, height) = parse_resolution(resolution)?;
            let hz = hz_text.split_whitespace().next()?.parse::<f64>().ok()?;
            (!key.is_empty()).then_some((key, width, height, hz))
        });
        if let Some((key, width, height, hz)) = parsed {
            entries.push(ModeOverride {
                line_number: index + 1,
                raw_line: raw.into(),
                key: key.into(),
                width,
                height,
                hz,
            });
        } else {
            evidence.push(format!(
                "modes.cfg line {}: ignored unparsable line: {raw}",
                index + 1
            ));
        }
    }
    if entries.is_empty() && nonblank > 0 {
        return Err("modes.cfg contains only unparsable nonblank lines".into());
    }
    if entries.is_empty() && nonblank == 0 {
        evidence.push("modes.cfg: no override present (file empty)".into());
    }
    Ok((entries, evidence))
}

fn find_gamescope_connector<'a>(
    state: &'a DrmState,
    override_: &ModeOverride,
) -> Option<&'a Connector> {
    let name = format!(
        "{} {}",
        state.gamescope_control.display_make, state.gamescope_control.display_model
    )
    .trim()
    .to_string();
    (name == override_.key)
        .then(|| {
            state.connectors.iter().find(|connector| {
                connector_short_name(connector) == state.gamescope_control.connector_name
            })
        })
        .flatten()
}

fn invalid_mode(
    override_: &ModeOverride,
    connector: &Connector,
    active: Option<(u32, u32)>,
) -> Option<Vec<String>> {
    let modes = mode_set(connector);
    let mut missing = Vec::new();
    if !modes.contains(&override_.resolution()) {
        missing.push(format!("pinned {}", override_.resolution_text()));
    }
    if let Some(active) = active.filter(|active| !modes.contains(active)) {
        missing.push(format!("active {}x{}", active.0, active.1));
    }
    (!missing.is_empty()).then(|| vec![
        format!("modes.cfg line {}: {}", override_.line_number, override_.raw_line),
        format!("{}: {} not present in resolution-only sysfs modes; Hz cannot be validated from /sys/class/drm modes data", connector.name, missing.join(", ")),
    ])
}

fn degraded_mode(
    override_: &ModeOverride,
    connector: &Connector,
    state: &DrmState,
    active: Option<(u32, u32)>,
) -> Option<(f64, Vec<String>)> {
    let preferred = preferred_resolution(connector)?;
    let mut branches = Vec::new();
    let pinned_normalized = orientation_normalize(override_.resolution());
    if internal_native_resolutions(state).contains(&pinned_normalized) {
        branches.push(format!(
            "pinned {} equals orientation-normalized internal-panel native {}x{}",
            override_.resolution_text(),
            pinned_normalized.0,
            pinned_normalized.1
        ));
    }
    let area_ratio = area(override_.resolution()) / area(preferred);
    let aspect_delta = (aspect(override_.resolution()) - aspect(preferred)).abs();
    if override_.resolution() != preferred
        && area_ratio < AREA_RATIO_MAX
        && aspect_delta > ASPECT_TOLERANCE
    {
        branches.push(format!("pinned area ratio {area_ratio:.3} < {AREA_RATIO_MAX} and aspect delta {aspect_delta:.3} > {ASPECT_TOLERANCE} versus preferred {}x{}", preferred.0, preferred.1));
    }
    if branches.is_empty() {
        return None;
    }
    let mut evidence = vec![
        format!(
            "modes.cfg line {}: {}",
            override_.line_number, override_.raw_line
        ),
        format!(
            "{}: preferred resolution is {}x{} (first sysfs mode)",
            connector.name, preferred.0, preferred.1
        ),
    ];
    match (&connector.active_mode, active) {
        (Some(mode), Some(active)) if active == override_.resolution() => evidence.push(format!(
            "{}: active_mode {mode} agrees with pinned {}",
            connector.name,
            override_.resolution_text()
        )),
        (Some(mode), _) => evidence.push(format!(
            "{}: active_mode {mode} differs from pinned {}",
            connector.name,
            override_.resolution_text()
        )),
        _ => {}
    }
    evidence.extend(
        branches
            .into_iter()
            .map(|branch| format!("{}: degraded branch: {branch}", connector.name)),
    );
    if !state.gamescope_control.valid_refresh_rates.is_empty()
        && !state
            .gamescope_control
            .valid_refresh_rates
            .iter()
            .any(|rate| (*rate - override_.hz).abs() < f64::EPSILON)
    {
        evidence.push(format!(
            "gamescope_control.valid_refresh_rates={:?} diverges from pinned refresh {}",
            state.gamescope_control.valid_refresh_rates, override_.hz
        ));
    }
    Some((
        if evidence
            .iter()
            .filter(|item| item.contains("degraded branch"))
            .count()
            >= 2
        {
            0.9
        } else {
            0.85
        },
        evidence,
    ))
}

fn plain_bad(override_: &ModeOverride, connector: &Connector) -> String {
    let native = preferred_resolution(connector)
        .map(|r| format!("{}x{}", r.0, r.1))
        .unwrap_or_else(|| "the display native/preferred mode".into());
    format!("Your display is being driven at {}@{} while {} prefers {native}. This driven-vs-native mismatch points to a stale gamescope mode override in modes.cfg. A reboot won't help because this is a home-dir config, not an EDID cache.", override_.resolution_text(), override_.hz, connector.name)
}

/// Diagnose captured text. An empty modes source has no override; malformed JSON and
/// all-unparsable nonblank mode files are detector-completion errors.
pub fn diagnose_inputs(
    modes_cfg: &str,
    drm_state: &str,
    host: Option<String>,
) -> Result<Outcome, String> {
    let (overrides, mut evidence) = parse_modes_cfg(modes_cfg)?;
    if overrides.is_empty() {
        return Ok(Outcome::Diagnosis(Diagnosis::new(
            "ok",
            0.7,
            "No modes.cfg override was present, so D6 has no mode pin to validate.",
            evidence,
            "No gamescope display mode override is present.",
            vec![],
            "A parsed gamescope mode override would cause D6 to validate it against DRM state.",
            host,
        )?));
    }
    let state: DrmState = serde_json::from_str(drm_state)
        .map_err(|error| format!("malformed DRM-state JSON: {error}"))?;
    evidence.extend(state.collection_notes.iter().cloned());
    let mut best: Option<(&ModeOverride, &Connector, String, f64, Vec<String>)> = None;
    let mut mapped = false;
    let mut ok_notes = Vec::new();
    for override_ in &overrides {
        let Some(connector) = find_gamescope_connector(&state, override_)
            .filter(|connector| is_connected_external(connector))
        else {
            evidence.push(format!(
                "modes.cfg line {}: {} -- display not currently connected; cannot validate",
                override_.line_number, override_.raw_line
            ));
            continue;
        };
        mapped = true;
        let active = connector.active_mode.as_deref().and_then(parse_resolution);
        if let Some(bad_evidence) = invalid_mode(override_, connector, active) {
            if best.is_none() {
                best = Some((
                    override_,
                    connector,
                    "mode-override-invalid".into(),
                    0.9,
                    bad_evidence,
                ));
            }
            continue;
        }
        if let Some((confidence, bad_evidence)) =
            degraded_mode(override_, connector, &state, active)
        {
            if best
                .as_ref()
                .is_none_or(|candidate| confidence > candidate.3)
            {
                best = Some((
                    override_,
                    connector,
                    "mode-override-degraded".into(),
                    confidence,
                    bad_evidence,
                ));
            }
            continue;
        }
        if preferred_resolution(connector)
            .is_some_and(|preferred| preferred != override_.resolution())
        {
            ok_notes.push(format!("modes.cfg line {}: {} -- below preferred {} but aspect/native checks are legitimate", override_.line_number, override_.raw_line, preferred_resolution(connector).map(|r| format!("{}x{}", r.0, r.1)).unwrap()));
        } else {
            ok_notes.push(format!(
                "modes.cfg line {}: {} -- pinned mode matches preferred resolution",
                override_.line_number, override_.raw_line
            ));
        }
    }
    if !mapped {
        return Ok(Outcome::NotApplicable(NotApplicable::new(
            "Parsed overrides did not map to a connected, usable external display.",
            evidence,
        )));
    }
    if let Some((override_, connector, verdict, confidence, bad_evidence)) = best {
        evidence.extend(bad_evidence);
        return Ok(Outcome::Diagnosis(Diagnosis::new(&verdict, confidence, if verdict == "mode-override-invalid" { "Pinned or active resolution is absent from the selected connector's resolution-only sysfs modes." } else { "One or more D6 degraded-mode branches matched the pinned mode against preferred and internal-panel evidence." }, evidence, plain_bad(override_, connector), vec!["Correct or delete the offending line in ~/.config/gamescope/modes.cfg.".into(), "Run: systemctl --user restart gamescope-session.target".into()], "The finding is falsified if a fresh DRM snapshot shows the pinned/active resolution valid and neither degraded branch matches.", host)?));
    }
    evidence.extend(ok_notes);
    Ok(Outcome::Diagnosis(Diagnosis::new("ok", 0.75, "All mapped overrides validated against the current connector modes and D6 degraded-mode checks.", evidence, "Mapped gamescope display overrides validate against the current DRM snapshot.", vec![], "A connector snapshot that makes an override absent or triggers a D6 degraded branch would falsify this validation.", host)?))
}

/// File wrapper. `None` is intentionally an absent optional source, not a read
/// attempt; it therefore returns the no-override `ok` outcome.
pub fn diagnose(
    modes_cfg: Option<&Path>,
    drm_state: &Path,
    host: Option<String>,
) -> Result<Outcome, String> {
    if modes_cfg.is_none() {
        return diagnose_inputs("", "", host);
    }
    let modes = match modes_cfg {
        Some(path) => std::fs::read_to_string(path)
            .map_err(|error| format!("cannot read modes.cfg {}: {error}", path.display()))?,
        None => unreachable!("handled above"),
    };
    let drm = std::fs::read_to_string(drm_state)
        .map_err(|error| format!("cannot read DRM-state {}: {error}", drm_state.display()))?;
    diagnose_inputs(&modes, &drm, host)
}

#[derive(Debug)]
struct ParsedGamescope {
    connector_name: String,
    display_make: String,
    display_model: String,
    valid_refresh_rates: Vec<f64>,
}

fn parse_gamescopectl(stdout: &str) -> Result<ParsedGamescope, String> {
    let mut connector_name = None;
    let mut display_make = None;
    let mut display_model = None;
    let mut valid_refresh_rates = None;
    for line in stdout.lines() {
        let line = line.trim();
        if let Some(value) = line.strip_prefix("- Connector Name:") {
            connector_name = Some(value.trim().to_string());
        }
        if let Some(value) = line.strip_prefix("- Display Make:") {
            display_make = Some(value.trim().to_string());
        }
        if let Some(value) = line.strip_prefix("- Display Model:") {
            display_model = Some(value.trim().to_string());
        }
        if let Some(value) = line.strip_prefix("- ValidRefreshRates:") {
            valid_refresh_rates = Some(
                value
                    .split_whitespace()
                    .filter_map(|rate| rate.trim_matches(',').parse().ok())
                    .collect::<Vec<f64>>(),
            );
        }
    }
    let result = ParsedGamescope {
        connector_name: connector_name.ok_or("gamescopectl output missing Connector Name")?,
        display_make: display_make.ok_or("gamescopectl output missing Display Make")?,
        display_model: display_model.ok_or("gamescopectl output missing Display Model")?,
        valid_refresh_rates: valid_refresh_rates
            .ok_or("gamescopectl output missing ValidRefreshRates")?,
    };
    if result.connector_name.is_empty()
        || result.display_make.is_empty()
        || result.display_model.is_empty()
        || result.valid_refresh_rates.is_empty()
    {
        return Err("gamescopectl output has empty required display fields".into());
    }
    Ok(result)
}

#[cfg(target_os = "linux")]
fn live_outcome(host: Option<String>) -> Result<Outcome, String> {
    use std::process::Command;
    let output = match Command::new("gamescopectl").output() {
        Ok(output) => output,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(Outcome::NotApplicable(NotApplicable::new(
                "gamescopectl is not installed; no Gamescope display session is available.",
                vec!["gamescopectl executable not found".into()],
            )))
        }
        Err(error) => return Err(format!("cannot execute gamescopectl: {error}")),
    };
    let text = String::from_utf8(output.stdout).map_err(|_| "gamescopectl stdout was not UTF-8")?;
    if !output.status.success() {
        if text.to_ascii_lowercase().contains("no gamescope")
            || text.to_ascii_lowercase().contains("no session")
        {
            return Ok(Outcome::NotApplicable(NotApplicable::new(
                "Gamescope reports no active display session.",
                vec![text],
            )));
        }
        return Err(format!("gamescopectl exited with {}", output.status));
    }
    let control = parse_gamescopectl(&text)?;
    let mut connectors = Vec::new();
    let mut collection_notes = Vec::new();
    for entry in std::fs::read_dir("/sys/class/drm")
        .map_err(|error| format!("cannot enumerate /sys/class/drm: {error}"))?
    {
        let entry =
            entry.map_err(|error| format!("cannot enumerate /sys/class/drm entry: {error}"))?;
        let name = entry.file_name().to_string_lossy().to_string();
        if !name.starts_with("card") || !name.contains('-') {
            continue;
        }
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let status = std::fs::read_to_string(path.join("status"))
            .map_err(|error| format!("cannot read mandatory status for {name}: {error}"))?
            .trim()
            .to_string();
        let selected = name
            .strip_prefix(|c: char| c != '-')
            .and_then(|rest| rest.strip_prefix('-'))
            == Some(control.connector_name.as_str())
            && status == "connected";
        let edid_bytes = if selected {
            let metadata = std::fs::metadata(path.join("edid"))
                .map_err(|error| format!("cannot read mandatory edid for {name}: {error}"))?;
            if metadata.len() == 0 {
                return Err(format!(
                    "selected connector {name} has empty mandatory edid"
                ));
            }
            metadata.len()
        } else {
            std::fs::metadata(path.join("edid"))
                .map(|m| m.len())
                .unwrap_or(0)
        };
        let modes = if selected {
            std::fs::read_to_string(path.join("modes"))
                .map_err(|error| format!("cannot read mandatory modes for {name}: {error}"))?
                .lines()
                .map(str::to_owned)
                .collect()
        } else {
            std::fs::read_to_string(path.join("modes"))
                .unwrap_or_default()
                .lines()
                .map(str::to_owned)
                .collect()
        };
        let enabled = match std::fs::read_to_string(path.join("enabled")) {
            Ok(value) => Some(value.trim().into()),
            Err(error) => {
                collection_notes.push(format!("{name}: enabled metadata unavailable: {error}"));
                None
            }
        };
        let dpms = match std::fs::read_to_string(path.join("dpms")) {
            Ok(value) => Some(value.trim().into()),
            Err(error) => {
                collection_notes.push(format!("{name}: dpms metadata unavailable: {error}"));
                None
            }
        };
        connectors.push(Connector {
            name,
            status,
            edid_bytes,
            modes,
            active_mode: None,
            enabled,
            dpms,
        });
    }
    let matches = connectors
        .iter()
        .filter(|connector| {
            connector_short_name(connector) == control.connector_name
                && is_connected_external(connector)
        })
        .count();
    if matches == 0 {
        return Ok(Outcome::NotApplicable(NotApplicable::new(
            "No connected usable external connector matches gamescopectl.",
            vec![format!("gamescopectl connector={}", control.connector_name)],
        )));
    }
    if matches > 1 {
        return Err(format!(
            "ambiguous connected connector {} across GPU cards",
            control.connector_name
        ));
    }
    let state = DrmState {
        connectors,
        gamescope_control: GamescopeControl {
            connector_name: control.connector_name,
            display_make: control.display_make,
            display_model: control.display_model,
            valid_refresh_rates: control.valid_refresh_rates,
        },
        collection_notes,
    };
    let modes = std::env::var_os("HOME")
        .map(std::path::PathBuf::from)
        .map(|home| home.join(".config/gamescope/modes.cfg"))
        .and_then(|path| std::fs::read_to_string(path).ok())
        .unwrap_or_default();
    diagnose_inputs(
        &modes,
        &serde_json::to_string(&state).map_err(|error| error.to_string())?,
        host,
    )
}

#[cfg(not(target_os = "linux"))]
fn live_outcome(_host: Option<String>) -> Result<Outcome, String> {
    Ok(Outcome::NotApplicable(NotApplicable::new(
        "Live D6 collection is only available on Linux.",
        vec!["non-Linux build".into()],
    )))
}

fn print_human(outcome: &Outcome) {
    match outcome {
        Outcome::Diagnosis(d) => {
            println!("detector_id: {}\nrule_version: {}\nverdict: {}\nconfidence: {}\nconfidence_basis: {}", d.detector_id, d.rule_version, d.verdict, d.confidence, d.confidence_basis);
            for item in &d.evidence {
                println!("evidence: {item}");
            }
            println!("plain_language: {}", d.plain_language);
            for fix in &d.suggested_fixes {
                println!("suggested_fix: {fix}");
            }
            println!("falsifier: {}", d.falsifier);
        }
        Outcome::NotApplicable(na) => {
            println!(
                "outcome: not-applicable\nverdict: not-applicable\nexplanation: {}",
                na.explanation
            );
            for item in &na.evidence {
                println!("evidence: {item}");
            }
        }
    }
}

pub fn run_cli(
    modes_cfg: Option<&Path>,
    drm_state: Option<&Path>,
    json: bool,
    host: Option<String>,
) -> ExitCode {
    let result = match (modes_cfg, drm_state) {
        (Some(modes), Some(drm)) => diagnose(Some(modes), drm, host),
        (None, None) => live_outcome(host),
        _ => Err("offline display diagnosis requires both --modes-cfg and --drm-state".into()),
    };
    match result {
        Ok(outcome) => {
            if json {
                match serde_json::to_string(&outcome) {
                    Ok(line) => println!("{line}"),
                    Err(error) => {
                        eprintln!("D6 incomplete: cannot serialize outcome: {error}");
                        return ExitCode::from(2);
                    }
                }
            } else {
                print_human(&outcome);
            }
            match outcome {
                Outcome::Diagnosis(d) if d.verdict != "ok" => ExitCode::from(1),
                _ => ExitCode::SUCCESS,
            }
        }
        Err(error) => {
            eprintln!("D6 incomplete: {error}");
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const DECK_BAD_MODES: &str = include_str!("../../fixtures/d6/deck-incident-bad/modes.cfg");
    const DECK_BAD_DRM: &str = include_str!("../../fixtures/d6/deck-incident-bad/drm-state.json");
    const DECK_REAL_MODES: &str = include_str!("../../fixtures/d6/deck-real/modes.cfg");
    const DECK_REAL_DRM: &str = include_str!("../../fixtures/d6/deck-real/drm-state.json");
    const REAL_254_MODES: &str = include_str!("../../fixtures/d6/real-254/modes.cfg");
    const REAL_254_DRM: &str = include_str!("../../fixtures/d6/real-254/drm-state.json");
    const SYNTHETIC_DRM: &str = include_str!("../../fixtures/d6/synthetic-4k-drm-state.json");

    fn diagnosis(modes: &str, drm: &str) -> Diagnosis {
        match diagnose_inputs(modes, drm, None).unwrap() {
            Outcome::Diagnosis(d) => d,
            _ => panic!("expected diagnosis"),
        }
    }
    #[test]
    fn incident_is_degraded() {
        let d = diagnosis(DECK_BAD_MODES, DECK_BAD_DRM);
        assert_eq!(d.verdict, "mode-override-degraded");
        assert_eq!(d.confidence, 0.9);
        assert!(d
            .evidence
            .iter()
            .any(|e| e.contains("Samsung Electric Company QCQ95S:1280x800@60")));
        assert!(d
            .evidence
            .iter()
            .any(|e| e.contains("active_mode 1280x800@60 agrees")));
        assert!(d.plain_language.contains("reboot won't help"));
        contract(&d);
    }
    #[test]
    fn deck_real_is_ok_and_skips_lg() {
        let d = diagnosis(DECK_REAL_MODES, DECK_REAL_DRM);
        assert_eq!(d.verdict, "ok");
        assert!(d
            .evidence
            .iter()
            .any(|e| e.contains("LG Electronics LG HDR 4K")
                && e.contains("display not currently connected")));
    }
    #[test]
    fn real_254_is_ok() {
        let d = diagnosis(REAL_254_MODES, REAL_254_DRM);
        assert_eq!(d.verdict, "ok");
        assert!(d
            .evidence
            .iter()
            .any(|e| e.contains("pinned mode matches preferred")));
    }
    #[test]
    fn same_aspect_downscale_is_ok() {
        let d = diagnosis(
            include_str!("../../fixtures/d6/legit-downscale-modes.cfg"),
            SYNTHETIC_DRM,
        );
        assert_eq!(d.verdict, "ok");
        assert!(d
            .evidence
            .iter()
            .any(|e| e.contains("below preferred 3840x2160")));
    }
    #[test]
    fn absent_mode_is_invalid() {
        let d = diagnosis(
            include_str!("../../fixtures/d6/invalid-mode-modes.cfg"),
            SYNTHETIC_DRM,
        );
        assert_eq!(d.verdict, "mode-override-invalid");
        assert!(d
            .evidence
            .iter()
            .any(|e| e.contains("2000x2000")
                && e.contains("not present in resolution-only sysfs modes")));
        contract(&d);
    }
    #[test]
    fn no_source_is_ok() {
        let d = diagnosis("", SYNTHETIC_DRM);
        assert_eq!(d.verdict, "ok");
        assert!(d.evidence.iter().any(|e| e.contains("no override present")));
    }
    #[test]
    fn none_path_is_no_override_not_a_missing_file_read() {
        match diagnose(
            None,
            Path::new("fixtures/d6/definitely-missing-drm-state.json"),
            None,
        )
        .unwrap()
        {
            Outcome::Diagnosis(d) => assert_eq!(d.verdict, "ok"),
            _ => panic!("expected no-override diagnosis"),
        }
    }
    #[test]
    fn explicit_missing_source_is_incomplete() {
        assert!(diagnose(
            Some(Path::new("fixtures/d6/definitely-missing-modes.cfg")),
            Path::new("fixtures/d6/synthetic-4k-drm-state.json"),
            None,
        )
        .is_err());
    }
    #[test]
    fn unparsable_is_incomplete() {
        assert!(diagnose_inputs("nonsense\n", SYNTHETIC_DRM, None).is_err());
    }
    #[test]
    fn unmapped_is_not_applicable() {
        match diagnose_inputs("Not This Display:1920x1080@60\n", SYNTHETIC_DRM, None).unwrap() {
            Outcome::NotApplicable(na) => assert_eq!(na.verdict, "not-applicable"),
            _ => panic!("expected not-applicable"),
        }
    }
    #[test]
    fn gamescopectl_parsers_cover_captures() {
        for source in [
            include_str!("../../fixtures/d6/deck-real/gamescopectl.txt"),
            include_str!("../../fixtures/d6/real-254/gamescopectl.txt"),
        ] {
            let p = parse_gamescopectl(source).unwrap();
            assert!(
                !p.connector_name.is_empty()
                    && !p.display_make.is_empty()
                    && !p.display_model.is_empty()
                    && !p.valid_refresh_rates.is_empty()
            );
        }
    }
    fn contract(d: &Diagnosis) {
        assert_eq!(d.rule_version, "d6.1");
        assert!((0.0..=1.0).contains(&d.confidence));
        assert!(
            !d.evidence.is_empty()
                && !d.plain_language.is_empty()
                && !d.suggested_fixes.is_empty()
                && !d.falsifier.is_empty()
                && !d.confidence_basis.is_empty()
        );
    }
}
