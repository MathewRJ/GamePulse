//! Shared output contract for detector CLI diagnoses.

use serde::Serialize;
use std::process::ExitCode;

/// Immutable identity and error-label metadata for a detector rule pack.
#[derive(Clone, Copy, Debug)]
pub struct DetectorContract {
    pub detector_id: &'static str,
    pub rule_version: &'static str,
    pub error_prefix: &'static str,
}

/// Exit behavior is deliberately separate from the serialized verdict text.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Disposition {
    NonFinding,
    Finding,
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
    pub supported_scope: Vec<String>,
    pub missing_evidence: Vec<String>,
    pub nearest_alternative: String,
    #[serde(skip)]
    disposition: Disposition,
}

impl Diagnosis {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        contract: DetectorContract,
        disposition: Disposition,
        verdict: &str,
        confidence: f64,
        confidence_basis: impl Into<String>,
        evidence: Vec<String>,
        plain_language: impl Into<String>,
        suggested_fixes: Vec<String>,
        falsifier: impl Into<String>,
        supported_scope: Vec<String>,
        missing_evidence: Vec<String>,
        nearest_alternative: impl Into<String>,
        host: Option<String>,
    ) -> Result<Self, String> {
        let result = Self {
            timestamp: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            detector_id: contract.detector_id.into(),
            rule_version: contract.rule_version.into(),
            verdict: verdict.into(),
            confidence,
            confidence_basis: confidence_basis.into(),
            evidence,
            plain_language: plain_language.into(),
            suggested_fixes,
            falsifier: falsifier.into(),
            host,
            supported_scope,
            missing_evidence,
            nearest_alternative: nearest_alternative.into(),
            disposition,
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

#[derive(Clone, Debug, Serialize)]
pub struct NotApplicable {
    pub outcome: &'static str,
    pub verdict: &'static str,
    pub explanation: String,
    pub evidence: Vec<String>,
    #[serde(skip)]
    disposition: Disposition,
}

impl NotApplicable {
    pub fn new(explanation: impl Into<String>, evidence: Vec<String>) -> Self {
        Self {
            outcome: "not-applicable",
            verdict: "not-applicable",
            explanation: explanation.into(),
            evidence,
            disposition: Disposition::NonFinding,
        }
    }
}

#[derive(Debug, Serialize)]
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
}

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

/// Emit a completed detector outcome and map its non-serialized disposition to
/// the process status. Contract/collection errors are always exit 2.
pub fn emit(contract: DetectorContract, result: Result<Outcome, String>, json: bool) -> ExitCode {
    match result {
        Ok(outcome) => {
            if json {
                match serde_json::to_string(&outcome) {
                    Ok(line) => println!("{line}"),
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
