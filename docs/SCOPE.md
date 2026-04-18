**GamePulse**

An Elastic Agent Integration for Gaming Telemetry

Project Scope & Implementation Plan

Version 3.2 --- Depth-First, Integration-Native, Distribution-Ready

April 2026

github.com/MathewRJ/GamePulse

0\. What Changed in v3.2

Version 3.2 incorporates requirements from a systematic review against official Elastic integration sources (elastic.co/docs/extend/integrations, elastic/package-spec, elastic/integrations). These are not new features --- they are compliance requirements that the Elastic integrations team would flag in a PR review. All changes are folded into the relevant phases rather than kept as a separate checklist.

Key changes: (1) TSDS mode with dimension/metric_type/unit annotations required for all metric data streams. (2) Events data stream corrected from metrics- to logs- type. (3) Dashboard panels must be by-value with per-visualization data_stream.dataset filters. (4) format_version 3.0.0 and _dev/build/build.yml added to package scaffolding. (5) Testing requirements enumerated by type. (6) Field limit audit added. (7) Visualization title conventions specified.

1\. What Changed and Why

Version 3.0 makes two fundamental reorientations based on an honest
assessment of what GamePulse actually is and what it is for.

1.1 Reorientation #1: Depth Before Breadth

The v2.0 scope front-loaded dashboard polish and cross-platform breadth.
This made sense if the goal was a user-facing product. But GamePulse is
an insight engine. The core motivation is understanding why games
perform the way they do at a level deep enough to enable optimisations
that don't require spending more on hardware.

Depth of instrumentation matters more than visual presentation. Seeing
the scheduler migrate a render thread across CCX boundaries on Zen 4 and
correlating that with a 3ms frame time spike --- that is the value. A
polished Kibana dashboard showing average FPS is packaging, not
substance. eBPF deep telemetry moves from Phase 5 to Phase 2.

1.2 Reorientation #2: GamePulse IS an Elastic Agent Integration

The v2.0 scope treated the Elastic Agent integration as Phase 4
packaging work --- a distribution wrapper around a standalone collector.
This is backwards. GamePulse is an Elastic Agent integration that
happens to need a collector binary. The integration package is the
product; the collector is a component of it.

This matters because Elastic integrations have a specific package format
(manifest.yml, data stream definitions, field mappings, ingest
pipelines, dashboards, agent policy templates) that must conform to the
Elastic package specification. Building freestyle and retrofitting later
means reworking field names for ECS compliance, restructuring data
streams, rebuilding dashboards as bundled saved objects, and rewriting
ingest pipelines to match the integration format. That is a second
rewrite on top of the Rust rewrite.

Instead, we use the elastic-package CLI tooling from day one. The
repository is structured as an Elastic integration package. Every data
stream, field mapping, ingest pipeline, and dashboard is developed
within that structure. The collector binary is developed in parallel but
its output conforms to the integration's data model from the start.

1.3 Reorientation #3: Distribution as a First-Class Concern

The integration must be trivially easy to install. The distribution path
has two stages:

-   **Closed beta (internal):** Share the integration with colleagues
    and trusted testers to gather data from diverse systems, games, and
    real-world use before public release. This means either providing a
    pre-configured Elastic Agent policy or a self-hosted Elastic Package
    Registry that colleagues can point their Fleet instance at. The goal
    is maximum data diversity with minimum installation friction ---
    ideally a one-click "Add integration" in Fleet.

-   **Public release:** Contribute the integration to the official
    elastic/integrations repository via pull request. Once merged,
    GamePulse appears in the Fleet UI for every Elastic user worldwide
    --- alongside Nginx, Kubernetes, and AWS integrations. This is the
    primary distribution mechanism and the definition of "done" for the
    project.

Every architectural decision is evaluated against this distribution
goal. If something makes the integration harder to install, configure,
or maintain for an end user, it is the wrong choice.

1.4 What This Unlocks

An open question that drives this project: would enforcing strict
real-time scheduling help games in specific scenarios? This is
well-understood in industrial and audio contexts (PREEMPT_RT kernels,
SCHED_FIFO/SCHED_RR, CPU isolation, interrupt affinity) but barely
explored for gaming workloads. Answering this requires the eBPF
scheduler tracing, runqueue latency measurement, CPU migration tracking,
and IRQ latency profiling that we are now prioritising.

The eBPF collector is a custom Rust binary (using the Aya framework)
that runs on the gaming PC and is managed by Elastic Agent as part of
the GamePulse integration. It outputs structured metrics documents that
ship to Elastic Cloud Serverless like any other integration data --- no
special backend required. This keeps the architecture simple and the
integration easy to distribute.

Note: Elastic's Universal Profiling (which also uses eBPF) provides
complementary CPU flamegraph capabilities. GamePulse's custom probes are
gaming-specific (scheduler, GPU fences, shader compilation, I/O
patterns) and do not depend on Universal Profiling. If deeper CPU
profiling is needed in future, Universal Profiling can be added
alongside GamePulse as a separate integration.

2\. Vision

2.1 The Problem

There is no unified, open platform for collecting, comparing, and
analysing real-world gaming performance across hardware configurations,
operating systems, driver versions, Proton/Wine layers, and kernel
versions. Existing tools (MSI Afterburner, MangoHud, CapFrameX) are
local-only, siloed, and capture surface-level metrics. None of them can
tell you why a game stutters --- only that it did.

2.2 The Vision

A deeply instrumented Elastic integration that captures gaming
performance from userspace metrics all the way down to kernel scheduler
behaviour, enabling insights that are currently impossible:

-   **For the builder:** Deep understanding of how games interact with
    every layer of the stack. The kind of understanding that lets you
    optimise a Steam Deck to run a AAA title it has no business running
    smoothly.

-   **For developers and maintainers:** Actionable data showing exactly
    where performance is lost --- not just that FPS dropped, but that
    the scheduler migrated the render thread, the filesystem prefetch
    stalled, or futex contention in the audio thread blocked the main
    loop.

-   **For the community:** A dataset of gaming telemetry across diverse
    configurations, with a collaborative culture of sharing findings and
    discovering optimisations. Performance knowledge democratised, not
    locked behind expensive hardware.

-   **For the Elastic ecosystem:** The first integration package
    purpose-built for gaming workload observability. A demonstration
    that Elastic's platform extends beyond enterprise IT into enthusiast
    and developer communities.

-   **North star:** The techniques and infrastructure are
    domain-agnostic. Long-term, the same approach could profile and
    optimise any application. Gaming is the proving ground.

3\. Architecture

3.1 GamePulse as an Elastic Agent Integration Package

An Elastic integration package is a structured collection of assets that
defines how to observe a specific product or service with the Elastic
Stack. GamePulse is an integration package that defines how to observe
gaming workloads. When a user adds GamePulse in Fleet, Elastic Agent
automatically manages the collection binaries, ships data to
Elasticsearch, and installs dashboards. The user experience is: click
"Add integration" → configure endpoint → play games → see data.

The integration package contains:

-   **manifest.yml:** Package metadata, version, categories, Kibana
    version constraints, required subscription level.

-   **Data stream definitions:** One per metric category (frame, gpu,
    cpu, memory, storage, network, power, audio, session, ebpf, events),
    each with its own manifest, field mappings, and ingest pipeline.

-   **Field mappings (fields.yml):** ECS-compliant field definitions for
    every data stream. All custom fields under the gamepulse.\*
    namespace.

-   **Ingest pipelines:** Per-data-stream pipelines for enrichment,
    validation, and derived field calculation.

-   **Kibana dashboards:** Bundled as saved objects, installed
    automatically when the integration is added.

-   **Agent policy templates:** Pre-configured policies (Standard,
    Developer, Minimal) that users select in Fleet.

-   **Documentation:** README, screenshots, configuration reference ---
    all rendered in the Fleet UI.

The integration is developed using elastic-package, the official CLI
tooling. This ensures package-spec compliance, enables local testing
against a full Elastic Stack, and produces packages that can be
submitted to the elastic/integrations repository via pull request.

3.2 Collection Architecture

