#[allow(dead_code)]
#[path = "../build.rs"]
mod build_info_build_script;

use serde_json::Value;
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::process::{Command, Output};
use std::time::{SystemTime, UNIX_EPOCH};

fn daemon() -> Command {
    Command::new(env!("CARGO_BIN_EXE_rigsignal-ebpf"))
}

fn expected_git_head() -> String {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let toplevel = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(manifest_dir)
        .output()
        .expect("run git rev-parse top-level");
    if !toplevel.status.success() {
        return "unknown".to_owned();
    }
    let repo_root = String::from_utf8(toplevel.stdout).expect("git top-level is UTF-8");
    if !build_info_build_script::is_rigsignal_repository(manifest_dir, Path::new(repo_root.trim()))
    {
        return "unknown".to_owned();
    }

    let output = Command::new("git")
        .args(["rev-parse", "--verify", "HEAD^{commit}"])
        .current_dir(manifest_dir)
        .output()
        .expect("run git rev-parse HEAD");
    if !output.status.success() {
        return "unknown".to_owned();
    }
    String::from_utf8(output.stdout)
        .expect("git HEAD is UTF-8")
        .trim()
        .to_owned()
}

fn assert_build_info(output: Output) {
    assert!(
        output.status.success(),
        "--build-info-json failed: {output:?}"
    );
    let stdout = String::from_utf8(output.stdout).expect("build info is UTF-8");
    assert!(
        stdout.ends_with('\n'),
        "build info ends with one newline: {stdout:?}"
    );
    let line = stdout.strip_suffix('\n').expect("checked trailing newline");
    assert!(
        !line.is_empty() && !line.contains('\n') && !line.contains('\r'),
        "build info is exactly one nonblank JSON line: {stdout:?}"
    );

    let info: Value = serde_json::from_str(line).expect("build info is valid JSON");
    let object = info.as_object().expect("build info is a JSON object");
    let keys: BTreeSet<_> = object.keys().map(String::as_str).collect();
    assert_eq!(keys, BTreeSet::from(["commit", "name", "version"]));
    assert_eq!(info["name"], "rigsignal-ebpf");
    assert_eq!(info["version"], env!("CARGO_PKG_VERSION"));
    let commit = info["commit"].as_str().expect("commit is a string");
    assert_eq!(
        commit,
        expected_git_head(),
        "commit is the resolved local HEAD or unknown"
    );
}

#[test]
fn resolver_rejects_foreign_repositories_and_stale_ci_sha() {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock after epoch")
        .as_nanos();
    let foreign_repo = std::env::temp_dir().join(format!("rigsignal-g4-foreign-{unique}"));
    let vendored_source = foreign_repo.join("vendor/rigsignal");
    fs::create_dir_all(vendored_source.join("src")).expect("create vendored source tree");
    fs::write(
        vendored_source.join("Cargo.toml"),
        "[workspace]\nmembers = [\"src\"]\n",
    )
    .expect("write vendored workspace manifest");
    fs::write(
        vendored_source.join("src/Cargo.toml"),
        "[package]\nname = \"rigsignal\"\n",
    )
    .expect("write vendored package manifest");

    assert!(
        !build_info_build_script::is_rigsignal_repository(
            &vendored_source.join("src"),
            &foreign_repo,
        ),
        "a source tree nested in an unrelated repository is not owned by it"
    );
    assert_eq!(
        build_info_build_script::resolve_commit(None, None).expect("unknown is valid"),
        "unknown"
    );

    let head = "a".repeat(40);
    let stale = "b".repeat(40);
    let error = build_info_build_script::resolve_commit(Some(stale), Some(head))
        .expect_err("stale GITHUB_SHA must fail when RigSignal HEAD is available");
    assert!(
        error.contains("does not match git HEAD"),
        "unexpected error: {error}"
    );

    fs::remove_dir_all(&foreign_repo).expect("remove temporary foreign repository");
}

#[test]
fn build_info_is_machine_readable_without_loading_probes() {
    assert_build_info(
        daemon()
            .arg("--build-info-json")
            .output()
            .expect("run rigsignal-ebpf --build-info-json"),
    );

    let output = daemon()
        .arg("--version")
        .output()
        .expect("run rigsignal-ebpf --version");
    assert!(output.status.success(), "--version failed: {output:?}");
    assert!(String::from_utf8_lossy(&output.stdout).contains(env!("CARGO_PKG_VERSION")));
}
