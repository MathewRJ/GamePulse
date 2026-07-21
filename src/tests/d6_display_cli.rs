//! Black-box CLI coverage for `rigsignal-agent diagnose display`.
//!
//! Every display invocation supplies captured fixtures; it never consults live
//! gamescope or sysfs state, so the checks are portable to the Windows CI job.

use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TempFixture {
    path: PathBuf,
}

impl TempFixture {
    fn new(label: &str, contents: &str) -> Self {
        let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "rigsignal-d6-cli-{label}-{}-{sequence}.tmp",
            std::process::id()
        ));
        fs::write(&path, contents).expect("write temporary fixture");
        Self { path }
    }
}

impl Drop for TempFixture {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

struct TempDirectory {
    path: PathBuf,
}

impl TempDirectory {
    fn new(label: &str) -> Self {
        let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "rigsignal-d6-cli-{label}-{}-{sequence}.tmp",
            std::process::id()
        ));
        fs::create_dir(&path).expect("create temporary directory");
        Self { path }
    }
}

impl Drop for TempDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir(&self.path);
    }
}

fn repository_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src crate has a repository parent")
        .to_path_buf()
}

fn fixture(relative: &str) -> PathBuf {
    repository_root().join("fixtures/d6").join(relative)
}

fn agent() -> Command {
    Command::new(env!("CARGO_BIN_EXE_rigsignal-agent"))
}

fn offline_display(modes_cfg: &Path, drm_state: &Path) -> Command {
    let mut command = agent();
    command
        .arg("diagnose")
        .arg("display")
        .arg("--modes-cfg")
        .arg(modes_cfg)
        .arg("--drm-state")
        .arg(drm_state);
    command
}

fn run(command: &mut Command) -> Output {
    command.output().expect("run rigsignal-agent")
}

fn status(output: &Output) -> i32 {
    output
        .status
        .code()
        .expect("process was not terminated by signal")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout is UTF-8")
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("stderr is UTF-8")
}

#[test]
fn display_is_a_nested_diagnose_subcommand() {
    let mut command = agent();
    command.arg("diagnose").arg("--help");
    let output = run(&mut command);
    assert_eq!(status(&output), 0);
    assert!(stdout(&output).contains("display"));

    let modes = fixture("deck-real/modes.cfg");
    let drm = fixture("deck-real/drm-state.json");
    let mut command = agent();
    command
        .arg("diagnose")
        .arg("--modes-cfg")
        .arg(modes)
        .arg("--drm-state")
        .arg(drm);
    let output = run(&mut command);
    assert_eq!(status(&output), 2);
    assert!(stderr(&output).contains("unexpected argument"));
}

#[test]
fn display_exit_codes_cover_ok_not_applicable_finding_and_incomplete() {
    let deck_modes = fixture("deck-real/modes.cfg");
    let deck_drm = fixture("deck-real/drm-state.json");
    assert_eq!(
        status(&run(&mut offline_display(&deck_modes, &deck_drm))),
        0
    );

    let unmapped = TempFixture::new("unmapped", "Not This Display:1920x1080@60\n");
    let synthetic_drm = fixture("synthetic-4k-drm-state.json");
    assert_eq!(
        status(&run(&mut offline_display(&unmapped.path, &synthetic_drm))),
        0
    );

    let incident_modes = fixture("deck-incident-bad/modes.cfg");
    let incident_drm = fixture("deck-incident-bad/drm-state.json");
    assert_eq!(
        status(&run(&mut offline_display(&incident_modes, &incident_drm))),
        1
    );

    let invalid_modes = fixture("invalid-mode-modes.cfg");
    assert_eq!(
        status(&run(&mut offline_display(&invalid_modes, &synthetic_drm))),
        1
    );

    let unparsable = TempFixture::new("unparsable", "not a modes.cfg line\n");
    let output = run(&mut offline_display(&unparsable.path, &synthetic_drm));
    assert_eq!(status(&output), 2);
    assert!(stderr(&output).contains("D6 incomplete"));
    assert!(
        stdout(&output).is_empty(),
        "errors must not emit stdout JSON"
    );

    let mut command = agent();
    command
        .arg("diagnose")
        .arg("display")
        .arg("--modes-cfg")
        .arg(&deck_modes);
    let output = run(&mut command);
    assert_eq!(status(&output), 2);
    assert!(stderr(&output).contains("requires both --modes-cfg and --drm-state"));

    let missing_modes = fixture("missing-modes.cfg");
    let output = run(&mut offline_display(&missing_modes, &synthetic_drm));
    assert_eq!(status(&output), 2);
    assert!(stderr(&output).contains("cannot read modes.cfg"));
}

#[test]
fn display_empty_modes_still_validates_explicit_drm_json() {
    let empty_modes = TempFixture::new("empty-modes", "");
    let malformed_drm = TempFixture::new("malformed-drm", "not JSON");
    let output = run(&mut offline_display(&empty_modes.path, &malformed_drm.path));
    assert_eq!(status(&output), 2);
    assert!(stderr(&output).contains("malformed DRM-state JSON"));
}

#[test]
fn display_oversized_fixture_is_incomplete() {
    let oversized_modes = TempFixture::new("oversized-modes", &"x".repeat(1024 * 1024 + 1));
    let drm = fixture("synthetic-4k-drm-state.json");
    let output = run(&mut offline_display(&oversized_modes.path, &drm));
    assert_eq!(status(&output), 2);
    assert!(stderr(&output).contains("exceeds 1048576-byte limit"));
}