The collector runs on the user's gaming PC, managed by Elastic Agent.
Data ships to Elastic Cloud Serverless, which handles storage, scaling,
and retention automatically via data stream lifecycle (no manual ILM
configuration needed). Serverless optimises storage costs internally.

There are three tiers of data collection:

  -----------------------------------------------------------------------
  **Tier**     **What It Captures**    **How**                 **When**
  ------------ ----------------------- ----------------------- ----------
  Tier 1:      FPS, GPU/CPU            sysfs, procfs, hwmon,   Phase 1
  Surface      utilisation, temps,     MangoHud                
  Metrics      memory, storage                                 
               throughput                                      

  Tier 2:      Scheduler latency, I/O  eBPF via Aya (Rust)     Phase 2
  Kernel       tracing, page faults,                           
  Telemetry    futex contention, GPU                           
               fence waits, IRQ                                
               latency                                         

  Tier 3:      Bottleneck              Elasticsearch           Phase 3+
  Derived      classification,         transforms, ML, runtime 
  Insights     real-time scheduling    fields                  
               analysis, stutter root                          
               cause, regression                               
               detection                                       
  -----------------------------------------------------------------------

3.3 Development Strategy: Three Tracks

-   **Track 1 --- Integration package:** The elastic-package structure
    is scaffolded first. Data stream definitions, field mappings, ingest
    pipelines, and dashboards are developed within this structure from
    day one. This is the skeleton everything hangs on.

-   **Track 2 --- Python collector prototype:** Rapid prototyping for
    surface metrics (Tier 1). Gets real data flowing fast, validates the
    data model. Outputs conform to the integration's field mappings. 1/s
    collection frequency means Python's overhead is negligible.

-   **Track 3 --- Rust eBPF daemon:** Kernel-level telemetry (Tier 2).
    Custom gaming-specific eBPF probes built with Aya (Rust). Runs as a
    binary managed by Elastic Agent --- the integration's manifest
    defines how Agent starts, stops, and configures it. Outputs
    structured metrics to Serverless like any other integration.

-   **Track 4 --- Rust production agent:** Merges Tracks 2 and 3 into a
    single binary. Elastic Agent wraps it as a custom input. This is the
    production collector that ships with the integration.

The key insight: the integration package structure (field mappings, data
streams, pipelines, dashboards) is developed and validated from the
start. The collector language can change (Python → Rust) without
affecting the integration package, because the package defines the
contract and the collector implements it.

4\. Integration Package Structure

The repository is structured as an Elastic integration package, with the
collector source code alongside it:

**gamepulse/**

-   manifest.yml --- Package metadata (name, version, description,
    categories, icons). Specifies format_version: 3.0.0 (current
    package spec major version) and owner.type.

-   changelog.yml --- Package version history

-   docs/ --- README.md, screenshots (rendered in Fleet UI)

-   \_dev/ --- Development tooling:

    -   \_dev/build/build.yml --- ECS reference configuration (required
        for ECS field definitions to resolve during elastic-package
        build)

    -   \_dev/deploy/ --- Docker/service deploy configs for testing

    -   \_dev/test/ --- Test fixtures and configuration per test type

-   data_stream/ --- One subdirectory per data stream:

    -   data_stream/frame/ --- manifest.yml, fields/\*.yml,
        elasticsearch/ingest_pipeline/\*.yml

    -   data_stream/gpu/ --- same structure

    -   data_stream/cpu/, memory/, storage/, network/, power/, audio/,
        session/, ebpf/, events/

-   kibana/dashboard/ --- Bundled dashboard NDJSON files

-   kibana/search/ --- Saved searches

-   agent/input/ --- Agent input configuration templates

Alongside the integration package:

-   collector/ --- Python prototype collector source

-   agent-binary/ --- Rust production collector + eBPF source (Phases
    2+)

-   tools/ --- Synthetic data generator, Steam AppID resolver, config
    parsers

4.1 ECS Compliance

The Elastic Common Schema (ECS) defines standard field names across all
integrations. GamePulse uses ECS fields wherever they exist (host.os.\*,
host.cpu.\*, process.\*, etc.) and defines custom fields under the
gamepulse.\* namespace for gaming-specific metrics. This is a
requirement for inclusion in the official integrations repository.

4.2 Distribution Strategy: Closed Beta → Official Repository

Distribution has two stages, each with a different mechanism:

Stage 1: Closed Beta (Internal Testing)

Before public release, the integration is shared with colleagues and
trusted testers to gather diverse telemetry data. Two distribution
options:

-   **Option A --- Self-hosted Package Registry:** Run a local Elastic
    Package Registry (Docker container) that serves the built GamePulse
    package. Colleagues add the registry URL to their Fleet settings.
    The integration then appears in their Fleet UI just like any
    official integration. This is the cleanest experience.

-   **Option B --- Direct agent policy:** Provide a pre-configured
    Elastic Agent policy that includes the GamePulse integration.
    Colleagues import the policy and enrol their agents. Simpler to set
    up but less flexible for updates.

-   **Option C --- elastic-package install:** For colleagues with the
    elastic-package CLI, they can install the package directly into
    their cluster from the built package. Best for developers who want
    to test locally.

In all cases, the integration must pass elastic-package check and
elastic-package test before sharing. The closed beta targets 10--50
colleagues across diverse hardware/OS/game configurations.

Stage 2: Official Elastic Integrations Repository

The process for contributing to elastic/integrations:

1.  Fork the elastic/integrations repository.

2.  Add the gamepulse package to packages/ using elastic-package create
    (or copy the already-developed package).

3.  Ensure the package passes elastic-package check (format, lint,
    build) and elastic-package test.

4.  Write comprehensive documentation (README with screenshots,
    configuration reference, troubleshooting).

5.  Submit a pull request. Elastic's integrations team reviews for
    package-spec compliance, ECS adherence, documentation quality, and
    test coverage.

6.  Once merged, GamePulse appears in the Fleet UI for all Elastic users
    worldwide.

There are no technical barriers to community-contributed integrations
--- the repository explicitly invites external contributions. The bar is
quality: ECS compliance, working tests, good documentation, and
meaningful dashboards. Being an Elastic employee provides direct access
to the integrations team for guidance during the review process.

5\. Implementation Phases

Each phase builds on the previous. Depth before breadth, integration
structure from the start.

Phase 0: Elasticsearch Foundation --- COMPLETE

**Status:** Done. 12 component templates, 6 ingest pipelines, 11 index
templates deployed to Elastic Cloud Serverless. 31,505 synthetic
documents bulk-indexed across 7 data streams. Repository scaffolded at
github.com/MathewRJ/GamePulse.

**Backend:** Elastic Cloud Serverless (Enterprise). Stays as-is ---
Serverless manages storage optimisation, scaling, and retention
automatically via data stream lifecycle. No ILM configuration needed. If
cost optimisation for long-term data becomes a concern, a future pivot
to Hosted with ILM frozen tier is possible but not currently required.

**Retrofit needed:** The existing ES infrastructure was built before the
integration-first decision. Phase 0.5 (below) restructures it into a
proper integration package.

Phase 0.5: Integration Package Scaffolding (Week 1)

**Goal:** Restructure the repository as a proper Elastic integration
package using elastic-package tooling. Migrate existing component
templates, index templates, and ingest pipelines into the package
format. Establish TSDS mode, field annotations, and build configuration
from the start.

7.  Install elastic-package CLI and scaffold the package structure with
    elastic-package create package. Set format_version: 3.0.0 in
    manifest.yml.

8.  Create data stream definitions for all 11 categories using
    elastic-package create data-stream. Note: 10 metric data streams
    use type: metrics; the events data stream uses type: logs (see
    Section 6 --- discrete events are not periodic measurements).

9.  Migrate existing component templates into per-data-stream fields.yml
    files with ECS-compliant field names. Every metric field must
    include:

    -   time_series_metric: gauge or counter (gauge for values that go
        up and down like temperature/utilisation; counter for
        monotonically increasing values like total bytes read)

    -   unit: the field's unit (byte, ms, percent using 1 for 100%, nanos,
        s). Full list in the package spec.

    -   dimension: true on fields that identify the time series (e.g.
        host.name, gamepulse.session.id, gamepulse.game.steam_app_id,
        data_stream.dataset). Maximum 21 dimensions per data stream by
        default (adjustable via index.mapping.dimension_fields.limit).

10. Enable TSDS mode on all metric data streams by adding
    elasticsearch.index_mode: "time_series" to each metric data
    stream's manifest.yml. This gives storage savings of up to 70% and
    enables synthetic _source automatically (no separate synthetic
    source configuration needed). The events data stream (type: logs)
    is excluded from TSDS.

11. Migrate existing ingest pipelines into per-data-stream
    elasticsearch/ingest_pipeline/ directories.

12. Define agent policy templates (Standard, Developer, Minimal).

13. Create \_dev/build/build.yml with the ECS reference configuration
    so that ECS field definitions resolve correctly during
    elastic-package build.

14. Audit field count per data stream. Default total_fields.limit is
    1000 (including dynamically generated ECS fields). The package spec
    enforces fieldsPerDataStreamLimit of 2048. With the large metric
    inventory, verify no single stream exceeds the limit. Adjust
    index_template settings if needed.

15. Verify the package builds and passes elastic-package check.

16. Deploy to local Elastic Stack via elastic-package stack up and
    verify data streams are created correctly.

17. Update the synthetic data generator to use the new ECS-compliant
    field names and include the required dimension/metric_type/unit
    annotations.

This is a one-time migration cost. After this, all subsequent
development happens within the integration package structure.

Phase 1: Deep Linux Collector (Weeks 2--5)

**Goal:** Working Python collector on the CachyOS AMD desktop and Steam
Deck that captures all surface-level metrics during real gaming
sessions. Output conforms to the integration's field mappings.

1a. Core Framework

-   Abstract Collector base class with collect() → dict interface

-   Collector registry with platform-appropriate auto-discovery

-   1/s collection loop: gather from all collectors, batch, ship to ES

-   Elasticsearch bulk API shipper with local file buffer for resilience

-   TOML configuration (\~/.config/gamepulse/config.toml) with CLI
    overrides

-   Field name mapping layer: collector outputs map to integration field
    names (gamepulse.\* + ECS)

1b. Game Session Lifecycle

-   Monitor for game processes (Steam library scan + process matching)

-   Parse Steam libraryfolders.vdf for installed games

-   Detect Proton/Wine wrapper processes, resolve actual game binary

-   Create session document (UUID, timestamps, full environment
    snapshot)

-   Detect session end (process exit), finalise session

1c. Surface Metric Collectors

-   Frame timing: MangoHud log parsing, Gamescope stats socket

-   FPS calculations: avg, 1% low, 0.1% low, frame time percentiles,
    stutter count

-   GPU (AMD Linux): sysfs hwmon for utilisation, clocks, temps, power,
    fan, VRAM

-   CPU: /proc/stat (per-core utilisation), cpufreq (clocks), hwmon
    (temps), RAPL (power)

-   Memory: /proc/meminfo (system), /proc/{pid}/status (game RSS/VMS),
    swap

-   Storage: device ID via sysfs, filesystem via /proc/mounts, I/O via
    /proc/diskstats, per-process via /proc/{pid}/io

-   Network: /proc/net/dev for basic throughput and packet stats

1d. Environment Fingerprinting

-   OS, kernel, desktop environment (ECS host.os.\* fields)

-   GPU driver, Mesa version, Vulkan driver version

-   Proton/Wine/DXVK/VKD3D-Proton/Gamescope versions

-   Full hardware inventory (CPU, GPU, RAM, storage details)

1e. Steam Deck Specifics

-   Read-only filesystem awareness, Gamescope FSR/TDP detection

-   SD card detection and performance metrics

-   Low-overhead mode for battery-conscious operation

1f. Integration Package Validation

-   Real gaming session data flows through the integration's ingest
    pipelines

-   Verify field mappings match actual collector output

-   Update fields.yml as the data model evolves from real-world testing

-   Run elastic-package test to validate the integration end-to-end

**Deliverable:** Python collector producing real gaming session data
that conforms to the GamePulse integration package's data model.
Integration package passes elastic-package check with real data flowing.

Phase 2: eBPF Deep Telemetry (Weeks 5--9)

**Goal:** Kernel-level observability that answers WHY games perform the
way they do. This is the differentiator. Implemented as a custom
Rust/Aya eBPF binary, managed by Elastic Agent as a custom input within
the GamePulse integration. Outputs structured metrics to Elastic Cloud
Serverless --- no special backend needed.

This is not Elastic Universal Profiling --- it is a purpose-built set of
gaming-specific eBPF probes. From Elasticsearch's perspective, the
output is just metrics documents like any other integration data. The
binary requires CAP_BPF + CAP_PERFMON capabilities (not full root). Each
probe is independent --- if a kernel lacks a tracepoint, that probe is
skipped gracefully. Minimum kernel: 5.8 for BTF support.

2a. Scheduler Observer --- the real-time scheduling question

The probe that directly enables investigating whether strict real-time
scheduling helps gaming workloads.

-   **Attach:** sched/sched_switch, sched/sched_migrate_task,
    sched/sched_wakeup

-   **Output:** Per-thread runqueue wait time, CPU core affinity,
    cross-CCX/CCD migration frequency (AMD Zen), wakeup latency

-   **Use case:** \"Render thread migrating between CCXs on Zen 4 ---
    pinning eliminates 3ms spikes\" or \"SCHED_FIFO reduces frame time
    variance by 40%\"

2b. I/O Tracer

-   **Attach:** block/block_rq_issue, block/block_rq_complete, kprobe on
    vfs_read/vfs_write

-   **Output:** Per-file I/O latency, sizes, sequential vs random
    patterns, queue depth

-   **Use case:** \"Stutters correlate with 4KB random reads --- asset
    streaming thrashing on SD card\"

2c. GPU Fence/Sync Observer (AMD initially)

-   **Attach:** amdgpu driver tracepoints (amdgpu_cs_ioctl,
    dma_fence_wait)

-   **Output:** GPU fence wait times, command submission latencies

-   **Use case:** \"CPU waiting 4ms/frame for GPU fence --- engine sync
    bottleneck\"

2d. Memory Tracker

-   **Attach:** kmem/mm_page_alloc, kmem/mm_page_free, mm/page_fault
    tracepoints

-   **Output:** Page fault rate/type, allocation rate, memory pressure
    events

-   **Use case:** \"Frame drops correlate with major page faults ---
    working set exceeds RAM\"

2e. Futex/Lock Contention

-   **Attach:** syscalls/sys_enter_futex

-   **Output:** Lock wait time distribution, contention frequency by
    thread

-   **Use case:** \"Audio and render threads contending on same mutex\"

2f. IRQ/Softirq Latency

-   **Attach:** irq/irq_handler_entry/exit, irq/softirq_entry/exit

-   **Output:** Interrupt handling duration, frequency, device source

-   **Use case:** \"Network IRQ handler competing with render thread on
    same core\"

2g. Syscall Profiler

-   **Attach:** raw_syscalls/sys_enter, raw_syscalls/sys_exit

-   **Output:** Per-process syscall frequency histogram, latency
    distribution

-   **Use case:** \"This game makes 50,000 read() calls/s on tiny
    buffers --- game engine bug\"

2h. Shader Compilation Tracer

-   **Attach:** uprobe on Mesa's shader compiler entry/exit

-   **Output:** Compilation duration, pipeline hash, correlation with
    frame time

-   **Use case:** \"Stutter map showing when/where shader compiles cause
    frame drops\"

2i. Wine/Proton Overhead Profiler (stretch)

-   **Attach:** kprobe on ntdll syscall translation paths

-   **Output:** Translation overhead per syscall type

-   **Use case:** \"Proton adds 0.3ms/frame of syscall translation
    overhead\"

Integration Package Updates

-   Add ebpf data stream fields.yml with all eBPF-specific field
    definitions

-   Create ebpf ingest pipeline for histogram/distribution normalisation

-   eBPF data ships as structured documents to
    metrics-gamepulse.ebpf-default

-   Timeline-correlatable with surface metrics via session ID and
    timestamp

**Deliverable:** Rust eBPF binary producing kernel-level data in
Elasticsearch via the integration's data model. Scheduler, I/O, GPU
fence, and memory probes operational. Enough to begin investigating the
real-time scheduling question.

Phase 3: Investigation Dashboards (Weeks 7--11)

**Goal:** Dashboards that make all collected data explorable. Optimised
for investigation, not polish. Built within the integration package as
bundled saved objects that comply with Elastic's dashboard guidelines.

Dashboards are developed using Kibana Lens (the only supported
visualization type for new integrations), exported as NDJSON, and
placed in the integration's kibana/dashboard/ directory. They install
automatically when the integration is added in Fleet.

**Dashboard compliance requirements (per Elastic integration guidelines):**

-   All visualizations must be defined by value (part of the dashboard),
    not by reference (saved to the Visualize library). This makes
    dashboards fully self-contained and installable via a single request.
    Do not use Analytics > Visualize library.

-   Every visualization must include a data_stream.dataset filter (e.g.
    data_stream.dataset: "gamepulse.frame") to avoid querying all
    metrics-\* or logs-\* indices. Without this, panels hit every
    integration's data and cause performance issues.

-   Visualization titles must not include the package name. Use "FPS
    Timeline" not "[GamePulse] FPS Timeline". Remove unnecessary or
    repetitive titles when the information is already clear from the
    chart content.

-   Build dashboards against a stable, released Kibana version, never
    SNAPSHOT. Use margins between panels for visual separation.

-   Note on TSDS compatibility: with TSDS enabled, certain aggregation
    functions are not supported on counter-type metric fields (e.g.
    avg() on counters). Use max() or rate() instead. Verify all
    visualizations render correctly with TSDS-backed data streams.

Dashboard 1: Session Deep-Dive

\"I just played for 2 hours --- what happened under the hood?\"

-   FPS timeline with frame time percentiles (p95, p99) overlaid

-   Stutter events as annotations, linked to root cause (shader compile,
    I/O stall, scheduler migration, futex contention)

-   GPU + CPU utilisation dual-axis timeline

-   Scheduler view: per-thread runqueue wait, CPU core assignment over
    time, migration events

-   I/O view: read/write throughput, latency heatmap, file-level access
    patterns

-   Memory view: page fault timeline, allocation rate, swap pressure

-   GPU fence wait overlay against frame time

-   Environment badge bar: game, OS, kernel, GPU driver, Proton,
    filesystem, I/O scheduler

Dashboard 2: Scheduler Analysis

Purpose-built for the real-time scheduling investigation.

-   Runqueue latency distribution per thread (histogram)

-   CPU migration frequency and CCX/CCD boundary crossing events

-   Comparison view: same game with different scheduler policies

-   IRQ latency overlay: which interrupts compete with game threads

-   Core utilisation heatmap

Dashboard 3: Storage & I/O Analysis

-   Per-drive-type performance comparison (NVMe vs SD card vs SATA)

-   File access pattern visualisation: which files cause latency spikes

-   Filesystem comparison (btrfs zstd vs ext4)

-   I/O stall correlation with frame time spikes

Dashboard 4: Configuration Comparison

-   Filter by: game, GPU, driver, Proton, kernel, filesystem, I/O
    scheduler, scheduler policy

-   Side-by-side FPS distributions as histograms

-   Scheduler behaviour diff (migration rate, runqueue wait)

Dashboards 5--6: System Health + Game Library

-   Thermal headroom, power draw, clock speed correlation

-   Game × metrics heatmap with trend sparklines

Phase 4: Closed Beta & Internal Distribution (Weeks 11--14)

**Goal:** Distribute GamePulse to colleagues and trusted testers with
minimum friction. Gather data from diverse systems, games, and
real-world use. This is the critical validation step before public
release.

4a. Integration Quality Gate

-   Integration package passes elastic-package check (format, lint,
    build)

-   Integration package passes all elastic-package test types required
    for official repository acceptance:

    -   **Asset tests:** Verify that all Elasticsearch and Kibana assets
        (index templates, ingest pipelines, dashboards) load correctly
        when the integration is installed.

    -   **Pipeline tests:** Exercise ingest pipelines with sample input
        documents and verify expected output. Requires test fixtures in
        each data stream's \_dev/test/pipeline/ directory.

    -   **Static tests:** Validate that all fields in sample_event.json
        are documented in fields.yml. Catches undocumented fields.

    -   **System tests:** End-to-end validation that real data flows
        through the integration correctly --- from collection through
        ingest pipeline to indexed documents with correct mappings.

    -   **Policy tests:** Verify that agent policy templates render
        correctly and produce valid configurations.

-   All data streams produce correctly-mapped documents with real gaming
    session data

-   Bundled dashboards install cleanly and show meaningful data

-   Documentation: clear README with screenshots, configuration
    reference, known limitations

4b. Distribution for Colleagues

-   **Primary method --- Self-hosted Package Registry:** Run a local
    Elastic Package Registry (Docker container:
    docker.elastic.co/package-registry/package-registry) serving the
    built GamePulse package. Colleagues add the registry URL in Fleet
    settings under "Integrations". GamePulse then appears in their Fleet
    UI exactly like any official integration. One-click "Add
    integration", configure the Serverless endpoint, done.

-   **Alternative --- Direct package install:** For colleagues with
    elastic-package CLI access, they can install directly into their
    cluster via elastic-package install. Good for developers who want to
    iterate.

-   **Pre-configured Agent policy:** Provide an exportable Agent policy
    JSON that includes the GamePulse integration pre-configured for
    Elastic Cloud Serverless. Colleagues import the policy and enrol
    their agents.

4c. eBPF Binary Distribution

-   Pre-built static Rust binary for x86_64 and ARM64 Linux, available
    from GitHub Releases

-   The Elastic Agent integration manifest references the binary and
    manages its lifecycle (start, stop, health check)

-   Binary requires CAP_BPF + CAP_PERFMON --- documented clearly with
    setup instructions

-   AUR package for Arch/CachyOS users (covers the eBPF binary + Python
    collector)

4d. Data Collection & Privacy

-   Privacy tier selection during setup (Tiers 0--3)

-   Separate data stream namespace for community/beta data vs personal
    data

-   Rate limiting and validation pipeline for shared data

-   Clear documentation: what data is collected, how to verify, how to
    opt out

Target: 10--50 colleagues across diverse hardware (AMD/NVIDIA/Intel
GPUs, various CPUs), OS configurations (Windows, various Linux distros,
Steam Deck), and game libraries. The goal is breadth of configuration
data and feedback on the installation experience.

Phase 5: Windows & Cross-Platform (Weeks 13--17)

**Goal:** Windows surface metric parity. NVIDIA support. Cross-platform
comparison data.

-   Windows collector: PresentMon (frame timing), NVML/ADL (GPU), PDH
    (CPU), WMI (system)

-   ETW-based deep telemetry (partial eBPF equivalent): syscall tracing,
    disk I/O, DWM composition

-   NVIDIA support cross-platform via NVML (GTX 1080 Ti, RTX 2080)

-   Windows data flows through the same integration package data streams

-   Cross-platform comparison dashboards added to the integration

Phase 6: Rust Agent & Official Integration Submission (Weeks 17--23)

**Goal:** Production Rust binary merging all collectors. Submit
GamePulse to elastic/integrations.

-   Port all Python collectors to Rust (data model is stable, field
    mappings proven)

-   Merge eBPF daemon into same binary

-   Binary packaged for Elastic Agent to wrap: handles collection, the
    integration package handles everything else

-   Distribution: .deb, .rpm, Flatpak, AUR, Windows MSI, winget, Steam
    Deck Flatpak/Decky plugin

-   Full elastic-package test suite passing: asset, pipeline, static,
    system, and policy tests. These are mandatory for
    elastic/integrations PR acceptance.

-   Documentation meets elastic/integrations contribution standards

-   Fork elastic/integrations, add gamepulse to packages/, submit PR

-   Engage with Elastic's integrations team for review

Phase 7: Community Platform (Weeks 23+)

**Goal:** Public-facing community value built on top of the official
integration.

-   Polished community dashboards: aggregate views, hardware class
    comparisons

-   Regression detection via Elastic ML anomaly detection

-   GamePulse Score --- composite performance rating per game per
    hardware class

-   Proton compatibility matrix (quantified, not binary)

-   Contributor messaging/collaboration --- optimisation tips, edge-case
    investigations

-   Public website with embedded dashboards

-   API for third-party tools

-   Embeddable widgets for journalists/reviewers

6\. Data Model

Data streams follow the Elastic naming convention:
{type}-gamepulse.{dataset}-default. Metric data streams use type
metrics; the events data stream uses type logs (discrete events are not
periodic measurements and must not use the metrics type per the Elastic
data stream naming scheme). All fields use ECS where applicable, with
custom gaming metrics under the gamepulse.\* namespace.

**TSDS (Time Series Data Stream) mode:** All metric data streams (10 of
11) use TSDS with index_mode: time_series. This provides up to 70%
storage savings via columnar co-location, synthetic _source (no separate
configuration needed), and optimised aggregation queries. Each metric
field must declare time_series_metric (gauge or counter) and unit. Each
data stream must define dimension fields that uniquely identify the time
series. The events data stream (type: logs) is excluded from TSDS.

Storage management: Elastic Cloud Serverless uses data stream lifecycle
(not ILM) to manage retention and storage optimisation automatically. No
manual tier configuration (hot/warm/cold/frozen) is needed. Data stream
lifecycle settings (e.g. retention period) are configurable per data
stream in the integration package. If long-term storage costs become a
concern at scale, a future pivot to Elastic Cloud Hosted with ILM frozen
tier (searchable snapshots on object storage, up to 90% cost reduction)
is possible without changing the integration's data model.

  ----------------------------------------------------------------------------------
  **Data Stream**                     **Content**                  **Frequency**
  ----------------------------------- ---------------------------- -----------------
  metrics-gamepulse.frame-default     Frame timing (FPS,           1/s
                                      percentiles, stutter)        

  metrics-gamepulse.gpu-default       GPU utilisation, clocks,     1/s
                                      temps, power, VRAM           

  metrics-gamepulse.cpu-default       Per-core utilisation,        1/s
                                      clocks, temps, power         

  metrics-gamepulse.memory-default    System + process memory,     1/s
                                      swap, page faults            

  metrics-gamepulse.storage-default   Storage I/O throughput,      1/s
                                      IOPS, latency                

  metrics-gamepulse.network-default   Network throughput, packets, 1/s
                                      RTT                          

  metrics-gamepulse.power-default     Power draw, battery, TDP     1/s

  metrics-gamepulse.audio-default     Audio pipeline stats, xruns, 1/s
                                      latency                      

  metrics-gamepulse.session-default   Full environment snapshot    1/session

  metrics-gamepulse.ebpf-default      eBPF histograms,             1/s aggregates
                                      distributions, traces        

  logs-gamepulse.events-default       Discrete events (shader      Event-driven
                                      compile, crash)              
  ----------------------------------------------------------------------------------

The full metric inventory is unchanged from v2.0 (see Appendix A in the
v2.0 scope document). What changes is that every field is defined in the
integration's fields.yml files with proper ECS mapping and type
annotations. Specifically, every metric field must include:

-   type: the Elasticsearch field type (long, float, half_float,
    scaled_float, etc.)

-   metric_type: gauge (values that go up and down, e.g. temperature,
    utilisation) or counter (monotonically increasing values, e.g.
    total_bytes_read). Note: counter fields do not support avg()
    aggregation in Kibana --- use max() or rate() instead.

-   unit: the field's unit of measurement. Common values: percent (use 1
    for 100%), byte, ms, nanos, s, rpm, w (watts), c (celsius). Full
    list in the package spec.

-   dimension: true on fields that form the time series identity (host,
    session, game, device identifiers). Maximum 21 per data stream by
    default.

These annotations are not optional. They are required metadata that
Kibana uses for formatting and that TSDS uses for storage optimisation.

7\. Metric Collection --- Full Inventory

This section defines every signal GamePulse collects, organised by
category. Each metric is tagged with its collection phase, platform
source, and priority. This inventory drives the integration's fields.yml
definitions and the collector implementation.

7.1 Hardware & Firmware Baseline (once per session)

Platform context without which performance findings are nearly useless.
All fields map to ECS host.\* or custom gamepulse.hardware.\* fields.

  ----------------------------------------------------------------------------------------
  **Metric**                  **Priority**   **Source (Linux)**     **Source (Windows)**
  --------------------------- -------------- ---------------------- ----------------------
  CPU model, core/thread      Critical       /proc/cpuinfo, sysfs   WMI / CPUID
  count, base/boost clocks                                          

  CPU cache topology          High           sysfs, lscpu           CPUID / WMI
  (L1/L2/L3 sizes)                                                  

  CPU boost/PBO/Curve         High           sysfs (amd_pstate),    Vendor tools / WMI
  Optimizer/undervolt state                  ryzenadj               

  GPU model, VRAM size, vBIOS Critical       sysfs / lspci / NVML   NVML / ADL / DXGI
  version                                                           

  GPU PCIe link speed/width   High           sysfs (pcie)           NVML / ADL

  RAM size, speed, timings,   Critical       dmidecode / sysfs      WMI / SMBIOS
  channel config                                                    

  Storage type, model,        Critical       sysfs / smartctl /     WMI / SMART
  firmware (game drive)                      nvme-cli               

  Storage interface (PCIe     High           sysfs                  WMI / DeviceIoControl
  gen, SATA, UHS class)                                             

  Storage capacity, free      High           statvfs / smartctl /   WMI / manage-bde
  space, health, encryption                  lsblk                  

  Filesystem type, mount      High           /proc/mounts / statfs  GetVolumeInformation
  options, compression                                              

  I/O scheduler, read-ahead   High           /sys/block/\*/queue/   N/A
  setting                                                           

  Motherboard model,          Medium         dmidecode              WMI
  BIOS/UEFI version                                                 

  Display model, native       High           DRM / xrandr / EDID    DXGI / EDID
  resolution, refresh rate                                          

  Display VRR/FreeSync/G-Sync High           DRM properties         Driver API
  support                                                           

  Cooling setup (fan count,   Low            sysfs hwmon            WMI (heuristic)
  AIO/air)                                   (heuristic)            

  Ambient temperature (if     Low            External sensor API    External sensor API
  sensor available)                                                 

  Power limits / TDP          High           sysfs / ryzenadj       Vendor API
  (especially handhelds)                                            

  Device type                 Critical       DMI / sysfs            WMI
  (desktop/handheld/laptop)                                         

  Steam Deck model/variant    High           DMI / device-specific  N/A
                                             sysfs                  
  ----------------------------------------------------------------------------------------

