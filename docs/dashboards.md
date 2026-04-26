# GamePulse — Kibana Dashboards

## Current dashboards

| Dashboard | File | Kibana ID |
|---|---|---|
| Home | `dashboards/home-dashboard.json` | `home-dashboard-2026-04-13` |
| Session Deep-Dive | `dashboards/session-deep-dive-dashboard.json` | `b68f1178-6923-4e92-819b-33eb595197a9` |
| Configuration Comparison | `dashboards/config-comparison-dashboard.json` | `21b663d6-de42-46c6-aeaf-e6c48e46ecec` |
| Storage & I/O Analysis | `dashboards/storage-io-dashboard.json` | `f8a9d960-130e-43db-8554-6033f45e8a9c` |
| System Health | `dashboards/system-health-dashboard.json` | `1b2a1b70-a315-4ed4-91c4-11aa0abe5e1d` |
| Game Library | `dashboards/game-library-dashboard.json` | `e7d878d0-e2d6-454b-9a95-d93a4aeb70a8` |
| Scheduler Analysis | `dashboards/scheduler-analysis-dashboard.json` | `89ca0908-5639-45f7-9a70-edadfe7d7124` |
| Games | `dashboards/games-dashboard.json` | `5e898d7c-8de1-45b8-ae04-4cdc745f046d` |
| Baseline (UI export) | `dashboards/gamepulse-dashboard.ndjson` | — |

---

## Dashboard build workflow

### Method A — Kibana Dashboards API (preferred)

Use the `kibana-dashboards` agent skill. Workflow:
1. Validate fields with ES|QL (`elasticsearch-esql` skill) before building panels
2. Generate dashboard JSON and POST via the skill
3. Retrieve the result and save to `dashboards/<name>.json`
4. Commit and push

### Method B — Kibana UI export (fallback)

Build in Kibana UI → Stack Management → Saved Objects → Export → commit as `dashboards/<name>.ndjson`.

**Never hand-author NDJSON.** These files are version-sensitive and will fail to import on Serverless. Always export from a live Kibana instance.

---

## Build guide

### Before you start: validate fields with ES|QL

Before building any Lens panel, confirm the field exists and has data:

```esql
FROM metrics-gamepulse.frame-default
| WHERE gamepulse.fps.avg_1s IS NOT NULL
| STATS avg_fps = AVG(gamepulse.fps.avg_1s),
        p95_frametime = PERCENTILE(gamepulse.fps.frametime_ms, 95)
  BY gamepulse.session.id
| SORT avg_fps DESC
| LIMIT 10
```

### Data views

| Data view ID | Pattern | Use for |
|---|---|---|
| `18dd83e8-6f88-474f-b434-a4b6c14a04a2` | `metrics-gamepulse.*` | Multi-stream dashboards |
| `gp-dv-frame` | `metrics-gamepulse.frame-default` | FPS/frametime panels |
| `gp-dv-gpu` | `metrics-gamepulse.gpu-default` | GPU metrics panels |
| `gp-dv-cpu` | `metrics-gamepulse.cpu-default` | CPU metrics panels |
| `gp-dv-session` | `metrics-gamepulse.session-default` | Session config panels |
| `gp-dv-storage` | `metrics-gamepulse.storage-default` | Storage I/O panels |
| `gp-dv-timeline` | `gamepulse-game-timeline` | Games dashboard |

### Building a dashboard — step by step

1. Go to Dashboards → Create new dashboard
2. Add filter controls first (before panels):
   - Game: `gamepulse.game.name.keyword`, data view = wildcard
   - Session ID: `gamepulse.session.id.keyword`, data view = wildcard
   - OS: `host.os.type.keyword`, data view = wildcard
3. Add panels using Lens only (never Vega, TSVB, or legacy visualizations)
4. For every panel: add a `data_stream.dataset` filter to the panel's filter bar
5. Set panel titles without the package name ("FPS Timeline" not "[GamePulse] FPS Timeline")
6. Save the dashboard

### Elastic compliance rules

- All visualizations must be defined by value (part of the dashboard, not saved to Visualize library)
- Every panel must include a `data_stream.dataset` filter
- Visualization titles must not include the package name
- Use Kibana Lens only — no TSVB, no Vega, no legacy aggregation-based panels
- Build against stable Kibana (Serverless current), never SNAPSHOT

### TSDS aggregation rules

