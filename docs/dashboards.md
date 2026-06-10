# RigSignal — Kibana Dashboards

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
| Environment | `dashboards/environment-dashboard.json` | `3a55c257-0537-42a8-94a7-24dc773a703b` |
| Hardware | `dashboards/hardware-dashboard.json` | `ed9d9b94-2003-429c-b294-9d3f2ef737e7` |
| Compare | `dashboards/compare-dashboard.json` | `828db140-b330-4d26-8045-40a7895bfc41` |
| Engine | `dashboards/engine-dashboard.json` | `7ec220c4-0c7a-4538-9b86-9a664b4a7d2f` |
| Baseline (UI export) | `dashboards/rigsignal-dashboard.ndjson` | — |

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

### Verification gates (run both before declaring a dashboard done)

The API gate and the browser-UI gate catch different failures. A dashboard
can be import-valid (API gate green) yet render as a blank panel in the
browser (UI gate red), typically when a Lens datasource layer is intact in
the saved object but a referenced field is missing from the index mapping
or the panel migration version is stale.

1. **`scripts/verify-dashboard.sh <id>`** — API gate. Exports the dashboard,
   asserts panel inventory, validates Lens datasource layers, and checks
   that the Kibana internal loader returns no `statusCode` error.

2. **`scripts/verify-dashboard-ui.sh <id>`** — browser-render gate.
   Headless Chromium loads the dashboard with a real browser-auth
   `storageState`, waits for the dashboard title and every panel title to
   be visible, scans for failure strings (`Cannot read properties`,
   `No embeddable factory found`, `Field not found`, `Error loading
   dashboard`, etc.), and saves a full-page PNG to
   `artifacts/dashboard-ui/<id>.png`.

   **One-time setup** (Playwright + a captured browser-auth session):
   ```sh
   npm install --no-save playwright
   npx playwright install chromium
   scripts/capture-kibana-auth.sh                  # opens headed Chromium for login
   export KIBANA_BROWSER_AUTH_STATE=.gpx/kibana-auth.storage-state.json
   ```
   The state file is gitignored; refresh it whenever Elastic Cloud
   invalidates the session (MFA / OTP).

`scripts/verify-dashboard.sh` chains the UI gate automatically when
`KIBANA_BROWSER_AUTH_STATE` is set, so one command runs both.

---

## Build guide

### Before you start: validate fields with ES|QL

Before building any Lens panel, confirm the field exists and has data:

```esql
FROM metrics-rigsignal.frame-default
| WHERE rigsignal.fps.avg_1s IS NOT NULL
| STATS avg_fps = AVG(rigsignal.fps.avg_1s),
        p95_frametime = PERCENTILE(rigsignal.fps.frametime_ms, 95)
  BY rigsignal.session.id
| SORT avg_fps DESC
| LIMIT 10
```

### Data views

| Data view ID | Pattern | Use for |
|---|---|---|
| `18dd83e8-6f88-474f-b434-a4b6c14a04a2` | `metrics-rigsignal.*` | Multi-stream dashboards |
| `gp-dv-frame` | `metrics-rigsignal.frame-default` | FPS/frametime panels |
| `gp-dv-gpu` | `metrics-rigsignal.gpu-default` | GPU metrics panels |
| `gp-dv-cpu` | `metrics-rigsignal.cpu-default` | CPU metrics panels |
| `gp-dv-session` | `metrics-rigsignal.session-default` | Session config panels |
| `gp-dv-storage` | `metrics-rigsignal.storage-default` | Storage I/O panels |
| `gp-dv-timeline` | `rigsignal-game-timeline` | Games dashboard |

### Building a dashboard — step by step

1. Go to Dashboards → Create new dashboard
2. Add filter controls first (before panels):
   - Game: `rigsignal.game.name.keyword`, data view = wildcard
   - Session ID: `rigsignal.session.id.keyword`, data view = wildcard
   - OS: `host.os.type.keyword`, data view = wildcard
3. Add panels using Lens only (never Vega, TSVB, or legacy visualizations)
4. For every panel: add a `data_stream.dataset` filter to the panel's filter bar
5. Set panel titles without the package name ("FPS Timeline" not "[RigSignal] FPS Timeline")
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

Most `rigsignal.*` fields are gauges (use any aggregation). Check `fields.yml` `metric_type` if unsure.

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