7.2 OS & Software Environment (once per session)

The exact runtime environment. Performance can change dramatically with
a driver update or a background process.

  ----------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)**   **Source (Windows)**
  ------------------------ -------------- -------------------- ---------------------
  OS name, version, build  Critical       /etc/os-release /    Registry / WMI
                                          uname                

  Kernel version           Critical       uname -r             NT version

  Desktop session type     High           \$XDG_SESSION_TYPE   N/A
  (X11 vs Wayland)                                             

  GPU driver version       Critical       modinfo / sysfs      Registry / NVML / ADL

  Mesa version             Critical       glxinfo / vulkaninfo N/A

  Vulkan loader version    High           vulkaninfo           vulkaninfo

  Vulkan driver name       Critical       vulkaninfo           vulkaninfo
  (radv, anv, etc.)                                            

  Proton version           Critical       \$PROTON_VERSION /   N/A
                                          steam compat         

  Wine version (via        High           wine \--version      N/A
  Proton)                                                      

  DXVK version             Critical       DXVK_LOG / library   N/A
                                          version              

  VKD3D-Proton version     Critical       env vars / library   N/A
                                          version              

  Gamescope version        High           gamescope \--version N/A

  MangoHud version         Medium         mangohud \--version  N/A

  PresentMon/CapFrameX     Medium         N/A                  File version /
  version                                                      registry

  Kernel parameters (if    Medium         /proc/cmdline        N/A
  tweaked)                                                     

  Power governor / Game    High           cpufreq sysfs /      Power plan API / Game
  Mode                                    gamemode             Mode

  HAGS (Hardware           High           N/A                  Registry
  Accelerated GPU                                              
  Scheduling)                                                  

  ReBAR / SAM state        High           sysfs                NVML / ADL / Registry
                                          (resizable_bar)      

  Background software      Medium         Process list         Process list snapshot
  inventory                               snapshot             

  Overlay software active  Medium         Process/library      Process/library
  (Steam, Discord, etc.)                  detection            detection
  ----------------------------------------------------------------------------------

