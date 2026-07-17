//! Minimal parser for tracepoint `format` files.
//!
//! Kernel tracepoint layouts are ABI supplied by tracefs, so matching the field name
//! and validating its width is safer than relying on a source-kernel offset.

use std::fmt;

#[derive(Debug, PartialEq, Eq)]
pub enum FormatError {
    MalformedField { line: usize, reason: &'static str },
    MissingField(String),
    AmbiguousField(String),
    WrongSize { field: String, size: u32 },
}

impl fmt::Display for FormatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MalformedField { line, reason } => {
                write!(f, "malformed field declaration on line {line}: {reason}")
            }
            Self::MissingField(field) => write!(f, "field '{field}' not found"),
            Self::AmbiguousField(field) => write!(f, "field '{field}' appears more than once"),
            Self::WrongSize { field, size } => {
                write!(f, "field '{field}' has size {size}, expected 8")
            }
        }
    }
}

impl std::error::Error for FormatError {}

struct Field<'a> {
    name: &'a str,
    offset: u32,
    size: u32,
}

fn parse_number(value: &str, line: usize, label: &'static str) -> Result<u32, FormatError> {
    value
        .trim()
        .parse()
        .map_err(|_| FormatError::MalformedField {
            line,
            reason: label,
        })
}

fn parse_field(line: &str, line_number: usize) -> Result<Field<'_>, FormatError> {
    let declaration = line
        .strip_prefix("field:")
        .expect("parse_field is only called for field declarations");
    let mut segments = declaration.split(';');
    let name = segments
        .next()
        .and_then(|part| part.split_whitespace().last())
        .filter(|name| !name.is_empty())
        .ok_or(FormatError::MalformedField {
            line: line_number,
            reason: "missing field name",
        })?;
    let offset = segments
        .find_map(|part| part.trim().strip_prefix("offset:"))
        .ok_or(FormatError::MalformedField {
            line: line_number,
            reason: "missing offset",
        })
        .and_then(|value| parse_number(value, line_number, "invalid offset"))?;
    let size = segments
        .find_map(|part| part.trim().strip_prefix("size:"))
        .ok_or(FormatError::MalformedField {
            line: line_number,
            reason: "missing size",
        })
        .and_then(|value| parse_number(value, line_number, "invalid size"))?;

    Ok(Field { name, offset, size })
}

/// Find a uniquely named, 64-bit key field and return its byte offset.
pub fn parse_key_field_offset(format: &str, key_field: &str) -> Result<u32, FormatError> {
    let mut matched = None;

    for (index, line) in format.lines().enumerate() {
        let line = line.trim_start();
        if !line.starts_with("field:") {
            continue;
        }
        let field = parse_field(line, index + 1)?;
        if field.name != key_field {
            continue;
        }
        if matched.is_some() {
            return Err(FormatError::AmbiguousField(key_field.to_owned()));
        }
        if field.size != 8 {
            return Err(FormatError::WrongSize {
                field: key_field.to_owned(),
                size: field.size,
            });
        }
        matched = Some(field.offset);
    }

    matched.ok_or_else(|| FormatError::MissingField(key_field.to_owned()))
}

#[cfg(test)]
mod tests {
    use super::{parse_key_field_offset, FormatError};

    const LEGACY_QUEUE: &str = include_str!("fixtures/valve-6.16-drm_sched_job.format");
    const LEGACY_RUN: &str = include_str!("fixtures/valve-6.16-drm_run_job.format");
    // Synthetic: documented renamed tracepoint layout; no live renamed-kernel fixture exists.
    const RENAMED: &str = include_str!("fixtures/synthetic-drm_sched_job_queue.format");
    const MALFORMED: &str = include_str!("fixtures/malformed.format");
    const AMBIGUOUS: &str = include_str!("fixtures/ambiguous-id.format");
    const WRONG_SIZE: &str = include_str!("fixtures/wrong-size-id.format");

    #[test]
    fn parses_both_real_legacy_formats() {
        assert_eq!(parse_key_field_offset(LEGACY_QUEUE, "id"), Ok(32));
        assert_eq!(parse_key_field_offset(LEGACY_RUN, "id"), Ok(32));
    }

    #[test]
    fn parses_synthetic_renamed_format() {
        assert_eq!(parse_key_field_offset(RENAMED, "fence_seqno"), Ok(32));
    }

    #[test]
    fn rejects_malformed_format() {
        assert!(matches!(
            parse_key_field_offset(MALFORMED, "id"),
            Err(FormatError::MalformedField { .. })
        ));
    }

    #[test]
    fn rejects_ambiguous_key_field() {
        assert_eq!(
            parse_key_field_offset(AMBIGUOUS, "id"),
            Err(FormatError::AmbiguousField("id".to_owned()))
        );
    }

    #[test]
    fn rejects_wrong_key_size() {
        assert_eq!(
            parse_key_field_offset(WRONG_SIZE, "id"),
            Err(FormatError::WrongSize {
                field: "id".to_owned(),
                size: 4,
            })
        );
    }

    #[test]
    fn rejects_missing_key_field() {
        assert_eq!(
            parse_key_field_offset(RENAMED, "id"),
            Err(FormatError::MissingField("id".to_owned()))
        );
    }
}