Counter-type fields do not support `avg()` in Kibana Lens on TSDS-backed streams:

| Field type | Wrong | Correct |
|---|---|---|
| counter | Average | Max or Rate |
| gauge | — | All aggregations supported |

Most `gamepulse.*` fields are gauges (use any aggregation). Check `fields.yml` `metric_type` if unsure.

### Recommended panel types

| Use case | Lens type | Notes |
|---|---|---|
| Metric over time | XY → Line | Use `date_histogram` on `@timestamp` |
| Single stat (peak temp) | Metric | Use `max()` aggregation |
| Distribution (frame times) | XY → Bar histogram | Use range on the field |
| Two metrics, different scales | XY → Line, dual axis | Right axis for second metric |
| Session list/table | Datatable | Group by `session.id.keyword` |
| Compare configs | XY → Line with split | Split by `session.id` or `game.name` |

---

## Serverless constraints

| Constraint | Detail |
|---|---|
| No legacy `visualization` type | Only Lens (`lnsXY`, `lnsMetric`, `lnsDatatable`, etc.) works |
| `_import` is the only programmatic path | `_find`, `_bulk_create`, direct PUT — all return 400 on Serverless |
| File extension for `_import` | Must be `.ndjson` — `.json` is rejected |
| Use `_export` to retrieve live dashboards | `GET /api/saved_objects/dashboard/{id}` returns 400 on Serverless; use `POST /api/saved_objects/_export` instead |
| Authentication | Use `ES_API_KEY` (not `KIBANA_API_KEY`) for `_import`, `_export`, and data view APIs |
| `adHocDataViews` must be empty `{}` | Populated inline data views cause 500 on import |
| `typeMigrationVersion` required | Missing = silent 500. Dashboards: `"10.3.0"`, index-patterns: `"8.0.0"` |
| Panels embedded in dashboard | Separate `lens` saved objects referenced by `panelRefName` do not render |
| Data views must exist before import | Field list is populated at creation time |

---

## NDJSON structure reference

### Panel embedding

Panels are embedded inline in `panelsJSON` within the dashboard object — not as separate `lens` saved objects. Every panel's full Lens state lives in `embeddableConfig.attributes`.

### Reference name format

Inside `embeddableConfig.attributes.references`:
```
{panelIndex}:indexpattern-datasource-layer-{layerId}
```

The dashboard-level `references` array must duplicate all panel references with the same name pattern.

### Three datasource states required

```json
"datasourceStates": {
  "formBased": { "layers": { ... } },
  "indexpattern": { "layers": {} },
  "textBased": { "layers": {} }
}
```

### lnsXY (line chart) column order

`columnOrder` for XY layers: terms bucket first, then date_histogram, then metrics.

### lnsDatatable column `isTransposed`

Bucket columns (terms): `"isTransposed": true`. Metric columns (count): `"isTransposed": false`.

### splitAccessors

`"splitAccessors": ["col-uuid-here"]` — plural array, not singular `splitAccessor`.

### Available operationTypes

| operationType | Use case | isBucketed |
|---|---|---|
| `date_histogram` | X-axis time buckets | `true` |
| `terms` | Split-by / group-by string field | `true` |
| `median` | Preferred over `average` for skewed data | `false` |
| `average` | Mean of numeric field | `false` |
| `max` | Max of numeric field | `false` |
| `min` | Min of numeric field | `false` |
| `count` | Document count | `false` |
| `unique_count` | Cardinality / distinct count | `false` |

### Filter controls (options_list_control)

Filter dropdowns are stored in `attributes.pinned_panels`. Field names for text fields must use `.keyword` sub-field (e.g. `gamepulse.game.name.keyword`). Using the bare text field silently produces a non-functional filter control.

**Note:** `.keyword` sub-fields only exist on indices created before the 2026-04-12 reindex. Indices from 2026-04-12 onwards use native `keyword` type — use base field paths (no `.keyword`) in ES|QL queries targeting these indices. Filter controls always need `.keyword` regardless (Kibana requirement).

---

## Importing dashboards

```bash
# Import a dashboard to Kibana Serverless
curl -X POST "https://your-kibana/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -H "Authorization: ApiKey $ES_API_KEY" \
  --form file=@dashboards/session-deep-dive-dashboard.ndjson
```

File must have `.ndjson` extension. Data view IDs (`gp-dv-frame`, etc.) must exist before importing — they are included in the export when "Include related objects" is checked.
