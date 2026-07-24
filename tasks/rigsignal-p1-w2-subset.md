# Task: rigsignal-p1-w2-subset — thin W2 space, viewer-role, and product-dashboard bundle extension

Session: `2026-07-24-w2-subset`. Work only in the supplied RigSignal worktree. Do not commit. `stm.sh` is sandbox-blocked for this task; skip it as directed.

## OPEN items — owner disposition required before implementation

1. **Bundle source paths and manifest names for `kibana-space` and `kibana-role`.** The ratified sources name the two new kinds and provide their payloads, but do not ratify their repository directories, filenames, or the corresponding manifest-count keys. The existing closed taxonomy only establishes the convention for `security-roles`. Do not choose e.g. `elastic/kibana-spaces/` or a singular/plural manifest key without owner disposition. [Contract Amendment A1.1–A1.5; provisioning-order §Deliverables lines 47–62; `tools/build_asset_bundle.py` lines 18–23, 77–91, 199–215]
2. **First-apply space branch.** The contract requires `POST /api/spaces/space` for first creation and `PUT /api/spaces/space/rigsignal` for re-apply, but does not ratify the installer’s existence-detection/recovery branch (for example, preflight GET versus POST-and-handle-conflict). The branch must be selected by the owner; it must still satisfy the PUT/GET rerun rules below. [Contract §2(a), especially create and idempotent re-apply; Contract Amendment A1.5]
3. **`rigsignal-streaming-lab` disposition outside `rigsignal`.** The scope ratifies only that it is owner-only and is never imported into `rigsignal`; it does not authorize a new default-space destination, retention/removal policy, or a different owner space. Preserve no invented behavior for it. [Contract DECIDED baseline; Contract Amendment A1.3–A1.4; `tools/install_assets.py` lines 838–847]

## Context, authority, and boundary

The normative authority is `W2-SPACE-ROLE-CONTRACT.md`, including Amendment A1; the existing provisioning-order task supplies the bundle-taxonomy, ordering, verification, and test-layout conventions which this task extends. The current builder and installer are implementation prior art, and the T8 directory supplies the accepted live payload/command evidence. [Contract §§2–5 and Amendment A1; provisioning-order §Deliverables and §Required provisioning design; `tools/build_asset_bundle.py` lines 18–23 and 175–215; `tools/install_assets.py` lines 799–862]

This is a thin W2 subset only: package and install the `rigsignal` space, `rigsignal_viewer` Kibana role, and the six product dashboards in that space; execute the authenticated live-gate subset only on ephemeral stacks. [Contract Amendment A1.3–A1.4]

The task must not alter the existing ES-native `security-roles` behavior: `security-roles` remains routed to Elasticsearch `PUT /_security/role/<escaped-name>`, while `kibana-role` is routed to Kibana’s Role API. [Contract §1 Role API decision and §3; `tools/install_assets.py` lines 363–372 and 799–807]

## Normative requirements

1. Extend the closed, checksummed bundle taxonomy with exactly two new asset kinds named `kibana-space` and `kibana-role`; validate their JSON payloads, include them in the manifest SHA-256 mapping and counts, reject unknown roots/kinds, and retain the existing validation behavior for all established kinds. [User task; provisioning-order §Deliverables lines 62–64; `tools/build_asset_bundle.py` lines 77–91 and 199–215; `tools/install_assets.py` lines 131–145]

2. Preserve the current established asset ordering—component templates, index templates, ES-native `security-roles`, pipelines, transforms—and insert the new Kibana sequence immediately before dashboard imports: `kibana-space` → `kibana-role` → `dashboard`. The order is deterministic by kind then asset name, as the current installer consumes the builder’s ordered manifest. [User task; provisioning-order §Required provisioning design items 3 and 5; `tools/install_assets.py` lines 838–862 and 1336–1345]

3. The `kibana-space` asset must carry the spike-accepted Variant A payload from `.../t8/payloads/space-variantA.json`: id `rigsignal`, name `RigSignal`, the ratified description, and its 56-item `disabledFeatures` allow-only-`dashboard_v2` list; omit `solution` entirely. Variant B, and any `discover_v2` or `visualize_v2` widening, are closed and forbidden. [Contract §2(a) Variant A; Contract Amendment A1.1 and A1.5; T8 `payloads/space-variantA.json`; T8 `943/T8-VERDICT.md` and `944/T8-VERDICT.md`]

