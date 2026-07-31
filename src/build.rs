use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn git_output(manifest_dir: &Path, args: &[&str]) -> Option<String> {
    let output = Command::new("git")
        .args(args)
        .current_dir(manifest_dir)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let value = String::from_utf8(output.stdout).ok()?;
    let value = value.trim();
    (!value.is_empty()).then(|| value.to_owned())
}

/// Return whether `repo_root` is the top-level directory of this RigSignal
/// source tree, rather than an unrelated repository containing a vendored copy.
pub fn is_rigsignal_repository(manifest_dir: &Path, repo_root: &Path) -> bool {
    let Ok(manifest_dir) = manifest_dir.canonicalize() else {
        return false;
    };
    let Ok(repo_root) = repo_root.canonicalize() else {
        return false;
    };

    // A Git worktree found by walking upwards must contain this crate. This
    // deliberately rejects a Git directory reached through a symlink or an
    // otherwise unrelated path.
    if !manifest_dir.starts_with(&repo_root) {
        return false;
    }

    // Require a RigSignal-specific sentinel at the Git top-level too. Merely
    // being below some repository is not evidence that its HEAD identifies us.
    let Ok(workspace_manifest) = fs::read_to_string(repo_root.join("Cargo.toml")) else {
        return false;
    };
    if !workspace_manifest.contains("[workspace]") {
        return false;
    }

    let agent_manifest = fs::read_to_string(repo_root.join("src/Cargo.toml"));
    let ebpf_manifest = fs::read_to_string(repo_root.join("ebpf/rigsignal-ebpf/Cargo.toml"));
    agent_manifest
        .as_deref()
        .is_ok_and(|manifest| manifest.contains("name = \"rigsignal\""))
        && ebpf_manifest
            .as_deref()
            .is_ok_and(|manifest| manifest.contains("name = \"rigsignal-ebpf\""))
}

fn rigsignal_git_root(manifest_dir: &Path) -> Option<PathBuf> {
    let repo_root = absolute_path(
        manifest_dir,
        git_output(manifest_dir, &["rev-parse", "--show-toplevel"])?,
    );
    is_rigsignal_repository(manifest_dir, &repo_root).then_some(repo_root)
}

fn absolute_path(manifest_dir: &Path, path: String) -> PathBuf {
    let path = PathBuf::from(path);
    if path.is_absolute() {
        path
    } else {
        manifest_dir.join(path)
    }
}

fn emit_rerun_paths(manifest_dir: &Path) {
    let Some(head) = git_output(manifest_dir, &["rev-parse", "--git-path", "HEAD"]) else {
        return;
    };
    let head = absolute_path(manifest_dir, head);
    println!("cargo:rerun-if-changed={}", head.display());

    let Some(common_dir) = git_output(manifest_dir, &["rev-parse", "--git-common-dir"]) else {
        return;
    };
    let common_dir = absolute_path(manifest_dir, common_dir);
    println!(
        "cargo:rerun-if-changed={}",
        common_dir.join("packed-refs").display()
    );

    let Ok(head_contents) = fs::read_to_string(&head) else {
        return;
    };
    let Some(reference) = head_contents.trim().strip_prefix("ref: ") else {
        return;
    };

    // Branch refs live in the common Git directory for linked worktrees.
    println!(
        "cargo:rerun-if-changed={}",
        common_dir.join(reference).display()
    );
}

fn github_sha() -> Option<String> {
    env::var("GITHUB_SHA")
        .ok()
        .map(|sha| sha.trim().to_ascii_lowercase())
        .filter(|sha| !sha.is_empty())
}

/// Apply the provenance precedence rules after the local Git SHA has already
/// been filtered to a RigSignal-owned repository.
pub fn resolve_commit(
    github_sha: Option<String>,
    rigsignal_git_sha: Option<String>,
) -> Result<String, String> {
    if let Some(sha) = &github_sha {
        if sha.len() != 40 || !sha.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(format!(
                "GITHUB_SHA must be a 40-character hexadecimal commit SHA, got {sha:?}"
            ));
        }
    }
    if let Some(sha) = &rigsignal_git_sha {
        if sha.len() != 40 || !sha.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(format!(
                "git rev-parse HEAD must be a 40-character hexadecimal commit SHA, got {sha:?}"
            ));
        }
    }

    match (github_sha, rigsignal_git_sha) {
        (Some(github_sha), Some(git_sha)) if github_sha != git_sha => Err(format!(
            "GITHUB_SHA ({github_sha}) does not match git HEAD ({git_sha}); refusing to stamp stale build provenance"
        )),
        (Some(github_sha), Some(_)) | (Some(github_sha), None) => Ok(github_sha),
        (None, Some(git_sha)) => Ok(git_sha),
        (None, None) => Ok("unknown".to_owned()),
    }
}

fn main() {
    println!("cargo:rerun-if-env-changed=GITHUB_SHA");

    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("manifest directory"));
    let rigsignal_git_root = rigsignal_git_root(&manifest_dir);
    if rigsignal_git_root.is_some() {
        emit_rerun_paths(&manifest_dir);
    }

    let github_sha = github_sha();
    let git_sha = rigsignal_git_root
        .as_ref()
        .and_then(|_| git_output(&manifest_dir, &["rev-parse", "--verify", "HEAD^{commit}"]));
    let commit = resolve_commit(github_sha, git_sha).unwrap_or_else(|error| panic!("{error}"));

    println!("cargo:rustc-env=RIGSIGNAL_BUILD_COMMIT={commit}");
}
