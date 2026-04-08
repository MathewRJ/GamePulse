/// xtask — build helpers for the gamepulse-ebpf workspace.
///
/// Commands:
///   cargo xtask build-ebpf          — compile BPF programs (debug)
///   cargo xtask build-ebpf --release — compile BPF programs (release/optimised)
///
/// Prerequisites:
///   rustup toolchain add nightly
///   rustup component add rust-src --toolchain nightly
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
    /// Compile BPF probes (gamepulse-ebpf-probes) for bpfel-unknown-none
    BuildEbpf {
        #[arg(long)]
        release: bool,
    },
    /// Build the userspace daemon (gamepulse-ebpf-daemon) for the host target
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

    let mut args = vec![
        "+nightly".to_string(),
        "build".to_string(),
        "-p".to_string(),
        "gamepulse-ebpf-probes".to_string(),
        "--target".to_string(),
        "bpfel-unknown-none".to_string(),
        "-Z".to_string(),
        "build-std=core".to_string(),
    ];

    if release {
        args.push("--release".to_string());
    }

    let status = Command::new("cargo")
        .current_dir(workspace)
        .args(&args)
        .env("CARGO_ENCODED_RUSTFLAGS", rustflags_for_bpf())
        .status()
        .context("running cargo build for BPF probes")?;

    if !status.success() {
        bail!("BPF probe build failed");
    }

    let profile = if release { "release" } else { "debug" };
    let out = workspace
        .join("target/bpfel-unknown-none")
        .join(profile)
        .join("gamepulse-ebpf-probes");

    println!("BPF probes built: {}", out.display());
    Ok(())
}

fn build_daemon(workspace: &Path, release: bool) -> Result<()> {
    println!("Building gamepulse-ebpf daemon...");

    let mut args = vec![
        "build".to_string(),
        "-p".to_string(),
        "gamepulse-ebpf-daemon".to_string(),
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
        .join("gamepulse-ebpf");

    println!("daemon built: {}", out.display());
    Ok(())
}

fn rustflags_for_bpf() -> String {
    // Flags passed to rustc when compiling BPF programs:
    //   --target=bpf     — tell LLVM to emit BPF bytecode
    //   -O2              — optimise (verifier prefers fewer instructions)
    //   --btf-vmlinux    — include BTF from running kernel for CO-RE
    let btf_path = "/sys/kernel/btf/vmlinux";
    format!(
        "-C link-arg=--target=bpf\x1f\
         -C link-arg=-O2\x1f\
         -C link-arg=--btf-vmlinux={btf_path}"
    )
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
        .find(|p| p.join("Cargo.toml").exists() && p.join("gamepulse-ebpf-daemon").exists())
        .map(|p| p.to_path_buf())
        .context("could not locate workspace root (expected gamepulse-ebpf-daemon/ directory)")
}
