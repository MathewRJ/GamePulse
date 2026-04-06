# GamePulse — Kibana Dashboard Build Guide

This guide covers how to build, export, and maintain GamePulse dashboards
on Elastic Cloud Serverless. Read it before opening Kibana.

## Golden rule

Never generate dashboard NDJSON programmatically. Always build in Kibana
UI, then export. The v2 dashboard that works was built this way. Every
dashboard that failed was generated from code.

## Before you start: validate fields with ES|QL

Before building any Lens panel, confirm the field exists and has data
using the Kibana Dev Console or Discover with ES|QL mode:
```esql
FROM metrics-gamepulse.frame-default
| WHERE gamepulse.fps.avg_1s IS NOT NULL
| STATS avg_fps = AVG(gamepulse.fps.avg_1s),
        p95_frametime = PERCENTILE(gamepulse.fps.frametime_ms, 95)
  BY gamepulse.session.id
| SORT avg_fps DESC
| LIMIT 10
```

If this returns data, the field is safe to use in Lens. If it returns
nothing, the field is either empty or the path is wrong.

## Data view setup

For dashboards that span multiple data streams (most of them), use the
wildcard data view:
- Name: GamePulse Metrics
- Pattern: metrics-gamepulse.*
- Time field: @timestamp
- ID: 18dd83e8-6f88-474f-b434-a4b6c14a04a2 (already exists in Serverless)

For single-stream panels, use the per-stream data views:
- gp-dv-frame → metrics-gamepulse.frame-default
- gp-dv-gpu → metrics-gamepulse.gpu-default
- gp-dv-cpu → metrics-gamepulse.cpu-default
- gp-dv-session → metrics-gamepulse.session-default
- gp-dv-storage → metrics-gamepulse.storage-default

## Building a dashboard — step by step

1. Go to Dashboards → Create new dashboard
2. Add filter controls first (before panels):
   - Click the controls icon → Add control → Options list
   - Game: field = gamepulse.game.name.keyword, data view = wildcard
   - Session ID: field = gamepulse.session.id.keyword, data view = wildcard
   - OS: field = host.os.type.keyword, data view = wildcard
3. Add panels using Lens (never Vega, TSVB, or legacy visualizations)
4. For every panel: add a data_stream.dataset filter to the panel's
   filter bar (click Add filter inside the Lens editor)
5. Set panel titles without the package name ("FPS Timeline" not
   "[GamePulse] FPS Timeline")
6. Save the dashboard with a clear name

## TSDS aggregation rules

Several GamePulse fields are counter-type metrics. These do not support
avg() in Kibana Lens on TSDS-backed streams. Use these alternatives:

| Field type | Wrong | Correct |
|------------|-------|---------|
| counter    | Average | Max or Rate |
| gauge      | Any aggregation | All supported |

Fields that are counters (monotonically increasing):
- storage.read_mbps, storage.write_mbps (actually gauges — use avg)
- Check fields.yml for metric_type annotation if unsure

Fields that are gauges (use any aggregation):
- All fps.*, gpu.*, cpu.*, memory.* fields

## Recommended panel types by use case

| Use case | Lens type | Notes |
|----------|-----------|-------|
| Metric over time | XY → Line | Use date_histogram on @timestamp |
| Single stat (e.g. peak temp) | Metric | Use max() aggregation |
| Distribution (e.g. frame times) | XY → Bar (histogram) | Use range on the field |
| Two metrics, different scales | XY → Line, dual axis | Right axis for second metric |
| Session list/table | Datatable | Group by session.id.keyword |
| Compare configs | XY → Line with split | Split by session.id or game.name |

## Exporting and committing

After building and verifying a dashboard shows real data:

1. Stack Management → Saved Objects
2. Check the dashboard (and its related data views if new)
3. Export → Include related objects: YES
4. Save as kibana/gamepulse-<name>.ndjson
5. Commit with message: "Add/update Kibana dashboard: <name>"
6. Push immediately

## Importing on a new Kibana instance

The data view IDs (gp-dv-frame, gp-dv-gpu, etc.) must exist before
importing a dashboard. They are included in the export if you check
"Include related objects". If import fails with Internal Server Error:

1. Strip version tokens from the NDJSON:
   ```
   python3 strip_versions.py kibana/gamepulse-dashboard.ndjson > clean.ndjson
   ```
   (see tools/strip_versions.py)
2. Try importing clean.ndjson
3. If data view conflict dialog appears, select the existing matching
   data view from the dropdown

## Building the next dashboard: Configuration Comparison

This is the highest-value next dashboard given current data availability.

Panels to build:
1. Filter controls:
   - Game: `gamepulse.game.name.keyword`
   - OS: `host.os.type.keyword`
   - Proton version: `gamepulse.compatibility.proton_version` (NOT `gamepulse.compat.*` — verified 2026-04-06)
   - Skip GPU driver filter — `gamepulse.gpu.driver_version` not collected by host enricher yet
2. FPS distribution histogram — split by gamepulse.game.name.keyword
   - XY → Bar (histogram) on gamepulse.fps.avg_1s
3. Frame time variance by session
   - Datatable: rows = session.id, columns = avg fps, p95 frametime, stutter count
4. GPU util vs CPU util over time — dual-line XY (split by data_stream or use separate series)
5. Metric tiles: sessions compared, games compared, date range

ES|QL validation queries for this dashboard:
```esql
FROM metrics-gamepulse.frame-default
| STATS
    avg_fps = AVG(gamepulse.fps.avg_1s),
    p95_ft = PERCENTILE(gamepulse.fps.frametime_ms, 95),
    stutter = SUM(gamepulse.fps.stutter_count)
  BY gamepulse.game.name.keyword, gamepulse.session.id
| SORT avg_fps DESC
```
```esql
FROM metrics-gamepulse.session-default
| STATS sessions = COUNT_DISTINCT(gamepulse.session.id.keyword)
  BY gamepulse.game.name.keyword, host.os.type.keyword
| SORT sessions DESC
```

## Elastic Content Share reference

https://elastic-content-share.eu/ — community-contributed dashboards.
Most are 7.x/8.x era and will not import cleanly to Serverless 9.x.
Use them for structural inspiration only — look at how panels are arranged
and what metrics are combined, not as importable NDJSON.

Observability dashboards there are the most relevant category for
GamePulse — they tend to use similar time-series + metric tile patterns.
