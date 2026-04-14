---
name: elasticsearch-tsds
description: >
  Elasticsearch TSDS (Time Series Data Store) mapping rules, field type detection,
  backing index conflict resolution, and ES|QL validation patterns. Use when working
  with GamePulse data streams, diagnosing verification_exception errors, or planning
  mapping changes. Covers keyword vs text, .keyword suffix rules, dimension restrictions,
  rollover procedure, and pre-dashboard field validation.
metadata:
  author: gamepulse-project
  version: 1.0.0
---

# Elasticsearch TSDS — Mapping Rules & Operations

## 1. Field Type Rules

### keyword vs text — how to tell which you have

Fields declared as `keyword` in `fields.yml` are **native keywords** — no `.keyword` sub-field.
Fields that were _not_ covered by an active index template when data first landed are
auto-mapped by ES as `text` with a `.keyword` multi-field sub-field.

**Before writing any query or dashboard panel, verify the field type:**

```esql
FROM metrics-gamepulse.session-default
| KEEP gamepulse.session.id, gamepulse.game.name
| LIMIT 1
```

If the query returns `verification_exception` ("keyword in 000002, text in 000001"), you
have a backing index type conflict — see Section 3.

### .keyword suffix rules

| Index generation | Correct path | Wrong path |
|---|---|---|
| Old (pre-template) | `gamepulse.game.name.keyword` | `gamepulse.game.name` (returns null) |
| New (post-template) | `gamepulse.game.name` | `gamepulse.game.name.keyword` (returns empty) |

**Rule:** Use bare field paths on indexes created after template deployment. Only use
`.keyword` for text+keyword multi-fields (old indices). When in doubt, run the ES|QL
validation above first.

**Kibana-specific exceptions** (always use `.keyword` regardless of index age):
- `options_list_control` `field_name` — must be `.keyword` for text fields
- `last_value` `sourceField` in data tables — must be `.keyword` for text fields
- Bare text fields in controls/tables silently produce non-functional results

### When .keyword does NOT exist

On native-keyword fields (post-template indices), `.keyword` returns empty composite
aggregation results rather than an error. This is a silent failure — ES returns an
empty result set with no error message.

## 2. TSDS Restrictions

### What TSDS requires

Data streams with `index_mode: "time_series"` in `manifest.yml` have these constraints:

1. **Synthetic source** — automatically enabled, cannot be disabled
2. **No `object` or `nested` field types** — incompatible with synthetic source on ES 8.13+
3. **`dimension: true` on at least one keyword field** — used for TSDS routing key
4. **`dimension: true` is ONLY valid in TSDS context** — remove it from regular metrics streams

### Dimension fields in GamePulse

TSDS dimension fields in the 8 metric streams (cpu, gpu, memory, storage, network, audio, power, frame):
- `host.name` — `dimension: true` (ECS field, hostname)
- `gamepulse.session.id` — `dimension: true` (session UUID)

TSDS dimension fields in the session stream:
- `host.name` — `dimension: true`
- `gamepulse.session.id` — `dimension: true`

**Do NOT add `dimension: true` to:**
- `gamepulse.session.label` (not a dimension — changes within a session on game detection)
- Any numeric/float/long/boolean field
- Any `nested` field
- ebpf stream fields (ebpf stream is NOT TSDS — no `index_mode` in its manifest)

### Nested types in TSDS

If a field needs to be `nested` (array of objects), the data stream CANNOT use TSDS mode.
The eBPF stream (`thread_breakdown` is `nested`) was converted from TSDS to regular metrics:
- Remove `index_mode: "time_series"` from manifest
- Remove all `dimension: true` annotations in that stream's fields.yml
- Run `elastic-package check` to confirm

### TSDS counter fields in Kibana

Counter-type metric fields do not support `avg()` aggregation in Kibana Lens.
Use `max()` or `rate()` instead for counter fields.

## 3. Backing Index Conflict Detection & Resolution

### Detection

```esql
FROM metrics-gamepulse.session-default
| STATS count = COUNT() BY gamepulse.session.id
| LIMIT 5
```

