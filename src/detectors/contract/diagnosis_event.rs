//! Pure construction and validation of the versioned diagnosis-event envelope.

use super::{DetectorContract, Disposition, Outcome};
use serde::Serialize;
use std::sync::Arc;

/// The sole compiled schema version emitted by this contract.
pub const DIAGNOSIS_SCHEMA_VERSION: u32 = 1;

const MAX_ARRAY_ELEMENTS: usize = 50;
const MAX_ARRAY_ELEMENT_CHARS: usize = 4096;
const MAX_DISPLAY_CHARS: usize = 8192;
const MAX_KEYWORD_CHARS: usize = 1024;
/// The immutable DiagnosisEvent envelope limit, measured after RFC 8785/JCS
/// serialization.  This is intentionally the only byte-limit authority.
pub const MAX_DIAGNOSIS_EVENT_BYTES: u32 = 1_048_576;

/// Source mode recorded with a diagnosis event.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InputMode {
    Live,
    Offline,
    Fixture,
}

/// Deterministic inputs supplied by the caller at the outbox boundary.
#[derive(Clone, Debug)]
pub struct EventContext<'a> {
    pub event_id: String,
    pub enqueue_timestamp: String,
    pub local_host_name: String,
    pub detector_contract: &'a DetectorContract,
    pub input_mode: InputMode,
}

/// A precise reason a candidate outcome cannot become an event envelope.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ValidationError {
    EmptyEvidence,
    EmptyPlainLanguage,
    EmptyRequiredField {
        field: &'static str,
    },
    EmptySuggestedFixes,
    MissingDetectorContractIdentity,
    ConfidenceOutOfRange,
    InvalidTimestamp {
        field: &'static str,
    },
    InvalidEventId,
    LimitExceeded {
        field: &'static str,
        limit: usize,
    },
    DetectorContractMismatch {
        field: &'static str,
        expected: String,
        actual: String,
    },
    EventBytesLimitExceeded {
        limit: u32,
        actual_saturated: u32,
    },
}

/// A validated envelope and the exact bytes which an outbox must persist/send.
///
/// `DiagnosisEvent` is kept private so a caller cannot mutate a value after the
/// byte boundary.  Callers deliberately receive only immutable references.
#[derive(Clone, Debug)]
pub struct ValidatedDiagnosisEvent {
    event: DiagnosisEvent,
    canonical_bytes: Arc<[u8]>,
}

impl ValidatedDiagnosisEvent {
    pub fn event(&self) -> &DiagnosisEvent {
        &self.event
    }

    pub fn canonical_bytes(&self) -> &[u8] {
        &self.canonical_bytes
    }
}

/// The minimal ECS plus RigSignal envelope required by the frozen contract.
#[derive(Clone, Debug, Serialize)]
pub struct DiagnosisEvent {
    #[serde(rename = "@timestamp")]
    pub timestamp: String,
    pub event: EventMetadata,
    pub host: HostMetadata,
    pub rigsignal: RigSignalMetadata,
}

