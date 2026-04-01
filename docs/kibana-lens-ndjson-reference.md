# Kibana Lens NDJSON Reference — GamePulse

This document records the exact structure required for programmatic Kibana Lens dashboard creation on **Kibana Serverless 9.4**. It was derived by manually building a working dashboard in the UI and analysing its export.

---

## Key discoveries

### 1. Panels are embedded in the dashboard, not separate saved objects

The working format is **NOT** separate `lens` saved objects referenced by `panelRefName`. Kibana Serverless exports all panel state inline inside `panelsJSON` within the dashboard object. Every panel's full Lens state lives in `embeddableConfig.attributes`.

### 2. `typeMigrationVersion` values differ by type

| Object type | typeMigrationVersion |
|-------------|---------------------|
| `dashboard` | `"10.3.0"` |
| `index-pattern` | `"8.0.0"` |
| `lens` (separate, if used) | `"8.9.0"` |

Using the wrong version causes silent 500 errors on import.

### 3. Reference name format for embedded panels

Inside `embeddableConfig.attributes.references`, the `name` field must follow this exact pattern:

```
{panelIndex}:indexpattern-datasource-layer-{layerId}
```

Where `panelIndex` is the UUID used as both `panelIndex` and `gridData.i`. At the dashboard level, the same reference is repeated with the same name pattern.

### 4. `adHocDataViews` must be present but empty

```json
"adHocDataViews": {}
```

It cannot be omitted — but populating it causes import failures.

### 5. Three datasource states are required, two empty

```json
"datasourceStates": {
  "formBased": { "layers": { ... } },
  "indexpattern": { "layers": {} },
  "textBased": { "layers": {} }
}
```

### 6. `splitAccessors` is a plural array

```json
"splitAccessors": ["col-uuid-here"]
```

Not `splitAccessor` (singular). Common mistake that silently breaks the split-by-session breakdown.

---

## Anatomy of a working lnsXY panel (line chart)

```json
{
  "type": "lens",
  "embeddableConfig": {
    "attributes": {
      "title": "FPS Timeline",
      "description": "",
      "visualizationType": "lnsXY",
      "type": "lens",
      "version": 2,
      "references": [
        {
          "type": "index-pattern",
          "id": "gp-dv-frame",
          "name": "{panelIndex}:indexpattern-datasource-layer-{layerId}"
        }
      ],
      "state": {
        "visualization": {
          "title": "Empty XY chart",
          "legend": { "isVisible": true, "position": "right" },
          "valueLabels": "hide",
          "preferredSeriesType": "line",
          "layers": [
            {
              "layerId": "{layerId}",
              "accessors": ["{colId-metric1}", "{colId-metric2}"],
              "position": "top",
              "seriesType": "line",
              "showGridlines": false,
              "layerType": "data",
              "colorMapping": {
                "assignments": [],
                "specialAssignments": [
                  {
                    "rules": [{ "type": "other" }],
                    "color": { "type": "loop" },
                    "touched": false
                  }
                ],
                "paletteId": "elastic_line_optimized",
                "colorMode": { "type": "categorical" }
              },
              "xAccessor": "{colId-timestamp}",
              "yConfig": [
                { "forAccessor": "{colId-metric1}", "color": "#0e9a15" },
                { "forAccessor": "{colId-metric2}", "color": "#f48e2e" }
              ],
              "splitAccessors": ["{colId-terms-split}"]
            }
          ],
          "axisTitlesVisibilitySettings": {
            "x": false,
            "yLeft": true,
            "yRight": true
          }
        },
        "query": { "query": "", "language": "kuery" },
        "filters": [],
        "datasourceStates": {
          "formBased": {
            "layers": {
              "{layerId}": {
                "columns": {
                  "{colId-timestamp}": {
                    "label": "@timestamp",
                    "dataType": "date",
                    "operationType": "date_histogram",
                    "sourceField": "@timestamp",
                    "isBucketed": true,
                    "params": {
                      "interval": "auto",
                      "includeEmptyRows": true,
                      "dropPartials": false
                    }
                  },
                  "{colId-terms-split}": {
                    "label": "Top 9 values of session_id",
                    "dataType": "string",
                    "operationType": "terms",
                    "sourceField": "session_id",
                    "isBucketed": true,
                    "params": {
                      "size": 9,
                      "orderBy": { "type": "column", "columnId": "{colId-metric1}" },
                      "orderDirection": "desc",
                      "otherBucket": true,
                      "missingBucket": false,
                      "parentFormat": { "id": "terms" },
                      "include": [], "exclude": [],
                      "includeIsRegex": false, "excludeIsRegex": false
                    }
                  },
                  "{colId-metric1}": {
                    "label": "Avg FPS",
                    "dataType": "number",
                    "operationType": "median",
                    "sourceField": "fps.avg_1s",
                    "isBucketed": false,
                    "customLabel": true,
                    "params": { "emptyAsNull": true }
                  },
                  "{colId-metric2}": {
                    "label": "1% Lows",
                    "dataType": "number",
                    "operationType": "median",
                    "sourceField": "fps.low_1pct",
                    "isBucketed": false,
                    "customLabel": true,
                    "params": { "emptyAsNull": true }
                  }
                },
                "columnOrder": [
                  "{colId-terms-split}",
                  "{colId-timestamp}",
                  "{colId-metric1}",
                  "{colId-metric2}"
                ],
                "sampling": 1,
                "ignoreGlobalFilters": false,
                "incompleteColumns": {}
              }
            }
          },
          "indexpattern": { "layers": {} },
          "textBased": { "layers": {} }
        },
        "internalReferences": [],
        "adHocDataViews": {}
      }
    }
  },
  "panelIndex": "{panelIndex}",
  "gridData": { "y": 5, "x": 0, "w": 40, "h": 10, "i": "{panelIndex}" }
}
```