4. The installer must create the space through Kibana `POST /api/spaces/space` on first apply and re-apply it through `PUT /api/spaces/space/rigsignal`, with JSON content and `kbn-xsrf: true`; after every successful PUT it must GET `/api/spaces/space/rigsignal` and assert the returned owned space body equals the packaged Variant A body. The first-apply branch remains OPEN item 2. [Contract §2(a) create/re-apply; Contract §5 steps 1 and smoke 1; T8 `scripts/run-t8.sh` steps 2 and 5; `tools/install_assets.py` lines 342–360]

5. The `kibana-role` asset must carry the spike-accepted `rigsignal_viewer` body from `.../t8/payloads/role-rigsignal_viewer.json`: empty Elasticsearch cluster privileges; exactly `read` and `view_index_metadata` on `logs-rigsignal.*` and `metrics-rigsignal.*`; and one Kibana block with empty `base`, `dashboard_v2: ["read"]`, and `spaces: ["rigsignal"]`. It must not grant `url_create`, Discover, Visualize, write/delete, cluster privileges, or privileges in another space. [Contract §3(b) and notes; Contract Amendment A1.1–A1.2; T8 `payloads/role-rigsignal_viewer.json`]

6. Install `kibana-role` using `PUT /api/security/role/rigsignal_viewer` on the Kibana endpoint, not native Elasticsearch `_security/role`; send JSON with `kbn-xsrf: true`, then GET that same Kibana Role API resource and compare its owned `elasticsearch` and `kibana` shape to the packaged body. [Contract §1 Role API decision, §3(b), and §5 step 2; T8 `scripts/run-t8.sh` step 2; `tools/install_assets.py` lines 342–360 and 799–807]

7. Treat both new kinds as normal manifest assets for dry run, including kind, asset name, HTTP method, and canonical Kibana request path; preserve the existing dry-run guarantee of no network access. [Provisioning-order §Deliverables lines 62–64; `tools/install_assets.py` lines 1264–1275]

8. Import exactly these six product dashboard NDJSON assets into the `rigsignal` space: `rigsignal-engine.ndjson`, `rigsignal-flamegraph-dashboard.ndjson`, `rigsignal-game-perf.ndjson`, `rigsignal-home.ndjson`, `rigsignal-software.ndjson`, and `rigsignal-system-health.ndjson`. Import each through `POST /s/rigsignal/api/saved_objects/_import?overwrite=true` as multipart `file` data of type `application/ndjson`, with `kbn-xsrf: true`; no product dashboard may use the current default-space import path. [Contract Amendment A1.3; User task; T8 `scripts/run-t8.sh` step 3; `tools/install_assets.py` lines 375–380 and 838–847; `tools/build_asset_bundle.py` lines 175–179]

9. `rigsignal-streaming-lab.ndjson` is owner-only and must never be imported into `/s/rigsignal`; its otherwise-unspecified disposition is OPEN item 3. [Contract DECIDED baseline; Contract Amendment A1.3]

10. For every dashboard import, assert the import result rather than treating an HTTP success alone as verification: parse the JSON result, require `success == true`, require a non-error result for every object in the submitted NDJSON, and require `successCount` to equal the submitted NDJSON object count. Re-import with `overwrite=true` must meet the same assertions, establishing safe rerun semantics. [User task; T8 `943/04-import-result.json`; T8 `scripts/run-t8.sh` step 3; `tools/install_assets.py` lines 148–171 and 838–847]

11. Preserve existing installer safety conventions: validate the complete bundle before network mutation, keep failures credential-free, stop before later success-only work on a failed asset verification, and do not advance the existing bundle marker as a partial-success signal. [Provisioning-order §Required provisioning design items 1 and marker-last paragraph; provisioning-order §Failure-mode and exit contract lines 150–170; `tools/install_assets.py` lines 1253–1262 and 1339–1345]

12. Do not package a native test user, test password, cross-space control role, or any other gate-only identity in the bundle. Create and remove such identities only inside the ephemeral live-gate leg. [Contract §3b(c) and §4; Contract Amendment A1.4; T8 `scripts/run-t8.sh` steps 2 and 6]

13. Add Python unit coverage only in `tools/tests/test_asset_tools.py`, using `unittest` and the existing import/mocking layout; pytest is unavailable on the gate host and must not be introduced as a required test runner. [User task; provisioning-order §Deliverables lines 47–60; `tools/tests/test_asset_tools.py` lines 1–25 and 402–403]