#[derive(Clone, Debug, Serialize)]
pub struct EventMetadata {
    pub id: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct HostMetadata {
    pub name: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct RigSignalMetadata {
    pub diagnosis: DiagnosisPayload,
}

#[derive(Clone, Debug, Serialize)]
pub struct DiagnosisPayload {
    pub schema_version: u32,
    pub outcome: &'static str,
    pub detector_id: String,
    pub rule_version: String,
    pub input_mode: InputMode,
    pub disposition: &'static str,
    pub evidence: Vec<String>,
    pub evidence_display: String,
    pub plain_language: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verdict: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence_basis: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub suggested_fixes: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub suggested_fixes_display: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub falsifier: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub supported_scope: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub missing_evidence: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nearest_alternative: Option<String>,
}

impl DiagnosisEvent {
    /// Validate and translate one in-process detector outcome into its envelope.
    ///
    /// This function deliberately consumes no ambient state: all identity, host, time,
    /// contract, and mode data is supplied through `event_context`.
    pub fn try_from_outcome(
        outcome: Outcome,
        event_context: &EventContext<'_>,
    ) -> Result<ValidatedDiagnosisEvent, ValidationError> {
        validate_context(event_context)?;

        let contract = event_context.detector_contract;
        // `Disposition` is intentionally skipped from raw CLI JSON, so capture it
        // from the in-process outcome before translating either branch.
        let outcome_disposition = outcome.disposition();
        let (timestamp, host_name, payload) = match outcome {
            Outcome::Diagnosis(diagnosis) => {
                if diagnosis.detector_id != contract.detector_id {
                    return Err(ValidationError::DetectorContractMismatch {
                        field: "detector_id",
                        expected: contract.detector_id.into(),
                        actual: diagnosis.detector_id,
                    });
                }
                if diagnosis.rule_version != contract.rule_version {
                    return Err(ValidationError::DetectorContractMismatch {
                        field: "rule_version",
                        expected: contract.rule_version.into(),
                        actual: diagnosis.rule_version,
                    });
                }
                validate_non_empty("verdict", &diagnosis.verdict)?;
                validate_keyword("verdict", &diagnosis.verdict)?;
                if !diagnosis.confidence.is_finite() || !(0.0..=1.0).contains(&diagnosis.confidence)
                {
                    return Err(ValidationError::ConfidenceOutOfRange);
                }
                validate_evidence_and_plain_language(
                    &diagnosis.evidence,
                    &diagnosis.plain_language,
                )?;
                validate_arrays(
                    &diagnosis.evidence,
                    &diagnosis.suggested_fixes,
                    &diagnosis.supported_scope,
                    &diagnosis.missing_evidence,
                )?;
                validate_required_diagnosis_fields(&diagnosis, outcome_disposition)?;

                let evidence_display = render_display(&diagnosis.evidence);
                validate_display("evidence_display", &evidence_display)?;
                let suggested_fixes_display = (!diagnosis.suggested_fixes.is_empty())
                    .then(|| render_display(&diagnosis.suggested_fixes));
                if let Some(display) = &suggested_fixes_display {
                    validate_display("suggested_fixes_display", display)?;
                }

                let disposition = disposition_name(outcome_disposition);
                let host_name = normalized_host_name(
                    diagnosis
                        .host
                        .as_deref()
                        .unwrap_or(&event_context.local_host_name),
                )?;
                (
                    diagnosis.timestamp,
                    host_name,
                    DiagnosisPayload {
                        schema_version: DIAGNOSIS_SCHEMA_VERSION,
                        outcome: "diagnosis",
                        detector_id: contract.detector_id.into(),
                        rule_version: contract.rule_version.into(),
                        input_mode: event_context.input_mode,
                        disposition,
                        evidence: diagnosis.evidence,
                        evidence_display,
                        plain_language: diagnosis.plain_language,
                        verdict: Some(diagnosis.verdict),
                        confidence: Some(diagnosis.confidence),
                        confidence_basis: Some(diagnosis.confidence_basis),
                        suggested_fixes: Some(diagnosis.suggested_fixes),
                        suggested_fixes_display,
                        falsifier: Some(diagnosis.falsifier),
                        supported_scope: Some(diagnosis.supported_scope),
                        missing_evidence: Some(diagnosis.missing_evidence),
                        nearest_alternative: Some(diagnosis.nearest_alternative),
                    },
                )
            }
            Outcome::NotApplicable(not_applicable) => {
                debug_assert_eq!(outcome_disposition, Disposition::NonFinding);
                validate_evidence_and_plain_language(
                    &not_applicable.evidence,
                    &not_applicable.explanation,
                )?;
                validate_array("evidence", &not_applicable.evidence)?;
                let evidence_display = render_display(&not_applicable.evidence);
                validate_display("evidence_display", &evidence_display)?;
                (
                    event_context.enqueue_timestamp.clone(),
                    normalized_host_name(&event_context.local_host_name)?,
                    DiagnosisPayload {
                        schema_version: DIAGNOSIS_SCHEMA_VERSION,
                        outcome: "not_applicable",
                        detector_id: contract.detector_id.into(),
                        rule_version: contract.rule_version.into(),
                        input_mode: event_context.input_mode,
                        disposition: "non_finding",
                        evidence: not_applicable.evidence,
                        evidence_display,
                        plain_language: not_applicable.explanation,
                        verdict: None,
                        confidence: None,
                        confidence_basis: None,
                        suggested_fixes: None,
                        suggested_fixes_display: None,
                        falsifier: None,
                        supported_scope: None,
                        missing_evidence: None,
                        nearest_alternative: None,
                    },
                )
            }
        };

        let event = Self {
            timestamp,
            event: EventMetadata {
                id: event_context.event_id.clone(),
            },
            host: HostMetadata { name: host_name },
            rigsignal: RigSignalMetadata { diagnosis: payload },
        };
        // Serialize exactly once at the validator boundary.  The retained
        // vector is the future outbox's wire/storage representation.
        let canonical_bytes = serialize_diagnosis_event_jcs(&event);
        let actual = canonical_bytes.len() as u64;
        if actual > u64::from(MAX_DIAGNOSIS_EVENT_BYTES) {
            return Err(ValidationError::EventBytesLimitExceeded {
                limit: MAX_DIAGNOSIS_EVENT_BYTES,
                actual_saturated: saturate_event_byte_count(actual),
            });
        }
        Ok(ValidatedDiagnosisEvent {
            event,
            canonical_bytes: Arc::from(canonical_bytes),
        })
    }
}

/// Serialize a DiagnosisEvent as RFC 8785/JCS UTF-8 bytes.
///
/// This is the stable canonical serialization contract for the validator and
/// future outbox.  Do not serialize a validated event a second time.
pub fn serialize_diagnosis_event_jcs(event: &DiagnosisEvent) -> Vec<u8> {
    let value = serde_json::to_value(event).expect("DiagnosisEvent is serializable");
    jcs_value(&value).into_bytes()
}

/// Convert a measured byte count without permitting a test or caller to
/// allocate a multi-gigabyte event.
pub fn saturate_event_byte_count(actual: u64) -> u32 {
    actual.min(u64::from(u32::MAX)) as u32
}

fn jcs_value(value: &serde_json::Value) -> String {
    use serde_json::Value;
    match value {
        Value::Null => "null".into(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => jcs_number(value),
        Value::String(value) => serde_json::to_string(value).expect("string is JSON"),
        Value::Array(values) => format!(
            "[{}]",
            values.iter().map(jcs_value).collect::<Vec<_>>().join(",")
        ),
        Value::Object(values) => {
            let mut entries = values.iter().collect::<Vec<_>>();
            // RFC 8785 orders member names by UTF-16 code units, rather than
            // UTF-8 bytes/Rust scalar ordering.  The difference matters when a
            // BMP key sorts around a supplementary-plane key.
            entries.sort_unstable_by(|(left, _), (right, _)| {
                left.encode_utf16().cmp(right.encode_utf16())
            });
            format!(
                "{{{}}}",
                entries
                    .into_iter()
                    .map(|(key, value)| format!(
                        "{}:{}",
                        serde_json::to_string(key).expect("key is JSON"),
                        jcs_value(value)
                    ))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
    }
}

fn jcs_number(value: &serde_json::Number) -> String {
    if value.as_i64().is_some() || value.as_u64().is_some() {
        return value.to_string();
    }
    let number = value
        .as_f64()
        .expect("serde_json Number is either an integer or finite f64");
    if number == 0.0 {
        return "0".into();
    }
    // serde_json/Ryu supplies the shortest round-tripping significand.  JCS
    // then uses decimal notation for [1e-6, 1e21), and scientific notation
    // outside that range (with an explicit '+' exponent sign).
    let raw = value.to_string();
    let magnitude = number.abs();
    if (1e-6..1e21).contains(&magnitude) {
        expand_jcs_decimal(&raw)
    } else {
        jcs_scientific(&raw)
    }
}

fn split_number(raw: &str) -> (&str, i32) {
    match raw.find(['e', 'E']) {
        Some(index) => (
            &raw[..index],
            raw[index + 1..]
                .parse()
                .expect("serde_json emits a valid exponent"),
        ),
        None => (raw, 0),
    }
}

fn expand_jcs_decimal(raw: &str) -> String {
    let (mantissa, exponent) = split_number(raw);
    let negative = mantissa.starts_with('-');
    let digits = mantissa.trim_start_matches('-').replace('.', "");
    let decimal_at = mantissa
        .trim_start_matches('-')
        .find('.')
        .unwrap_or(digits.len()) as i32
        + exponent;
    let mut rendered = if decimal_at <= 0 {
        format!("0.{}{}", "0".repeat((-decimal_at) as usize), digits)
    } else if decimal_at as usize >= digits.len() {
        format!(
            "{}{}",
            digits,
            "0".repeat(decimal_at as usize - digits.len())
        )
    } else {
        format!(
            "{}.{}",
            &digits[..decimal_at as usize],
            &digits[decimal_at as usize..]
        )
    };
    if rendered.contains('.') {
        rendered = rendered
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_owned();
    }
    if negative {
        format!("-{rendered}")
    } else {
        rendered
    }
}

fn jcs_scientific(raw: &str) -> String {
    let (mantissa, exponent) = split_number(raw);
    let negative = mantissa.starts_with('-');
    let unsigned = mantissa.trim_start_matches('-');
    let decimal_at = unsigned.find('.').unwrap_or(unsigned.len()) as i32 + exponent;
    let digits = unsigned.replace('.', "");
    let leading_zeroes = digits.bytes().take_while(|byte| *byte == b'0').count();
    let significant = &digits[leading_zeroes..];
    let exponent = decimal_at - 1 - leading_zeroes as i32;
    let significand = if significant.len() == 1 {
        significant.to_owned()
    } else {
        format!("{}.{}", &significant[..1], &significant[1..])
    };
    let sign = if exponent >= 0 { "+" } else { "" };
    format!(
        "{}{}e{sign}{exponent}",
        if negative { "-" } else { "" },
        significand
    )
}

/// Return whether the compiled schema version occurs in the accepted set.
pub fn is_accepted_schema_version(compiled: u32, accepted: &[String]) -> bool {
    let compiled = compiled.to_string();
    accepted.iter().any(|version| version == &compiled)
}

fn validate_context(event_context: &EventContext<'_>) -> Result<(), ValidationError> {
    if event_context.detector_contract.detector_id.is_empty()
        || event_context.detector_contract.rule_version.is_empty()
    {
        return Err(ValidationError::MissingDetectorContractIdentity);
    }
    validate_non_empty("event_id", &event_context.event_id)?;
    validate_keyword("event_id", &event_context.event_id)?;
    let event_id = uuid::Uuid::parse_str(&event_context.event_id)
        .map_err(|_| ValidationError::InvalidEventId)?;
    if event_id.get_version_num() != 7 {
        return Err(ValidationError::InvalidEventId);
    }
    validate_non_empty("host_name", &event_context.local_host_name)?;
    validate_keyword("host_name", &event_context.local_host_name)?;
    validate_keyword("detector_id", event_context.detector_contract.detector_id)?;
    validate_keyword("rule_version", event_context.detector_contract.rule_version)?;
    validate_timestamp("enqueue_timestamp", &event_context.enqueue_timestamp)
}

fn validate_evidence_and_plain_language(
    evidence: &[String],
    plain_language: &str,
) -> Result<(), ValidationError> {
    if evidence.is_empty() {
        return Err(ValidationError::EmptyEvidence);
    }
    if plain_language.is_empty() {
        return Err(ValidationError::EmptyPlainLanguage);
    }
    Ok(())
}

fn validate_required_diagnosis_fields(
    diagnosis: &super::Diagnosis,
    disposition: Disposition,
) -> Result<(), ValidationError> {
    validate_non_empty("confidence_basis", &diagnosis.confidence_basis)?;
    validate_non_empty("falsifier", &diagnosis.falsifier)?;
    if diagnosis.supported_scope.is_empty() {
        return Err(ValidationError::EmptyRequiredField {
            field: "supported_scope",
        });
    }
    validate_non_empty("nearest_alternative", &diagnosis.nearest_alternative)?;
    if disposition == Disposition::Finding && diagnosis.suggested_fixes.is_empty() {
        return Err(ValidationError::EmptySuggestedFixes);
    }
    validate_timestamp("timestamp", &diagnosis.timestamp)
}

fn validate_arrays(
    evidence: &[String],
    suggested_fixes: &[String],
    supported_scope: &[String],
    missing_evidence: &[String],
) -> Result<(), ValidationError> {
    validate_array("evidence", evidence)?;
    validate_array("suggested_fixes", suggested_fixes)?;
    validate_array("supported_scope", supported_scope)?;
    validate_array("missing_evidence", missing_evidence)
}

fn validate_array(field: &'static str, values: &[String]) -> Result<(), ValidationError> {
    if values.len() > MAX_ARRAY_ELEMENTS {
        return Err(ValidationError::LimitExceeded {
            field,
            limit: MAX_ARRAY_ELEMENTS,
        });
    }
    if values
        .iter()
        .any(|value| value.chars().count() > MAX_ARRAY_ELEMENT_CHARS)
    {
        return Err(ValidationError::LimitExceeded {
            field,
            limit: MAX_ARRAY_ELEMENT_CHARS,
        });
    }
    Ok(())
}

fn validate_display(field: &'static str, value: &str) -> Result<(), ValidationError> {
    if value.chars().count() > MAX_DISPLAY_CHARS {
        return Err(ValidationError::LimitExceeded {
            field,
            limit: MAX_DISPLAY_CHARS,
        });
    }
    Ok(())
}

fn validate_keyword(field: &'static str, value: &str) -> Result<(), ValidationError> {
    if value.chars().count() > MAX_KEYWORD_CHARS {
        return Err(ValidationError::LimitExceeded {
            field,
            limit: MAX_KEYWORD_CHARS,
        });
    }
    Ok(())
}

fn validate_non_empty(field: &'static str, value: &str) -> Result<(), ValidationError> {
    if value.is_empty() {
        return Err(ValidationError::EmptyRequiredField { field });
    }
    Ok(())
}

fn validate_timestamp(field: &'static str, value: &str) -> Result<(), ValidationError> {
    chrono::DateTime::parse_from_rfc3339(value)
        .map(|_| ())
        .map_err(|_| ValidationError::InvalidTimestamp { field })
}

fn normalized_host_name(value: &str) -> Result<String, ValidationError> {
    // Rust's Unicode lowercasing is locale-independent.  Validate the emitted
    // form because a lowercase mapping may expand one scalar into several.
    let normalized = value.to_lowercase();
    validate_non_empty("host_name", &normalized)?;
    validate_keyword("host_name", &normalized)?;
    Ok(normalized)
}

fn render_display(values: &[String]) -> String {
    values
        .iter()
        .enumerate()
        .map(|(index, value)| format!("{}. {value}", index + 1))
        .collect::<Vec<_>>()
        .join("\n")
}

fn disposition_name(disposition: Disposition) -> &'static str {
    match disposition {
        Disposition::Finding => "finding",
        Disposition::NonFinding => "non_finding",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::detectors::contract::{Diagnosis, NotApplicable};
    use serde::Deserialize;
    use serde_json::Value;
    use std::fs;
    use std::path::{Path, PathBuf};

    const FIXTURE_ROOT: &str = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../fixtures/diagnosis_event/v1"
    );

    #[derive(Deserialize)]
    struct FixtureContext {
        event_id: String,
        enqueue_timestamp: String,
        local_host_name: String,
        detector_contract: FixtureDetectorContract,
        input_mode: String,
        in_process_disposition: String,
    }

    #[derive(Deserialize)]
    struct FixtureDetectorContract {
        detector_id: String,
        rule_version: String,
        error_prefix: String,
    }

    #[test]
    fn frozen_fixture_corpus_is_the_oracle() {
        let mut validator_positive = 0;
        let mut validator_negative = 0;
        let mut serde_cases = 0;
        let mut helper_cases = 0;

        for input in fixture_inputs() {
            let name = input.file_name().unwrap().to_str().unwrap();
            let expected = input.with_file_name(name.replace(".input.json", ".expected.json"));
            let input_text = fs::read_to_string(&input).unwrap();
            let expected_value: Value =
                serde_json::from_str(&fs::read_to_string(&expected).unwrap()).unwrap();

            if name.starts_with("09-") {
                serde_cases += 1;
                assert!(
                    serde_json::from_str::<Outcome>(&input_text).is_err(),
                    "{name}"
                );
                continue;
            }
            if name.starts_with("10-") || name.starts_with("16-") {
                helper_cases += 1;
                let helper_input: Value = serde_json::from_str(&input_text).unwrap();
                let compiled = helper_input["compiled_schema_version"].as_u64().unwrap() as u32;
                let accepted = helper_input["accepted_schema_versions"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|value| value.as_str().unwrap().to_owned())
                    .collect::<Vec<_>>();
                assert_eq!(
                    Value::Bool(is_accepted_schema_version(compiled, &accepted)),
                    expected_value,
                    "{name}"
                );
                continue;
            }
            if name.starts_with("26-event-bytes-saturation") {
                helper_cases += 1;
                let helper_input: Value = serde_json::from_str(&input_text).unwrap();
                let actual = helper_input["synthetic_serialized_bytes_u64"]
                    .as_u64()
                    .unwrap();
                assert_eq!(
                    saturate_event_byte_count(actual),
                    expected_value["actual_saturated"].as_u64().unwrap() as u32,
                    "{name}"
                );
                assert_eq!(expected_value["allocation_bytes_max"], 1_048_576, "{name}");
                continue;
            }

            let fixture_context = load_context(context_file_for(name));
            let event_context = event_context_from_fixture(&fixture_context);
            let disposition = match fixture_context.in_process_disposition.as_str() {
                "finding" => Disposition::Finding,
                "non_finding" => Disposition::NonFinding,
                other => panic!("unexpected fixture disposition {other}"),
            };
            let outcome = serde_json::from_str::<Outcome>(&input_text)
                .unwrap_or_else(|error| panic!("{name} must deserialize: {error}"))
                .with_disposition(disposition);
            let actual = DiagnosisEvent::try_from_outcome(outcome, &event_context);

            if expected_value.get("variant").is_some() {
                validator_negative += 1;
                let error = actual.unwrap_err();
                assert_eq!(error_sidecar(&error), expected_value, "{name}");
            } else {
                validator_positive += 1;
                let event = actual.unwrap_or_else(|error| panic!("{name} rejected: {error:?}"));
                if let Some(serialized_bytes) = expected_value.get("serialized_bytes") {
                    assert_eq!(
                        event.canonical_bytes().len() as u64,
                        serialized_bytes.as_u64().unwrap(),
                        "{name}"
                    );
                    if let Some(source_bytes) =
                        expected_value.get("source_plain_language_utf8_bytes")
                    {
                        let source: Value = serde_json::from_str(&input_text).unwrap();
                        assert_eq!(
                            source["plain_language"].as_str().unwrap().len() as u64,
                            source_bytes.as_u64().unwrap(),
                            "{name}"
                        );
                    }
                    continue;
                }
                if expected_value.get("result") == Some(&Value::String("accepted".into())) {
                    assert_eq!(
                        Value::Number(
                            (event
                                .event()
                                .rigsignal
                                .diagnosis
                                .evidence_display
                                .chars()
                                .count() as u64)
                                .into()
                        ),
                        expected_value["evidence_display_length"],
                        "{name}"
                    );
                    assert_eq!(
                        Value::Number(
                            (event
                                .event()
                                .rigsignal
                                .diagnosis
                                .suggested_fixes_display
                                .as_ref()
                                .unwrap()
                                .chars()
                                .count() as u64)
                                .into()
                        ),
                        expected_value["suggested_fixes_display_length"],
                        "{name}"
                    );
                } else {
                    // Compare through the production JCS primitive, rather
                    // than carrying a fixture-side serializer solely to turn
                    // f64 `0.0` into its canonical JSON spelling.
                    assert_eq!(
                        jcs_value(&serde_json::to_value(event.event()).unwrap()),
                        jcs_value(&expected_value),
                        "{name}"
                    );
                }
            }
        }

        assert_eq!(
            (
                validator_positive,
                validator_negative,
                serde_cases,
                helper_cases
            ),
            (11, 21, 1, 3)
        );
    }

    #[test]
    fn non_finite_confidence_is_rejected() {
        for confidence in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let outcome = valid_diagnosis(confidence, Disposition::Finding);
            assert_eq!(
                DiagnosisEvent::try_from_outcome(outcome, &test_context()).unwrap_err(),
                ValidationError::ConfidenceOutOfRange
            );
        }
    }

    #[test]
    fn jcs_uses_ecmascript_number_and_utf16_key_ordering() {
        let number = |text: &str| serde_json::from_str::<Value>(text).unwrap();
        assert_eq!(jcs_value(&number("0.000001")), "0.000001");
        assert_eq!(jcs_value(&number("0.0000001")), "1e-7");
        assert_eq!(jcs_value(&number("1000000000000000000000.0")), "1e+21");
        assert_eq!(jcs_value(&number("-0.0")), "0");
        // U+10000 is a high-surrogate pair and sorts before U+E000 in UTF-16.
        assert_eq!(
            jcs_value(&serde_json::json!({"\u{e000}": 1, "\u{10000}": 2})),
            "{\"𐀀\":2,\"\":1}"
        );
    }

    #[test]
    fn unicode_limits_count_scalar_values_and_rendered_display() {
        let crab = "🦀";
        let mut outcome = valid_diagnosis(0.5, Disposition::Finding);
        let diagnosis = match &mut outcome {
            Outcome::Diagnosis(diagnosis) => diagnosis,
            Outcome::NotApplicable(_) => unreachable!(),
        };
        diagnosis.evidence = vec![crab.repeat(4093), crab.repeat(4092)];

        let event = DiagnosisEvent::try_from_outcome(outcome, &test_context()).unwrap();
        assert_eq!(
            event
                .event()
                .rigsignal
                .diagnosis
                .evidence_display
                .chars()
                .count(),
            8192
        );

        let mut outcome = valid_diagnosis(0.5, Disposition::Finding);
        let diagnosis = match &mut outcome {
            Outcome::Diagnosis(diagnosis) => diagnosis,
            Outcome::NotApplicable(_) => unreachable!(),
        };
        diagnosis.evidence = vec![crab.repeat(4093), crab.repeat(4093)];
        assert_eq!(
            DiagnosisEvent::try_from_outcome(outcome, &test_context()).unwrap_err(),
            ValidationError::LimitExceeded {
                field: "evidence_display",
                limit: 8192,
            }
        );

        let mut at_element_limit = valid_diagnosis(0.5, Disposition::Finding);
        match &mut at_element_limit {
            Outcome::Diagnosis(diagnosis) => diagnosis.evidence = vec![crab.repeat(4096)],
            Outcome::NotApplicable(_) => unreachable!(),
        }
        assert!(DiagnosisEvent::try_from_outcome(at_element_limit, &test_context()).is_ok());

        let mut over_element_limit = valid_diagnosis(0.5, Disposition::Finding);
        match &mut over_element_limit {
            Outcome::Diagnosis(diagnosis) => diagnosis.evidence = vec![crab.repeat(4097)],
            Outcome::NotApplicable(_) => unreachable!(),
        }
        assert_eq!(
            DiagnosisEvent::try_from_outcome(over_element_limit, &test_context()).unwrap_err(),
            ValidationError::LimitExceeded {
                field: "evidence",
                limit: 4096,
            }
        );
    }

    #[test]
    fn whitespace_is_content_but_empty_required_diagnosis_values_are_rejected() {
        let mut whitespace_only = valid_diagnosis(0.5, Disposition::Finding);
        let diagnosis = match &mut whitespace_only {
            Outcome::Diagnosis(diagnosis) => diagnosis,
            Outcome::NotApplicable(_) => unreachable!(),
        };
        diagnosis.evidence = vec![" \t".into()];
        diagnosis.plain_language = "\n".into();
        assert!(DiagnosisEvent::try_from_outcome(whitespace_only, &test_context()).is_ok());

        type FieldClearer = fn(&mut Diagnosis);
        let required_string_fields: [(&str, FieldClearer); 4] = [
            ("verdict", |diagnosis: &mut Diagnosis| {
                diagnosis.verdict.clear()
            }),
            ("confidence_basis", |diagnosis: &mut Diagnosis| {
                diagnosis.confidence_basis.clear()
            }),
            ("falsifier", |diagnosis: &mut Diagnosis| {
                diagnosis.falsifier.clear()
            }),
            ("nearest_alternative", |diagnosis: &mut Diagnosis| {
                diagnosis.nearest_alternative.clear()
            }),
        ];
        for (field, set_empty) in required_string_fields {
            let mut outcome = valid_diagnosis(0.5, Disposition::Finding);
            let diagnosis = match &mut outcome {
                Outcome::Diagnosis(diagnosis) => diagnosis,
                Outcome::NotApplicable(_) => unreachable!(),
            };
            set_empty(diagnosis);
            assert_eq!(
                DiagnosisEvent::try_from_outcome(outcome, &test_context()).unwrap_err(),
                ValidationError::EmptyRequiredField { field }
            );
        }

        let mut empty_scope = valid_diagnosis(0.5, Disposition::Finding);
        match &mut empty_scope {
            Outcome::Diagnosis(diagnosis) => diagnosis.supported_scope.clear(),
            Outcome::NotApplicable(_) => unreachable!(),
        }
        assert_eq!(
            DiagnosisEvent::try_from_outcome(empty_scope, &test_context()).unwrap_err(),
            ValidationError::EmptyRequiredField {
                field: "supported_scope"
            }
        );

        let mut empty_fixes = valid_diagnosis(0.5, Disposition::Finding);
        match &mut empty_fixes {
            Outcome::Diagnosis(diagnosis) => diagnosis.suggested_fixes.clear(),
            Outcome::NotApplicable(_) => unreachable!(),
        }
        assert_eq!(
            DiagnosisEvent::try_from_outcome(empty_fixes, &test_context()).unwrap_err(),
            ValidationError::EmptySuggestedFixes
        );

        let mut non_finding = valid_diagnosis(0.5, Disposition::NonFinding);
        match &mut non_finding {
            Outcome::Diagnosis(diagnosis) => diagnosis.suggested_fixes.clear(),
            Outcome::NotApplicable(_) => unreachable!(),
        }
        assert!(DiagnosisEvent::try_from_outcome(non_finding, &test_context()).is_ok());
    }

    #[test]
    fn normalized_unicode_host_is_checked_after_lowercasing() {
        let mut at_limit = valid_diagnosis(0.5, Disposition::Finding);
        match &mut at_limit {
            Outcome::Diagnosis(diagnosis) => diagnosis.host = Some("İ".repeat(512)),
            Outcome::NotApplicable(_) => unreachable!(),
        }
        let event = DiagnosisEvent::try_from_outcome(at_limit, &test_context()).unwrap();
        assert_eq!(event.event().host.name.chars().count(), 1024);

        let mut over_limit = valid_diagnosis(0.5, Disposition::Finding);
        match &mut over_limit {
            Outcome::Diagnosis(diagnosis) => diagnosis.host = Some("İ".repeat(513)),
            Outcome::NotApplicable(_) => unreachable!(),
        }
        assert_eq!(
            DiagnosisEvent::try_from_outcome(over_limit, &test_context()).unwrap_err(),
            ValidationError::LimitExceeded {
                field: "host_name",
                limit: 1024,
            }
        );
    }

    #[test]
    fn context_requires_a_v7_event_id_and_rfc3339_timestamps() {
        let outcome = valid_diagnosis(0.5, Disposition::Finding);
        let mut context = test_context();
        context.event_id = "not-a-uuid".into();
        assert_eq!(
            DiagnosisEvent::try_from_outcome(outcome, &context).unwrap_err(),
            ValidationError::InvalidEventId
        );

        let outcome = valid_diagnosis(0.5, Disposition::Finding);
        let mut context = test_context();
        context.enqueue_timestamp = "not-a-timestamp".into();
        assert_eq!(
            DiagnosisEvent::try_from_outcome(outcome, &context).unwrap_err(),
            ValidationError::InvalidTimestamp {
                field: "enqueue_timestamp"
            }
        );

        let mut outcome = valid_diagnosis(0.5, Disposition::Finding);
        match &mut outcome {
            Outcome::Diagnosis(diagnosis) => diagnosis.timestamp = "not-a-timestamp".into(),
            Outcome::NotApplicable(_) => unreachable!(),
        }
        assert_eq!(
            DiagnosisEvent::try_from_outcome(outcome, &test_context()).unwrap_err(),
            ValidationError::InvalidTimestamp { field: "timestamp" }
        );
    }

    #[test]
    fn outcome_deserialization_rejects_ambiguous_or_duplicate_shapes_and_ignores_disposition() {
        let diagnosis = valid_diagnosis(0.5, Disposition::Finding);
        let diagnosis_json = serde_json::to_string(&diagnosis).unwrap();
        let duplicate_verdict = diagnosis_json.replacen(
            "\"verdict\":\"finding\"",
            "\"verdict\":\"finding\",\"verdict\":\"finding\"",
            1,
        );
        assert!(serde_json::from_str::<Outcome>(&duplicate_verdict).is_err());

        let mut diagnosis_with_outcome: Value = serde_json::from_str(&diagnosis_json).unwrap();
        diagnosis_with_outcome["outcome"] = Value::String("not-applicable".into());
        assert!(serde_json::from_value::<Outcome>(diagnosis_with_outcome).is_err());

        let mut diagnosis_with_unknown: Value = serde_json::from_str(&diagnosis_json).unwrap();
        diagnosis_with_unknown["unexpected"] = Value::Bool(true);
        assert!(serde_json::from_value::<Outcome>(diagnosis_with_unknown).is_err());

        let mut diagnosis_with_disposition: Value = serde_json::from_str(&diagnosis_json).unwrap();
        diagnosis_with_disposition["disposition"] = Value::String("finding".into());
        let parsed = serde_json::from_value::<Outcome>(diagnosis_with_disposition).unwrap();
        assert_eq!(parsed.disposition(), Disposition::NonFinding);

        let duplicate_outcome = r#"{"outcome":"not-applicable","outcome":"not-applicable","verdict":"not-applicable","explanation":"plain","evidence":["evidence"]}"#;
        assert!(serde_json::from_str::<Outcome>(duplicate_outcome).is_err());

        let invalid_not_applicable_verdict = r#"{"outcome":"not-applicable","verdict":"finding","explanation":"plain","evidence":["evidence"]}"#;
        assert!(serde_json::from_str::<Outcome>(invalid_not_applicable_verdict).is_err());
    }

    #[test]
    fn schema_acceptance_is_exact_string_membership_and_empty_is_fail_closed() {
        assert!(!is_accepted_schema_version(1, &[]));
        assert!(!is_accepted_schema_version(1, &["01".into(), "1.0".into()]));
        assert!(is_accepted_schema_version(1, &["1".into()]));
    }

    #[test]
    fn raw_cli_json_shapes_and_exit_codes_are_stable() {
        let diagnosis = valid_diagnosis(0.5, Disposition::Finding);
        assert_eq!(serde_json::to_string(&diagnosis).unwrap(), "{\"@timestamp\":\"2026-07-22T10:11:12.131415Z\",\"detector_id\":\"D6\",\"rule_version\":\"d6.2\",\"verdict\":\"finding\",\"confidence\":0.5,\"confidence_basis\":\"basis\",\"evidence\":[\"evidence\"],\"plain_language\":\"plain\",\"suggested_fixes\":[\"fix\"],\"falsifier\":\"falsifier\",\"supported_scope\":[\"scope\"],\"missing_evidence\":[],\"nearest_alternative\":\"alternative\"}");
        assert_eq!(
            super::super::json_output_line(&diagnosis).unwrap(),
            format!("{}\n", serde_json::to_string(&diagnosis).unwrap())
        );

        let not_applicable =
            Outcome::NotApplicable(NotApplicable::new("plain", vec!["evidence".into()]));
        assert_eq!(serde_json::to_string(&not_applicable).unwrap(), "{\"outcome\":\"not-applicable\",\"verdict\":\"not-applicable\",\"explanation\":\"plain\",\"evidence\":[\"evidence\"]}");
        assert_eq!(
            super::super::json_output_line(&not_applicable).unwrap(),
            format!("{}\n", serde_json::to_string(&not_applicable).unwrap())
        );

        assert_eq!(
            super::super::emit(test_contract(), Ok(diagnosis), true),
            std::process::ExitCode::from(1)
        );
        assert_eq!(
            super::super::emit(test_contract(), Ok(not_applicable), true),
            std::process::ExitCode::from(0)
        );
        assert_eq!(
            super::super::emit(test_contract(), Err("broken".into()), true),
            std::process::ExitCode::from(2)
        );
    }

    #[test]
    fn outcome_serde_round_trip_is_exhaustive() {
        let diagnosis = valid_diagnosis(0.5, Disposition::Finding);
        let not_applicable =
            Outcome::NotApplicable(NotApplicable::new("plain", vec!["evidence".into()]));
        for outcome in [diagnosis, not_applicable] {
            let round_trip: Outcome =
                serde_json::from_str(&serde_json::to_string(&outcome).unwrap()).unwrap();
            match round_trip {
                Outcome::Diagnosis(_) | Outcome::NotApplicable(_) => {}
            }
        }
    }

    fn fixture_inputs() -> Vec<PathBuf> {
        let mut inputs = ["positive", "negative"]
            .iter()
            .flat_map(|directory| fs::read_dir(Path::new(FIXTURE_ROOT).join(directory)).unwrap())
            .map(Result::unwrap)
            .map(|entry| entry.path())
            .filter(|path| path.to_string_lossy().ends_with(".input.json"))
            .collect::<Vec<_>>();
        inputs.sort();
        inputs
    }

    fn context_file_for(name: &str) -> &'static str {
        if name.starts_with("05-missing") {
            "missing-detector-identity.json"
        } else if name.starts_with("05b-") {
            "missing-detector-rule-version-identity.json"
        } else if name.starts_with("02-") || name.starts_with("04-") || name.starts_with("15-") {
            "non-finding.json"
        } else {
            "diagnosis-finding.json"
        }
    }

    fn load_context(name: &str) -> FixtureContext {
        serde_json::from_str(
            &fs::read_to_string(Path::new(FIXTURE_ROOT).join("contexts").join(name)).unwrap(),
        )
        .unwrap()
    }

    fn event_context_from_fixture(context: &FixtureContext) -> EventContext<'static> {
        let contract = DetectorContract {
            detector_id: Box::leak(
                context
                    .detector_contract
                    .detector_id
                    .clone()
                    .into_boxed_str(),
            ),
            rule_version: Box::leak(
                context
                    .detector_contract
                    .rule_version
                    .clone()
                    .into_boxed_str(),
            ),
            error_prefix: Box::leak(
                context
                    .detector_contract
                    .error_prefix
                    .clone()
                    .into_boxed_str(),
            ),
        };
        EventContext {
            event_id: context.event_id.clone(),
            enqueue_timestamp: context.enqueue_timestamp.clone(),
            local_host_name: context.local_host_name.clone(),
            detector_contract: Box::leak(Box::new(contract)),
            input_mode: match context.input_mode.as_str() {
                "fixture" => InputMode::Fixture,
                other => panic!("unexpected fixture input mode {other}"),
            },
        }
    }

    fn error_sidecar(error: &ValidationError) -> Value {
        match error {
            ValidationError::EmptyEvidence => serde_json::json!({"variant": "EmptyEvidence"}),
            ValidationError::EmptyPlainLanguage => {
                serde_json::json!({"variant": "EmptyPlainLanguage"})
            }
            ValidationError::EmptyRequiredField { field } => {
                serde_json::json!({"variant": "EmptyRequiredField", "field": field})
            }
            ValidationError::EmptySuggestedFixes => {
                serde_json::json!({"variant": "EmptySuggestedFixes"})
            }
            ValidationError::MissingDetectorContractIdentity => {
                serde_json::json!({"variant": "MissingDetectorContractIdentity"})
            }
            ValidationError::ConfidenceOutOfRange => {
                serde_json::json!({"variant": "ConfidenceOutOfRange"})
            }
            ValidationError::InvalidTimestamp { field } => {
                serde_json::json!({"variant": "InvalidTimestamp", "field": field})
            }
            ValidationError::InvalidEventId => serde_json::json!({"variant": "InvalidEventId"}),
            ValidationError::LimitExceeded { field, limit } => {
                serde_json::json!({"variant": "LimitExceeded", "field": field, "limit": limit})
            }
            ValidationError::DetectorContractMismatch {
                field,
                expected,
                actual,
            } => {
                serde_json::json!({"variant": "DetectorContractMismatch", "field": field, "expected": expected, "actual": actual})
            }
            ValidationError::EventBytesLimitExceeded {
                limit,
                actual_saturated,
            } => serde_json::json!({
                "variant": "EventBytesLimitExceeded",
                "limit": limit,
                "actual_saturated": actual_saturated,
            }),
        }
    }

    fn test_contract() -> DetectorContract {
        DetectorContract {
            detector_id: "D6",
            rule_version: "d6.2",
            error_prefix: "D6",
        }
    }

    fn test_context() -> EventContext<'static> {
        EventContext {
            event_id: "01890f3e-7b64-7cc7-8a3d-5e6f708192a3".into(),
            enqueue_timestamp: "2026-07-22T12:34:56.789012Z".into(),
            local_host_name: "Fixture-LOCAL.EXAMPLE".into(),
            detector_contract: Box::leak(Box::new(test_contract())),
            input_mode: InputMode::Fixture,
        }
    }

    fn valid_diagnosis(confidence: f64, disposition: Disposition) -> Outcome {
        Outcome::Diagnosis(Box::new(Diagnosis {
            timestamp: "2026-07-22T10:11:12.131415Z".into(),
            detector_id: "D6".into(),
            rule_version: "d6.2".into(),
            verdict: "finding".into(),
            confidence,
            confidence_basis: "basis".into(),
            evidence: vec!["evidence".into()],
            plain_language: "plain".into(),
            suggested_fixes: vec!["fix".into()],
            falsifier: "falsifier".into(),
            host: None,
            supported_scope: vec!["scope".into()],
            missing_evidence: vec![],
            nearest_alternative: "alternative".into(),
            disposition,
        }))
    }
}
