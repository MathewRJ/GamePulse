//! Portable black-box coverage for the explicit D3 offline CLI surface.

use serde_json::Value;
use std::fs;
use std::path::PathBuf;
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TempState(PathBuf);
impl TempState {
    fn new() -> Self {
        let number = SEQUENCE.fetch_add(1, Ordering::Relaxed);
        Self(std::env::temp_dir().join(format!(
            "rigsignal-d3-cli-{}-{number}/state.json",
            std::process::id()
        )))
    }
}
impl Drop for TempState {
    fn drop(&mut self) {
        if let Some(parent) = self.0.parent() {
            let _ = fs::remove_dir_all(parent);
        }
    }
}

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("repository parent")
        .to_path_buf()
}
fn fixture(relative: &str) -> PathBuf {
    root().join("fixtures/d3").join(relative)
}
fn agent() -> Command {
    Command::new(env!("CARGO_BIN_EXE_rigsignal-agent"))
}
fn status(output: &Output) -> i32 {
    output.status.code().expect("normal exit")
}
fn offline(snapshot: &str, inventory: &str, state: &TempState) -> Command {
    let mut command = agent();
    command
        .args(["diagnose", "gpu-boot", "--offline", "--pci-snapshot"])
        .arg(fixture(snapshot))
        .arg("--boot-list")
        .arg(fixture(inventory))
        .arg("--state-file")
        .arg(&state.0);
    command
}

fn offline_current(snapshot: &str, current_boot_id: &str, state: &TempState) -> Command {
    let mut command = agent();
    command
        .args(["diagnose", "gpu-boot", "--offline", "--pci-snapshot"])
        .arg(fixture(snapshot))
        .args(["--current-boot-id", current_boot_id, "--state-file"])
        .arg(&state.0);
    command
}

fn learn(snapshot: &str, inventory: &str, state: &TempState) {
    let output = offline(snapshot, inventory, state)
        .args(["--learn-baseline", "--slot", "0000:03:00.0"])
        .output()
        .unwrap();
    assert_eq!(status(&output), 0);
}

fn assert_verdict(output: Output, expected_status: i32, expected_verdict: &str) {
    assert_eq!(status(&output), expected_status);
    let diagnosis: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(diagnosis["verdict"], expected_verdict);
}

#[test]
fn gpu_boot_is_nested_and_fixture_flags_require_offline() {
    let output = agent().args(["diagnose", "--help"]).output().unwrap();
    assert_eq!(status(&output), 0);
    assert!(String::from_utf8_lossy(&output.stdout).contains("gpu-boot"));

    let output = agent()
        .args(["diagnose", "gpu-boot", "--pci-snapshot"])
        .arg(fixture("synthetic/recovery-healthy-pci-topology.txt"))
        .output()
        .unwrap();
    assert_eq!(status(&output), 2);
    assert!(String::from_utf8_lossy(&output.stderr).contains("require explicit --offline"));
}

#[test]
fn offline_trio_is_preflighted_before_baseline_verdict() {
    let output = agent()
        .args(["diagnose", "gpu-boot", "--offline", "--pci-snapshot"])
        .arg(fixture("synthetic/recovery-healthy-pci-topology.txt"))
        .output()
        .unwrap();
    assert_eq!(status(&output), 2);
    assert!(String::from_utf8_lossy(&output.stderr).contains("non-default --state-file"));
}

#[test]
fn offline_learn_and_bus_absence_use_the_real_cli_contract() {
    let state = TempState::new();
    let output = offline(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    )
    .args(["--learn-baseline", "--slot", "0000:03:00.0", "--json"])
    .output()
    .unwrap();
    assert_eq!(status(&output), 0);
    let learned: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(learned["verdict"], "baseline-learned");

    let output = offline(
        "synthetic/absent-gpu-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    )
    .arg("--json")
    .output()
    .unwrap();
    assert_eq!(status(&output), 1);
    let absent: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(absent["verdict"], "bus-absent");
    assert!(absent["missing_evidence"].is_array());
}

#[test]
fn gpu_boot_ok_exits_zero() {
    let state = TempState::new();
    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    );
    assert_verdict(
        offline(
            "synthetic/recovery-healthy-pci-topology.txt",
            "synthetic/recovery-boot-inventory.txt",
            &state,
        )
        .arg("--journal-prior-tail")
        .arg(fixture("synthetic/clean-tail-prior-tail.log"))
        .arg("--json")
        .output()
        .unwrap(),
        0,
        "ok",
    );
}

#[test]
fn gpu_boot_hardware_changed_exits_one() {
    let state = TempState::new();
    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    );
    assert_verdict(
        offline_current(
            "synthetic/different-device-at-bdf-pci-topology.txt",
            "22222222222242228222222222222222",
            &state,
        )
        .arg("--json")
        .output()
        .unwrap(),
        1,
        "hardware-changed",
    );
}