7.3 Game Build & Configuration (once per session)

The "what exactly was tested?" layer. Without this, comparisons are
meaningless.

  -----------------------------------------------------------------------------------------------
  **Metric**                         **Priority**   **Source (Linux)**      **Source (Windows)**
  ---------------------------------- -------------- ----------------------- ---------------------
  Game name (auto-detected)          Critical       Process name + Steam    Process name + Steam
                                                    API                     API

  Steam App ID                       Critical       Steam client API / env  Steam client API /
                                                                            registry

  Game version / patch / build       Critical       Steam manifest          File version info
  number                                                                    

  Store (Steam, GOG, Epic, etc.)     High           Process/env detection   Process/registry
                                                                            detection

  Graphics API                       Critical       Vulkan layer / procfs   ETW / process
  (DX11/DX12/Vulkan/OpenGL)                                                 inspection

  Upscaler: FSR/DLSS/XeSS mode +     Critical       Game-specific / driver  Game-specific /
  quality                                           hints                   driver hints

  Frame generation on/off            Critical       Game-specific / driver  Game-specific /
                                                    hints                   driver hints

  Resolution (render + output)       Critical       DRM / gamescope         DXGI

  Resolution scale / dynamic         High           Config file parsing     Config file parsing
  resolution                                                                

  Graphics preset + all deviations   High           Config file parsing     Config file parsing
                                                    (per-game)              (per-game)

  Ray tracing / path tracing         High           Config file parsing     Config file parsing
  settings                                                                  

  V-Sync state                       High           DRM / driver API        DXGI / driver API

  Frame cap setting                  High           Config file / MangoHud  Config file / RTSS

  Fullscreen mode                    High           Gamescope / window      DXGI / window props
  (exclusive/borderless/gamescope)                  props                   

  Benchmark mode vs real gameplay    High           User annotation /       User annotation /
  (tagged)                                          heuristic               heuristic

  Game launch parameters             High           /proc/\[pid\]/cmdline   Command line
                                                                            inspection

  Mods, reshades, texture packs      Medium         Game-specific / file    Game-specific / file
                                                    detection               detection

  HDR state                          Medium         Gamescope / KMS         DXGI
  -----------------------------------------------------------------------------------------------