#[test]
fn display_non_regular_fixture_is_incomplete() {
    let directory = TempDirectory::new("directory-modes");
    let drm = fixture("synthetic-4k-drm-state.json");
    let output = run(&mut offline_display(&directory.path, &drm));
    assert_eq!(status(&output), 2);
    assert!(stderr(&output).contains("not a regular file"));
}

#[test]
fn display_diagnosis_json_is_wire_compatible_except_d6_2_contract_additions() {
    let modes = fixture("deck-incident-bad/modes.cfg");
    let drm = fixture("deck-incident-bad/drm-state.json");
    let mut command = offline_display(&modes, &drm);
    command.arg("--json");
    let output = run(&mut command);
    assert_eq!(status(&output), 1);

    let text = stdout(&output);
    assert_eq!(text.lines().count(), 1, "JSON must occupy one line: {text}");
    let value: Value = serde_json::from_str(&text).expect("valid diagnosis JSON");
    let expected_fields = [
        "@timestamp",
        "detector_id",
        "rule_version",
        "verdict",
        "confidence",
        "confidence_basis",
        "evidence",
        "plain_language",
        "suggested_fixes",
        "falsifier",
        "supported_scope",
        "missing_evidence",
        "nearest_alternative",
    ];
    assert_eq!(
        value
            .as_object()
            .expect("diagnosis is an object")
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>(),
        expected_fields.into_iter().collect(),
        "only the d6.2 contract fields may differ from the frozen d6.1 shape"
    );
    for field in expected_fields {
        assert!(value.get(field).is_some(), "missing {field}");
    }
    assert_eq!(value["detector_id"], "D6");
    assert_eq!(value["rule_version"], "d6.2");
    assert_eq!(value["verdict"], "mode-override-degraded");
    assert!(value["@timestamp"].is_string());
    assert!(value["detector_id"].is_string());
    assert!(value["rule_version"].is_string());
    assert!(value["verdict"].is_string());
    assert!(value["confidence"].is_number());
    assert!(value["confidence_basis"].is_string());
    assert!(value["evidence"].is_array());
    assert!(value["plain_language"].is_string());
    assert!(value["suggested_fixes"].is_array());
    assert!(value["falsifier"].is_string());
    assert!(value["supported_scope"].is_array());
    assert!(value["missing_evidence"].is_array());
    assert!(value["nearest_alternative"].is_string());
    assert!(value.get("outcome").is_none(), "diagnosis is not a wrapper");
}

#[test]
fn display_not_applicable_json_is_one_line_and_typed() {
    let unmapped = TempFixture::new("json-unmapped", "Not This Display:1920x1080@60\n");
    let drm = fixture("synthetic-4k-drm-state.json");
    let mut command = offline_display(&unmapped.path, &drm);
    command.arg("--json");
    let output = run(&mut command);
    assert_eq!(status(&output), 0);

    let text = stdout(&output);
    assert_eq!(text.lines().count(), 1, "JSON must occupy one line: {text}");
    let value: Value = serde_json::from_str(&text).expect("valid not-applicable JSON");
    assert_eq!(
        value
            .as_object()
            .expect("not-applicable is an object")
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>(),
        ["outcome", "verdict", "explanation", "evidence"]
            .into_iter()
            .collect(),
        "not-applicable must retain its smaller untagged wire shape"
    );
    assert_eq!(value["outcome"], "not-applicable");
    assert_eq!(value["verdict"], "not-applicable");
    assert!(value["explanation"].is_string());
    assert!(value["evidence"].is_array());
    assert!(value.get("detector_id").is_none(), "no fake diagnosis");
}

#[test]
fn display_human_output_includes_the_diagnosis_contract_fields() {
    let modes = fixture("deck-incident-bad/modes.cfg");
    let drm = fixture("deck-incident-bad/drm-state.json");
    let output = run(&mut offline_display(&modes, &drm));
    assert_eq!(status(&output), 1);

    let text = stdout(&output);
    for field in [
        "detector_id: D6",
        "rule_version: d6.2",
        "verdict: mode-override-degraded",
        "confidence:",
        "confidence_basis:",
        "evidence:",
        "plain_language:",
        "suggested_fix:",
        "falsifier:",
        "supported_scope:",
        "missing_evidence: []",
        "nearest_alternative:",
    ] {
        assert!(text.contains(field), "human output missing {field}: {text}");
    }
}

#[test]
fn legacy_diagnose_output_flag_still_writes_the_plain_text_report() {
    let config = TempFixture::new(
        "legacy-config",
        "[elasticsearch]\nendpoint = \"http://127.0.0.1:9\"\n",
    );
    let report = TempFixture::new("legacy-report", "placeholder\n");
    fs::remove_file(&report.path).expect("remove placeholder report");

    let mut command = agent();
    command
        .arg("--config")
        .arg(&config.path)
        .arg("diagnose")
        .arg("--output")
        .arg(&report.path)
        .env("ES_URL", "http://127.0.0.1:9")
        .env_remove("HTTP_PROXY")
        .env_remove("HTTPS_PROXY")
        .env_remove("ALL_PROXY");
    let output = run(&mut command);
    assert_eq!(status(&output), 0, "{}", stderr(&output));
    assert!(stderr(&output).contains("diagnostic report written to"));

    let text = fs::read_to_string(&report.path).expect("legacy diagnose report was written");
    assert!(text.contains("=== RigSignal Diagnostic Report ==="));
    assert!(text.contains("Config\n"));
    assert!(text.contains("Endpoint:   http://127.0.0.1:9"));
}
