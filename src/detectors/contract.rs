//! Shared output contract for detector CLI diagnoses.

use serde::{Deserialize, Deserializer, Serialize};
use std::process::ExitCode;

/// Immutable identity and error-label metadata for a detector rule pack.
#[derive(Clone, Copy, Debug)]
pub struct DetectorContract {
    pub detector_id: &'static str,
    pub rule_version: &'static str,
    pub error_prefix: &'static str,
}

/// Exit behavior is deliberately separate from the serialized verdict text.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum Disposition {
    #[default]
    NonFinding,
    Finding,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
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
    pub supported_scope: Vec<String>,
    pub missing_evidence: Vec<String>,
    pub nearest_alternative: String,
    #[serde(
        skip_serializing,
        default,
        deserialize_with = "deserialize_ignored_disposition"
    )]
    disposition: Disposition,
}

impl Diagnosis {
    pub fn build(contract: DetectorContract, fields: DiagnosisFields) -> Result<Self, String> {
        let result = Self {
            timestamp: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            detector_id: contract.detector_id.into(),
            rule_version: contract.rule_version.into(),
            verdict: fields.verdict,
            confidence: fields.confidence,
            confidence_basis: fields.confidence_basis,
            evidence: fields.evidence,
            plain_language: fields.plain_language,
            suggested_fixes: fields.suggested_fixes,
            falsifier: fields.falsifier,
            host: fields.host,
            supported_scope: fields.supported_scope,
            missing_evidence: fields.missing_evidence,
            nearest_alternative: fields.nearest_alternative,
            disposition: fields.disposition,
        };
        result.validate(contract)?;
        Ok(result)
    }

    fn validate(&self, contract: DetectorContract) -> Result<(), String> {
        if !(0.0..=1.0).contains(&self.confidence) {
            return Err(format!(
                "{} diagnosis confidence must be in [0,1]",
                contract.error_prefix
            ));
        }
        if self.evidence.is_empty()
            || self.plain_language.is_empty()
            || self.falsifier.is_empty()
            || self.confidence_basis.is_empty()
            || self.supported_scope.is_empty()
            || self.nearest_alternative.is_empty()
        {
            return Err(format!(
                "{} diagnosis contract requires evidence, plain language, confidence basis, falsifier, supported scope, and nearest alternative",
                contract.error_prefix
            ));
        }
        if self.disposition == Disposition::Finding && self.suggested_fixes.is_empty() {
            return Err(format!(
                "{} real findings require suggested fixes",
                contract.error_prefix
            ));
        }
        Ok(())
    }
}

/// Named construction fields keep detector contracts reviewable as they grow.
#[derive(Clone, Debug)]
pub struct DiagnosisFields {
    pub disposition: Disposition,
    pub verdict: String,
    pub confidence: f64,
    pub confidence_basis: String,
    pub evidence: Vec<String>,
    pub plain_language: String,
    pub suggested_fixes: Vec<String>,
    pub falsifier: String,
    pub supported_scope: Vec<String>,
    pub missing_evidence: Vec<String>,
    pub nearest_alternative: String,
    pub host: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NotApplicable {
    #[serde(deserialize_with = "deserialize_not_applicable_outcome")]
    pub outcome: String,
    #[serde(deserialize_with = "deserialize_not_applicable_verdict")]
    pub verdict: String,
    pub explanation: String,
    pub evidence: Vec<String>,
    #[serde(
        skip_serializing,
        default,
        deserialize_with = "deserialize_ignored_disposition"
    )]
    disposition: Disposition,
}

impl NotApplicable {
    pub fn new(explanation: impl Into<String>, evidence: Vec<String>) -> Self {
        Self {
            outcome: "not-applicable".into(),
            verdict: "not-applicable".into(),
            explanation: explanation.into(),
            evidence,
            disposition: Disposition::NonFinding,
        }
    }
}

fn deserialize_not_applicable_outcome<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let outcome = String::deserialize(deserializer)?;
    if outcome == "not-applicable" {
        Ok(outcome)
    } else {
        Err(serde::de::Error::custom(
            "not-applicable outcome must be exactly \"not-applicable\"",
        ))
    }
}