14. Unit tests must cover closed-taxonomy rejection, JSON/checksum/count round trip, deterministic extended ordering, dry-run paths, the unchanged ES-native `security-roles` route, Kibana space/role request paths and GET-after-PUT projections, product-dashboard allowlist selection, exclusion of streaming-lab from `rigsignal`, import-result failure detection, and a repeat installation with the same safe results. [User task; provisioning-order §Deliverables lines 54–55 and §Per-checkbox conformance-map testing convention; `tools/install_assets.py` lines 799–862 and 1264–1275]

## Acceptance criteria — executable assertions

### Bundle and installer assertions

| ID | Testable assertion | Source |
|---|---|---|
| A1 | A valid bundle includes both new kinds in its SHA-256 mapping and counts; an unknown kind/path, malformed JSON, missing count member, or checksum mismatch rejects before HTTP. | [Provisioning-order §Deliverables lines 62–64; `tools/build_asset_bundle.py` lines 77–91 and 199–215; `tools/install_assets.py` lines 131–145] |
| A2 | Ordering is exactly established ES kinds followed by `kibana-space`, `kibana-role`, then dashboards; names are deterministic within a kind. | [User task; provisioning-order §Required provisioning design item 3; `tools/install_assets.py` lines 1339–1345] |
| A3 | Space application uses the A1 Variant A payload with no `solution`, and PUT followed by GET returns the packaged body. | [Contract §2(a); Contract Amendment A1.1 and A1.5; T8 `943/01-space-get.json`] |
| A4 | The Kibana Role API PUT/GET round-trip returns only the packaged viewer role’s allowed owned shape; the ES-native shipper role still routes unchanged to `/_security/role/`. | [Contract §3(b); T8 `payloads/role-rigsignal_viewer.json`; `tools/install_assets.py` lines 363–372 and 799–807] |
| A5 | Each allowed product NDJSON is posted only to `/s/rigsignal/api/saved_objects/_import?overwrite=true`; its parsed result has `success: true`, no per-object error, and a matching success count. | [User task; T8 `943/04-import-result.json`; T8 `scripts/run-t8.sh` step 3] |
| A6 | A second run repeats the same space/role verification and dashboard import-result assertions without failure; no dashboard is imported into the default space by this W2 product path. | [Contract §2(a) re-apply; User task; `tools/install_assets.py` lines 838–847] |
| A7 | `rigsignal-streaming-lab.ndjson` has no request to `/s/rigsignal/...`; no additional destination behavior is claimed without resolving OPEN item 3. | [Contract Amendment A1.3; User task] |

### Ephemeral authenticated live-gate subset

All rows below run on ephemeral ES/Kibana 9.4.3 and 9.4.4 stacks after A1–A6, using temporary test identities only; T16–T17 and appliance/full-network checks are excluded. Each denial assertion records status shape where version-variable and verifies the stated absence of leaked data. [Contract §4; Contract Amendment A1.4; provisioning-order §Clean-stack and independent live evidence lines 135–148]

| Gate | Concrete check and pass assertion | Source |
|---|---|---|
| T1 | As `rigsignal_viewer_test1`, `GET /logs-rigsignal.events-default/_search` is 200 with seeded allowed hits. | [Contract §4 T1] |
| T2 | As viewer, `GET /metrics-rigsignal.gpu-default/_field_caps?fields=*` is 200. | [Contract §4 T2] |
| T3 | As viewer, search `metrics-system.cpu-default` is denied with no document, hit-count, or mapping leak. | [Contract §4 T3] |
| T4 | As viewer, search `logs-asus_bt10-default` is denied with the same zero-leak rule. | [Contract §4 T4] |
| T5 | As viewer, search `rigsignal-game-timeline` is 403. | [Contract §4 T5] |
| T6 | After an admin seed, as viewer POST a document to `metrics-rigsignal.gpu-default`; it is 403 and setup data is cleaned up. | [Contract §4 T6] |
| T7 | As viewer, `POST /logs-rigsignal.events-default/_delete_by_query` is 403 and the pre/post count is unchanged. | [Contract §4 T7] |
| T8 | A real Kibana viewer session renders `rigsignal-flamegraph-dashboard` in `/s/rigsignal` fully, including all four by-reference objects, with Variant A and no Discover/Visualize widening. | [Contract Amendment A1.1; T8 `943/T8-VERDICT.md`; T8 `944/T8-VERDICT.md`] |
| T9 | As viewer, `/s/rigsignal/api/saved_objects/_find?type=dashboard` lists the five other product dashboards and each renders fully; streaming-lab is not among this product-space set. | [Contract §4 T9 as corrected by Amendment A1.3] |
| T10 | As viewer, update or save-as-new of an existing product dashboard is denied (403); clean up if a write unexpectedly succeeds. | [Contract §4 T10] |
| T11 | As viewer, Share/Get-links URL creation is absent/disabled or its backing request is denied; record the observed version-specific surface. | [Contract §4 T11] |
| T12 | As viewer, cross-space `GET /s/gaming/api/saved_objects/_find?type=dashboard` is 403 or 404 with zero titles, IDs, or saved-object content. | [Contract §4 T12] |
| T13 | As an ephemeral control user scoped only to `default`, `GET /s/rigsignal/api/saved_objects/_find?type=dashboard` is 403 or 404 with zero saved-object content. | [Contract §4 T13; Contract §3b(c)] |
| T14 | As viewer, a disabled management path such as `/s/rigsignal/app/management/kibana/dev_tools` is hidden or denied. | [Contract §4 T14; Contract §2(a) Variant A] |
| T15 | As viewer, `GET /api/security/role/rigsignal_viewer` is 403. | [Contract §4 T15] |
| T18 | As viewer, `GET /logs-rigsignal.diagnosis-default/_search` is 200 with zero hits on a fresh stack, not 403. | [Contract §4 T18] |
| T19 | As viewer, ES|QL `POST /_query` against `metrics-system.cpu-default` is denied without index-existence, field-name, `columns`, or `values` leakage. | [Contract §4 T19] |
| T20 | As viewer, field caps on `metrics-system.network-default` is denied rather than a permitted empty success and leaks no field/type data. | [Contract §4 T20] |
| T21 | As viewer, explicit `POST /s/rigsignal/api/saved_objects/dashboard` is 403; clean up if it unexpectedly creates an object. | [Contract §4 T21] |

