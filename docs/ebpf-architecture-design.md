# GamePulse Phase 2: eBPF Daemon Architecture Design

**Author:** Mat (with architectural analysis by Claude Opus)
**Date:** April 2026
**Status:** Design — ready for Claude Code implementation
**Target file:** `docs/ebpf-architecture.md`

---

## 1. Executive Summary

Phase 2 adds kernel-level telemetry to GamePulse via a standalone Rust daemon built on the Aya eBPF framework. This daemon answers the question existing tools cannot: *why* did performance degrade? The Python collector (Phase 1) tells you FPS dropped; the eBPF daemon tells you the kernel scheduler migrated the render thread across CCX boundaries, or that a futex contention in the audio subsystem blocked the main loop for 4ms.

The daemon is a separate binary from the Python collector, communicating indirectly through Elasticsearch (shared session ID, shared timestamps). It ships structured metrics to `metrics-gamepulse.ebpf-default` using the same ES bulk API and credential infrastructure the Python collector already uses. No IPC, no shared memory, no coupling — just two processes writing to the same data stream with a common session correlation key.

This design prioritises three things: **correctness** (eBPF programs must be verifier-safe and never crash the kernel), **minimal overhead** (the daemon must not measurably affect gaming performance), and **graceful degradation** (every probe is independent; missing tracepoints or insufficient capabilities are handled without affecting other probes).

---

## 2. Why a Separate Binary (Not Integrated into the Python Collector)

The decision to build a separate daemon rather than embedding eBPF into the Python collector is deliberate, not accidental:

**Capability isolation.** The eBPF daemon requires `CAP_BPF` + `CAP_PERFMON` (or root). The Python collector runs unprivileged. Combining them would force the entire collector to run with elevated privileges, violating least-privilege. A user who wants surface metrics without kernel telemetry should never need to grant BPF capabilities.

**Language boundary.** eBPF programs must be compiled to BPF bytecode. Aya compiles Rust to BPF bytecode at build time and embeds it in the userspace binary. There's no clean way to do this from Python without shelling out to a compiled binary anyway — at which point you have a separate binary with extra plumbing.

**Lifecycle independence.** The Python collector starts and stops with game sessions. The eBPF daemon can optionally run continuously (for background baseline measurement) or be session-scoped. Decoupling lets each process manage its own lifecycle.

**Phase 4 convergence.** When the Rust production agent (Phase 4) replaces the Python collector, the eBPF daemon merges into it. The separate-binary design is a stepping stone, not a permanent architecture. The probe implementations, aggregation logic, and ES shipping code will all be reused.

**Correlation mechanism.** Both processes read the same `session.id` from the GamePulse session file (`$XDG_RUNTIME_DIR/gamepulse/session.json`, falling back to `/tmp/gamepulse/session.json` if `XDG_RUNTIME_DIR` is unset). The Python collector writes this file when it detects a game; the eBPF daemon reads it. Timestamp alignment is implicit — both use `@timestamp` in UTC, and Kibana correlates them by time range and session ID.

---

## 3. Repository Structure

```
gamepulse-ebpf/
├── Cargo.toml                      # Workspace root
├── gamepulse-ebpf-probes/          # eBPF kernel-space programs
│   ├── Cargo.toml                  # target = bpfel-unknown-none
│   └── src/
│       ├── sched.rs                # Scheduler observer
│       ├── bio.rs                  # Block I/O tracer
│       ├── vfs.rs                  # VFS read/write tracer
│       ├── gpu_fence.rs            # DMA fence wait tracer
│       ├── gpu_submit.rs           # GPU command submission tracer (AMD)
│       ├── gpu_sched.rs            # DRM scheduler tracer (vendor-neutral)
│       ├── mem.rs                  # Page fault / memory tracker
│       ├── futex.rs                # Futex contention tracer
│       ├── irq.rs                  # IRQ/softirq latency tracer
│       ├── syscall.rs              # Syscall profiler
│       ├── shader.rs               # Shader compilation tracer (uprobe)
│       └── common.rs               # Shared types, constants, histogram helpers
├── gamepulse-ebpf-daemon/          # Userspace daemon
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs                 # Entry point, CLI, signal handling
│       ├── config.rs               # TOML config parsing, probe enable/disable
│       ├── loader.rs               # Probe loading, capability checks, feature gating
│       ├── aggregator.rs           # 1/s ring buffer drain + histogram aggregation
│       ├── shipper.rs              # ES bulk API client (reuse collector's credential path)
│       ├── session.rs              # Session file reader, game PID tracking
│       ├── probes/
│       │   ├── mod.rs              # Probe trait, probe registry
│       │   ├── sched.rs            # Scheduler probe userspace handler
│       │   ├── bio.rs              # Block I/O probe userspace handler
│       │   ├── vfs.rs              # VFS probe userspace handler
│       │   ├── gpu.rs              # GPU fence + submit + DRM scheduler handlers
│       │   ├── mem.rs              # Memory probe userspace handler
│       │   ├── futex.rs            # Futex probe userspace handler
│       │   ├── irq.rs              # IRQ probe userspace handler
│       │   ├── syscall.rs          # Syscall probe userspace handler
│       │   └── shader.rs           # Shader compilation probe handler
│       └── es_model.rs             # Elasticsearch document structs
└── xtask/
    └── src/main.rs                 # Build helper (BPF compile + embed)
```

### Why a Cargo workspace with two crates

The eBPF kernel programs compile to `bpfel-unknown-none` (no standard library, no heap, 512-byte stack). The userspace daemon compiles to the host target with full `std`. These are fundamentally different compilation targets — they cannot share a `Cargo.toml`. The workspace pattern with `xtask` for build orchestration is the standard Aya project structure.

---

## 4. Probe Architecture

### 4.1 The Probe Trait

Every probe implements a common trait in userspace:

```rust
pub trait Probe: Send + 'static {
    /// Human-readable probe name (e.g., "schedlatency")
    fn name(&self) -> &'static str;

    /// Required kernel features (tracepoints, kprobe symbols)
    fn requirements(&self) -> ProbeRequirements;

    /// Attempt to load and attach. Returns Err if requirements unmet.
    fn attach(&mut self, ebpf: &mut Ebpf, game_pid: Option<u32>) -> Result<()>;

    /// Drain ring buffer / perf buffer, aggregate into 1/s snapshot.
    fn collect(&mut self) -> Result<Vec<EbpfMetricDoc>>;

    /// Detach and clean up.
    fn detach(&mut self) -> Result<()>;
}

pub struct ProbeRequirements {
    /// Tracepoints that must exist (checked via /sys/kernel/tracing/available_events)
    pub tracepoints: Vec<&'static str>,
    /// Kernel symbols that must exist for kprobes (checked via /proc/kallsyms)
    pub kprobe_symbols: Vec<&'static str>,
    /// Kernel modules that must be loaded (checked via /proc/modules)
    pub kernel_modules: Vec<&'static str>,
    /// Minimum kernel version (major, minor)
    pub min_kernel: (u32, u32),
}
```

### 4.2 Probe Loading Strategy

At startup, the daemon:

1. **Checks capabilities.** If missing `CAP_BPF` or `CAP_PERFMON`, logs a clear error and exits. No silent fallback — the user asked for eBPF and should know it failed.

2. **Checks BTF availability.** Verifies `/sys/kernel/btf/vmlinux` exists. Without BTF, CO-RE relocation fails and no probes can load. Minimum kernel: 5.8.

3. **Iterates probes in priority order.** For each probe:
   - Checks `ProbeRequirements` against the running kernel.
   - Attempts `attach()`.
   - On success: adds to active probe list.
   - On failure: logs the reason (missing tracepoint, symbol not in kallsyms, verifier rejection) and continues to the next probe. The failing probe does not prevent other probes from loading.

4. **Reports probe status.** Logs a summary: "Loaded 7/9 probes. Skipped: shader (Mesa uprobe target not found), proton (ntdll translation symbols not available)."

This is the correct behaviour because:
- GPU probes require specific drivers to be loaded (e.g., `amdgpu_cs_ioctl` only exists when the amdgpu module is loaded).
- Shader probes require Mesa to be installed and the uprobe target binary to be at a known path.
- Proton probes require Wine/Proton to be running.
- A user on NVIDIA won't have amdgpu tracepoints, and that's fine.

### 4.3 Probe Priority and Implementation Phases

Implementation is phased within Phase 2. Not all probes ship in the first version.

**Phase 2a — Core probes (ship first):**

| Probe | Attach Points | Why First |
|-------|--------------|-----------|
| `schedlatency` | `sched/sched_wakeup`, `sched/sched_switch`, `sched/sched_migrate_task` | Directly enables the RT scheduling investigation question. Universal (no driver dependency). |
| `bio` | `block/block_rq_issue`, `block/block_rq_complete` | Universal. Immediately explains storage stutters (SD card vs NVMe). |
| `gpu_sched` | `gpu_scheduler/drm_run_job`, `gpu_scheduler/drm_sched_process_job`, `gpu_scheduler/drm_sched_job_wait_dep` | Vendor-neutral DRM scheduler tracepoints (stable uAPI). Works on AMD, Intel, Nouveau. |
| `mem` | `kmem/mm_page_alloc`, `kmem/mm_page_free`, `exceptions/page_fault_user` | Universal. Explains memory-pressure stutters. |

**Phase 2b — Extended probes:**

| Probe | Attach Points | Dependency |
|-------|--------------|------------|
| `gpu_fence` | kprobe on `dma_fence_default_wait` | DRM subsystem (always present with a GPU) |
| `gpu_submit` | kprobe on `amdgpu_cs_ioctl` | amdgpu kernel module loaded |
| `futex` | `syscalls/sys_enter_futex` | Universal |
| `irq` | `irq/irq_handler_entry`, `irq/irq_handler_exit`, `irq/softirq_entry`, `irq/softirq_exit` | Universal |
| `vfs` | kprobe on `vfs_read`, `vfs_write` | Universal |

**Phase 2c — Advanced probes (stretch):**

| Probe | Attach Points | Dependency |
|-------|--------------|------------|
| `syscall` | `raw_syscalls/sys_enter`, `raw_syscalls/sys_exit` | Universal but high-volume |
| `shader` | uprobe on Mesa's `nir_shader_compiler_init` or equivalent | Mesa installed at known path |
| `proton` | kprobe on ntdll translation entry points | Wine/Proton running |

---

## 5. Kernel-Space Design Decisions

### 5.1 BPF Map Strategy

The kernel-space programs need to communicate data to userspace. The choice of BPF map type has significant performance and correctness implications.

**BPF Ring Buffer (`BPF_MAP_TYPE_RINGBUF`) — primary choice.**

We use ring buffers for all event-driven probes (individual events that need to reach userspace with full context). Ring buffers have several advantages over the older perf buffer:
- Single shared buffer across all CPUs (no per-CPU allocation waste).
- Lock-free MPSC (multiple producer, single consumer) design.
- `bpf_ringbuf_reserve()` + `bpf_ringbuf_submit()` is zero-copy from the BPF program's perspective.
- Automatic back-pressure: if the consumer falls behind, `reserve()` fails and the BPF program can increment a drop counter rather than corrupting data.

**BPF HashMap (`BPF_MAP_TYPE_HASH`) — for in-kernel aggregation.**

For high-frequency events where shipping every event would be prohibitively expensive (e.g., syscall profiling at 50k events/second), we aggregate in-kernel. The BPF program increments counters in a HashMap keyed by (e.g.) syscall number. Userspace reads and resets the map once per second.

**Specific map assignments per probe:**