#[test]
fn gpu_boot_precursor_warning_deduplicates_overlapping_inputs() {
    let state = TempState::new();
    learn(
        "synthetic/precursor-current-healthy-pci-topology.txt",
        "synthetic/precursor-boot-inventory.txt",
        &state,
    );
    assert_verdict(
        offline(
            "synthetic/precursor-current-healthy-pci-topology.txt",
            "synthetic/precursor-boot-inventory.txt",
            &state,
        )
        .arg("--journal-prior-kernel")
        .arg(fixture("synthetic/precursor-prior-kernel.log"))
        .arg("--journal-prior-tail")
        .arg(fixture("synthetic/precursor-prior-tail.log"))
        .arg("--json")
        .output()
        .unwrap(),
        1,
        "precursor-warning",
    );
}

#[test]
fn gpu_boot_overlap_below_threshold_exits_zero() {
    let state = TempState::new();
    learn(
        "synthetic/precursor-current-healthy-pci-topology.txt",
        "synthetic/precursor-boot-inventory.txt",
        &state,
    );
    assert_verdict(
        offline(
            "synthetic/precursor-current-healthy-pci-topology.txt",
            "synthetic/precursor-boot-inventory.txt",
            &state,
        )
        .arg("--journal-prior-kernel")
        .arg(fixture("synthetic/threshold-minus-one-prior-kernel.log"))
        .arg("--journal-prior-tail")
        .arg(fixture("synthetic/threshold-minus-one-prior-tail.log"))
        .arg("--json")
        .output()
        .unwrap(),
        0,
        "ok",
    );
}

#[test]
fn gpu_boot_recovered_exits_zero() {
    let state = TempState::new();
    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    );
    let absent = offline(
        "synthetic/absent-gpu-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    )
    .output()
    .unwrap();
    assert_eq!(status(&absent), 1);
    assert_verdict(
        offline(
            "synthetic/recovery-healthy-pci-topology.txt",
            "synthetic/recovery-boot-inventory.txt",
            &state,
        )
        .arg("--json")
        .output()
        .unwrap(),
        0,
        "recovered",
    );
}

#[test]
fn gpu_boot_history_unavailable_exits_zero() {
    let state = TempState::new();
    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    );
    assert_verdict(
        offline_current(
            "synthetic/recovery-healthy-pci-topology.txt",
            "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
            &state,
        )
        .arg("--json")
        .output()
        .unwrap(),
        0,
        "history-unavailable",
    );
}

#[test]
fn gpu_boot_baseline_required_exits_zero() {
    let state = TempState::new();
    assert_verdict(
        offline_current(
            "synthetic/recovery-healthy-pci-topology.txt",
            "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
            &state,
        )
        .arg("--json")
        .output()
        .unwrap(),
        0,
        "baseline-required",
    );
}

#[test]
fn gpu_boot_reset_baseline_is_idempotent_and_exits_zero() {
    let state = TempState::new();
    for _ in 0..2 {
        assert_verdict(
            offline_current(
                "synthetic/recovery-healthy-pci-topology.txt",
                "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
                &state,
            )
            .args(["--reset-baseline", "--json"])
            .output()
            .unwrap(),
            0,
            "baseline-reset",
        );
    }
}

#[test]
fn gpu_boot_unknown_state_schema_exits_two() {
    let state = TempState::new();
    fs::create_dir_all(state.0.parent().unwrap()).unwrap();
    fs::write(
        &state.0,
        r#"{"schema_version":99,"baseline":null,"pending":null}"#,
    )
    .unwrap();
    let output = offline_current(
        "synthetic/recovery-healthy-pci-topology.txt",
        "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
        &state,
    )
    .output()
    .unwrap();
    assert_eq!(status(&output), 2);
    assert!(String::from_utf8_lossy(&output.stderr).contains("unknown D3 state schema"));
}

#[test]
fn no_entries_each_offline_journal_input_is_reported_as_missing_evidence() {
    let state = TempState::new();
    let tail_state = TempState::new();
    let kernel_state = TempState::new();
    fs::create_dir_all(state.0.parent().unwrap()).unwrap();
    let no_entries = state.0.parent().unwrap().join("no-entries.log");
    fs::write(&no_entries, "-- No entries --\n").unwrap();
    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    );

    let current = offline(
        "synthetic/absent-gpu-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    )
    .arg("--journal-current")
    .arg(&no_entries)
    .arg("--json")
    .output()
    .unwrap();
    assert_eq!(status(&current), 1);
    let current: Value = serde_json::from_slice(&current.stdout).unwrap();
    assert!(current["missing_evidence"]
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item.as_str().unwrap().contains("no entries")));

    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &tail_state,
    );
    let tail = offline(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &tail_state,
    )
    .arg("--journal-prior-tail")
    .arg(&no_entries)
    .arg("--json")
    .output()
    .unwrap();
    assert_verdict(tail, 0, "history-unavailable");

    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &kernel_state,
    );
    let kernel = offline(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &kernel_state,
    )
    .arg("--journal-prior-kernel")
    .arg(&no_entries)
    .arg("--journal-prior-tail")
    .arg(fixture("synthetic/clean-tail-prior-tail.log"))
    .arg("--json")
    .output()
    .unwrap();
    assert_eq!(status(&kernel), 0);
    let kernel: Value = serde_json::from_slice(&kernel.stdout).unwrap();
    assert!(kernel["missing_evidence"]
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item.as_str().unwrap().contains("no entries")));
}

