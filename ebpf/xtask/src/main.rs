/// xtask — build helpers for the rigsignal-ebpf workspace.
///
/// Commands:
///   cargo xtask build-ebpf          — compile BPF programs (debug)
///   cargo xtask build-ebpf --release — compile BPF programs (release/optimised)
///
/// Prerequisites:
///   (toolchain + rust-src come from ebpf/rust-toolchain.toml automatically)
///   cargo install bpf-linker
use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Parser)]
#[command(name = "xtask")]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Compile BPF probes (rigsignal-ebpf-probes) for bpfel-unknown-none
    BuildEbpf {
        #[arg(long)]
        release: bool,
    },
    /// Build the userspace daemon (rigsignal-ebpf) for the host target
    Build {
        #[arg(long)]
        release: bool,
    },
    /// Build both BPF probes and daemon
    BuildAll {
        #[arg(long)]
        release: bool,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let workspace = workspace_root()?;

    match cli.command {
        Cmd::BuildEbpf { release } => build_ebpf(&workspace, release),
        Cmd::Build { release } => build_daemon(&workspace, release),
        Cmd::BuildAll { release } => {
            build_ebpf(&workspace, release)?;
            build_daemon(&workspace, release)
        }
    }
}

fn build_ebpf(workspace: &Path, release: bool) -> Result<()> {
    println!("Building BPF probes (target: bpfel-unknown-none)...");

    check_bpf_linker()?;

    // Linker flags and linker selection are in .cargo/config.toml:
    //   [target.bpfel-unknown-none]
    //   linker = "bpf-linker"
    //   rustflags = ["-C", "link-arg=--target=bpf", ...]
    // No explicit +toolchain: ebpf/rust-toolchain.toml pins the nightly (with
    // rust-src) for every cargo invocation in this workspace — a hardcoded
    // "+nightly" here would bypass that pin (floating nightly lacks rust-src on CI).
    let mut args = vec![
        "build",
        "-p", "rigsignal-ebpf-probes",
        "--target", "bpfel-unknown-none",
        "-Z", "build-std=core",
    ];

    if release {
        args.push("--release");
    }

    let status = Command::new("cargo")
        .current_dir(workspace)
        .args(&args)
        .status()
        .context("running cargo build for BPF probes")?;

    if !status.success() {
        bail!("BPF probe build failed");
    }

    let profile = if release { "release" } else { "debug" };
    let out = workspace
        .join("target/bpfel-unknown-none")
        .join(profile)
        .join("rigsignal-ebpf-probes");

    println!("BPF probes built: {}", out.display());
    Ok(())
}

fn build_daemon(workspace: &Path, release: bool) -> Result<()> {
    println!("Building rigsignal-ebpf daemon...");

    let mut args = vec![
        "build".to_string(),
        "-p".to_string(),
        "rigsignal-ebpf".to_string(),
    ];

    if release {
        args.push("--release".to_string());
    }

    let status = Command::new("cargo")
        .current_dir(workspace)
        .args(&args)
        .status()
        .context("running cargo build for daemon")?;

    if !status.success() {
        bail!("daemon build failed");
    }

    let profile = if release { "release" } else { "debug" };
    let out = workspace
        .join("target")
        .join(profile)
        .join("rigsignal-ebpf");

    println!("daemon built: {}", out.display());
    Ok(())
}

fn check_bpf_linker() -> Result<()> {
    let output = Command::new("bpf-linker").arg("--version").output();
    if output.is_err() || !output.unwrap().status.success() {
        bail!(
            "bpf-linker not found. Install it with:\n  \
             cargo install bpf-linker\n\n  \
             See: https://github.com/aya-rs/bpf-linker"
        );
    }
    Ok(())
}

fn workspace_root() -> Result<PathBuf> {
    // xtask binary lives at <workspace>/target/debug/xtask
    // Walk up to find the Cargo.toml workspace root.
    let exe = std::env::current_exe().context("getting executable path")?;
    exe.ancestors()
        .find(|p| p.join("Cargo.toml").exists() && p.join("rigsignal-ebpf").exists())
        .map(|p| p.to_path_buf())
        .context("could not locate workspace root (expected rigsignal-ebpf/ directory)")
}