7.4 Frame Performance (1/s during gameplay)

The headline outputs. Frametime consistency is often more important than
average FPS.

  ---------------------------------------------------------------------------------
  **Metric**                **Priority**   **Source (Linux)** **Source (Windows)**
  ------------------------- -------------- ------------------ ---------------------
  FPS (current, avg, 1%     Critical       MangoHud / Vulkan  PresentMon / ETW
  low, 0.1% low)                           layer              

  Frame time (avg, p50,     Critical       MangoHud / Vulkan  PresentMon / ETW
  p95, p99, max)                           layer              

  Frame time variance /     Critical       Calculated         Calculated
  jitter                                                      

  Stutter events +          Critical       Calculated         Calculated
  timestamps                               (threshold)        (threshold)

  Displayed FPS vs          High           Gamescope stats    PresentMon (app vs
  presented FPS                                               displayed)

  Dropped frames            High           Gamescope /        DXGI / ETW
                                           compositor stats   

  Present mode              High           Gamescope /        DXGI / ETW
  (flip/copy/composition)                  Wayland info       

  Input latency (if         High           eBPF (HID → frame) ETW / vendor tools
  measurable)                                                 

  Load times (session       Medium         Timestamp          Timestamp heuristics
  start, level transitions)                heuristics / I/O   / I/O burst
                                           burst              

  Crash events + last N     High           Process monitor /  Process monitor / ETW
  seconds of telemetry                     eBPF               

  Shader compilation        Critical       eBPF uprobe /      ETW / driver API
  hitching + timestamps                    pipeline cache     
  ---------------------------------------------------------------------------------

7.5 GPU Telemetry (1/s during gameplay)

Whether the game is GPU-bound, power-limited, thermally constrained, or
memory-limited.

  ----------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)**   **Source (Windows)**
  ------------------------ -------------- -------------------- ---------------------
  GPU utilisation %        Critical       sysfs (hwmon) / NVML NVML / ADL / ADLX

  GPU busy / engine load   High           sysfs                NVML / ADL
                                          (gpu_busy_percent)   

  Core clock               Critical       sysfs / NVML         NVML / ADL
  (current/effective)                                          

  Memory clock             High           sysfs / NVML         NVML / ADL

  VRAM used / total        Critical       sysfs / NVML         NVML / ADL

  GPU temperature (core)   Critical       sysfs (hwmon)        NVML / ADL

  GPU hotspot temperature  High           sysfs (hwmon) where  NVML / ADL
                                          avail                

  GPU memory temperature   Medium         sysfs (hwmon) where  NVML / ADL
                                          avail                

  Power draw (W)           Critical       sysfs (hwmon)        NVML / ADL

  Board power limit        High           sysfs / NVML         NVML / ADL

  Fan speed (RPM / %)      High           sysfs (hwmon)        NVML / ADL

  GPU voltage              Medium         sysfs where          NVML / ADL
                                          available            

  P-state / performance    High           sysfs / NVML         NVML / ADL
  state                                                        

  Throttling reason        High           NVML / sysfs         NVML / ADL
  (thermal/power/other)                   heuristic            
  ----------------------------------------------------------------------------------

7.6 CPU & System Telemetry (1/s during gameplay)

Whether the game is CPU-bound or being disrupted by the system.

  -------------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)**     **Source (Windows)**
  ------------------------ -------------- ---------------------- ----------------------
  Total CPU utilisation %  Critical       /proc/stat             PDH / WMI

  Per-core/thread          Critical       /proc/stat             PDH / WMI
  utilisation %                                                  

  Game process CPU         High           /proc/\[pid\]/stat     Process API / ETW
  utilisation                                                    

  Per-core clock speed     Critical       cpufreq sysfs          WMI / CPUID

  CPU temperature          Critical       sysfs (hwmon) /        WMI /
  (package/per-die)                       k10temp                LibreHardwareMonitor

  CPU package power (RAPL) High           sysfs (RAPL)           WMI / RAPL

  Thread count (game       High           /proc/\[pid\]/status   Process API
  process)                                                       

  Context switches/sec     High           /proc/\[pid\]/status / ETW
                                          eBPF                   

  IPC (instructions per    Medium         perf_event / eBPF PMU  PMU via ETW
  cycle)                                                         

  CPU governor / boost     High           cpufreq sysfs          Power plan API
  state                                                          

  C-state residency        Medium         sysfs / turbostat      ETW

  Background process CPU   Medium         /proc/stat             ETW / Process API
  spikes                                  (per-process)          

  Interrupt/DPC latency    Medium         eBPF (irq tracepoints) ETW / DPC latency
                                                                 tools
  -------------------------------------------------------------------------------------