## Non-goals

- Owner-stack application or closure is excluded; this W2 phase uses ephemeral stacks only. [Contract Amendment A1.4]
- T16–T17 are excluded. [Contract Amendment A1.4]
- Appliance checks and full-network checks are excluded. [Contract Amendment A1.4]
- Variant B and all privilege/feature widening are excluded. [Contract Amendment A1.1]
- Release-artifact packaging is excluded. [User task; provisioning-order §Scope and exclusions]
- Native packaged test users and any production user-management work are excluded. [Contract §3b(c); Contract Amendment A1.4]

## Evidence obligations

Retain a sanitized result for every unit assertion and every required ephemeral live-gate row, including exact ES/Kibana image tags or digests, commands, exit status, request status, Variant A payload identity, role/space GET verification result, dashboard import success counts, and the T8 browser render artifacts for both versions. Do not retain credentials, passwords, authorization headers, or raw secret-bearing bodies. [Provisioning-order §Clean-stack and independent live evidence lines 135–148 and §Failure-mode contract lines 150–152; Contract Amendment A1.1; T8 `README.md`]

Run the unit suite with `python3 -m unittest tools/tests/test_asset_tools.py`; do not make pytest a gate requirement. The live-gate runner must use the existing clean-stack test layout, but its exact command integration is subject to OPEN item 1 only where bundle path/taxonomy naming blocks it. [User task; provisioning-order §Deliverables lines 55–57; `tools/tests/test_asset_tools.py` lines 402–403]

The final handoff must identify the resolved values for all OPEN items, list every required gate row as PASS/FAIL/UNRUN, and explicitly call out any unavailable ephemeral infrastructure rather than reporting an unrun leg as passing. [Provisioning-order §Clean-stack and independent live evidence lines 135–148 and final-handoff rule lines 209–221]

---

## Owner dispositions for OPEN items (2026-07-24, orchestrator)

1. **Taxonomy**: extend `ASSET_TYPES` symmetrically with `"kibana-spaces": "kibana_spaces"`
   and `"kibana-roles": "kibana_roles"`; files `elastic/kibana-spaces/rigsignal.json` and
   `elastic/kibana-roles/rigsignal_viewer.json` (same `ASSET_NAME` rules, same manifest-count
   key pattern as the five existing kinds).
2. **First-apply space branch**: preflight `GET /api/spaces/space/rigsignal` → 404 ⇒ `POST
   /api/spaces/space`; 200 ⇒ `PUT /api/spaces/space/rigsignal`. Any other status = hard fail
   (no POST-and-catch-conflict). The GET-after-apply assertion in requirement 4 applies to
   both branches.
3. **streaming-lab**: NO new behavior — it keeps exactly its current default-space handling in
   the existing tooling, and this task only guarantees it is never sent to `/s/rigsignal`
   (acceptance A7 as written). Any relocation is out of scope.