## Kibana 9.5.0 Dashboards API — schema breaking changes

Discovered 2026-04-26 when building the Games dashboard against Kibana 9.5.0 serverless.
**These replace the patterns in the `kibana-dashboards` SKILL.md**, which was written for the 9.4 SNAPSHOT.
Apply to every subsequent dashboard build (Environment, Hardware, Compare, Engine).

### 1. `Elastic-Api-Version` header format

| Before (9.4 SNAPSHOT) | After (9.5.0) |
|---|---|
| `"Elastic-Api-Version": "1"` | `"Elastic-Api-Version": "2023-10-31"` |

The integer form is rejected with HTTP 400 `"Invalid version. Received "1", expected a valid date string formatted as YYYY-MM-DD."`. The `kibana-dashboards` script in `.agents/skills/kibana-dashboards/scripts/kibana-dashboards.js` must have the four `getDashboard` / `createDashboard` / `updateDashboard` / `deleteDashboard` calls patched to `"2023-10-31"` before use (the skill directory is in `.gitignore` and won't re-apply automatically).

### 2. Panel type: `"lens"` → `"vis"`

All inline visualization panels now use `"type": "vis"`. The old `"type": "lens"` is not in the allowed set and returns HTTP 400.

```json
// Before
{ "type": "lens", "uid": "...", "grid": {...}, "config": { "attributes": { ... } } }

// After
{ "type": "vis", "id": "...", "grid": {...}, "config": { ... } }
```

### 3. Panel identifier: `uid` → `id`

The panel-level unique identifier key changed. `uid` on any panel type causes a validation error. `options_list_control` panels silently dropped uid in 9.4; in 9.5.0 it is an explicit schema error.

### 4. Data source key: `dataset` → `data_source`, and type values changed

The visualization dataset descriptor moved out of `config.attributes` and into `config` directly, with renamed keys:

| Before (9.4) | After (9.5.0) |
|---|---|
| `"dataset": { "type": "dataView", "id": "..." }` | `"data_source": { "type": "data_view_reference", "ref_id": "..." }` |
| `"dataset": { "type": "esql", "query": "..." }` | `"data_source": { "type": "esql", "query": "..." }` *(type string unchanged)* |

For `xy` charts, `data_source` goes inside each layer (same as before). For `metric` and `data_table`, `data_source` goes at the `config` level.

`"data_view_spec"` is also accepted as a `data_source.type` (for inline index patterns without a saved data view).

### 5. ES|QL metric items: `operation` field removed

For ES|QL-backed metric panels, the `operation` field is not allowed. Reference the query output column by name only:

```json
// Before
{ "type": "primary", "operation": "value", "column": "Total Sessions", "label": "..." }

// After
{ "type": "primary", "column": "Total Sessions", "label": "..." }
```

dataView metric items are **unchanged** — they still use `{ "type": "primary", "operation": "average", "field": "...", "label": "..." }`.

### 6. `last_value` metrics: `sort_by` → `time_field`, `show_array_values` → `multi_value`

```json
// Before
{ "operation": "last_value", "field": "game_name", "sort_by": "@timestamp", "show_array_values": false }

// After
{ "operation": "last_value", "field": "game_name", "time_field": "@timestamp", "multi_value": false }
```

### 7. `data_table` rows `rank_by`: `"column"` type removed

Valid `rank_by.type` values in 9.5.0: `"alphabetical"`, `"rare"`, `"significant"`, `"metric"`, `"custom"`. The `"column"` type (used in SKILL.md examples) is no longer accepted.

Use `"alphabetical"` for default ordering. In-dashboard column-click sorting still works for the user.

```json
// Before (9.4)
{ "operation": "terms", "fields": ["session_id"], "rank_by": { "type": "column", "metric": 0, "direction": "desc" } }

// After (9.5.0)
{ "operation": "terms", "fields": ["session_id"], "limit": 50, "rank_by": { "type": "alphabetical", "direction": "asc" } }
```

### 8. XY chart axis key: `"left"` → `"y"`

The left/primary y-axis is now addressed as `"y"` in the `axis` config object. `"left"` silently has no effect.

```json
// Before
"axis": { "x": { "title": { "visible": false } }, "left": { "title": { "visible": false } } }

// After
"axis": { "x": { "title": { "visible": false } }, "y": { "title": { "visible": false } } }
```

### 9. Config structure flattened: `config.attributes` → `config`

In 9.4, inline Lens definitions were nested under `config.attributes`. In 9.5.0, the visualization definition sits directly in `config`. The `"attributes"` wrapper key is gone.

```json
// Before (9.4)
"config": {
  "attributes": { "title": "...", "type": "metric", "dataset": {...}, "metrics": [...] }
}

// After (9.5.0)
"config": {
  "title": "...", "type": "metric", "data_source": {...}, "metrics": [...]
}
```

### 10. Dual y-axis XY charts: `"axis": "y2"` for right axis (discovered in Environment dashboard)

XY chart `y` metric items accept an `"axis"` property to assign them to a right axis. Valid values: `"y"` (left, default) and `"y2"` (right). Both metrics can be in the same layer's `y` array — no need for a separate layer.

```json
// Single layer with two y-metrics on different axes
"layers": [{
  "type": "line",
  "data_source": { "type": "data_view_reference", "ref_id": "..." },
  "x": { "operation": "date_histogram", "field": "@timestamp" },
  "y": [
    { "operation": "average", "field": "rigsignal.gpu.utilisation_pct", "label": "GPU Util %" },
    { "operation": "max", "field": "rigsignal.gpu.temperature_c", "label": "GPU Temp °C", "axis": "y2" }
  ]
}]
```

`"axis": "right"` is rejected. The schema error exposes the allowed enum: `"y"` | `"y2"`.

### Quick-reference: working panel skeletons for 9.5.0

**ES|QL metric tile:**
```json
{
  "type": "vis", "id": "my-tile",
  "grid": { "x": 0, "y": 0, "w": 12, "h": 6 },
  "config": {
    "type": "metric",
    "data_source": { "type": "esql", "query": "FROM idx | STATS `My Value` = COUNT(*)" },
    "metrics": [{ "type": "primary", "column": "My Value", "label": "My Value" }]
  }
}
```

**dataView metric tile:**
```json
{
  "type": "vis", "id": "my-tile",
  "grid": { "x": 0, "y": 0, "w": 12, "h": 6 },
  "config": {
    "type": "metric",
    "data_source": { "type": "data_view_reference", "ref_id": "<data-view-id>" },
    "metrics": [{ "type": "primary", "operation": "average", "field": "my_field", "label": "My Label" }]
  }
}
```

**XY line chart (dataView, split by field):**
```json
{
  "type": "vis", "id": "my-chart",
  "grid": { "x": 0, "y": 6, "w": 48, "h": 12 },
  "config": {
    "type": "xy",
    "axis": { "x": { "title": { "visible": false } }, "y": { "title": { "visible": false } } },
    "layers": [{
      "type": "line",
      "data_source": { "type": "data_view_reference", "ref_id": "<data-view-id>" },
      "x": { "operation": "date_histogram", "field": "@timestamp" },
      "y": [{ "operation": "average", "field": "my_metric", "label": "My Metric" }],
      "breakdown_by": { "operation": "terms", "fields": ["my_keyword_field"] }
    }]
  }
}
```

**data_table (dataView):**
```json
{
  "type": "vis", "id": "my-table",
  "grid": { "x": 0, "y": 18, "w": 48, "h": 14 },
  "config": {
    "type": "data_table",
    "data_source": { "type": "data_view_reference", "ref_id": "<data-view-id>" },
    "metrics": [
      { "operation": "last_value", "field": "my_keyword", "time_field": "@timestamp", "multi_value": false, "label": "Label" },
      { "operation": "max", "field": "my_numeric", "label": "Label" }
    ],
    "rows": [{ "operation": "terms", "fields": ["group_field"], "limit": 50, "rank_by": { "type": "alphabetical", "direction": "asc" } }]
  }
}
```

**options_list_control (unchanged except no uid/no attributes wrapper):**
```json
{
  "type": "options_list_control", "id": "ctrl-game",
  "grid": { "x": 0, "y": 0, "w": 24, "h": 4 },
  "config": { "title": "Game", "data_view_id": "<data-view-id>", "field_name": "game_name" }
}
```

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

Filter dropdowns are stored in `attributes.pinned_panels`. Field names for text fields must use `.keyword` sub-field (e.g. `rigsignal.game.name.keyword`). Using the bare text field silently produces a non-functional filter control.

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