7.7 Memory (1/s during gameplay)

  -------------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)**     **Source (Windows)**
  ------------------------ -------------- ---------------------- ----------------------
  RAM used / total /       Critical       /proc/meminfo          GlobalMemoryStatusEx
  available                                                      

  Game process RSS / VMS   Critical       /proc/\[pid\]/status   Process API

  Swap / pagefile usage    High           /proc/meminfo          Performance counters

  Swap / pagefile activity High           /proc/vmstat / eBPF    Performance counters /
  (reads/writes)                                                 ETW

  Page faults              High           /proc/\[pid\]/stat /   ETW
  (major/minor)                           eBPF                   
  -------------------------------------------------------------------------------------

7.8 Translation & Compatibility Layer (1/s during gameplay, Linux)

Crucial for Linux gaming. The translation/runtime layer is part of the
performance story. DXVK's HUD can expose frametimes, draw calls,
submissions, pipelines, and descriptors.

  --------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source**
  ------------------------ -------------- ----------------------------------------
  Native vs translated     Critical       Process inspection / library detection
  path                                    

  esync / fsync / ntsync   Critical       Environment vars / /proc/\[pid\]/maps
  state                                   

  Shader cache state       Critical       DXVK cache file presence + size / Steam
  (first run vs warmed)                   shader cache

  Pipeline compilation     Critical       DXVK HUD / eBPF uprobe on compiler
  events + duration                       

  Draw calls per second    High           DXVK HUD (drawCalls)

  Command submissions per  High           DXVK HUD (submissions)
  second                                  

  Graphics pipeline count  High           DXVK HUD (pipelines)

  Descriptor pool pressure High           DXVK HUD (descriptors)

  Proton/Wine syscall      Medium         eBPF kprobe on ntdll (Phase 2)
  translation overhead                    
  --------------------------------------------------------------------------------

7.9 Display Chain & Presentation (1/s during gameplay)

Many "game performance" complaints are really presentation-chain
problems. PresentMon's distinction between app-presented and actually
displayed frames is especially important with frame generation.

  --------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)** **Source (Windows)**
  ------------------------ -------------- ------------------ ---------------------
  Actual achieved refresh  High           DRM / gamescope    DXGI / ETW
  rate                                                       

  VRR / FreeSync / G-Sync  High           DRM properties     Driver API
  active                                                     

  V-Sync active            High           DRM / driver API   DXGI

  HDR active / colour      Medium         KMS / gamescope    DXGI
  space                                                      

  FSR/DLSS/XeSS mode +     High           Game/driver        Game/driver specific
  scaling ratio                           specific           

  Compositor latency       High           Gamescope /        DWM stats / ETW
  contribution                            Wayland stats      

  Scaling path (GPU /      Medium         Gamescope / DRM    DXGI / DWM
  display / compositor)                                      

  Game being captured /    Medium         Process detection  Process detection
  streamed                                                   

  Stream resolution +      Low            Steam Remote Play  Steam Remote Play
  refresh (if remote play)                detection          detection
  --------------------------------------------------------------------------------

7.10 Storage I/O (1/s during gameplay)

  ----------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)**   **Source (Windows)**
  ------------------------ -------------- -------------------- ---------------------
  Read/write throughput    High           /proc/diskstats /    PDH / ETW
  (MB/s)                                  eBPF                 

  Read/write IOPS          High           /proc/diskstats /    ETW
                                          eBPF                 

  I/O latency (avg, p50,   High           eBPF (biolatency)    ETW
  p95, p99)                                                    

  I/O queue depth          High           /sys/block/\*/stat   PDH
  (current, max)                                               

  I/O wait % (CPU time on  High           /proc/stat (iowait)  PDH
  storage)                                                     

  Game-process-specific    High           /proc/\[pid\]/io     ETW / Process API
  I/O                                                          

  Drive temperature during Medium         sysfs / smartctl     SMART / WMI
  load                                                         

  I/O merge rate           Medium         /proc/diskstats      ETW
  ----------------------------------------------------------------------------------

7.11 Network (1/s, multiplayer relevant)

  --------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)** **Source (Windows)**
  ------------------------ -------------- ------------------ ---------------------
  Network RTT to game      Medium         eBPF (tcp_rtt)     ETW / raw sockets
  server                                                     

  Packets sent/received    Medium         /proc/net/dev /    Performance counters
                                          eBPF               

  Packet loss %            Medium         eBPF               Calculated

  Bandwidth utilisation    Medium         /proc/net/dev      Performance counters

  Connection type          Medium         NetworkManager /   WMI
  (WiFi/Ethernet)                         sysfs              
  --------------------------------------------------------------------------------

7.12 Audio Pipeline (1/s during gameplay)

  -----------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)**    **Source (Windows)**
  ------------------------ -------------- --------------------- ---------------------
  Audio backend & version  Medium         PipeWire/PulseAudio   WASAPI enumeration
                                          version               

  Buffer underruns (xruns) Medium         PipeWire/PA stats     WASAPI diagnostics

  Audio latency            Medium         PipeWire/PA stats     WASAPI
  (roundtrip)                                                   

  Sample rate & buffer     Medium         PipeWire/PA config    WASAPI
  size                                                          
  -----------------------------------------------------------------------------------

7.13 Power & Battery (1/s, critical for handhelds)

  --------------------------------------------------------------------------------
  **Metric**               **Priority**   **Source (Linux)** **Source (Windows)**
  ------------------------ -------------- ------------------ ---------------------
  Battery drain rate       High           upower / sysfs     Power API

  TDP limit (configurable  High           sysfs / ryzenadj   Vendor API
  on Deck)                                                   

  Power plan / governor    High           cpufreq sysfs      Power API

  AC vs battery state      High           upower / sysfs     Power API

  Performance-per-watt     High           Calculated         Calculated
  (derived)                                                  
  --------------------------------------------------------------------------------

7.14 eBPF Deep Telemetry (Phase 2, Linux only)

Custom gaming-specific probes managed by Elastic Agent. These capture
WHY performance issues occur, not just THAT they occurred.

  ------------------------------------------------------------------------------------
  **Probe**     **Priority**   **Attach Points**          **Output**
  ------------- -------------- -------------------------- ----------------------------
  Scheduler     Critical       sched/sched_switch,        Runqueue wait, CPU affinity,
  Observer                     sched_migrate_task,        CCX migrations, wakeup
                               sched_wakeup               latency

  I/O Tracer    Critical       block/block_rq\_\*, kprobe Per-file I/O latency, sizes,
                               vfs_read/write             sequential vs random, queue
                                                          depth

  GPU           Critical       amdgpu tracepoints,        GPU fence wait times,
  Fence/Sync                   dma_fence_wait             submission latencies
  (AMD)                                                   

  Memory        High           kmem/mm_page_alloc/free,   Page fault rate/type,
  Tracker                      mm/page_fault              allocation rate, pressure
                                                          events

  Futex/Lock    High           syscalls/sys_enter_futex   Lock wait distribution,
  Contention                                              contention by thread

  IRQ/Softirq   High           irq/irq_handler\_\*,       Interrupt duration,
  Latency                      irq/softirq\_\*            frequency, device source

  Syscall       High           raw_syscalls/sys_enter,    Per-process syscall
  Profiler                     sys_exit                   histograms, latency
                                                          distribution

  Shader        High           uprobe on Mesa shader      Compilation duration,
  Compilation                  compiler                   pipeline hash, frametime
                                                          correlation

  Wine/Proton   Medium         kprobe on ntdll            Translation overhead per
  Overhead                     translation                syscall type
  ------------------------------------------------------------------------------------

8\. Privacy & Security

Four-tier privacy model, opt-in everything, no PII by default.
Configurable via the integration's agent policy template in Fleet.

  -----------------------------------------------------------------------
  **Tier**           **Data**                       **Default**
  ------------------ ------------------------------ ---------------------
  Tier 0 --- Local   All raw data, stored locally   Always on
  only                                              

  Tier 1 ---         FPS, temps, utilisation,       Opt-in
  Anonymous metrics  hardware config                

  Tier 2 --- Session Game name, driver versions,    Opt-in
  metadata           Proton version                 

  Tier 3 --- Deep    eBPF traces, I/O patterns,     Opt-in
  telemetry          syscall data                   
  -----------------------------------------------------------------------

