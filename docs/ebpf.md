# eBPF Deep Telemetry Guide

GamePulse's eBPF probes trace kernel-level behaviour to answer the question that FPS counters can't: *why* did performance drop?

## Requirements

- Linux kernel 5.8 or newer
- BTF (BPF Type Format) enabled — check with `ls /sys/kernel/btf/vmlinux`
- CAP_BPF + CAP_PERFMON capabilities, or root

Most modern gaming distros (SteamOS 3.5+, Fedora 38+, Ubuntu 22.04+, Arch) meet these requirements out of the box.

## Enabling eBPF

### Option 1: System service (recommended)

The system service runs with the required capabilities:

```bash
sudo systemctl enable --now gamepulse-agent
```

Set `ebpf = true` in `/etc/gamepulse/gamepulse.toml`.

### Option 2: Capabilities on the binary

If you prefer not to run as root:

```bash
sudo setcap 'cap_bpf,cap_perfmon,cap_sys_admin,cap_dac_read_search+ep' /usr/local/bin/gamepulse-agent
```

Then set `ebpf = true` in your config and run the user service.

### Option 3: Run manually as root

```bash
sudo gamepulse-agent --config ~/.config/gamepulse/gamepulse.toml --debug
```

## The probes

### biolatency — Block I/O latency

Attaches to `block_rq_issue` and `block_rq_complete` tracepoints. Measures the time from when a disk I/O request is submitted to the device driver until it completes.

**What it reveals:** asset loading stalls, shader cache reads hitting slow storage, texture streaming bottlenecks. High p99 bio latency correlating with frame drops means the game is I/O bound.

### schedlatency — Scheduler run-queue latency

Attaches to `sched_wakeup` and `sched_switch` tracepoints. Measures how long a game thread waits in the CPU run queue before being scheduled.

**What it reveals:** CPU contention from background processes, poor thread affinity, kernel preemption issues. If a game thread is runnable but waits >5ms to be scheduled, that's a visible stutter.

### vfslatency — VFS read/write latency

Kprobes on `vfs_read` and `vfs_write`, filtered to the game process. Measures individual file operation latency.

**What it reveals:** which file operations are slow — shader compilation reading from cache, asset loading, save file writes. Correlate high VFS latency with specific frame drops to identify the exact bottleneck.

### syscount — Syscall profiling

Traces `sys_enter` and `sys_exit` for the game process. Counts and times each syscall type.

**What it reveals:** the game's kernel interaction profile. Heavy `futex` usage indicates threading contention. Heavy `ioctl` indicates GPU command submission. Heavy `read`/`pread64` indicates asset loading.

### pagefault — Page fault latency

Kprobe on `handle_mm_fault`. Measures page fault resolution time.

**What it reveals:** major page faults (requiring disk I/O) cause significant stutter. High page fault latency indicates memory pressure, VRAM overcommit (textures being evicted from VRAM and faulting back in), or large memory-mapped files being accessed.

### futex — Futex contention

Traces the `futex` syscall with `FUTEX_WAIT` operations. Counts waits and measures total contention time per futex address.

**What it reveals:** thread synchronisation bottlenecks in the game engine. If many threads are contending on the same futex, the engine's threading model has a hot lock. This is actionable data for engine developers.

### tcpretransmit — TCP retransmit tracking

Kprobe on `tcp_retransmit_skb`. Counts retransmit events and tracks destination addresses.

**What it reveals:** packet loss on the network path to game servers. High retransmit rates cause multiplayer lag. Grouping by destination identifies whether the problem is your network, the game server, or a specific route.

### gpu_submit — GPU command submission latency (AMD)

Kprobe on `amdgpu_cs_ioctl`. Measures the time the kernel driver spends validating and submitting GPU command buffers.

**What it reveals:** driver-side overhead. If submission latency is high, the driver is spending significant time on memory pinning, command validation, or buffer management. This is actionable data for Mesa/AMDGPU driver developers.

### gpu_fence — GPU fence wait time

Kprobe on `dma_fence_default_wait`. Measures how long the CPU blocks waiting for the GPU to complete work.

**What it reveals:** this is the single most important eBPF metric for gaming performance. A fence wait > 16.6ms at 60fps means a frame was dropped because the GPU couldn't finish in time. Causes include GPU being overloaded, shader compilation on the GPU, VRAM pressure causing eviction, or V-Sync stalls.

## Stutter correlation

GamePulse automatically correlates eBPF data to detect stutter causes:

- **Scheduling stutter**: p99 scheduler latency > 5ms
- **I/O stutter**: p99 bio latency > 10ms
- **GPU stutter**: p99 fence wait > 16ms

When these thresholds are crossed, a `StutterEvent` is emitted with the cause classification, making it easy to filter and analyse in Kibana.

## Building the BPF programs

The eBPF kernel programs are in `gamepulse-ebpf/`. To build them from source:

```bash
# Install nightly Rust and BPF linker
rustup install nightly
cargo install bpf-linker

# Build BPF programs
cd gamepulse-ebpf
cargo +nightly build --target bpfel-unknown-none -Z build-std=core --release
```

The compiled BPF bytecode is then embedded into the agent binary at compile time.

## Troubleshooting

**"eBPF not available"** — check:
- Kernel version: `uname -r` (need 5.8+)
- BTF: `ls /sys/kernel/btf/vmlinux`
- Capabilities: run as root or set capabilities with `setcap`

**Probe failed to attach** — the kernel symbol might not exist:
- `amdgpu_cs_ioctl` requires the amdgpu driver to be loaded
- `dma_fence_default_wait` requires DRM subsystem
- Check available symbols: `grep function_name /proc/kallsyms`

**No data from probes** — the probes are attached but no events are hitting:
- Ensure a game is running (probes filter by game PID)
- Check that `game_detection = true` in config
- Run with `RUST_LOG=gamepulse=debug` for verbose output
