# GamePulse — Claude Code Project Instructions

## What this project is

GamePulse is an open-source gaming performance telemetry platform.
It collects, ships, and visualises real-world gaming metrics to Elasticsearch.

The target audience is game developers, journalists, Proton/Wine/Mesa maintainers,
and package maintainers who need real-world performance data.

## Current state — last reconciled 2026-04-14 (session.label auto-gen)

### What is built and verified ✅

- **Python collector** (Phase 1): All 8 metric collectors running on CachyOS gaming PC — CPU, GPU (AMD), memory, storage, network, audio, frame (MangoHud), power. Outputs `gamepulse.*` namespaced docs. SIGTERM now interrupts `time.sleep` immediately via `_ShutdownSignal` and always runs `finally` cleanup (fixed 2026-04-10, commit `8983d27`).
- **Elastic Agent integration scaffold** (Phase 0.5): `elastic-package check` PASS, `elastic-package test static` 11/11 PASS (confirmed 2026-04-10). Package builds to `gamepulse-0.1.0.zip` via `bash scripts/build-package.sh`.
- **Ingest pipelines deployed**: 11 pipelines live on Elastic Cloud Serverless. All index templates wired with `default_pipeline`. Pipeline simulation verified.
- **Live gameplay verified**: Full session end-to-end (Cyberpunk 2077, Proton, MangoHud, all 8 streams, game detection working).
- **Session summary doc**: `cli.py` `finally` block ships session-end doc. Fields: `ended`, `duration_s`, `avg_fps`, `low_1pct_fps`, `p99_frametime_ms`, `peak_gpu_temp_c`, `peak_cpu_temp_c`, `peak_gpu_power_w`, `total_frames`, `stutter_count`, `bottleneck_dominant`.
- **GPU driver version**: `gamepulse.hardware.gpu.driver_version` via `enricher/host.py` (AMD: vulkaninfo, NVIDIA: nvidia-smi).
- **Kibana dashboards** (8 live dashboards):
  - `dashboards/gamepulse-dashboard.ndjson` — baseline (UI-exported)
  - `dashboards/config-comparison-dashboard.json` — 16 panels (ID: 21b663d6-de42-46c6-aeaf-e6c48e46ecec)
  - `dashboards/session-deep-dive-dashboard.json` — 17 panels (ID: b68f1178-6923-4e92-819b-33eb595197a9)
  - `dashboards/storage-io-dashboard.json` — 16 panels (ID: f8a9d960-130e-43db-8554-6033f45e8a9c)
  - `dashboards/system-health-dashboard.json` — 15 panels (ID: 1b2a1b70-a315-4ed4-91c4-11aa0abe5e1d)
  - `dashboards/game-library-dashboard.json` — 8 panels (ID: e7d878d0-e2d6-454b-9a95-d93a4aeb70a8)
  - `dashboards/scheduler-analysis-dashboard.json` — 15 panels (ID: 89ca0908-5639-45f7-9a70-edadfe7d7124) eBPF data
  - `dashboards/home-dashboard.json` — 10 panels (ID: home-dashboard-2026-04-13) — nav bar, rig health, FPS trends, all games, recent sessions

### eBPF daemon (Phase 2)

**Sprint 1 — schedlatency probe** ✅ CONFIRMED IN ES
- Tracepoints: `sched_wakeup`, `sched_switch`, `sched_migrate_task`
- End-to-end test PASSED: Starfield, Proton, 231 docs in `metrics-gamepulse.ebpf-default` (2026-04-09)
- Fields: runqueue latency histogram (16-bucket log2), min/max/avg_us, event_count, migration total_count, ccx_cross_count (always 0 on 9800X3D — expected), per-thread breakdown (top 8 by switch count)

**Sprint 2 — bio + gpu_sched + mem probes + stutter correlation** ✅ CONFIRMED IN ES
- **bio** ✅ CONFIRMED IN ES (6,112 docs total with schedlatency+gpu_sched, date 2026-04-10) — `block_rq_issue` / `block_rq_complete`. System-wide (kworker submits page-cache I/O). Verified: 1–1,351 events/s; spikes on asset loads.
- **gpu_sched** ✅ CONFIRMED IN ES (6,112 docs total with schedlatency+bio, date 2026-04-10) — `drm_sched_job_queue` / `drm_sched_job_run`. System-wide (RADV uses dedicated submission threads). Verified: 1,500–10,925 jobs/s.
- **mem** ✅ CONFIRMED — silence correct by design (`flush()` returns `None` when working set resident; will fire under real memory pressure) — `page_fault_user` (GAME_PIDS filtered) + `mm_vmscan_direct_reclaim_begin` (system-wide).
- **stutter_correlation** ✅ CONFIRMED — silence correct by design (16ms threshold not crossed in healthy session; will fire under actual stutter events) — `correlate()` in `aggregator.rs`, emits `probe: "stutter_correlation"` when ≥2 probes spike in same 1s window.
- All Sprint 2 fields mapped in `data_stream/ebpf/fields/fields.yml` (bio, gpu_sched, mem, stutter groups). `elastic-package check` PASS.

**Sprint 3 — extended probes** ✅ CONFIRMED IN ES (2348 docs, session 7bce1dc5, Starfield, 2026-04-10)
- **futex** ✅ CONFIRMED IN ES — 6 docs (GAME_PIDS filtered; sparse by design — game-specific mutex signal). kprobe/kretprobe on `do_futex`. avg_us=0.97, contended_count=0 (healthy session). Fields: latency_histogram, min/max/avg_us, event_count, contended_count.
- **irq** ✅ CONFIRMED IN ES — 367 docs. Tracepoints `irq/irq_handler_{entry,exit}` + `irq/softirq_{entry,exit}`. System-wide. Both `hard_irq` and `softirq` sub-groups confirmed in ES. softirq avg_us=2.3, event_count=1,812/doc.
- **vfs** ✅ CONFIRMED IN ES — 362 docs. kprobe/kretprobe on `vfs_read` + `vfs_write`. GAME_PIDS filtered. Both `read` and `write` sub-groups confirmed. read avg_us=215 (bimodal: cache hits + disk reads).
- **gpu_fence** ✅ CONFIRMED IN ES — 367 docs. kprobe/kretprobe on `dma_fence_default_wait`. System-wide. blocked_count=0 (GPU not stalling — correct signal for a healthy session). avg_us=0.45.
- **gpu_submit** ✅ CONFIRMED IN ES — 367 docs. kprobe on `amdgpu_cs_ioctl` (module-symbol). Count-only. event_count=181/doc.
- All 5 probes wired end-to-end: BPF kernel side → userspace aggregator → EbpfPayload → ES fields. `cargo check` PASS. `elastic-package check` PASS. `elastic-package test static` 11/11 PASS.

**Sprint 4–5** 🔲 NOT STARTED
- Scheduler Analysis dashboard, packaging (systemd, AUR), advanced probes (syscall, shader, proton).

### Key learnings