Security: minimum privileges (CAP_BPF + CAP_PERFMON for eBPF, not root),
TLS-only ES communication, API key auth, no shell-out from config, eBPF
compiled into binary, no game process hooking.

9\. Risks & Mitigations

  ------------------------------------------------------------------------------
  **Risk**               **Impact**     **Mitigation**
  ---------------------- -------------- ----------------------------------------
  Agent overhead affects Undermines     \<1% CPU, \<50MB RAM. 1/s surface
  gaming                 core mission   collection. eBPF probes are
                                        kernel-resident and lightweight.

  eBPF kernel            Older kernels  CO-RE/BTF. Each probe independent,
  compatibility          unsupported    graceful skip. Min kernel 5.8.

  Anti-cheat             Agent blocked  No game process hooking. OS-level APIs
  interference           or user banned only.

  elastic/integrations   Rejected from  Build to spec from day one. ECS
  acceptance             official repo  compliance. Use elastic-package tooling.
                                        Engage integrations team early (internal
                                        access).

  Closed beta adoption   Colleagues     Self-hosted package registry for
  friction               don't install  one-click Fleet install. Pre-configured
                         it             agent policies. Clear docs with
                                        screenshots.

  Serverless storage     Expensive with Configurable sample rate, data stream
  costs at scale         many           lifecycle retention. Serverless
                         contributors   optimises storage automatically. Pivot
                                        to Hosted+frozen tier if needed.

  Serverless feature     Missing        GamePulse uses standard metrics data
  gaps                   capabilities   streams --- no advanced features (ILM,
                         vs Hosted      Universal Profiling) required. Monitor
                                        Serverless roadmap.

  ECS field mapping      Rework if      Use elastic-package from Phase 0.5.
  retrofit               fields diverge Validate continuously.

  GPU vendor             Maintenance    AMD-only start. NVML (cross-platform)
  fragmentation          burden         Phase 5. Others via community.

  Windows ETW complexity Frame timing   PresentMon (MIT licensed) as library.
                         difficult      

  Python prototype       Overhead       1/s is low-frequency. Async I/O. Rust
  performance            during gaming  rewrite Phase 6.
  ------------------------------------------------------------------------------

10\. Success Metrics

  -----------------------------------------------------------------------
  **Milestone**       **How We Know It Works**
  ------------------- ---------------------------------------------------
  Phase 0.5 complete  elastic-package check passes. Integration package
                      with all 11 data streams (10 metrics + 1 logs for
                      events). TSDS enabled on all metric streams. All
                      metric fields annotated with metric_type, unit, and
                      dimensions. No data stream exceeds field limit.
                      Synthetic data flows through integration pipelines
                      on Serverless. _dev/build/build.yml configured.

  Phase 1 complete    Real gaming session data in Serverless via
                      integration data model. Game name, FPS,
                      GPU/CPU/memory/storage, environment fingerprint all
                      present.

  Phase 2 complete    Can answer \"why did this frame take 30ms?\" with
                      kernel evidence: scheduler migration, I/O stall,
                      futex contention, or GPU fence wait.

  RT scheduling test  Same game under CFS vs SCHED_FIFO: quantified frame
                      time variance, runqueue latency, and migration
                      frequency differences.

  Phase 3 complete    All data explorable in bundled dashboards. Session
                      deep-dive shows full-stack view from FPS to kernel.
                      All panels by-value with data_stream.dataset
                      filters. Visualizations render correctly with TSDS.

  Phase 4 --- closed  10+ colleagues running GamePulse via self-hosted
  beta                package registry. Integration installs in Fleet
                      with one click. Diverse hardware/OS data flowing.

  Phase 5 complete    Same game, same hardware: Windows vs Linux
                      comparison in Elasticsearch.

  Phase 6 ---         GamePulse PR submitted to elastic/integrations.
  official repo       Under review or merged.

  Phase 7 complete    GamePulse visible in Fleet UI for all Elastic
                      users. Community dashboards live. Contributor
                      collaboration active.
  -----------------------------------------------------------------------

11\. Tools & Hardware

  -------------------------------------------------------------------------------------
  **Resource**             **Purpose**                  **Status**
  ------------------------ ---------------------------- -------------------------------
  Elastic Cloud Serverless Backend: storage, ingest,    ✔ Available
  (Enterprise)             dashboards, data stream      
                           lifecycle                    

  elastic-package CLI      Integration development,     Install (Go binary)
                           testing, building, local     
                           stack                        

  Docker                   Required for elastic-package ✔ Available
                           stack up (local testing)     

  Self-hosted Package      Closed beta distribution to  Docker image available
  Registry                 colleagues via Fleet         

  AMD GPU Linux desktop    Primary dev + test, eBPF     ✔ Available
  (CachyOS)                target                       

  Windows 11 desktop (AMD) Windows collector            ✔ Available
                           development                  

  Steam Deck               Handheld testing, SD card    ✔ Available
                           scenarios                    

  GTX 1080 Ti / RTX 2080   NVIDIA testing               ✔ Available

  MacBook                  Development machine (code    ✔ Available
                           lives here)                  

  Python 3.11+             Prototype collector (Phase   Install
                           1)                           

  Rust toolchain           eBPF (Phase 2), production   Install
                           agent (Phase 6)              

  GitHub                   Source + CI/CD               ✔ github.com/MathewRJ/GamePulse
  -------------------------------------------------------------------------------------

12\. Extended Scope --- Future Vision

Capabilities that become possible once the integration and deep
telemetry foundation exist:

-   **Shader Compilation Stutter Maps:** eBPF shader-compile events
    correlated with frame time spikes. Data for Valve's pre-caching
    team.

-   **Proton/DXVK Overhead Quantification:** Exact translation layer
    overhead for games with native + Proton builds.

-   **Power Efficiency Scoring:** Performance-per-watt, battery life
    estimates, optimal TDP per game on handhelds.

-   **Thermal Throttling Detection:** Automatic detection via
    temperature + clock speed correlation.

-   **Crash & Hang Detection:** Process monitoring + eBPF capture of
    pre-crash telemetry.

-   **Input Latency:** End-to-end via eBPF: USB HID → kernel → game read
    → frame presented.

-   **ML Anomaly Detection:** Elastic ML for automatic regression and
    throttling detection.

-   **Game Config Parsing:** Per-game graphics settings extraction.

-   **General-Purpose Profiling (north star):** Domain-agnostic
    application profiling using the same stack. Gaming is the proving
    ground.

-   **Universal Profiling Integration:** Add Elastic's Universal
    Profiling alongside GamePulse for CPU flamegraphs across the full
    application stack. Requires pivot to Elastic Cloud Hosted. Consider
    when deeper CPU profiling beyond gaming-specific probes is needed.

-   **Elastic Cloud Hosted + ILM Frozen Tier:** If long-term storage
    costs become significant at scale, pivot from Serverless to Hosted
    with hot → warm → frozen tier lifecycle. Frozen tier uses searchable
    snapshots on object storage for up to 90% cost reduction. The
    integration's data model is unchanged --- only the backend
    deployment changes.

13\. Immediate Next Steps

Phase 0 infrastructure exists. Begin Phase 0.5:

15. Install elastic-package CLI on MacBook

16. Run elastic-package create package to scaffold the GamePulse
    integration. Verify format_version: 3.0.0 in manifest.yml.

17. Create all 11 data stream definitions via elastic-package create
    data-stream. Use type: metrics for 10 streams, type: logs for
    events.

18. Migrate existing field mappings into ECS-compliant fields.yml files.
    Annotate every metric field with metric_type (gauge/counter), unit,
    and dimension where applicable.

19. Enable TSDS on all metric data streams (elasticsearch.index_mode:
    "time_series" in each data stream manifest)

20. Create \_dev/build/build.yml with ECS reference

21. Audit field counts per data stream against the 1000 default limit

22. Migrate existing ingest pipelines into the package structure

23. Run elastic-package check until clean

24. Test against local Elastic Stack via elastic-package stack up

25. Begin Phase 1 Python collector in parallel, outputting data
    conforming to the integration's field names

*This is a living document. Next review: after Phase 0.5 completion and
first closed beta feedback.*