If this returns `verification_exception` listing field conflicts (e.g., "keyword in
000002, text in 000001"), two backing indices have incompatible mappings.

Check which backing indices exist:
```
GET /_data_stream/metrics-gamepulse.session-default
```
Response includes `indices[]` with each backing index name and creation date.

### Root cause

Old backing indices were created before the integration package's index template
was deployed. ES auto-mapped string fields as `text`. New indices get the correct
mapping from the template (`keyword`, `float`, `histogram`, `nested`).

### Resolution

**Option 1 — Delete old backing index** (preferred when old data is dispensable):
```
DELETE /metrics-gamepulse.session-default-000001
```
This removes the conflict. New data flows into the remaining index with correct types.

**Option 2 — Reindex** (often impossible for TSDS):
TSDS backing indices have time-bounded write windows. Old-timestamped documents
cannot be inserted into a new backing index — ES rejects with `timestamp_error`.
Reindex is only viable for non-TSDS streams where the new index accepts the old timestamps.

**After deletion:** Run the ES|QL query again to confirm `verification_exception` is gone.

### Prevention

> **Always deploy the integration package and verify index templates are active BEFORE
> collecting any live data.**

After any `fields.yml` or template change:
1. Run `elastic-package check` to validate the package
2. Roll over all affected data streams (forces a new backing index with the new template):
   ```
   POST /metrics-gamepulse.cpu-default/_rollover
   POST /metrics-gamepulse.gpu-default/_rollover
   # ... repeat for all streams
   ```
3. Verify the new backing index has correct field types via the ES|QL check above

## 4. Rollover Procedure

When to roll over:
- After adding/changing a field mapping in `fields.yml` and redeploying the template
- Before collecting new data after a mapping change
- After deleting a conflicting old index and wanting to force a fresh start

```
POST /metrics-gamepulse.session-default/_rollover
POST /metrics-gamepulse.cpu-default/_rollover
POST /metrics-gamepulse.gpu-default/_rollover
POST /metrics-gamepulse.memory-default/_rollover
POST /metrics-gamepulse.storage-default/_rollover
POST /metrics-gamepulse.network-default/_rollover
POST /metrics-gamepulse.audio-default/_rollover
POST /metrics-gamepulse.power-default/_rollover
POST /metrics-gamepulse.frame-default/_rollover
POST /metrics-gamepulse.ebpf-default/_rollover
```

## 5. ES|QL Validation Pattern (Before Dashboard Building)

Always validate fields against live data before building a Lens panel. This catches
type conflicts, missing fields, and `.keyword` suffix errors before they become silent
dashboard failures.

```esql
-- 1. Confirm the field exists and has the right type
FROM metrics-gamepulse.session-default
| KEEP gamepulse.game.name, gamepulse.session.id, gamepulse.session.label
| LIMIT 3

-- 2. Confirm aggregations work
FROM metrics-gamepulse.cpu-default
| STATS avg_cpu = AVG(gamepulse.cpu.total_utilisation_pct) BY gamepulse.session.id
| LIMIT 5

-- 3. Confirm filter controls will work (keyword fields only)
FROM metrics-gamepulse.session-default
| WHERE gamepulse.game.name == "Starfield"
| STATS count = COUNT()
```

If any of these return errors, diagnose the field type before building the dashboard.

## 6. ES Transform Notes

- `top_metrics` with nested field paths (e.g., `gamepulse.hardware.gpu.model`) writes
  the full object hierarchy to the destination, not just the scalar. Use Python
  post-enrichment for nested keyword fields instead.
- Composite `terms` aggregations and `top_metrics` require keyword-type fields.
  Text fields without `fielddata: true` fail with "Fielddata is disabled".
- Cumulative sum window functions are NOT available in ES pivot transforms — must
  be computed in Python post-enrichment.
- `.keyword` suffix in group_by only works on old (pre-template) backing indices.
  On new indices it returns empty results. Use base field paths with a date range
  filter to restrict to post-template indices.