**Key rules:**
- `columnOrder` for XY: terms bucket first, then date_histogram, then metrics
- `customLabel: true` required whenever the label differs from the default Kibana-generated label
- `panelIndex` and `gridData.i` must be the same UUID
- For dual right-axis: add `"axisMode": "right"` alongside `"forAccessor"` in `yConfig`

---

## Dual-axis yConfig (right axis)

To put a series on the right axis (e.g. temperature on right, utilisation on left):

```json
"yConfig": [
  { "forAccessor": "{colId-temp}", "axisMode": "right" }
]
```

Accessors not listed in `yConfig` default to the left axis.

---

## Anatomy of a working lnsMetric panel

```json
{
  "type": "lens",
  "embeddableConfig": {
    "attributes": {
      "title": "",
      "visualizationType": "lnsMetric",
      "type": "lens",
      "version": 2,
      "references": [
        {
          "type": "index-pattern",
          "id": "gp-dv-frame",
          "name": "{panelIndex}:indexpattern-datasource-layer-{layerId}"
        }
      ],
      "state": {
        "visualization": {
          "layerId": "{layerId}",
          "layerType": "data",
          "metricAccessor": "{colId-metric}",
          "secondaryTrend": { "type": "none" },
          "secondaryLabelPosition": "before"
        },
        "query": { "query": "", "language": "kuery" },
        "filters": [],
        "datasourceStates": {
          "formBased": {
            "layers": {
              "{layerId}": {
                "columns": {
                  "{colId-metric}": {
                    "label": "Median FPS",
                    "dataType": "number",
                    "operationType": "median",
                    "sourceField": "fps.avg_1s",
                    "isBucketed": false,
                    "customLabel": true,
                    "params": { "emptyAsNull": true }
                  }
                },
                "columnOrder": ["{colId-metric}"],
                "sampling": 1,
                "ignoreGlobalFilters": false,
                "incompleteColumns": {}
              }
            }
          },
          "indexpattern": { "layers": {} },
          "textBased": { "layers": {} }
        },
        "internalReferences": [],
        "adHocDataViews": {}
      }
    }
  },
  "panelIndex": "{panelIndex}",
  "gridData": { "y": 0, "x": 0, "w": 10, "h": 5, "i": "{panelIndex}" }
}
```

---

## Anatomy of a working lnsDatatable panel

```json
{
  "type": "lens",
  "embeddableConfig": {
    "attributes": {
      "title": "",
      "visualizationType": "lnsDatatable",
      "type": "lens",
      "version": 2,
      "references": [
        {
          "type": "index-pattern",
          "id": "gp-dv-session",
          "name": "{panelIndex}:indexpattern-datasource-layer-{layerId}"
        }
      ],
      "state": {
        "visualization": {
          "columns": [
            { "columnId": "{colId-game}",   "isTransposed": true,  "isMetric": false },
            { "columnId": "{colId-os}",     "isTransposed": true,  "isMetric": false },
            { "columnId": "{colId-count}",  "isTransposed": false, "isMetric": true  }
          ],
          "layerId": "{layerId}",
          "layerType": "data",
          "showRowNumbers": true
        },
        "query": { "query": "", "language": "kuery" },
        "filters": [],
        "datasourceStates": {
          "formBased": {
            "layers": {
              "{layerId}": {
                "columns": {
                  "{colId-game}": {
                    "label": "Top 100 values of game.name.keyword",
                    "dataType": "string",
                    "operationType": "terms",
                    "sourceField": "game.name.keyword",
                    "isBucketed": true,
                    "params": {
                      "size": 100,
                      "orderBy": { "type": "column", "columnId": "{colId-count}" },
                      "orderDirection": "desc",
                      "otherBucket": true,
                      "missingBucket": false,
                      "parentFormat": { "id": "terms" },
                      "include": [], "exclude": [],
                      "includeIsRegex": false, "excludeIsRegex": false
                    }
                  },
                  "{colId-count}": {
                    "label": "Count of records",
                    "dataType": "number",
                    "operationType": "count",
                    "isBucketed": false,
                    "params": { "emptyAsNull": false }
                  }
                },
                "columnOrder": ["{colId-game}", "{colId-count}"],
                "sampling": 1,
                "ignoreGlobalFilters": false,
                "incompleteColumns": {}
              }
            }
          },
          "indexpattern": { "layers": {} },
          "textBased": { "layers": {} }
        },
        "internalReferences": [],
        "adHocDataViews": {}
      }
    }
  },
  "panelIndex": "{panelIndex}",
  "gridData": { "y": 45, "x": 0, "w": 40, "h": 11, "i": "{panelIndex}" }
}
```