fn deserialize_not_applicable_verdict<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: Deserializer<'de>,
{
    let verdict = String::deserialize(deserializer)?;
    if verdict == "not-applicable" {
        Ok(verdict)
    } else {
        Err(serde::de::Error::custom(
            "not-applicable verdict must be exactly \"not-applicable\"",
        ))
    }
}

/// `disposition` is an in-process signal, never a raw CLI JSON input.  Accept
/// a stale/injected key at the serde boundary but discard it so it cannot affect
/// CLI exit behavior or envelope construction.
fn deserialize_ignored_disposition<'de, D>(deserializer: D) -> Result<Disposition, D::Error>
where
    D: Deserializer<'de>,
{
    let _ = serde::de::IgnoredAny::deserialize(deserializer)?;
    Ok(Disposition::default())
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum Outcome {
    Diagnosis(Box<Diagnosis>),
    NotApplicable(NotApplicable),
}

impl Outcome {
    fn disposition(&self) -> Disposition {
        match self {
            Self::Diagnosis(diagnosis) => diagnosis.disposition,
            Self::NotApplicable(not_applicable) => not_applicable.disposition,
        }
    }

    /// Attach the in-process disposition before the immutable event boundary.
    /// Fixture tooling uses this same production path rather than serializing
    /// an ad-hoc JSON document.
    pub fn with_disposition(mut self, disposition: Disposition) -> Self {
        match &mut self {
            Self::Diagnosis(diagnosis) => diagnosis.disposition = disposition,
            Self::NotApplicable(not_applicable) => not_applicable.disposition = disposition,
        }
        self
    }
}

pub mod diagnosis_event;

fn print_human(outcome: &Outcome) {
    match outcome {
        Outcome::Diagnosis(diagnosis) => {
            println!(
                "detector_id: {}\nrule_version: {}\nverdict: {}\nconfidence: {}\nconfidence_basis: {}",
                diagnosis.detector_id,
                diagnosis.rule_version,
                diagnosis.verdict,
                diagnosis.confidence,
                diagnosis.confidence_basis
            );
            for item in &diagnosis.evidence {
                println!("evidence: {item}");
            }
            println!("plain_language: {}", diagnosis.plain_language);
            for fix in &diagnosis.suggested_fixes {
                println!("suggested_fix: {fix}");
            }
            println!("falsifier: {}", diagnosis.falsifier);
            for scope in &diagnosis.supported_scope {
                println!("supported_scope: {scope}");
            }
            if diagnosis.missing_evidence.is_empty() {
                println!("missing_evidence: []");
            } else {
                for missing in &diagnosis.missing_evidence {
                    println!("missing_evidence: {missing}");
                }
            }
            println!("nearest_alternative: {}", diagnosis.nearest_alternative);
        }
        Outcome::NotApplicable(not_applicable) => {
            println!(
                "outcome: not-applicable\nverdict: not-applicable\nexplanation: {}",
                not_applicable.explanation
            );
            for item in &not_applicable.evidence {
                println!("evidence: {item}");
            }
        }
    }
}

fn json_output_line(outcome: &Outcome) -> Result<String, serde_json::Error> {
    serde_json::to_string(outcome).map(|line| format!("{line}\n"))
}

/// Emit a completed detector outcome and map its non-serialized disposition to
/// the process status. Contract/collection errors are always exit 2.
pub fn emit(contract: DetectorContract, result: Result<Outcome, String>, json: bool) -> ExitCode {
    match result {
        Ok(outcome) => {
            if json {
                match json_output_line(&outcome) {
                    Ok(line) => print!("{line}"),
                    Err(error) => {
                        eprintln!(
                            "{} incomplete: cannot serialize outcome: {error}",
                            contract.error_prefix
                        );
                        return ExitCode::from(2);
                    }
                }
            } else {
                print_human(&outcome);
            }
            match outcome.disposition() {
                Disposition::NonFinding => ExitCode::SUCCESS,
                Disposition::Finding => ExitCode::from(1),
            }
        }
        Err(error) => {
            eprintln!("{} incomplete: {error}", contract.error_prefix);
            ExitCode::from(2)
        }
    }
}