| Probe | Map Type | Key | Value | Rationale |
|-------|----------|-----|-------|-----------|
| `schedlatency` | RingBuf | — | `SchedEvent { pid, tid, prev_cpu, next_cpu, wait_ns, comm }` | Individual scheduling events are low-volume enough (hundreds/sec for the game process) to ship individually. Userspace computes p50/p95/p99. |
| `bio` | RingBuf | — | `BioEvent { dev, sector, bytes, latency_ns, rwflag }` | Individual I/O events. Low volume during gaming (tens to hundreds/sec). |
| `gpu_sched` | RingBuf | — | `GpuSchedEvent { ring, fence_ptr, submit_ts, complete_ts }` | DRM scheduler events. Low volume (frame rate ≈ number of GPU jobs). |
| `gpu_fence` | RingBuf | — | `FenceWaitEvent { fence_ptr, wait_ns, caller }` | Fence waits directly cause frame drops — every one matters. |
| `gpu_submit` | RingBuf | — | `GpuSubmitEvent { latency_ns, ring_idx }` | AMD CS ioctl events. One per GPU submission. |
| `mem` | HashMap | `fault_type: u8` | `count: u64, total_ns: u64` | Page faults can be very frequent. Aggregate by type (minor/major) in-kernel. |
| `futex` | HashMap | `futex_addr: u64` | `wait_count: u64, total_wait_ns: u64` | Futex operations can be extremely frequent. Aggregate by address. |
| `irq` | HashMap | `irq_num: u32` | `count: u64, total_ns: u64` | IRQs are high-frequency. Aggregate by IRQ number. |
| `syscall` | HashMap | `syscall_nr: u32` | `count: u64, total_ns: u64` | Syscalls are very high-frequency. Aggregate by syscall number. |
| `vfs` | RingBuf | — | `VfsEvent { filename_hash, bytes, latency_ns, rw }` | File-level I/O. Moderate volume. |
| `shader` | RingBuf | — | `ShaderEvent { duration_ns, pipeline_hash }` | Shader compiles are rare events (tens per session) but each one matters. |

### 5.2 Ring Buffer Sizing

Ring buffer size must be a power of 2. The sizing trade-off:

- **Too small:** Events dropped under burst conditions (e.g., initial shader compilation storm). BPF program's `reserve()` fails.
- **Too large:** Wasted locked memory (ring buffers are memory-locked, counting against `RLIMIT_MEMLOCK`).

**Default sizes:**

| Probe Category | Ring Buffer Size | Rationale |
|---------------|-----------------|-----------|
| Scheduler | 256 KB | ~4000 events before drain. At 1/s drain cadence and ~200 sched events/sec for a game, this is ~20x headroom. |
| Block I/O | 64 KB | I/O events are less frequent during gaming (assets are cached after initial load). |
| GPU (fence + submit + sched) | 128 KB | GPU submissions are at frame rate (~60–240/sec). Generous for burst conditions. |
| VFS | 128 KB | File I/O is bursty (shader cache reads, save files). |
| Shader | 16 KB | Very rare events. Minimal allocation. |

Total locked memory: ~592 KB. Well under the typical default `RLIMIT_MEMLOCK` of 64 MB (and CachyOS with kernel 6.19 uses unlimited by default via `memcg`-based accounting).

### 5.3 PID Filtering

Every probe filters events to the game process (and its child threads). This is critical for two reasons:
1. **Performance:** Without PID filtering, the scheduler probe alone would fire for every context switch on the system (~tens of thousands per second). With filtering, it fires only for the game's threads.
2. **Relevance:** We want gaming telemetry, not system-wide profiling.

The game PID is communicated to BPF programs via a `BPF_MAP_TYPE_ARRAY` with a single element (`game_pid_map`). When the daemon detects a game session (by reading `$XDG_RUNTIME_DIR/gamepulse/session.json`), it writes the game PID to this map. BPF programs check `bpf_get_current_pid_tgid() >> 32` against this value.

**Special case: Proton/Wine games.** Under Proton, the "game" is a tree of processes: `steam` → `proton` → `wine-preloader` → `wine64-preloader` → actual game `.exe`. The daemon needs to track the entire process tree, not just one PID. Solution: use a `BPF_MAP_TYPE_HASH` (`game_pids_map`) with up to 64 PIDs. The daemon populates it by walking `/proc/<pid>/task/` and the process tree from the session file's root PID.

```rust
// In BPF program:
fn is_game_process(pids_map: &HashMap<u32, u8>) -> bool {
    let tgid = (bpf_get_current_pid_tgid() >> 32) as u32;
    unsafe { pids_map.get(&tgid).is_some() }
}
```

The daemon refreshes `game_pids_map` every 5 seconds (game processes can spawn new threads/children) by re-walking the process tree.

### 5.4 Handling eBPF Verifier Constraints

The BPF verifier enforces strict safety rules. Several of these directly affect our probe design:

**512-byte stack limit.** BPF programs cannot allocate large local variables. For events with variable-length data (e.g., filenames in VFS probes), we use `bpf_ringbuf_reserve()` to allocate directly in the ring buffer rather than building the event on the stack first.

**Bounded loops only.** Loops must have provably bounded iteration counts. This affects the syscall profiler (cannot iterate an arbitrary-length array) and the process tree walker (cannot recursively walk a tree of unknown depth). Solution: fixed-size arrays with known upper bounds.

**No floating-point.** All latency values are stored as `u64` nanoseconds. Percentile calculations happen in userspace. Histogram bucket boundaries are precomputed integer thresholds.

**Helper function availability.** `bpf_get_current_comm()`, `bpf_ktime_get_ns()`, `bpf_probe_read_kernel()` are universally available on kernel 5.8+. `bpf_get_current_cgroup_id()` is available but we don't need it. `bpf_ringbuf_reserve()` requires kernel 5.8+ (which is our minimum anyway).

---

## 6. Userspace Architecture

### 6.1 Runtime and Async Model

The daemon uses **tokio** for its async runtime. Aya has first-class tokio support via `aya::maps::AsyncPerfEventArray` and `aya::maps::ring_buf::RingBuf` (with `async` drain support). The daemon's main loop:

```
┌────────────────────────────────────────────────────────────────┐
│                    Daemon Main Loop (tokio)                     │
│                                                                │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐  │
│  │ Ring Buffer   │    │  Aggregation     │    │  ES Bulk     │  │
│  │ Drain Tasks   │───▶│  Timer (1/s)     │───▶│  Shipper     │  │
│  │ (per probe)   │    │                  │    │              │  │
│  └──────────────┘    └──────────────────┘    └──────────────┘  │
│                                                                │
│  ┌──────────────┐    ┌──────────────────┐                      │
│  │ Session File  │    │  PID Refresh     │                      │
│  │ Watcher       │    │  Timer (5s)      │                      │
│  └──────────────┘    └──────────────────┘                      │
│                                                                │
│  ┌──────────────┐                                              │
│  │ Signal        │  SIGTERM/SIGINT → graceful shutdown          │
│  │ Handler       │  SIGUSR1 → dump probe status to log         │
│  └──────────────┘                                              │
└────────────────────────────────────────────────────────────────┘
```

**Ring buffer drain tasks** run continuously, pulling events as they arrive (non-blocking, epoll-based via Aya's `RingBuf::next()`). Events are pushed into per-probe in-memory buffers.

**Aggregation timer** fires every 1 second. For each probe:
- Calls `probe.collect()`, which drains the in-memory event buffer and computes the 1-second aggregation snapshot.
- Snapshot includes: counts, histograms (log2 buckets), min/max/sum for latency distributions, per-thread breakdowns for scheduler events.
- Returns a `Vec<EbpfMetricDoc>` — one or more ES documents ready for shipping.

**ES bulk shipper** batches documents from all probes and ships them to `metrics-gamepulse.ebpf-default` via the ES bulk API. Uses the same API key and endpoint as the Python collector (reads from `~/.config/gamepulse/gamepulse.toml`).

### 6.2 Aggregation Design

Raw eBPF events are not shipped individually to Elasticsearch. Instead, they're aggregated into 1-second snapshots. This is critical for both ES storage efficiency and overhead minimisation.

**Histogram representation.** Latency distributions use log2 buckets, producing a compact histogram that captures the full distribution shape:

```rust
/// Log2 histogram with 16 buckets covering 1μs to 33ms
/// Bucket boundaries: [0, 1μs), [1μs, 2μs), [2μs, 4μs), ..., [16ms, 33ms), [33ms, ∞)
pub struct LatencyHistogram {
    buckets: [u64; 16],
    count: u64,
    sum_ns: u64,
    min_ns: u64,
    max_ns: u64,
}
```

This maps naturally to Elasticsearch's `histogram` field type, which is supported in TSDS mode. The ingest pipeline can compute p50/p95/p99 from the bucket distribution, or it can be done in Kibana at query time using the `percentile_from_histogram` function.

**Scheduler aggregation.** The scheduler probe produces per-second snapshots with:
- Runqueue latency histogram (how long game threads waited to be scheduled)
- Migration count (how many times game threads moved between CPU cores)
- CCX migration count (how many times game threads crossed CCX/CCD boundaries — this is the AMD Zen-specific metric that enables the RT scheduling investigation)
- Per-thread breakdown (render thread vs audio thread vs worker threads, identified by `comm` name heuristics)

**CCX detection (AMD Zen).** On Zen 3/4/5, the CPU topology is exposed via:
- `/sys/devices/system/cpu/cpu*/topology/core_cpus_list` — which CPUs share an L3 cache (= same CCX)
- `/sys/devices/system/cpu/cpu*/cache/index3/shared_cpu_list` — equivalent

The daemon reads this at startup and builds a `cpu_to_ccx: HashMap<u32, u32>` lookup. When a `sched_migrate_task` event shows `prev_cpu` and `next_cpu` in different CCX groups, it increments `ccx_migration_count`. On non-AMD or non-Zen CPUs, this counter stays at zero (no CCX concept).

### 6.3 Session Correlation

The eBPF daemon doesn't detect games itself. It relies on the Python collector's session management:

1. Python collector detects a game launch, creates `$XDG_RUNTIME_DIR/gamepulse/session.json` (falls back to `/tmp/gamepulse/session.json`):
   ```json
   {
     "session_id": "a1b2c3d4-...",
     "game_pid": 12345,
     "game_name": "Cyberpunk 2077",
     "steam_app_id": 1091500,
     "started": "2026-04-08T18:30:00Z"
   }
   ```

2. eBPF daemon watches this file via `inotify`. On creation/modification:
   - Reads the session ID and game PID.
   - Populates `game_pids_map` with the game's process tree.
   - Begins including `gamepulse.session.id` in all ES documents.

3. On game exit, the Python collector removes the session file. The eBPF daemon detects this, clears `game_pids_map`, and stops emitting session-correlated data (probes remain attached but PID filter rejects all events).

**Fallback when no session is active.** If configured with `background_baseline = true`, the daemon can emit system-wide (unfiltered) scheduler and I/O metrics at a reduced rate (every 10 seconds instead of every 1 second). This provides a baseline for comparison: "your idle system has this much scheduler jitter; with the game running, it increases by this much."

---

## 7. Elasticsearch Data Model

### 7.1 Document Structure

All eBPF metrics ship to a single data stream: `metrics-gamepulse.ebpf-default`. The documents are polymorphic — different probe types produce documents with different field subsets. Common fields are always present.

```json
{
  "@timestamp": "2026-04-08T18:30:01.000Z",
  "data_stream": {
    "type": "metrics",
    "dataset": "gamepulse.ebpf",
    "namespace": "default"
  },
  "gamepulse": {
    "session": {
      "id": "a1b2c3d4-..."
    },
    "ebpf": {
      "probe": "schedlatency",
      "runqueue": {
        "latency_histogram": {
          "values": [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768],
          "counts": [0, 12, 45, 89, 120, 67, 23, 8, 3, 1, 0, 0, 0, 0, 0, 0]
        },
        "latency_min_us": 1.2,
        "latency_max_us": 890.5,
        "latency_avg_us": 28.3,
        "latency_p99_us": 512.0
      },
      "migration": {
        "total_count": 14,
        "ccx_cross_count": 3
      },
      "thread_breakdown": [
        { "comm": "CyberpunkMain", "tid": 12346, "runqueue_avg_us": 22.1, "migrations": 5 },
        { "comm": "RenderThread", "tid": 12347, "runqueue_avg_us": 31.7, "migrations": 8 },
        { "comm": "AudioThread", "tid": 12348, "runqueue_avg_us": 15.4, "migrations": 1 }
      ]
    }
  },
  "host": {
    "name": "mat-gaming-pc"
  }
}
```

### 7.2 Per-Probe Document Fields

**`schedlatency`:**
```
gamepulse.ebpf.probe: "schedlatency"
gamepulse.ebpf.runqueue.latency_histogram: histogram
gamepulse.ebpf.runqueue.latency_min_us: float
gamepulse.ebpf.runqueue.latency_max_us: float
gamepulse.ebpf.runqueue.latency_avg_us: float
gamepulse.ebpf.runqueue.latency_p99_us: float
gamepulse.ebpf.migration.total_count: long
gamepulse.ebpf.migration.ccx_cross_count: long
gamepulse.ebpf.wakeup.latency_avg_us: float
gamepulse.ebpf.thread_breakdown: nested
```

**`bio`:**
```
gamepulse.ebpf.probe: "bio"
gamepulse.ebpf.bio.latency_histogram: histogram
gamepulse.ebpf.bio.latency_min_us: float
gamepulse.ebpf.bio.latency_max_us: float
gamepulse.ebpf.bio.latency_p99_us: float
gamepulse.ebpf.bio.read_count: long
gamepulse.ebpf.bio.write_count: long
gamepulse.ebpf.bio.read_bytes: long
gamepulse.ebpf.bio.write_bytes: long
gamepulse.ebpf.bio.queue_depth_max: long
```

**`gpu_sched`:**
```
gamepulse.ebpf.probe: "gpu_sched"
gamepulse.ebpf.gpu_sched.job_count: long
gamepulse.ebpf.gpu_sched.execution_histogram: histogram
gamepulse.ebpf.gpu_sched.execution_avg_us: float
gamepulse.ebpf.gpu_sched.execution_max_us: float
gamepulse.ebpf.gpu_sched.wait_dep_count: long
gamepulse.ebpf.gpu_sched.wait_dep_total_us: float
```

**`gpu_fence`:**
```
gamepulse.ebpf.probe: "gpu_fence"
gamepulse.ebpf.gpu_fence.wait_count: long
gamepulse.ebpf.gpu_fence.wait_histogram: histogram
gamepulse.ebpf.gpu_fence.wait_avg_us: float
gamepulse.ebpf.gpu_fence.wait_max_us: float
gamepulse.ebpf.gpu_fence.wait_p99_us: float
gamepulse.ebpf.gpu_fence.frame_blocking_count: long  # waits > 16.6ms (1 frame at 60fps)
```

**`gpu_submit` (AMD only):**
```
gamepulse.ebpf.probe: "gpu_submit"
gamepulse.ebpf.gpu_submit.submit_count: long
gamepulse.ebpf.gpu_submit.latency_histogram: histogram
gamepulse.ebpf.gpu_submit.latency_avg_us: float
gamepulse.ebpf.gpu_submit.latency_max_us: float
```

**`mem`:**
```
gamepulse.ebpf.probe: "mem"
gamepulse.ebpf.mem.page_fault_minor_count: long
gamepulse.ebpf.mem.page_fault_major_count: long
gamepulse.ebpf.mem.page_fault_latency_histogram: histogram
gamepulse.ebpf.mem.page_alloc_count: long
gamepulse.ebpf.mem.page_free_count: long
```

**`futex`:**
```
gamepulse.ebpf.probe: "futex"
gamepulse.ebpf.futex.contention_count: long
gamepulse.ebpf.futex.wait_histogram: histogram
gamepulse.ebpf.futex.wait_total_us: float
gamepulse.ebpf.futex.top_contended: nested  # top 5 futex addresses by wait time
```

**`irq`:**
```
gamepulse.ebpf.probe: "irq"
gamepulse.ebpf.irq.hardirq_count: long
gamepulse.ebpf.irq.hardirq_total_us: float
gamepulse.ebpf.irq.softirq_count: long
gamepulse.ebpf.irq.softirq_total_us: float
gamepulse.ebpf.irq.top_irqs: nested  # top 5 IRQs by total duration
```

**`vfs`:**
```
gamepulse.ebpf.probe: "vfs"
gamepulse.ebpf.vfs.read_count: long
gamepulse.ebpf.vfs.write_count: long
gamepulse.ebpf.vfs.read_latency_histogram: histogram
gamepulse.ebpf.vfs.write_latency_histogram: histogram
gamepulse.ebpf.vfs.read_bytes: long
gamepulse.ebpf.vfs.write_bytes: long
```

**`syscall`:**
```
gamepulse.ebpf.probe: "syscall"
gamepulse.ebpf.syscall.total_count: long
gamepulse.ebpf.syscall.top_syscalls: nested  # top 10 by count, each with count + avg latency
```

**`shader`:**
```
gamepulse.ebpf.probe: "shader"
gamepulse.ebpf.shader.compile_count: long
gamepulse.ebpf.shader.compile_total_ms: float
gamepulse.ebpf.shader.compile_max_ms: float
gamepulse.ebpf.shader.compile_histogram: histogram
```

### 7.3 TSDS Considerations

The `gamepulse.ebpf` data stream uses TSDS mode. Dimension fields (time series identity):
- `host.name`
- `gamepulse.session.id`
- `gamepulse.ebpf.probe`
- `data_stream.dataset`

All histogram and latency fields are `metric_type: gauge` (they represent instantaneous 1-second snapshots, not monotonically increasing counters). Count fields within a single snapshot (e.g., `bio.read_count`) are also gauges — they represent "reads in this 1-second window," not lifetime totals.

### 7.4 Stutter Correlation Events

When eBPF probes detect values that cross stutter-relevant thresholds, the daemon emits a correlation event to `logs-gamepulse.events-default` (the existing events stream):

```json
{
  "@timestamp": "2026-04-08T18:31:42.000Z",
  "gamepulse": {
    "session": { "id": "a1b2c3d4-..." },
    "event": {
      "type": "stutter_cause",
      "cause": "scheduling",
      "detail": "Render thread runqueue wait exceeded 5ms (actual: 7.2ms)",
      "probe": "schedlatency",
      "severity": "warning"
    }
  }
}
```

Stutter detection thresholds (configurable):
- **Scheduling stutter:** p99 runqueue latency > 5ms
- **I/O stutter:** p99 block I/O latency > 10ms
- **GPU stutter:** any fence wait > 16.6ms (one frame at 60fps, or dynamically adjusted to `1000 / current_fps`)
- **Memory stutter:** major page fault count > 10 in a 1-second window
- **Futex stutter:** any single futex wait > 5ms

These events correlate temporally with the frame timing data from the Python collector. In Kibana's Session Deep-Dive dashboard, stutter events appear as annotations on the FPS timeline, enabling direct visual correlation: "FPS dropped to 45 here — and look, the scheduler probe shows a 7ms CCX migration at the same timestamp."

---

## 8. GPU Probe Architecture — A Closer Look

The GPU probes deserve detailed treatment because they span three layers of abstraction in the kernel, and the choice of attach point determines what we can observe.

### 8.1 Three Layers of GPU Tracing

**Layer 1: DRM Scheduler (vendor-neutral, stable uAPI)**

The `gpu_scheduler` tracepoint group is part of the DRM subsystem's generic scheduler, used by all modern GPU drivers. These tracepoints are stable uAPI — their format is guaranteed not to change.

```
gpu_scheduler:drm_run_job        — GPU job submitted to hardware
gpu_scheduler:drm_sched_process_job  — GPU job completed
gpu_scheduler:drm_sched_job_wait_dep — GPU job waiting on a dependency
```

By correlating `drm_run_job` timestamps with `drm_sched_process_job` for the same fence pointer, we get **GPU execution duration** per job. This works identically on AMD, Intel, and Nouveau.

This is the `gpu_sched` probe — vendor-neutral, always available, and the first GPU probe to implement.

**Layer 2: DMA Fence Waits (vendor-neutral kernel API, kprobe)**

`dma_fence_default_wait` is the kernel function called when any process blocks waiting for a GPU fence to signal. This captures CPU-side stalls caused by GPU work taking too long.

This is the `gpu_fence` probe. While `dma_fence_default_wait` is not a tracepoint (it's a regular kernel function we attach via kprobe), it's stable across kernel versions and vendor-neutral.

**Key insight for gaming:** A fence wait exceeding the frame budget (16.6ms at 60fps, 6.9ms at 144fps) means a frame was dropped because the CPU had to wait for the GPU. This is the single most actionable GPU metric for gaming performance analysis.

**Layer 3: Driver-Specific Internals (vendor-specific, kprobe)**

`amdgpu_cs_ioctl` is the AMD-specific command submission ioctl handler. Tracing it reveals the driver-side overhead of submitting work to the GPU: memory pinning, command validation, buffer management.

This is the `gpu_submit` probe — AMD-only, requires the amdgpu kernel module. NVIDIA's equivalent (`nvidia_ioctl`) is in a proprietary module and cannot be kprobed. Intel's equivalent is in the i915 module and could be added later.

### 8.2 RDNA 4 / GFX1201 Considerations

Your RX 9070 XT is GFX1201 (RDNA 4). The amdgpu kernel module for RDNA 4 uses the same `amdgpu_cs_ioctl` entry point as RDNA 2/3 — the function signature hasn't changed. However, RDNA 4 introduces changes to the GPU scheduler (new hardware queues, different fence signalling paths) that may affect the correlation between `drm_run_job` and job completion. The daemon should log the GPU architecture (from sysfs or the session document) and we can validate timing accuracy on RDNA 4 during the first live test.

The DRM scheduler tracepoints are architecture-independent and will work correctly on GFX1201.

---

## 9. Build System

### 9.1 Build Process

The eBPF daemon uses a two-stage build:

**Stage 1: Compile BPF programs (nightly Rust + bpf-linker)**
```bash
cd gamepulse-ebpf-probes
cargo +nightly build \
  --target bpfel-unknown-none \
  -Z build-std=core \
  --release
```

This produces `.o` files containing BPF bytecode for each probe.

**Stage 2: Compile userspace daemon (stable Rust)**
```bash
cd gamepulse-ebpf-daemon
cargo build --release
```

The `build.rs` in `gamepulse-ebpf-daemon` uses `aya_build::build_ebpf()` (from the `aya-build` crate) to automatically trigger Stage 1 and embed the resulting BPF bytecode into the daemon binary via `include_bytes_aligned!()`.

The result is a single static binary with no runtime BPF object loading — the BPF programs are baked in at compile time. This is important for distribution: the user gets one binary, not a binary plus a directory of `.o` files.

### 9.2 CI/CD

GitHub Actions workflow:

```yaml
# .github/workflows/ebpf.yml
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - name: Install Rust nightly + BPF toolchain
        run: |
          rustup install nightly
          rustup component add rust-src --toolchain nightly
          cargo install bpf-linker
      - name: Build eBPF probes
        run: cargo xtask build-ebpf --release
      - name: Build daemon
        run: cargo build --release -p gamepulse-ebpf-daemon
      - name: Run clippy
        run: cargo clippy --workspace -- -D warnings
      - name: Run unit tests
        run: cargo test --workspace
```

Integration tests (actually loading BPF programs and attaching to tracepoints) require a VM with `CAP_BPF`. This can be done with `vmtest` or a privileged GitHub Actions runner. Deferred to Phase 2b.

### 9.3 Dependencies

```toml
# gamepulse-ebpf-probes/Cargo.toml
[dependencies]
aya-ebpf = "0.1"
aya-log-ebpf = "0.1"

# gamepulse-ebpf-daemon/Cargo.toml
[dependencies]
aya = { version = "0.13", features = ["async_tokio"] }
aya-log = "0.2"
tokio = { version = "1", features = ["full"] }
reqwest = { version = "0.12", features = ["json", "rustls-tls"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
toml = "0.8"
tracing = "0.1"
tracing-subscriber = "0.3"
notify = "7"              # inotify for session file watching
nix = { version = "0.29", features = ["process", "signal"] }
```

---

## 10. Configuration

The eBPF daemon reads the same config file as the Python collector (`~/.config/gamepulse/gamepulse.toml`) with an additional `[ebpf]` section:

```toml
[ebpf]
enabled = true

# Which probes to enable (all enabled by default)
# Set to false to disable specific probes
[ebpf.probes]
schedlatency = true
bio = true
gpu_sched = true
gpu_fence = true
gpu_submit = true     # AMD only; silently skipped on non-AMD
mem = true
futex = true
irq = true
vfs = true
syscall = true
shader = false        # Requires Mesa uprobe target; disabled by default
proton = false        # Stretch goal; disabled by default

# Aggregation interval (seconds). Default 1. Range: 1-10.
aggregation_interval = 1

# Background baseline metrics when no game is running
background_baseline = false

# Stutter detection thresholds
[ebpf.stutter_thresholds]
sched_p99_us = 5000       # 5ms
bio_p99_us = 10000        # 10ms
gpu_fence_us = 16600      # 16.6ms (one frame at 60fps)
major_pagefault_count = 10
futex_wait_us = 5000      # 5ms

# Ring buffer sizes (KB, must be power of 2)
[ebpf.ring_buffer_sizes]
sched = 256
bio = 64
gpu = 128
vfs = 128
shader = 16
```

---

## 11. Deployment and Packaging

### 11.1 systemd Service

```ini
# /etc/systemd/system/gamepulse-ebpf.service
[Unit]
Description=GamePulse eBPF Telemetry Daemon
Documentation=https://github.com/<org>/gamepulse
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gamepulse-ebpf
Restart=on-failure
RestartSec=5

# Capabilities instead of root
CapabilityBoundingSet=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_DAC_READ_SEARCH

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/run/gamepulse /tmp/gamepulse
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=no  # BPF JIT needs W+X

[Install]
WantedBy=multi-user.target
```

**Why `CAP_SYS_ADMIN` in addition to `CAP_BPF`?** On some kernel configurations, loading BPF programs into tracepoints requires `CAP_SYS_ADMIN`. The kernel is moving towards `CAP_BPF` + `CAP_PERFMON` being sufficient (since 5.8), but some distributions backport security patches that reintroduce the `SYS_ADMIN` requirement for certain program types. Including it avoids debugging capability failures on distros that do this.

**Why `CAP_DAC_READ_SEARCH`?** Reading `/proc/<pid>/` for processes owned by other users (the game process might run as a different UID in some containerised setups, though this is rare for gaming).

### 11.2 Steam Deck Considerations

On Steam Deck (SteamOS), the root filesystem is read-only. The eBPF daemon:
- Installs to `/home/deck/.local/bin/` (survives OS updates).
- Uses a user systemd service (`~/.config/systemd/user/`) if running without root.
- **However:** eBPF requires elevated capabilities. On Steam Deck, the user must either run the system-level service (which survives in `/etc/` across updates if placed correctly) or set capabilities on the binary via `setcap`. The system-level service is the recommended path.

### 11.3 Package Formats

The eBPF daemon is distributed as:
- **AUR PKGBUILD** (primary, since the gaming PC runs CachyOS/Arch)
- **Debian `.deb`** (for Ubuntu/SteamOS users)
- **RPM** (for Fedora/Nobara users)
- **Static binary tarball** (fallback, with install script that sets capabilities)

---

## 12. Performance Budget

The eBPF daemon must not measurably affect gaming performance.

**CPU budget:** < 0.5% of a single core. At 1/s aggregation and typical event volumes (~500 sched events/sec, ~50 I/O events/sec, ~200 GPU events/sec), the userspace processing is trivially cheap. The kernel-side BPF program overhead is bounded by the number of tracepoint fires and the work done per fire (lookup in a 64-entry PID map + ring buffer reserve + submit ≈ 100-200ns per event).

**Memory budget:** < 30 MB RSS. Ring buffers total ~600 KB locked memory. The daemon's own heap usage is dominated by the Aya runtime and the ES HTTP client.

**Network budget:** At 1 document/probe/second with 9 probes active, that's 9 JSON documents per second, each ~500 bytes. Total: ~4.5 KB/s to Elasticsearch. Negligible.

**Worst-case analysis:** The most dangerous probe is `syscall` — a game making 50,000 syscalls/second would fire the BPF program 50,000 times/second. With in-kernel aggregation (HashMap increment, ~50ns per fire), that's 2.5ms of BPF execution time per second across all CPUs — still < 0.3% of a single core. If overhead is observed, the `aggregation_interval` can be increased or the `syscall` probe can be disabled.

---

## 13. Testing Strategy

### 13.1 Unit Tests

- Histogram computation, percentile calculation, CCX mapping logic, config parsing, ES document serialisation.
- These run without BPF capabilities (no kernel interaction).

### 13.2 Integration Tests (Require VM)

- Load each probe, verify it attaches without verifier rejection.
- Generate synthetic workloads (file I/O, futex contention, scheduler pressure) and verify events appear in ring buffers.
- Verify PID filtering: events from non-game processes are not captured.
- Run on kernel 5.8 (minimum) and kernel 6.19 (CachyOS current) to verify CO-RE compatibility.

### 13.3 Live Validation

- Run the daemon alongside the Python collector during a Cyberpunk 2077 session on the CachyOS gaming PC.
- Verify all probe data appears in `metrics-gamepulse.ebpf-default`.
- Verify session correlation: eBPF documents have the same `gamepulse.session.id` as the Python collector's documents.
- Verify stutter correlation events appear in `logs-gamepulse.events-default`.
- Check dashboard: Scheduler Analysis dashboard shows runqueue latency timeline, CCX migration markers, GPU fence wait overlay.
- **Measure overhead:** Compare FPS with and without the daemon running. Target: < 1 FPS difference at 144 FPS (i.e., within measurement noise).

---

## 14. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| BPF verifier rejects a probe on a specific kernel version | Probe doesn't load | Graceful skip. Each probe is independent. Log the verifier error for debugging. |
| Ring buffer overflow under burst conditions (shader compilation storm) | Events dropped | Drop counter in BPF program. Log when drops exceed threshold. Size buffers for worst observed case + 4x margin. |
| `amdgpu_cs_ioctl` signature changes in a future kernel | GPU submit probe breaks | kprobe by symbol name (not offset). CO-RE handles struct layout changes. If attach fails, skip gracefully. |
| RDNA 4 changes DRM scheduler timing | GPU execution duration inaccurate | Validate in live test. DRM scheduler tracepoints are uAPI-stable; only the timing distribution might differ. |
| High syscall rate causes measurable overhead | FPS drop | `syscall` probe disabled by default. Enable only for developer profile. In-kernel aggregation limits userspace cost. |
| Proton process tree walking misses game threads | PID filter too narrow | Refresh PID set every 5 seconds. Walk `/proc/<root_pid>/task/` for threads. Walk child PIDs recursively (bounded depth). |
| Steam Deck kernel lacks BTF | No eBPF at all | SteamOS 3.5+ includes BTF. Older versions: document as unsupported. |

---

## 15. Implementation Handoff to Claude Code

This design document is the input for Claude Code to begin implementation. The recommended implementation order:

### Sprint 1: Scaffold and First Probe
1. `cargo generate` the Aya project structure.
2. Implement `ProbeRequirements` checking (BTF, capabilities, tracepoints, kallsyms).
3. Implement the `schedlatency` probe end-to-end: BPF program → ring buffer → userspace aggregation → ES document → bulk ship.
4. Session file watcher + PID filtering.
5. Config file parsing (`[ebpf]` section in `gamepulse.toml`).
6. First live test on CachyOS gaming PC.

### Sprint 2: Core Probes
7. `bio` probe.
8. `gpu_sched` probe (DRM scheduler tracepoints).
9. `mem` probe (page faults).
10. Stutter correlation event emission.
11. Live validation with all 4 core probes active.

### Sprint 3: Extended Probes
12. `gpu_fence` probe (kprobe on `dma_fence_default_wait`).
13. `gpu_submit` probe (kprobe on `amdgpu_cs_ioctl`).
14. `futex` probe.
15. `irq` probe.
16. `vfs` probe.

### Sprint 4: Integration
17. `fields.yml` for the ebpf data stream (update integration package).
18. Ingest pipeline for histogram normalisation.
19. Scheduler Analysis dashboard.
20. Packaging (AUR PKGBUILD, systemd service).
21. README and user documentation.

### Sprint 5 (Stretch): Advanced Probes
22. `syscall` probe.
23. `shader` probe (Mesa uprobe).
24. `proton` probe (Wine/Proton translation overhead).

---

## 16. Open Questions for Implementation

1. **Elasticsearch histogram field type support on Serverless.** Verify that Elastic Cloud Serverless supports the `histogram` field type in TSDS mode. If not, fall back to storing bucket arrays as nested objects. Test this before implementing the `fields.yml` definitions.

2. **CCX detection on Zen 5 (9800X3D) — VERIFIED.** The 9800X3D exposes all 16 logical CPUs (8 cores, SMT) as a single L3 sharing group (`0-15`). Single CCD, single CCX, single socket. The `ccx_cross_count` metric will always be zero on this CPU. The implementation is unchanged — the daemon reads `/sys/devices/system/cpu/cpu*/cache/index3/shared_cpu_list`, finds one group, and correctly produces `ccx_cross_count = 0`. Core-to-core migrations within the single CCX are still tracked by `migration.total_count`. Multi-CCD chips (7950X, 9950X, etc.) will produce non-zero `ccx_cross_count` values.

3. **Shader uprobe target path.** Mesa's shader compiler entry point varies by version and build configuration. The uprobe target (e.g., `/usr/lib/dri/radeonsi_dri.so`) needs to be discoverable at runtime. Consider using `dladdr` or parsing `/proc/<pid>/maps` to find the correct shared object.

### Resolved Design Decisions

- **`session.json` location:** `$XDG_RUNTIME_DIR/gamepulse/session.json` with fallback to `/tmp/gamepulse/session.json`. No root or tmpfiles.d required. Portable across distributions.

- **Histogram bucket boundaries:** Keep fine-grained log2 μs-scale buckets uniformly across all probes. Precision at collection time cannot be recovered; coarser visualization can always be done at query time in Kibana by merging adjacent buckets. All probes use the same 16-bucket log2 histogram (1μs to 33ms+) for consistency.

---

## Appendix A: Kernel Symbol Verification Commands

Run these on the gaming PC to verify tracepoint and symbol availability:

```bash
# Verify BTF is available
ls -la /sys/kernel/btf/vmlinux

# List available scheduler tracepoints
grep sched /sys/kernel/tracing/available_events

# List DRM scheduler tracepoints (gpu_scheduler group)
grep gpu_scheduler /sys/kernel/tracing/available_events

# Verify amdgpu_cs_ioctl exists in kallsyms (AMD GPU loaded)
grep amdgpu_cs_ioctl /proc/kallsyms

# Verify dma_fence_default_wait exists
grep dma_fence_default_wait /proc/kallsyms

# Check kernel version
uname -r

# Check capabilities available to current user
capsh --print

# Check RLIMIT_MEMLOCK
ulimit -l
```

## Appendix B: Reference — Aya Program Types Used

| Aya Type | Kernel Concept | Used By |
|----------|---------------|---------|
| `TracePoint` | Static kernel tracepoints (`/sys/kernel/tracing/events/`) | sched, bio, mem, irq, futex, syscall, gpu_sched |
| `KProbe` | Dynamic instrumentation on kernel function entry | gpu_fence, gpu_submit, vfs |
| `KRetProbe` | Dynamic instrumentation on kernel function return | gpu_fence (measures duration), gpu_submit, vfs |
| `UProbe` | Dynamic instrumentation on userspace function entry | shader (Mesa) |
| `URetProbe` | Dynamic instrumentation on userspace function return | shader (measures duration) |