**Datatable column `isTransposed`:** bucket columns (terms) use `true`, metric columns (count) use `false`. This is the opposite of what feels intuitive.

---

## Dashboard wrapper structure

```json
{
  "type": "dashboard",
  "id": "{dashboardId}",
  "typeMigrationVersion": "10.3.0",
  "attributes": {
    "title": "Dashboard Title",
    "description": "",
    "timeRestore": false,
    "kibanaSavedObjectMeta": {
      "searchSourceJSON": "{\"query\":{\"query\":\"\",\"language\":\"kuery\"}}"
    },
    "optionsJSON": "{\"useMargins\":true,\"syncColors\":false,\"syncCursor\":true,\"syncTooltips\":false,\"hidePanelTitles\":false}",
    "panelsJSON": "[...array of embedded panel objects as JSON string...]",
    "pinned_panels": { "panels": { ... } }
  },
  "references": [
    {
      "type": "index-pattern",
      "id": "gp-dv-frame",
      "name": "{panelIndex}:indexpattern-datasource-layer-{layerId}"
    }
  ]
}
```

**Important:** The `references` array at the dashboard level must duplicate all references from all embedded panels. Each entry maps the same `name` as in the panel's `embeddableConfig.attributes.references`.

---

## Filter controls (pinned_panels / options_list_control)

Filter dropdowns above the dashboard are stored in `attributes.pinned_panels`:

```json
"pinned_panels": {
  "panels": {
    "{controlId}": {
      "config": {
        "dataViewRefName": "{controlId}:optionsListDataView",
        "field_name": "game.name.keyword",
        "title": "Game",
        "exclude": false,
        "exists_selected": false,
        "ignore_validations": false,
        "run_past_timeout": false,
        "search_technique": "wildcard",
        "selected_options": [],
        "single_select": false,
        "sort": { "by": "_count", "direction": "desc" },
        "use_global_filters": true
      },
      "grow": false,
      "order": 0,
      "type": "options_list_control",
      "width": "medium"
    }
  }
}
```

Each control's data view is wired via a reference in the dashboard `references` array:

```json
{
  "type": "index-pattern",
  "id": "gp-dv-session",
  "name": "{controlId}:optionsListDataView"
}
```

The `controlId` must be a UUID, and it appears as both the key in `panels` and the prefix in `dataViewRefName` and the reference `name`.

---

## Serverless constraints summary

| Constraint | Detail |
|-----------|--------|
| No legacy `visualization` type | Only Lens (`lnsXY`, `lnsMetric`, `lnsDatatable`, etc.) works |
| `_import` is the only programmatic path | `saved_objects/_find`, `_bulk_create`, direct PUT — all return 400 "not available with current configuration" |
| `adHocDataViews` must be empty `{}` | Populated inline data views cause 500 on import |
| `typeMigrationVersion` required | Missing = silent 500. Dashboards need `"10.3.0"`, index-patterns `"8.0.0"` |
| Panels embedded in dashboard | Separate `lens` saved objects referenced by `panelRefName` do not render |
| Data views must exist before import | Field list is populated at creation time; creating before data exists = empty fields |

---

## Available operationTypes

| operationType | Use case | `isBucketed` |
|--------------|----------|--------------|
| `date_histogram` | X-axis time buckets | `true` |
| `terms` | Split-by / group-by string field | `true` |
| `median` | Preferred over `average` for skewed data | `false` |
| `average` | Mean of numeric field | `false` |
| `max` | Max of numeric field | `false` |
| `min` | Min of numeric field | `false` |
| `count` | Document count | `false` |
| `unique_count` | Cardinality / distinct count | `false` |

---

## Workflow for future dashboard updates

1. **Build or edit in the Kibana UI** — it's far easier than hand-authoring NDJSON
2. Export via **Stack Management → Saved Objects → Export** (check "Include related objects")
3. Commit the exported NDJSON as `kibana/gamepulse-dashboard.ndjson`
4. To re-import after changes: `curl -X POST "$KIBANA_URL/api/saved_objects/_import?overwrite=true" -H "kbn-xsrf: true" -H "Authorization: ApiKey $KIBANA_API_KEY" --form file=@kibana/gamepulse-dashboard.ndjson`