#[test]
fn oversized_offline_journal_is_bounded_like_live_collection() {
    let state = TempState::new();
    fs::create_dir_all(state.0.parent().unwrap()).unwrap();
    let large_tail = state.0.parent().unwrap().join("legacy-large-tail.log");
    // The byte cap falls in the second byte of this valid UTF-8 character.
    let shutdown = b"2026-07-21T23:59:30+00:00 host systemd[1]: Reached target Shutdown.\n";
    let mut source = "é".as_bytes().to_vec();
    source.extend(vec![
        b'x';
        1024 * 1024 + 1 - source.len() - 1 - shutdown.len()
    ]);
    source.push(b'\n');
    source.extend_from_slice(shutdown);
    assert_eq!(source.len(), 1_048_577);
    fs::write(&large_tail, source).unwrap();
    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    );
    let output = offline(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    )
    .arg("--journal-prior-tail")
    .arg(&large_tail)
    .arg("--json")
    .output()
    .unwrap();
    assert_eq!(status(&output), 0);
    let diagnosis: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(diagnosis["verdict"], "ok");
    assert!(diagnosis["missing_evidence"]
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item.as_str().unwrap().contains("bounded")));
}

#[test]
fn supplied_current_journal_from_another_boot_is_an_invocation_error() {
    let state = TempState::new();
    learn(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    );
    let output = offline(
        "synthetic/recovery-healthy-pci-topology.txt",
        "synthetic/recovery-boot-inventory.txt",
        &state,
    )
    .arg("--journal-current")
    .arg(fixture("synthetic/absent-gpu-current-journal.log"))
    .arg("--json")
    .output()
    .unwrap();
    assert_eq!(status(&output), 2);
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8_lossy(&output.stderr)
        .contains("D3 incomplete: boot identity pairing invalid"));
}

#[test]
fn legacy_prior_kernel_without_inventory_prior_is_history_unavailable() {
    let state = TempState::new();
    fs::create_dir_all(state.0.parent().unwrap()).unwrap();
    let slot: Value =
        serde_json::from_slice(&fs::read(fixture("real/legacy/healthy-slot.json")).unwrap())
            .unwrap();
    let inventory = fs::read_to_string(fixture("real/legacy/boot-inventory.txt")).unwrap();
    let current_boot_id = inventory.split_whitespace().nth(1).unwrap();
    let snapshot = format!(
        "# converted authoritative healthy snapshot from healthy-slot.json\n# boot_id={current_boot_id}\n0000:00:03.1 path=pci0000:00/0000:00:03.1 vendor=0x1022 device=0x14db class=0x060400\n{} path=pci0000:00/0000:00:03.1/{} vendor={} device={} class={}\n",
        slot["slot"].as_str().unwrap(),
        slot["slot"].as_str().unwrap(),
        slot["vendor"].as_str().unwrap(),
        slot["device"].as_str().unwrap(),
        slot["class"].as_str().unwrap(),
    );
    let snapshot_path = state.0.parent().unwrap().join("legacy-pci-topology.txt");
    fs::write(&snapshot_path, snapshot).unwrap();

    let learned = agent()
        .args(["diagnose", "gpu-boot", "--offline", "--pci-snapshot"])
        .arg(&snapshot_path)
        .arg("--boot-list")
        .arg(fixture("real/legacy/boot-inventory.txt"))
        .args([
            "--state-file",
            state.0.to_str().unwrap(),
            "--learn-baseline",
            "--slot",
        ])
        .arg(slot["slot"].as_str().unwrap())
        .arg("--json")
        .output()
        .unwrap();
    assert_verdict(learned, 0, "baseline-learned");

    let output = agent()
        .args(["diagnose", "gpu-boot", "--offline", "--pci-snapshot"])
        .arg(&snapshot_path)
        .arg("--boot-list")
        .arg(fixture("real/legacy/boot-inventory.txt"))
        .args([
            "--state-file",
            state.0.to_str().unwrap(),
            "--journal-prior-kernel",
        ])
        .arg(fixture("real/legacy/good-boot-kernel.log"))
        .arg("--json")
        .output()
        .unwrap();
    assert_eq!(status(&output), 0);
    let diagnosis: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(diagnosis["verdict"], "history-unavailable");
    assert!(diagnosis["missing_evidence"]
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item
            .as_str()
            .unwrap()
            .contains("supplied prior boot kernel journal could not be attributed")));
}