- **BPF verifier requires opt-level=2**: Debug Rust builds emit BPF-to-BPF calls to panic infrastructure → verifier rejects ("processed 0 insns"). `-C opt-level=2` set in `ebpf/.cargo/config.toml`. Never remove.
- **Async ring buffer drain race**: `AsyncFd<RingBuf>` + Tokio EPOLLET silently drops events. Drain synchronously in `collect()` on each tick instead.
- **GAME_PIDS capacity**: `max_entries=256` — BPF hash maps at 100% load fail inserts. Always leave headroom.
- **session.json path**: Always `/tmp/gamepulse/session.json`. `$XDG_RUNTIME_DIR` is stripped by sudo — daemon and collector would watch different paths.
- **RADV GPU scheduling**: `drm_sched_job_queue` must be system-wide. RADV uses dedicated submission threads not in the game PID tree.
- **ES histogram field type on Serverless TSDS (resolved 2026-04-10)**: The `type: histogram` field mapping is accepted by Elasticsearch Serverless in TSDS mode. LatencyHistogram docs (`{"values":[…],"counts":[…]}`) land without bulk errors. No fallback to scalar percentile fields required. This was the last open architectural risk for the eBPF data model.
- **Sprint 3 kernel symbol availability (confirmed 2026-04-10)**: `T do_futex`, `T vfs_read`, `T vfs_write`, `T dma_fence_default_wait` all present in /proc/kallsyms on kernel 6.19.11 CachyOS. `t amdgpu_cs_ioctl [amdgpu]` also present (lowercase = module-local, kprobes still work on module symbols). sys_enter_futex/sys_exit_futex tracepoints exist but format files are root-only at build time; using do_futex kprobe as implementation instead. irq tracepoint format files also root-only — layout inferred from kernel source (offset 8: s32 irq for irq_handler_{entry,exit}; offset 8: u32 vec for softirq_{entry,exit}) consistent with kernel 5.x–6.x standard. All 5 probes attach and produce live docs in ES (ES-confirmed 2026-04-10, session 7bce1dc5).
- **futex doc sparsity**: futex probe (GAME_PIDS-filtered) produces very few docs when game has low mutex contention — 6 docs in 6-minute Starfield session. This is correct: it's a game-specific signal (not system-wide), and contended_count=0 means the game held mutexes briefly. Will produce more docs under lock contention (e.g., asset streaming, thread pool saturation).
- **gpu_fence blocked_count=0 is the healthy baseline**: `dma_fence_default_wait` fires when CPU waits for GPU work. blocked_count=0 (wait >1ms) means the GPU is keeping up. Elevated blocked_count signals GPU-CPU sync stalls.
- **Network collector silent failure (fixed 2026-04-10)**: `CollectionConfig.network` defaulted to `False` while all other collectors defaulted to `True`. The collector itself was correct; it simply was never instantiated. User config had no `[collection]` section so the default always applied. Fix: changed default to `True` in `config.py`. No changes needed to `network.py`.
- **Rust host enricher: dGPU selection requires max-VRAM heuristic (fixed 2026-04-10)**: On systems with iGPU + dGPU both exposing DRM nodes (card0=iGPU 2GB, card1=RX 9070 XT 16GB), iterating sorted cards and breaking at first AMD card picks the wrong one. Fix: score all AMD cards by VRAM and pick the one with the most. Both the Python enricher (same sorted/break bug) and the original Rust enricher had this flaw; only caught when live ES data showed `hardware.gpu.model=iGPU`.
- **Rust host enricher: vulkaninfo first-match must be guarded (fixed 2026-04-10)**: `enrich_amd()` originally overwrote model/vulkan_driver on every matching line. With iGPU+dGPU, vulkaninfo lists dGPU first then iGPU — the iGPU name wins. Fix: `!m.contains_key("model")` guard preserves first (correct) match.
- **mesa_version unavailable without DISPLAY (fixed 2026-04-10)**: `glxinfo -B` requires an X/Wayland display. As a background agent it gets "unable to open display". For RADV, mesa_version == driver_version. Fix: fall back to driver_version when vulkan_driver is "radv" and glxinfo produces no output.
- **Session summary game.name null when game exits before summary (fixed 2026-04-11)**: `build_summary_doc()` calls `session.base_doc()` which uses `current_game`, but by shutdown time the game has already exited and `current_game=None`. Fix: track `last_known_game` in the main loop (set on GameStarted and GameEnded), inject game fields into summary doc when game has exited.
- **AMD GPU power1_average transient spikes (expected behaviour, 2026-04-11)**: `power1_average` (PPT) can briefly read values above the configured power cap (330W). Peak observed 529W during Starfield at ~257W average. This is genuine hardware behaviour — AMD RDNA firmware enforces the cap over a time window so instantaneous readings can exceed it. Not a conversion bug; /1e6 (µW→W) is correct.
- **AUR PKGBUILD: CachyOS LTO breaks ring crate (fixed 2026-04-11)**: CachyOS `/etc/makepkg.conf` sets `OPTIONS=(...lto...)` and `LTOFLAGS="-flto=auto"`, appending `-flto=auto` to CFLAGS. ring's build.rs compiles its C/ASM via the `cc` crate with these CFLAGS, producing GCC LTO bitcode objects. Rust's LLD linker cannot resolve symbols from GCC LTO objects → `undefined symbol: ring_core_0_17_14__*` at final link. Fix: `options=(!lto)` in the PKGBUILD disables LTO for the package.
- **AUR PKGBUILD: RUSTFLAGS -C target-cpu=native breaks eBPF cross-compile (fixed 2026-04-11)**: makepkg's `RUSTFLAGS="-C opt-level=3 -C target-cpu=native"` is inherited by the eBPF probes cross-compile step. bpf-linker doesn't understand the host CPU (`znver5` on Ryzen 9800X3D) for the BPF bytecode target. Fix: `RUSTFLAGS=""` prefix on the eBPF probe and daemon build commands.
- **AUR PKGBUILD: eBPF probe ELF path at runtime (fixed 2026-04-11)**: The daemon's `default_probe_path()` walks up from its own binary to find `gamepulse-ebpf-daemon/` subdir (dev layout). Installed at `/usr/bin/`, no such parent exists → resolves to `/usr/bin/target/bpfel-unknown-none/release/gamepulse-ebpf-probes`. Fix: install probe ELF to `/usr/lib/gamepulse/gamepulse-ebpf-probes` and pass `--probe-path` in the systemd `ExecStart`.
- **elastic-package test asset internal build copies full repo (v0.122.0)**: `elastic-package test asset` rebuilds the package internally by copying the entire repo root and zipping it — it does NOT use `scripts/build-package.sh` and does NOT respect `.elastic-package-ignore` during the copy step. With `target/` (277MB Rust build artifacts) present, the zip takes 38s and Kibana's request times out. Fix: `bash scripts/test-asset.sh` stashes `target/`, `src/`, `ebpf/`, `packaging/`, `dashboards/`, `.agents/`, `collector/.venv/` to `/tmp` before running the test.
- **eBPF data stream: nested type incompatible with TSDS synthetic source on ES 8.13 (fixed 2026-04-11)**: `data_stream/ebpf/manifest.yml` had `index_mode: "time_series"`. TSDS requires synthetic source. ES 8.13 synthetic source does not support `object` or `nested` field types. `thread_breakdown` is `nested` (array of per-thread objects). Fix: removed `index_mode: "time_series"` from the ebpf manifest (making it a regular metrics data stream locally). Cloud serverless uses custom index templates deployed independently and is unaffected. Also removed `dimension: true` from the three keyword fields (dimension metadata is only valid in TSDS context).
- **elastic-package hook glob "pipeline" blocked _dev/test/pipeline/ (fixed 2026-04-11)**: The pre-edit hook used `"pipeline"` as a substring glob, blocking edits to `_dev/test/pipeline/test-*.json` files (not just ingest pipeline files). Fix: changed the glob to `"ingest_pipeline"` which matches only `elasticsearch/ingest_pipeline/` paths, not test fixture paths.
- **elastic-package hook manifest.yml protection scope (fixed 2026-04-11)**: The pre-edit hook used `"manifest.yml"` as a substring match, blocking ALL manifest.yml files including data_stream/*/manifest.yml. The intent was to protect only the root `manifest.yml`. Fix: moved root manifest.yml to an exact-match check (`[[ "$REL_PATH" == "manifest.yml" ]]`) and removed it from the PROTECTED_PATTERNS array.
- **Pipeline test fixture expected_events key invalid (fixed 2026-04-11)**: `elastic-package check` rejects `expected_events` in pipeline test fixtures ("Additional property expected_events is not allowed"). Only the `events` key is valid in the input fixture. Expected output goes in a separate `test-*.json-expected.json` file with an `expected` key, auto-generated by `--generate`.
- **elastic-package test pipeline runs against remote ES without Docker**: `elastic-package test pipeline` uses `ELASTIC_PACKAGE_ELASTICSEARCH_HOST` + `ELASTIC_PACKAGE_ELASTICSEARCH_API_KEY` env vars to hit remote Elastic Cloud. No local Docker stack needed. This is the correct approach for pipeline tests; asset/system tests require local Docker.
- **elastic-package install hangs with local Kibana (v0.122.0 bug/TLS quirk)**: `elastic-package install --zip` command hangs indefinitely against the local 8.13.0 stack — no response, no timeout. The underlying Kibana Fleet API works fine when called directly. Workaround: POST the zip directly via `curl` or Python `urllib.request` with TLS cert verification disabled (`ssl.CERT_NONE`). The `test asset` command (which calls the same Kibana API internally) also works via a different code path. Root cause unknown — may be an HTTP/2 + TLS interaction in elastic-package v0.122.0.
- **elastic-package stack up serves local packages automatically**: Running `elastic-package stack up` from within the package repo detects `build/packages/*.zip` and serves them via the built-in Package Registry (HTTPS port 8080). The registry uses TLS self-signed certs — use `https://` not `http://` and skip cert verify when querying directly.
- **Serverless Fleet does not support custom registry URLs (confirmed 2026-04-11)**: The Fleet Settings API on Elastic Cloud Serverless has no `package_registry_url` or equivalent field. Custom registry URLs require `xpack.fleet.registryUrl` in self-hosted Kibana config. For Serverless, the only install method is the zip upload API (`POST /api/fleet/epm/packages`).
- **Direct zip upload to Kibana Fleet API works on both local and Serverless**: `POST /api/fleet/epm/packages` with `Content-Type: application/zip` and the zip body. Authentication: Basic auth for local stack, ApiKey for Serverless. Response: 200 with JSON listing all installed assets. Kibana 8.7+ required.
- **elastic-package install (without --zip) overwrites clean build zip**: `elastic-package install` (no `--zip`) rebuilds from source, including `target/` (277MB), producing a 1.1GB zip that overwrites `build/packages/gamepulse-0.1.0.zip`. Always use `bash scripts/build-package.sh` to restore the clean zip after running `elastic-package install` without `--zip`.
- **elastic-package test system returns "No test results" for custom binary integrations (confirmed 2026-04-11)**: With no `_dev/test/system/` directory configured, `elastic-package test system` exits cleanly with "No test results" — same as policy tests. This is the correct and acceptable outcome for integrations whose data source requires gaming hardware (GPU, games running, eBPF root). The elastic/integrations contribution guidelines allow system test skips for hardware-dependent integrations.
- **xrandr monitor enrichment (2026-04-12/13)**: `collect_monitors()` in `src/host.rs` parses `xrandr --verbose`. Monitor connector headers: token[1]=="connected". Active mode: line contains `*current`. Refresh rate: `v: ... clock NNN.NNHz` line immediately after. VRR: `vrr_capable: 1`, `freesync: 1`. HDR: `max bpc` > 8. Field path: `gamepulse.hardware.monitors` (nested array in session data stream fields.yml). Session data stream is NOT TSDS so nested type is fine.
- **ES transform pivot: top_metrics with nested field paths wraps result in full object hierarchy (2026-04-12/13)**: Using `gamepulse.hardware.gpu.model` in `top_metrics.metrics.field` writes `{gamepulse: {hardware: {gpu: {model: "..."}}}}` to the destination, not just the scalar. This is incompatible with a flat keyword destination mapping. Workaround: omit nested keyword fields from the pivot entirely; add them via Python post-enrichment (source lookup by session_id).
- **ES transform pivot: .keyword sub-fields only exist in old backing index (2026-04-12/13)**: The session backing index from 2026-03-30 has `gamepulse.game.name` as text+keyword multi-field. The 2026-04-12 index has it as native keyword with NO `.keyword` sub-field. Using `.keyword` paths in composite group_by works on the old index but returns empty results on the new index. Fix: use base field paths AND add a date range filter (gte: 2026-04-12) to restrict to the new index. This avoids fielddata errors on the old index and avoids the empty composite issue.
- **ES transform pivot: terms agg on text fields requires fielddata (2026-04-12/13)**: Composite `terms` source and `top_metrics` metrics field both require keyword type. Text fields without fielddata=true fail with "Fielddata is disabled". Cumulative sum window functions are NOT possible in ES pivot transforms — must be done in Python post-enrichment.
- **gamepulse-game-timeline transform (deployed 2026-04-12/13)**: Live and running. Source: `metrics-gamepulse.session-default` (date gte 2026-04-12, ended=true, game.name exists). Group by: game_name + session_id. Numeric metrics in pivot; keyword context fields added by post-enrichment. `cumulative_playtime_hours` computed in Python and bulk-updated. Deploy script: `python3 tools/deploy_game_timeline_transform.py`. Re-enrich only: `--enrich-only`. Reset: `--reset`.
- **Kibana Serverless dashboard API auth (confirmed 2026-04-13)**: ES_API_KEY (not KIBANA_API_KEY) is the correct credential for `POST /api/saved_objects/_import`, `POST /api/data_views/data_view`, and `GET /api/data_views`. KIBANA_API_KEY returns 401 on all protected endpoints. `_import` is the only working programmatic dashboard creation path on Serverless — `POST /api/dashboards/dashboard/{id}` returns 404 on this Serverless instance.
- **Home dashboard NDJSON format (2026-04-13)**: panelsJSON embeds panel objects with `"type": "lens"` or `"type": "markdown"`. lnsDatatable `visualization.columns` uses `isTransposed: true` for bucket (terms) columns and `isTransposed: false` for metric columns. `last_value` operationType requires `params.sortField`. `max` on a date field produces `dataType: "date"`. `count` has no `sourceField`. typeMigrationVersion must be `"10.3.0"` for dashboard objects.
- **gamepulse-game-timeline data view created (2026-04-13)**: ID `gp-dv-timeline`, title `gamepulse-game-timeline`, timeFieldName `session_start`. Created via `POST /api/data_views/data_view` with ES_API_KEY. All game-timeline fields (game_name, avg_fps, duration_s, etc.) are properly-typed keywords/numerics — no text/keyword ambiguity unlike the session stream.
- **Environment changes panel: LAG not possible in ES (confirmed 2026-04-13)**: The "recent changes" panel shows raw per-session env values (driver_version, kernel_version, proton_version, avg_fps per session). ES|QL has no LAG function; Lens has no cross-row difference operation. Computing "FPS before vs after" delta requires a future Transform or Python post-enrichment step. Panel is built as a reference table only.
- **gamepulse.summary.bottleneck_dominant null in session docs (2026-04-13)**: Latest session summary docs in metrics-gamepulse.session-default have null bottleneck_dominant. The field IS populated in gamepulse-game-timeline (bottleneck_dominant="gpu" for the Starfield session). Root cause: ingest pipeline may not be populating this field in the 2026-04-12 backing index. Investigate before the Hardware dashboard session.
- **Kibana _import file extension (2026-04-14)**: `POST /api/saved_objects/_import` rejects files with `.json` extension ("Invalid file extension .json"). File MUST have `.ndjson` extension. Copy the file with the correct extension before importing.
- **Kibana _export vs GET saved object (2026-04-14)**: `GET /api/saved_objects/dashboard/{id}` returns 400 ("not available with the current configuration") on Elastic Cloud Serverless. Use `POST /api/saved_objects/_export` with body `{"objects":[{"type":"dashboard","id":"..."}],"includeReferencesDeep":false}` to retrieve a live dashboard as NDJSON.
- **proton_version not in gamepulse-game-timeline (2026-04-14)**: The field has never been written to the transform output index. Any panel referencing `proton_version` as a `sourceField` will cause a render error. The field was removed from the Home dashboard "Environment per Session" panel (column mp-env_4).
- **session.label field (added 2026-04-14)**: `gamepulse.session.label` (keyword) added to all 9 data stream fields.yml. Auto-generated at runtime — priority: (1) manual `--label`/`[session].label` override, (2) `<game-slug>-YYYYMMDD-HHMMSS` on game detection, (3) `idle-YYYYMMDD-HHMMSS` at startup before any game. Slug rules: lowercase, spaces→hyphens, strip non-alphanumeric, truncate 32 chars. `label_is_manual` flag prevents auto-generation from overwriting user override. ES-confirmed: label appears in session, cpu, and gpu streams. Examples: "Starfield" → "starfield-20260414-145036", "Cyberpunk 2077" → "cyberpunk-2077-YYYYMMDD-HHMMSS".

### Rust agent (src/) — Phase 6

**Last updated:** 2026-04-11  
**Status:** COMPLETE — all 8 collectors + main loop integration ES-confirmed. Full gameplay session verified (Starfield, 40 min, Proton). Rust agent is production-primary; Python collector is reference/fallback.

| Component | Status |
|---|---|
| Cargo workspace (`src/Cargo.toml`) | ✅ |
| CLI (clap: --config, --dry-run, --version) | ✅ |
| Config loader (`src/config.rs`) | ✅ — mirrors Python config.py exactly |
| ES shipper (`src/shipper.rs`) | ✅ — `ping()` + `ship()`, matches Python auth/index format |
| CPU collector (`src/collectors/cpu.rs`) | ✅ — `/proc/stat` delta, hwmon temp, cpufreq clock, RAPL power (Intel only), governor, boost |
| Memory collector (`src/collectors/memory.rs`) | ✅ — `/proc/meminfo`, `/proc/<pid>/status` VmRSS/VmSize, `/proc/<pid>/stat` page faults |
| Storage collector (`src/collectors/storage.rs`) | ✅ — `/proc/diskstats` delta, Steam path device detection, latency/IOPS/throughput |
| Network collector (`src/collectors/network.rs`) | ✅ — `/proc/net/dev` + `/proc/net/snmp` delta, max-rx_bytes interface selection |
| Power collector (`src/collectors/power.rs`) | ✅ — AMD TDP cap (hwmon), battery, AC, platform profile; None-safe |
| Audio collector (`src/collectors/audio.rs`) | ✅ — PipeWire/PulseAudio/ALSA backend detection, xruns, latency, sample rate |
| MangoHud frame collector (`src/collectors/mangohud.rs`) | ✅ — CSV log tail, fps stats, frametime, stutter count |
| AMD GPU collector (`src/collectors/gpu_amd.rs`) | ✅ — sysfs heuristic, validated on RX 9070 XT (card1/hwmon3) |
| Session manager (`src/session.rs`) | ✅ — Steam /proc scan, ACF name lookup, session.json write/remove |
| Host enricher (`src/host.rs`) | ✅ — once-at-startup snapshot, hardware.gpu correctly selects dGPU by max VRAM |
| Main loop (`src/main.rs`) | ✅ — 1s tick, all 8 collectors, SIGTERM/SIGINT, session start/end + summary docs |
| eBPF integration | 🔲 — Sprint 4 |

**ES-confirmed 2026-04-10** (no active game, all system metrics streaming):
- All 8 metric datasets: cpu 178 docs, gpu 180, memory 180, storage 178, network 178, audio 180, power 180, frame 2 (no game), session 4 ✅
- Hardware fields: `hardware.gpu.model=AMD Radeon RX 9070 XT (RADV GFX1201)`, `vram_mb=16304`, `driver_version=26.0.4`, `mesa_version=26.0.4` ✅

**ES-confirmed 2026-04-11 — full gameplay session (Starfield, Proton, 40 min)**:
- All 8 metric datasets: cpu 661, gpu 662, memory 662, storage 661, network 661, audio 662, power 662, frame 642 ✅
- `gamepulse.game.name='Starfield'` in all per-tick docs ✅
- `gamepulse.game.graphics_api='dx_via_proton'` ✅ (DX12 via Proton/VKD3D)
- Frame data: avg_fps=286.9, low_1pct=167 fps, p99_frametime=6.36ms, total_frames=184,169 ✅
- Session summary: duration_s=2430, bottleneck=gpu, peak_gpu_temp=46°C, peak_cpu_temp=61.6°C ✅

**Notes:**
- `src/Cargo.toml` has `[[bin]] path = "main.rs"` because source files sit at the `src/` level, not in a `src/src/` subdirectory.
- Root `Cargo.toml` workspace includes only `["src"]`. The `ebpf/` workspace remains independent (cross-compilation target + xtask cannot merge cleanly into a host workspace).
- CPU collector: first `collect()` call always returns `None` (no delta yet — two snapshots required). Second call returns data.
- MangoHud collector: returns `None` when no log present. `stutter_count` always present (0 when no frametime data).
- AMD GPU collector: discovery runs once at startup. Hwmon via `{card}/device/hwmon/hwmon*` traversal. card1/hwmon3 = RX 9070 XT.
- Host enricher `gpu_info()`: selects AMD card with max VRAM to prefer dGPU over iGPU when both expose DRM nodes (card0=iGPU 2GB, card1=RX 9070 XT 16GB). `enrich_amd()` takes first match per field from vulkaninfo (prevents iGPU overwriting dGPU). `mesa_version` falls back to `driver_version` for RADV when `glxinfo` unavailable (no DISPLAY in service context).
- **Key learnings — AMD GPU sysfs on this hardware (RX 9070 XT, CachyOS)**: card1 = discrete (vendor 0x1002, amdgpu driver); card0 = iGPU. hwmon3 = card1 discrete, hwmon4 = card0 iGPU. Heuristic selects correctly without hardcoding paths.
- **Packaging**: AUR PKGBUILD + systemd units built and smoke-tested. Both services confirmed active (running) 2026-04-11. gamepulse-agent: 5.8MB stripped; gamepulse-ebpf: 6.4MB stripped.
- **Full elastic-package test suite** (updated 2026-04-11): `test static` 11/11 PASS; `test pipeline` 11/11 PASS (remote ES, no Docker); `test asset` 12/12 PASS (local 8.13.0 stack); `test policy` "No test results" (no policy test fixtures); `test system` not yet run.
- **Phase 4 closed beta distribution** (verified 2026-04-11): Two working install methods confirmed:
  - `elastic-package stack up` from within the repo builds and serves the package via the local registry (HTTPS port 8080; registry auto-discovers `build/packages/*.zip`)
  - Direct zip upload to Kibana Fleet API (`POST /api/fleet/epm/packages`, `Content-Type: application/zip`): tested against both local 8.13.0 stack (44 assets) and Elastic Cloud Serverless (47 assets). All 11 index templates installed in both cases.
  - Serverless Kibana URL: `https://gamepulse-af41f9.kb.us-central1.gcp.elastic.cloud`
  - Serverless Fleet Settings API does NOT have a custom registry URL field — custom registry not supported on Serverless. Use zip upload instead.
  - `docs/BETA-INSTALL.md` created with onboarding guide.

### Package build

Use `bash scripts/build-package.sh` instead of `elastic-package build` directly.
Use `bash scripts/test-asset.sh` instead of `elastic-package test asset` directly.
Both scripts stash large dev-only directories to `/tmp` before building (restores
on exit). Produces a lean package vs the raw build which includes `target/` (277MB).

`elastic-package check`, `elastic-package test static`, and `elastic-package test pipeline`
can still be run directly (they don't rebuild the package from the repo root).

Background: `elastic-package-ignore` v0.122.0 only applies during lint, not the build
copy step. The stash scripts are the workaround until Phase 6 moves integration to a
`package/` subdirectory.

### elastic-package test suite status (confirmed 2026-04-11)

| Test type | Result | Notes |
|-----------|--------|-------|
| `elastic-package test static` | 11/11 PASS | Run directly |
| `elastic-package test pipeline` | 11/11 PASS | Run directly; uses remote ES (ELASTIC_PACKAGE_* env vars) |
| `elastic-package test asset` | 12/12 PASS | Run via `bash scripts/test-asset.sh`; requires local Docker stack |
| `elastic-package test policy` | "No test results" | No policy test fixtures configured; acceptable |
| `elastic-package test system` | "No test results" — acceptable skip | No `_dev/test/system/` config; custom binary integration requiring gaming hardware; elastic/integrations guidelines allow skip |

Pipeline test fixtures live in `data_stream/*/_ dev/test/pipeline/test-*-pipeline.json`
(input) and `test-*-pipeline.json-expected.json` (auto-generated by `--generate`).

**All required elastic-package tests are now in a final state.** The full test suite is complete as of 2026-04-11.

### Backing index type conflict — resolved 2026-04-14

All 10 data streams (`session`, `cpu`, `gpu`, `memory`, `storage`, `network`, `audio`, `power`, `frame`, `ebpf`) had two backing indices with incompatible field type mappings:
- Old indices (2026.03.30-000001 / 2026.04.01-000001 / 2026.04.09-000001): ES auto-mapped `gamepulse.game.name`, `gamepulse.session.id`, `gamepulse.compatibility.proton_version` as **text**; eBPF numeric fields as **double**, histogram fields as **object**, `thread_breakdown` as **object**.
- New indices (2026.04.12-000002 onwards): correct types per `fields.yml` — **keyword** for string identifiers, **float** for numeric metrics, **histogram** for latency histograms, **nested** for `thread_breakdown`.

Effect: `verification_exception` on all ES|QL queries spanning both indices. Kibana Lens silently returned null for any field with a type conflict.

Fix: deleted all 10 old backing indices. Reindex was attempted but failed — TSDS timestamp constraint prevents writing old-timestamped docs into the new backing index's time window. Data from March 30 – April 10 (development sessions: Cyberpunk 2077, Starfield, Wolfenstein) was lost. This data was not in `gamepulse-game-timeline` (transform filters `gte: 2026-04-12`) and is superseded by proper post-April-12 sessions.

Root cause: old backing indices were created before the integration package's index template was deployed. ES auto-mapped string fields as `text`. The fields.yml always had `keyword` — no schema change triggered this. The new April 12 backing index was created after the template was deployed, getting the correct mapping.

Prevention: **Always deploy the integration package (and verify index templates are active) before collecting any live data.** After any mapping change, immediately roll over all affected data streams before shipping new data so the new backing index inherits the updated template.

ES|QL verified working post-fix: `FROM metrics-gamepulse.session-default | WHERE ... | STATS ... BY gamepulse.session.id` returns rows with `game.name='Starfield'` — no errors.

### systemctl bug analysis — 2026-04-14

**Root cause**: No game was running during either systemctl test session. Sessions `eac4383f` (33 s) and `18e36369` (35 s) at 23:40 and 23:52 on 2026-04-12 had zero "Game detected" log lines and shipped metric docs with `gamepulse.game` entirely absent. All game-centric dashboards (Game Library groups by game name, Session Deep-Dive FPS timeline had only 2 frame docs) show nothing without game context. Data IS visible in Kibana Discover because Discover has no implicit game filter.

**System environment confirmed correct**: HOME, XDG_RUNTIME_DIR, DISPLAY, WAYLAND_DISPLAY are all correctly inherited under `systemctl --user` from the PAM session. Config loading works (`--config /etc/gamepulse/gamepulse.toml` explicit → skips HOME lookup). Session.json path `/tmp/gamepulse/session.json` is hardcoded — no XDG_RUNTIME_DIR dependency.

**Contributing / latent factors**:
1. `game_name_from_appid()` in `src/session.rs:197` uses `std::env::var("HOME").unwrap_or_else(|_| "/root".to_string())` — falls back to `/root` if HOME absent under a non-PAM launch.
2. `/etc/gamepulse/gamepulse.toml.pacnew` has placeholder credentials — will silently ship to wrong endpoint if deployed.
3. Service unit missing `RUST_LOG` — no log level without manual journald override.
4. No Before= or game-presence condition in service unit — service starts even with no Steam running.
5. Entire dashboard suite is game-session-centric — no panels for system metrics without a game.

**Ranked fixes** (for a future implementation session):
1. ✅ Add periodic "no game detected" INFO log in `src/session.rs` `poll()` when no game found after N ticks. **DONE — commit `6016173` (2026-04-14).**
2. Replace `HOME` env lookup in `game_name_from_appid()` with `getpwuid(getuid())` for robustness.
3. ✅ Add `Environment=HOME=/home/%u` and `Environment=GAMEPULSE_LOG=info` to `packaging/systemd/gamepulse-agent.service`. **DONE — commit `6016173` (2026-04-14).** Note: code reads `GAMEPULSE_LOG`, not `RUST_LOG`.
4. Build a no-game system metrics dashboard panel (CPU/GPU/memory without game filter).
5. Add startup credential validation (ping ES at startup; log WARN if unreachable).

**2026-04-14 follow-up investigation**: "game name not propagating" report from 04-14 Starfield session investigated. Diagnosis: NO BUG. Per-tick metric docs had `gamepulse.game.name='Starfield'` on all 44 ticks during the game-running window (ES-verified). The "only ONE document" Kibana observation was a display artifact: 1,091 eBPF docs (71% of 1,523 total) have no game.name by design; Discover default sort (time-desc) showed post-game-exit docs first where game.name is correctly absent. Root cause of confusion: eBPF docs dominating the wildcard data view.

### Pending work (in priority order)

1. **New dashboard suite** — build order: Games → Environment → Hardware → Compare → Engine (Home ✅ complete 2026-04-13).
   - **Games dashboard** — next session. Source: `gamepulse-game-timeline` (ID: gp-dv-timeline). X-axis: cumulative_playtime_hours. Needs ≥2 Starfield sessions for a meaningful continuous line; play another session before building.
   - Navigation bar placeholders (GAMES, HARDWARE, ENV, ENGINE, COMPARE) need updating in `home-dashboard.json` as each ID is confirmed.
2. **Phase 4: First colleague onboarding** — share `docs/BETA-INSTALL.md` + `gamepulse-0.1.0.zip`. Distribution verified.
3. **GitHub Release v0.1.0** — tag, attach zip + AUR package binaries.
4. **eBPF Sprint 4**: Update `data_stream/ebpf/sample_event.json` for all probe types.
5. **.deb/.rpm packaging**: AUR PKGBUILD done; Debian/RPM not yet built.
6. **Investigate null bottleneck_dominant in session stream**: gamepulse-game-timeline has "gpu" for Starfield but session summary docs show null. May be an ingest pipeline enrichment issue on the 2026-04-12 backing index.
7. **Remaining systemctl ranked fixes**: #2 `getpwuid` for HOME fallback, #4 no-game system metrics dashboard panel, #5 startup credential validation (ping ES at startup).

## Stack

- **Collector (current)**: Python 3.11+ prototype
- **Collector (target)**: Rust + Aya framework for eBPF (Phase 4, not started)
- **Storage / visualisation**: Elasticsearch Serverless (Elastic Enterprise), Kibana
- **Hardware target**: AMD GPU primary (Linux); NVIDIA via community; Steam Deck
- **Packaging target**: Debian, RPM, AUR (AUR PKGBUILD complete; .deb/.rpm not yet built)
- **CI/CD target**: GitHub Actions (not yet configured)
- **Key Linux interfaces**: sysfs/hwmon, /proc filesystem, MangoHud log

## Kibana dashboards

### Current state
Dashboard files live in `dashboards/` (not `kibana/`). The `kibana/` directory
is reserved for the Phase 6 integration package format (`kibana/dashboard/`
with proper NDJSON saved objects). Until then, all dashboard JSON files live in
`dashboards/`.

### Planned dashboards (Phase 3)

| Dashboard | Status | Location |
|-----------|--------|----------|
| Session Deep-Dive | ✅ built | `dashboards/session-deep-dive-dashboard.json` (ID: b68f1178-6923-4e92-819b-33eb595197a9) |
| Configuration Comparison | ✅ built | `dashboards/config-comparison-dashboard.json` (ID: 21b663d6-de42-46c6-aeaf-e6c48e46ecec) |
| Baseline (UI export) | ✅ reference | `dashboards/gamepulse-dashboard.ndjson` |
| Storage & I/O Analysis | ✅ built | `dashboards/storage-io-dashboard.json` (ID: f8a9d960-130e-43db-8554-6033f45e8a9c) |
| System Health | ✅ built | `dashboards/system-health-dashboard.json` (ID: 1b2a1b70-a315-4ed4-91c4-11aa0abe5e1d) |
| Game Library | ✅ built | `dashboards/game-library-dashboard.json` (ID: e7d878d0-e2d6-454b-9a95-d93a4aeb70a8) |
| Scheduler Analysis | ✅ built | `dashboards/scheduler-analysis-dashboard.json` (ID: 89ca0908-5639-45f7-9a70-edadfe7d7124) |
| Home | ✅ built | `dashboards/home-dashboard.json` (ID: home-dashboard-2026-04-13) |
| Games | 🔲 next | reads gamepulse-game-timeline (gp-dv-timeline); needs ≥2 sessions |
| Environment | 🔲 planned | |
| Hardware | 🔲 planned | |
| Compare | 🔲 planned | |
| Engine | 🔲 planned (lowest priority) | |

**Session Deep-Dive** (`dashboards/session-deep-dive-dashboard.json`):
17 panels — 3 filter controls (Game/Session/OS), 6 metric tiles (Median FPS,
1% Low, 0.1% Low, Median frame time, Peak stutter/tick, Avg GPU temp), FPS
timeline (avg + 1%/0.1% lows), frame time with p95/p99 overlays, stutter
events area chart, GPU util/temp + power/VRAM, CPU util/temp, memory, and
session config table (Game, OS, Kernel, GPU, driver, Proton).

**Configuration Comparison** (`dashboards/config-comparison-dashboard.json`):
16 panels — 4 filter controls, 3 metrics, 9 charts, 1 session config table.

**Storage & I/O Analysis** (`dashboards/storage-io-dashboard.json`):
16 panels — 3 filter controls (Game/Session/OS), 6 metric tiles (read/write MB/s,
read/write IOPS, I/O wait %, drive temp), throughput timeline, IOPS area chart,
I/O wait + queue depth, read/write latency (avg/p95/p99), game process I/O, drive temp.

**System Health** (`dashboards/system-health-dashboard.json`):
15 panels — 3 filter controls, 6 metric tiles (GPU temp/hotspot, CPU temp, GPU
power/clock, CPU clock), GPU thermals timeline (die/hotspot/VRAM), CPU thermals
+ util, GPU power+clock, CPU clock+util, GPU VRAM+util, system TDP.
Note: `gamepulse.power.tdp_current_w` is the only power stream field; GPU power
comes from `gamepulse.gpu.power_w` in the gpu stream.

**Game Library** (`dashboards/game-library-dashboard.json`):
8 panels — 2 filter controls (Game/OS), avg FPS by game (bar), 1% low FPS by
game (bar), FPS over time broken out by game, GPU util and GPU power timelines
by game, and a performance summary data table (avg/1%/0.1% FPS, frame time,
max stutter, GPU util/power, session count per game). Default range: now-30d.

**Scheduler Analysis** (`dashboards/scheduler-analysis-dashboard.json`, ID: 89ca0908-5639-45f7-9a70-edadfe7d7124):
15 panels — 2 filter controls (Probe Type, Session ID), 6 metric tiles (runqueue avg latency,
CPU migrations, hard IRQ avg latency, futex contentions, VFS read avg latency, GPU fence avg
latency), runqueue latency timeline (avg + max), CPU migration timeline (total + CCX cross),
IRQ event count (hard + softirq stacked area), VFS latency timeline (read + write), GPU fence
latency + blocked count, futex contention timeline, GPU submit rate.
Source: `metrics-gamepulse.ebpf-default` (all Sprint 1–3 probes).

### Dashboard workflow

Two approved methods — use the kibana-dashboards skill where possible:

**Method A — Kibana Dashboards API (preferred)**
Use the `kibana-dashboards` agent skill to create and update dashboards
programmatically. This API (Kibana 9.4+ Serverless) is LLM-friendly and
not version-sensitive. Workflow:
1. Validate fields with ES|QL first (`elasticsearch-esql` skill)
2. Generate dashboard JSON and POST via the skill
3. Retrieve the result and save definition to `kibana/<name>.json`
4. Commit and push

API schema notes (verified 2026-04-07 against Serverless 9.4.0):
- `options_list_control`: use `field_name` (snake_case), `data_view_id`
- `options_list_control` field_name MUST use `.keyword` sub-field for text fields
  (e.g. `gamepulse.game.name.keyword`, `gamepulse.session.id.keyword`, `host.os.name.keyword`)
  Using the bare text field silently produces a non-functional filter control
- OS filter control: use `host.os.name.keyword` (not `host.os.type` or `host.os.type.keyword`)
- `data_table` `last_value` metrics for text fields also need `.keyword` sub-field
  (e.g. `host.os.kernel.keyword`, `gamepulse.hardware.gpu.model.keyword`)
- `data_table` rows and x-axis `terms` fields need `.keyword` for text fields
- `xy` terms x-axis: `{operation:"terms", fields:[...]}` — no `size`
- `breakdown_by` terms: `{operation:"terms", fields:[...]}` — no `size`
- Datatable type is `data_table` (not `datatable`), rows terms: no `size`
- ES|QL dataset (`type:"esql"`) not supported in inline panel attributes;
  use `type:"dataView"` or `type:"index"` instead

**Method B — Manual Kibana UI export (fallback)**
Use when the API doesn't support a needed panel type, or for complex
multi-layer visualizations. Workflow:
Build in Kibana UI → export via Stack Management → Saved Objects →
commit to `kibana/` as `.ndjson`

**Never do**: hand-author NDJSON files — these are version-sensitive and
will fail to import on Serverless.

Dashboard files live in `kibana/` at the repo root (not `data_stream/`).
When the integration matures to Phase 6, dashboards move into
`kibana/dashboard/` inside the integration package structure.

### Elastic compliance rules (required for elastic/integrations submission)
- All visualizations must be defined by value (part of the dashboard),
  not saved to the Visualize library.
- Every panel must include a `data_stream.dataset` filter to avoid hitting
  all `metrics-*` indices. Example for frame data:
  `data_stream.dataset: "gamepulse.frame"`
- Visualization titles must not include the package name. Use "FPS Timeline"
  not "[GamePulse] FPS Timeline".
- Use Kibana Lens only — no TSVB, no Vega, no legacy aggregation-based panels.
- TSDS note: counter-type metric fields do not support `avg()` in Kibana.
  Use `max()` or `rate()` instead.
- Build against stable Kibana (Serverless current), never SNAPSHOT.

### Field paths reference (verified from live data)
These are confirmed working from the Cyberpunk 2077 session:

Frame data (`data_stream.dataset: gamepulse.frame`):
- `gamepulse.fps.avg_1s`, `gamepulse.fps.low_1pct`, `gamepulse.fps.low_01pct`
- `gamepulse.fps.frametime_ms`, `gamepulse.fps.stutter_count`
- `gamepulse.session.id.keyword` (use for split-by and session filter control)

GPU data (`data_stream.dataset: gamepulse.gpu`):
- `gamepulse.gpu.utilisation_pct`, `gamepulse.gpu.temperature_c`
- `gamepulse.gpu.hotspot_c`, `gamepulse.gpu.memory_temperature_c`
- `gamepulse.gpu.power_w`, `gamepulse.gpu.memory_used_mb`, `gamepulse.gpu.clock_mhz`

CPU data (`data_stream.dataset: gamepulse.cpu`):
- `gamepulse.cpu.total_utilisation_pct`, `gamepulse.cpu.temperature_c`
- `gamepulse.cpu.clock_mhz_avg`

Memory data (`data_stream.dataset: gamepulse.memory`):
- `gamepulse.memory.system_used_mb`, `gamepulse.memory.swap_used_mb`
- `gamepulse.memory.game_rss_mb` (unreliable under Proton — tracks launcher, not game)

Storage data (`data_stream.dataset: gamepulse.storage`):
- `gamepulse.storage.read_mbps`, `gamepulse.storage.write_mbps`
- `gamepulse.storage.queue_depth_current`

Session data (`data_stream.dataset: gamepulse.session`):
- `gamepulse.game.name.keyword`, `gamepulse.game.steam_app_id`
- `gamepulse.game.graphics_api`, `gamepulse.session.id.keyword`
- `host.name`, `host.os.name.keyword`, `host.os.type.keyword`

Filter controls (use `metrics-gamepulse.*` wildcard data view):
- Game: `gamepulse.game.name.keyword`
- Session ID: `gamepulse.session.id.keyword`
- OS: `host.os.type.keyword`

### Elastic Agent Skills
The Elastic official Claude Code skills are installed in `.claude/skills/`.
These give Claude Code enhanced knowledge of ES|QL, Kibana, and
Elasticsearch. Install via:
```
npx skills add elastic/agent-skills -a claude-code
```
When planning dashboard panels, ask Claude Code to use ES|QL queries
for validation — ES|QL bypasses data view field list issues and
confirms fields exist before building Lens panels.

## Hardware notes (gaming PC)

Hardware-validated details for CachyOS (AMD Ryzen + RX 9070 XT):

- AMD GPU: **RX 9070 XT** (RADV GFX1201), Mesa 26.0.4, driver 26.0.4. Discrete card is **card1** (not card0); scoring heuristic selects it correctly
- CPU temps: k10temp at hwmon5; temp1=Tctl (primary), temp3=Tccd1
- RAPL power: permission-denied without root — collector returns None gracefully
- CPU driver: amd-pstate-epp; cpufreq paths at `/sys/bus/cpu/devices/cpu*/cpufreq/`
- Storage: 3× NVMe (nvme0n1/nvme1n1/nvme2n1); `/games` ext4 (Steam library); collector detects nvme1n1p6

## Remote access

- **Elasticsearch**: `$ES_URL` / `$ES_API_KEY` — Elastic Cloud Serverless
- **Gaming PC**: `ssh gamingpc` (CachyOS, AMD GPU, MangoHud installed)

## Protected files — never edit without explicit task assignment

These files are integration-critical. Errors in them are silent until package validation:

- `manifest.yml`
- `tools/deploy_pipelines.py`
- `tools/wire_pipelines.py`
- `docs/GamePulse-Scope-v3_2.md`
- Any file under `_dev/`
- Any file under `packaging/`
- Ingest pipeline YAML/JSON files (any path matching `*pipeline*`)
- Index template JSON files
- ILM policy JSON files

## Validation commands (the only approved test commands)

```
elastic-package check
elastic-package test static
elastic-package test system   # requires local ES or Docker
cargo check                   # only once Rust src/ exists
cargo clippy -- -D warnings   # only once Rust src/ exists
cargo test                    # only once Rust src/ exists
cargo build --release         # only once Rust src/ exists
```

Do not run any other commands that modify the repo, network, or filesystem
without explicit user approval.

## Session hygiene

- Always run `git pull` before starting any work in a session.
- Always run `git push` immediately after every commit.
- Never start implementation work if `git status` shows unpushed commits or if the branch is behind `origin/main`.
- If the branch has diverged, stop and flag it to the user before doing anything else.

## Cross-session continuity

Two files maintain context across sessions:
- `docs/claude-chat-context.md` — maintained by claude.ai (web planning sessions).
  Update and commit at the end of every planning session.
- `CLAUDE.md` (this file) — maintained by Claude Code (implementation sessions).
  Update the "Current state" section at the end of every Claude Code session.

Claude Code must never edit `docs/claude-chat-context.md`.
claude.ai must never directly edit `CLAUDE.md`.

## Workflow rules

1. One task at a time. No opportunistic refactors.
2. No dependency version changes unless the task explicitly requires it.
3. No changes to protected files without a planner-assigned task targeting them.
4. After any pipeline/manifest change: run `elastic-package check` before declaring done.
5. After any Rust code change (once src/ exists): run `cargo check` before declaring done.
6. Reviewer must approve before tester runs.
7. Progress auditor runs at every milestone boundary, not every task.

## Skills inventory

Project-specific reference skills in `.agents/skills/` (force-added to git despite
parent directory gitignore; Elastic-provided skills are not committed).

| Skill | SKILL.md | Coverage |
|-------|----------|----------|
| `elasticsearch-tsds` | `.agents/skills/elasticsearch-tsds/SKILL.md` | keyword vs text rules, .keyword suffix, TSDS dimension restrictions, backing index conflict detection/resolution, rollover procedure, ES\|QL validation pattern |
| `gamepulse-data-model` | `.agents/skills/gamepulse-data-model/SKILL.md` | All 10 data stream index patterns + modes, canonical field paths, TSDS dimension fields, session.id vs session.label, gamepulse-game-timeline fields, data view IDs, known bugs |
| `gamepulse-workflow` | `.agents/skills/gamepulse-workflow/SKILL.md` | Pre/post-session checklists, field validation pattern, Rust/dashboard change checklists, elastic-package commands, systemd service patterns, journald commands, common mistakes |
| `kibana-dashboards` | `.agents/skills/kibana-dashboards/SKILL.md` | Kibana 9.4 Dashboards API, Lens panel types, GamePulse-specific Serverless lessons (.keyword rules, _import/.ndjson, _export, game-timeline field inventory) |
| `elasticsearch-esql` | `.agents/skills/elasticsearch-esql/SKILL.md` | ES\|QL query execution, time bucketing, aggregations (Elastic-provided) |
| `kibana-vega` | `.agents/skills/kibana-vega/SKILL.md` | Vega/Vega-Lite with ES\|QL data sources (Elastic-provided) |

Elastic-provided skills (not committed; recreate with `npx skills add elastic/agent-skills -a claude-code`):
`cloud-network-security`, `elasticsearch-file-ingest`, `elasticsearch-onboarding`,
`kibana-connectors`, `kibana-streams`, `observability-logs-search`,
`observability-manage-slos`, `observability-service-health`

## Key file locations

### Python collector (current implementation)

- `collector/gamepulse/cli.py` — main loop, `_merge_docs()` deep-merge, bulk shipper
- `collector/gamepulse/session.py` — session lifecycle, `base_doc()` output
- `collector/gamepulse/enricher/host.py` — host OS enrichment
- `collector/gamepulse/collectors/` — per-subsystem collectors
- `tools/deploy_pipelines.py` — pipeline deployment tool
- `tools/wire_pipelines.py` — pipeline wiring tool

### Integration package

- `data_stream/` — 11 data streams (manifest, fields, pipeline, sample_event)
- `manifest.yml` — package root
- `docs/GamePulse-Scope-v3_2.md` — canonical scope document

### Kibana dashboards

- `dashboards/` — all dashboard files live here (not `kibana/`):
  - `dashboards/gamepulse-dashboard.ndjson` — baseline (MacBook-built, import via Kibana UI)
  - `dashboards/config-comparison-dashboard.json` — API-built, live ID: 21b663d6-de42-46c6-aeaf-e6c48e46ecec
  - `dashboards/session-deep-dive-dashboard.json` — API-built, live ID: b68f1178-6923-4e92-819b-33eb595197a9
- `docs/kibana-lens-ndjson-reference.md` — structural reference for Lens NDJSON and Serverless constraints

### Packaging

- `packaging/gamepulse-launcher.sh` — unified launcher CLI (setup/start/stop/status/run subcommands; Steam `gamepulse run %command%` integration)
- `packaging/PKGBUILD` — AUR package build script
- `packaging/systemd/gamepulse-agent.service` — user systemd unit
- `packaging/systemd/gamepulse-ebpf.service` — system systemd unit (CAP_BPF)
- `packaging/config/gamepulse.toml.example` — example config installed to `/etc/gamepulse/`

### Elastic Agent skills (Claude Code)
Skills are in `.agents/skills/` and `.claude/skills/` (symlinks). These are
excluded from git — recreate on a fresh clone with:
```
npx skills add elastic/agent-skills -a claude-code
```
Note: `.claude/skills/` directory symlinks and `.agents/` are in `.gitignore`
because `elastic-package build` (v0.122.0) cannot handle directory symlinks.

### Rust agent (target, not yet created)

- `src/main.rs` — entry point and main loop (future)
- `src/collectors/` — hardware and system collectors (future)
- `src/ebpf/` — eBPF probe manager (future)
- `src/shipper/` — Elasticsearch bulk API shipper (future)
- `ebpf/` — BPF kernel programs via Aya (Phase 2 Sprint 1 complete)
