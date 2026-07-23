# Task: rigsignal-p1-provisioning-order v2 — W1 asset obligation and §15.1 validator extension

Session: `2026-07-23d-provisioning-order`. Work only in the supplied RigSignal worktree. Do not commit; the orchestrator commits after review. Do not bump versions.

## STM contract

Before work:

```bash
CHRONO_SESSION=2026-07-23d-provisioning-order STM_AGENT=codex-spec@nuc \
  bash /home/dev/coding/Workflow/scripts/stm.sh recall
```

After every non-obvious discovery, decision, or failure and at completion, save a concise entry with that same session and `STM_AGENT=codex-spec@nuc`, using `--kind learning`, `decision`, `failure`, or `status` as appropriate. If STM is unreachable, record that once in the final summary and proceed without retrying.

## Authority, goal, and boundary

The normative source is `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p0-2026-07-22/W1-DIAGNOSISEVENT-CONTRACT.md`, especially Amendments 3 (§13), 5 (§15), and 6 (§16). The ratified shipper role is `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-handshake-2026-07-23/rigsignal_shipper-role.json`. The §15 operational sizing/serialization source is `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-outbox-spec-2026-07-23/`. If this task conflicts with those sources or a decision below, report the conflict; the normative sources and owner-ratified decisions control.

**Postcondition:** from a bundle-provisioned **fresh** install, `rigsignal-agent handshake check` exits `0` on ephemeral Elasticsearch/Kibana **9.4.3** and **9.4.4**. This means the complete enrollment chain works: W1 templates, exact scoped role, pre-created stream, scoped API key, protected credential/configuration material, expected cluster UUID, and target generation. Templates plus a role alone do not satisfy this task.

This task owns only the **post-Kibana asset installer** (`tools/install_assets.py`) and the repository asset bundle. It does not turn the clean-stack harness into a product installer. Starting Elasticsearch, setting `kibana_system`, starting Kibana, and waiting for Kibana's built-in assets remain harness/appliance territory. The existing `scripts/clean-stack/spike.sh` bootstrap is evidence prior art, not an installer implementation to copy into production.

The default namespace is exactly `logs-rigsignal.diagnosis-default`: it is the sole v1 DiagnosisEvent data stream. It is not shorthand for a wildcard and must never expand to another `logs-rigsignal.diagnosis-*` stream or a backing index.

## Scope and exclusions

In scope:

- Ship W1 component/index template assets and the exact `rigsignal_shipper` role through a new checksummed `security-roles` bundle kind.
- Make `install_assets.py` a fresh-install-only provisioning transaction that creates and validates the full W1 enrollment chain, including a scoped API key and protected local credential/configuration outputs.
- Add canonical-content, privilege, mapping, stream, and lifecycle checks; fix the current early bundle-marker defect by making marker/key release/configuration the final operations.
- Extend the clean-stack matrix for 9.4.3/9.4.4 fresh, rerun, pre-W1 refusal, and 9.4.3-to-9.4.4 upgrade/rollover evidence; rerun the pre-existing wire gate once as independent contract evidence.
- Extend the frozen DiagnosisEvent v1 corpus and validator with the §15.1 total serialized-byte rule, using one canonical serialization helper that the future outbox will use.
- Preserve ordinary uninstall behavior and add only the installation-specific API-key revocation required by `--purge`.

Out of scope, and explicitly not to be absorbed:

- migrating or repairing a pre-existing non-W1 diagnosis stream;
- release-artifact packaging of the bundle (repository bundle shipping is the definition of shipped here);
- appliance/ES/Kibana bootstrap, including ES → `kibana_system` → Kibana sequencing;
- Windows shipping and its primitive/live-gate work (§15.4);
- Kibana-side spaces, viewer role, dashboards, and viewer assets (W2 task);
- native-user creation (the production credential is a superuser-minted scoped API key); and
- durable outbox, drainer, spool semantics, status command, or reuse of `SpoolWriter` as durability prior art. `SpoolWriter` has no fsync and provides locking prior art only; `try_from_outcome` currently has no production caller.

## Deliverables and landing paths

| Path | Required deliverable |
|---|---|
| `elastic/component-templates/logs-rigsignal.diagnosis-mappings.json` | Byte-for-byte W1 component-template body from the contract, including `_meta.accepted_schema_versions: ["1"]`; SHA-256 must be `345e0d2898279929eb613b60d2bd250bbf73a13c7b4bbd1b793384e2ae00410c`. |
| `elastic/index-templates/logs-rigsignal.diagnosis.json` | W1 composition: `logs@mappings`, `logs@settings`, `logs@custom`, `ecs@mappings`, and W1 component last; `failure_store.enabled: false` and `index.mapping.ignore_malformed: false`. |
| `elastic/security-roles/rigsignal_shipper.json` | The ratified exact-name role body: cluster `monitor`; only `view_index_metadata` and `create_doc` on `logs-rigsignal.diagnosis-default`; no wildcard. |
| `tools/build_asset_bundle.py`, `tools/install_assets.py` | `security-roles` source/bundle taxonomy, checksums/counts/order/dry-run/canonical paths, fresh-only provisioning, verification barrier, marker ordering, local enrollment outputs, and uninstall/key-revocation hook. |
| `tools/tests/test_asset_tools.py` | Unit coverage for every taxonomy, checksum, canonical-equality, ordering, refusal, role-drift, marker-last, and failure-path assertion named below. |
| `packaging/uninstall.sh` and its existing test surface | Default uninstall preserves shared cluster state; `--purge` revokes only the stored installation-specific API key and removes protected local credentials/configuration. |
| `scripts/clean-stack/{lib.sh,matrix.sh}` | Fresh, rerun, pre-W1-refusal, upgrade/reverify, rollover, role CAN/CANNOT, and §15.1 live gate legs on 9.4.3 and 9.4.4. |
| `src/detectors/contract/diagnosis_event.rs` | The sole validator-boundary byte check, exact new error variant, and canonical serializer helper shared by the future outbox. |
| `fixtures/diagnosis_event/v1/{MANIFEST.md,positive/,negative/}` | EXTEND-only, reviewed-before-implementation §15.1 fixtures; no existing case or expected output may change. |
| `tasks/rigsignal-p1-provisioning-order.md` | This implementation contract and acceptance oracle. |

`security-roles` is a closed-taxonomy extension, not an ad-hoc file bypass. Both builder and installer must recognize only `elastic/security-roles/<name>.json`, use the same filename grammar as other assets, include each role in manifest `sha256` and `counts`, reject unlisted roots/kinds, and route the role to `PUT /_security/role/<url-escaped-name>`. Its dry-run line must name the kind, name, and that canonical request path. Bundle input must still exactly equal manifest entries.

## Required provisioning design

### Assets, ordering, and fresh-install fence

1. The installer validates the bundle completely before a network mutation: filename grammar, JSON, checksums, manifest counts (including `security_roles`), and canonical W1 asset bodies/digest.
2. Before any W1 PUT, inspect the diagnosis destination. If a diagnosis stream already exists and it is not demonstrably W1-composed/effective, refuse before mutation. The refusal must leave templates, role, stream, credentials, marker, and local output files unchanged. This is not a best-effort migration and not a delete/recreate path.
3. Install component templates before index templates; because the index template references the component, the component cannot be deleted while referenced. Any replacement/removal dependency handling is a future migration note only: this task performs no shared-asset deletion or repair path.
4. Obtain/verify required built-ins before W1 composition: `logs@mappings`, `logs@settings`, and `ecs@mappings` are required; `logs@custom` is optional only because the W1 index template declares it `ignore_missing_component_templates`. Gate both ES and Kibana to supported exact versions 9.4.3 or 9.4.4 before declaring installation successful.
5. PUT the canonical W1 component and index template. Presence from GET is insufficient: normalize the GET response to the canonical request body and compare content equality, including W1 metadata, composition order, `failure_store: false`, and `ignore_malformed: false`.
6. PUT the canonical role body. Its canonical JSON SHA-256 is the stored role digest. It must be compared by canonical content, not role-name presence. Reject a role body with wildcards, extra cluster/index/application/run-as privileges, an absent exact stream, or an altered grant set.
7. Administratively create `logs-rigsignal.diagnosis-default` with `PUT /_data_stream/logs-rigsignal.diagnosis-default`; verify it resolves to that exact data stream, not a backing-index name or wildcard target.

### Credential lifecycle and local enrollment

The installer executes superuser-side API-key minting with `role_descriptors.rigsignal_shipper` equal to the canonical shipped role. The shipper role itself must not gain `manage_own_api_key`; no native user is created. Store no encoded key in logs, marker metadata, fixture output, or reports; the complete non-secret lifecycle state is the versioned v2.4 state machine, rather than only one key ID/digest.

Write the encoded API key into a distinct credential file (`[elasticsearch] api_key = ...`) using secure create/replace semantics and mode `0600`; the containing directory is private. Write the non-secret generated handshake configuration separately with endpoint/CA only: v2.3's versioned §15.3 capsule, not a `[shipping]` config table, supplies expected UUID and 64-lowercase-hex target generation at tiers 3–4. The acceptance command may use the generated config plus the generated credential-file reference; it must not put credentials in argv or environment. The expected cluster UUID is a validated 22-byte base64url `ElasticsearchClusterUuid`, read from the provisioned target and explicitly recorded by this enrollment. No target-observation-on-first-use behavior is permitted.

On a rerun:

- If canonical role digest is unchanged and the stored key proves the required handshake/write behavior, retain the working key and do not churn credentials.
- If role digest has drifted, mint a replacement scoped key first; prove the replacement with handshake and a create write; atomically replace the 0600 credential file; then invalidate the stored old key by ID. A failure at any point before replacement leaves the old credential file/key usable and reports failure. Failure invalidating the old key after successful replacement is a non-successful cleanup failure: retain state identifying both keys for retry and never pretend that old-key revocation succeeded.

The bundle marker is not proof of partial work. It is written only after all required verification has passed, the active key has been released, and configuration/enrollment output has been atomically published. Any earlier failure must have no marker write. The current installer writes its marker even after failures; this task removes that behavior.

### Uninstall

Default uninstall removes local executable/service files according to current policy but leaves shared W1 templates, the shared `rigsignal_shipper` role, and diagnosis data stream/data intact. `--purge` additionally removes the protected local enrollment/credential state and revokes the installation-specific API key by its stored API-key ID. It still does not delete shared roles, templates, streams, or diagnosis data. A shared-role/data deletion operation requires a separately named, explicit destructive action and is not introduced here.

## §15.1 validator and corpus extension

Extend `DiagnosisEvent::try_from_outcome` at its one existing validation boundary. After the event is completely constructed but before it can be returned/enqueued, serialize it through one canonical `serialize_diagnosis_event(...) -> Vec<u8>` helper. That helper is the single byte-boundary authority: the validator counts its exact UTF-8 bytes, and the future outbox must persist/send those same bytes rather than independently serializing or recounting an event. It must have a documented stable serialization contract suitable for a future outbox caller; do not add a second byte validator to the outbox.

The only new error is exactly:

```text
ValidationError::EventBytesLimitExceeded { limit: u32, actual_saturated: u32 }
```

`limit` is exactly `1_048_576`. `actual_saturated` is `min(exact_serialized_utf8_byte_length, u32::MAX)`. Values above the cap are rejected, never truncated. The count is bytes after JSON escaping/serialization, not Rust `chars()`, source-text length, or an approximate envelope estimate. Keep existing error semantics unchanged.

Freeze the extension before validator implementation. Add fixture manifest rows and new EXTEND-only cases that prove:

- an exact 1,048,576-byte valid serialized event is accepted, plus a clean-stack live `201`/round-trip leg;
- a 1,048,577-byte event fails with exactly `EventBytesLimitExceeded { limit: 1048576, actual_saturated: 1048577 }`;
- multi-byte UTF-8 and JSON-escape constructions are measured from the serializer bytes, not scalar count or input source length;
- saturation maps a synthetic size-helper value above `u32::MAX` to `u32::MAX` without allocating more than 4 GiB; and
- the test harness calls the production canonical serialization helper, not a fixture-side duplicate.

The exact-cap live document must avoid unrelated per-field failures (notably Lucene keyword-term limits) and prove no `_ignored` field and no failure-store artifact. Existing fixtures are frozen: do not reformat, rename, mutate, reorder, or regenerate an existing input/expected file.

## Acceptance oracle — ordered executable verification barrier

All steps below run before a successful installer exit, in the stated order. A failed step stops success declaration, prints only the mapped sanitized failure, and prevents later success-only operations.

| Step | Executable assertion | Expected result/pass criterion |
|---:|---|---|
| 1 | Parse bundle; validate closed taxonomy, all checksums/counts, canonical W1 assets and role. | Every manifest input is known and checksummed; W1 component SHA equals `345e0d28…e00410c`; no network mutation. |
| 2 | Recover transaction state and obtain/validate the cluster UUID. | Recovery handles every persisted phase; UUID is pinned or initial bundled enrollment is the only accepted observation; redirects are disabled. |
| 3 | Read ES/Kibana versions and required built-ins. | ES and Kibana are each 9.4.3 or 9.4.4; `logs@mappings`, `logs@settings`, and `ecs@mappings` GET as present; `logs@custom` may be absent only under the declared optional rule. |
| 4 | Inspect existing diagnosis stream/template state before a W1 write. | No diagnosis stream, or a compatible W1 stream. Any pre-W1 stream produces the refusal in the table and a before/after state diff of zero changes. |
| 5 | Install and verify every manifest asset and every established G4 row: PUT component then index templates and `rigsignal_shipper`, canonical-GET-compare each desired body after its PUT, administratively create/verify the exact stream, and run `_simulate_index`. | Exact bodies equal desired canonical JSON; `_simulate_index` for `logs-rigsignal.diagnosis-default` shows W1 effective composition and strict mapping/settings; canonical role equals shipped body; exact stream exists; no wildcards or backing-index target. |
| 6 | Stage all local candidate credentials, handshake configuration, state, and capsule files; mint/reuse the scoped API key according to the stored canonical role digest when required. | Staged files are protected and not consumer-visible; key has role descriptors equal to shipped role; raw encoded secret is absent from output/evidence. |
| 7 | Verify the candidate with real-stream behavior. | Valid v1 DiagnosisEvent `_create` returns 201; strict unknown-field and malformed-scalar writes return client-visible rejection; no accepted write has `failure_store:"used"`, `_ignored`, or a failure-store document. |
| 8 | Exercise the exact-role matrix using the candidate key. | CAN: E1 `GET /`, E2 exact component-template read with required filter path, E3 exact stream mapping read, and exact-stream `create_doc`. CANNOT: overwrite/re-create, update, delete, get, search, other-stream write, component/index-template mutation. |
| 9 | Transactionally publish credentials.toml, handshake.toml, state.json, and shipping-policy-v1.toml so consumers cannot observe mixed generations. | The published generation, UUID, credential, configuration, state, and capsule are one generation. |
| 10 | Run the final zero-environment enrollment handshake against the published files, revoke and confirm every pending old ID using the protected administrator credential, then publish committed state. | `rigsignal-agent handshake check --config <published-config> --credentials-file <published-0600-file>` exits 0 and emits `ready/ready` with the published expected UUID and target generation; each pending ID is invalidated or already-invalidated before committed state, which retains no candidate/mint data or unresolved pending ID. |
| 11 | PUT/GET-verify `rigsignal-bundle-meta` last. | Marker appears only after Steps 1–10 pass, and its version/source commit match the bundle. |

`_simulate_index` is composition evidence only; it never substitutes for Steps 7–8's real stream and scoped-credential proof. The verifier must use canonical request-body equality instead of a `GET 200` presence check for component, index, and role bodies.

## Clean-stack and independent live evidence

Extend `scripts/clean-stack/matrix.sh` rather than treating a one-off manual stack as acceptance. Each leg records exact image tags/digests, command exit, sanitized request statuses, canonical role/template digests, marker timing, and assertion rows.

| Leg | Versions | Assert | Pass criterion |
|---|---|---|---|
| `fresh` | 9.4.3; 9.4.4 | Build bundle; bootstrap stack via harness; run installer; execute barrier. | Fresh installation reaches handshake exit 0 and all Step 1–11 assertions pass. |
| `idempotent-rerun` | 9.4.3; 9.4.4 | Re-run same bundle after `fresh`. | All canonical bodies remain equal, role/key state is safe, no unnecessary key replacement, and marker remains last/successful. |
| `pre-w1-refusal` | 9.4.3; 9.4.4 | Administratively create a diagnosis stream first using the old/non-W1 template, snapshot state, invoke installer. | Nonzero clear migration-required refusal; state snapshot unchanged; no key, marker, or local enrollment output is written. |
| `stackupgrade` | 9.4.3 → 9.4.4 | Provision on 9.4.3; retain volumes; upgrade ES/Kibana through harness; re-run verification barrier. | W1 bodies/effective composition/handshake remain valid on 9.4.4. |
| `rollover` | after 9.4.3 → 9.4.4 | Superuser pre-creates/rolls over exact diagnosis stream; use shipper key after new backing index exists. | `handshake check` still exits 0 and exact stream `create_doc` succeeds; this proves grant is on stream name, not a backing-index pattern. |
| `bytes-live` | 9.4.3; 9.4.4 | Construct exact-cap EventBytes fixture and one-over fixture using canonical helper. | At cap gets real 201 and exact round-trip with no `_ignored`/failure-store; one-over is exact local validator error. |

After matrix completion, run `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-handshake-2026-07-23/wire-gate/run-wire-gate.sh` once, standalone, against the built binary. It is independent §16 contract evidence, not the installer gate and not a replacement for matrix legs.

## Failure-mode and exit contract

Installer exits nonzero for every row below; it must emit the stated stable, credential-free message prefix and a failure table where applicable. API keys, passwords, authorization headers, and all raw installer/probe bodies are forbidden from output/evidence. The only body exception is v2.7's §16.2.17 bounded, allowlisted, structurally redacted non-secret standalone-gate evidence.

| Path | Trigger | Exit/message contract | Required no-change or recovery assertion |
|---|---|---|---|
| bundle invalid | Unknown asset kind/path, malformed JSON, checksum/count mismatch, or noncanonical W1 asset/role. | nonzero: `install failed: bundle validation:` | No HTTP request/mutation. |
| unsupported stack | ES/Kibana not exactly 9.4.3 or 9.4.4, or required built-in absent. | nonzero: `install failed: prerequisite:` | No W1/role/stream/key/marker/local enrollment mutation. |
| pre-W1 stream | Existing diagnosis stream is non-W1/incompatible. | nonzero: `install refused: existing diagnosis stream is not W1; migration is required` | Before/after cluster and local-state snapshots equal; reruns make no marker write/advance. |
| asset mismatch | PUT/GET canonical component or index body differs; simulation fails. | nonzero: `install failed: W1 asset verification:` | No marker/key release/configuration publish; reruns make no marker write/advance. |
| role mismatch | Canonical role equality, no-wildcard policy, or requested role descriptor differs. | nonzero: `install failed: shipper role verification:` | No active credential replacement; existing key/file retained; reruns make no marker write/advance. |
| stream/mapping failure | Stream cannot be created/resolved; valid/negative real-write proof fails; failure-store or `_ignored` observed. | nonzero: `install failed: diagnosis stream verification:` | No marker/local enrollment publish; reruns make no marker write/advance. |
| key mint/use failure | Superuser mint, scoped key handshake, CAN/CANNOT matrix, or active-key test fails. | nonzero: `install failed: shipper credential verification:` | New secret removed if unused; old working key/file remains when replacing; reruns make no marker write/advance. |
| role-drift revoke failure | Replacement is published but old key cannot be invalidated. | nonzero: `install failed: old shipper API key revocation:` | New verified file/key stays active; state retains both IDs for retry; reruns make no marker write/advance. |
| local output failure | Secure credential write/atomic replace, config/enrollment write, mode/ownership verification fails. | nonzero: `install failed: enrollment output:` | Previous valid file remains if replacing; reruns make no marker write/advance. |
| marker failure | All prior barriers passed but marker PUT/GET fails. | nonzero: `install failed: bundle marker:` | Credential/config remain valid; failed reruns make no marker write/advance and never report full success. |
| purge revoke failure | Stored installation key cannot be revoked. | nonzero: `uninstall purge failed: shipper API key revocation:` | Do not claim purge complete; shared W1 assets/data are untouched. |
| byte cap | Serialized event is over 1 MiB. | Validator returns exactly `EventBytesLimitExceeded { limit, actual_saturated }`; no enqueue/network attempt. | Existing frozen error variants/cases remain unchanged. |

The pre-W1 refusal is intentionally distinct from compatibility repair: do not auto-delete, overwrite, migrate, or silently accept it. Handshake classified exits remain its existing closed map `0`, `10–16`; provisioning acceptance requires `0` only.

## Per-checkbox conformance map

Every row has a falsifiable assertion, a named verification method, and an objective pass criterion. “Implementation + live gate” means neither code review nor a `GET 200` can discharge it.

| Contract / task checkbox | Assert | How verified | Pass criterion |
|---|---|---|---|
| W1 blocker / postcondition | A fresh bundle install completes full enrollment, not merely templates/role. | `fresh` matrix on both versions, Step 1–11 barrier. | Generated-config handshake exits 0 and scoped create succeeds. |
| Installer boundary | No ES/Kibana bootstrap becomes installer behavior. | Unit test dry-run/network call log; matrix harness bootstrap log. | Installer begins after ready ES/Kibana/built-ins; it never writes `kibana_system` or starts services. |
| Fresh-only fail closed | Pre-W1 stream has no mutation path. | `pre-w1-refusal` snapshot diff. | Required refusal, nonzero, zero state delta. |
| W1 component asset | Component content and accepted set are exactly W1. | SHA-256 test and canonical GET equality. | SHA exactly `345e0d28…e00410c`; `_meta.accepted_schema_versions == ["1"]`. |
| W1 index asset | Effective W1 composition plus hardened settings. | Canonical equality + `_simulate_index` + real negative writes. | Component last; failure store disabled; malformed write rejected with no `_ignored`. |
| §16.1 role | Exact role grants only required exact-name operations. | JSON structural test, canonical role GET, live CAN/CANNOT rows. | Role equals ratified body; all CANs pass and every forbidden operation is denied. |
| security-roles taxonomy | Role assets are bundle assets, checksummed/countable/installable/dry-runnable. | Builder/loader malformed/missing/checksum/count/dry-run tests. | Manifest has `security_roles`; canonical role PUT path shown and used. |
| stream provisioning | Exact default stream is administratively present. | GET/resolve data-stream and post-rollover create. | Only `logs-rigsignal.diagnosis-default` is target; shipper writes after rollover. |
| §15.2 enrollment | Cluster UUID is validated/pinned; no TOFU. | Generated config inspection plus handshake expected-vs-observed test. | Stored UUID is 22-char base64url and handshake succeeds only on exact match. |
| target generation | The exact Option A tuple/KAT/zero-request rejection oracle governs the published 32-octet/64-lowercase-hex generation. | Offline tuple/KAT tests before mocked HTTP and generated config/handshake parser test. | Exact scheme, algorithm, count, UTF-8 sorted paths, raw hashes, and KAT pass; wrong count/order/path/member digest/manifest value rejects with zero requests. |
| key scope/mint | Superuser mints an API key using exact role descriptors; no native user/`manage_own_api_key`. | API request capture redacted; role JSON/static test; live key matrix. | Descriptor canonically equals role; no native-user API call; no secret leakage. |
| credential lifecycle | Role drift replaces safely and invalidates only after new key passes. | Inject failures across mint/test/replace/revoke; inspect file/key state. | Old key remains until replacement validated; file replace atomic/0600; old ID revoked last. |
| marker-last | Marker/config/key release occur only after all barriers. | Ordered mock request log, published-file handshake, and injected failure tests. | Published credential/config/state/capsule verify, every pending old ID is revoked, committed state is published, then marker is last; no marker write/advance on earlier failure. |
| uninstall | Default preserves shared state; purge revokes installation key only. | Shell/unit tests with mocked ES and staging root. | No template/role/data deletion request; purge calls invalidate stored ID. |
| §15.1 mechanism | One canonical serializer is the byte authority and validator has exact variant. | Rust unit tests and static call-path test. | At-cap accepts; one-over has exact fields; no duplicate byte validator. |
| §15.1 Unicode/escape | Count is exact serialized UTF-8 byte count. | New frozen fixtures using multibyte and escaped strings. | Helper count matches emitted byte vector, not character/input length. |
| §15.1 saturation | `actual_saturated` saturates without huge allocation. | Size-helper unit test with synthetic `usize > u32::MAX`. | Returned `u32::MAX`; test allocates no >4 GiB buffer. |
| §15.1 live | Exact-cap event survives real ES with strict semantics. | `bytes-live` matrix leg. | 201 + exact round trip; no failure-store/_ignored; over-cap never reaches ES. |
| corpus discipline | Corpus extension precedes code and existing corpus is untouched. | `fixture-only-§15.1-byte-cap-v1` checkpoint identifier, its recorded approval, then hash/path diff test. | Recorded approval of `fixture-only-§15.1-byte-cap-v1` precedes the production diff; only new §15.1 files/manifest rows added; all pre-existing fixture bytes identical. |
| live matrix | Fresh/rerun/refusal on both versions and upgrade/rollover evidence are mandatory. | Named matrix legs above. | Every assertion row PASS on all required legs. |
| independent §16 evidence | Existing wire contract remains independently runnable. | Standalone `run-wire-gate.sh <binary>`. | Script finishes PASS; evidence is separate from installer report. |

## Implementation hazards and verification discipline

- `CLI` dispatches before ordinary config loading and there is no production `status` command; this task must not rely on either as provisioning infrastructure. The acceptance command uses the existing handshake-specific config/credential inputs.
- The generated role must remain exact-name scoped after rollover. Never “fix” a rollover failure by granting backing-index or wildcard names.
- PUT dependency order matters: component before index; do not attempt deletion while the index template references the component.
- Treat response-body evidence as sensitive by default. Persist only sanitized statuses, hashes, IDs where allowed, image identity, and bounded non-secret diagnostics.
- Release artifact packaging is deliberately not a reason to omit repository assets. “Shipped” for this task means source-tree bundle inclusion verified by `build_asset_bundle.py` and `load_bundle`.
- Keep fixture creation and review before validator code, so the implementation cannot define its own oracle.

Run at least:

```bash
python3 -m pytest tools/tests/test_asset_tools.py
cargo fmt --manifest-path src/Cargo.toml -- --check
cargo test --manifest-path src/Cargo.toml --locked
bash scripts/clean-stack/matrix.sh fresh 9.4.3
bash scripts/clean-stack/matrix.sh fresh 9.4.4
bash scripts/clean-stack/matrix.sh stackupgrade 9.4.3 9.4.4
bash /home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-handshake-2026-07-23/wire-gate/run-wire-gate.sh <built-rigsignal-agent>
```

The final implementation handoff must include the retained matrix evidence for all legs, fixture-corpus before/after hashes, the canonical role/component hashes, and a conformance-row result for every table row above. It must explicitly call out any unavailable Docker/Windows gate rather than treating an unrun command as a pass.

## Historical v1 open question — resolved by v2.1

Only one owner fork remains: §16.4.2c deliberately assigns **target-generation derivation** to provisioning/outbox work but provides neither canonical input tuple nor generation-rotation rule. This task must receive a ratified derivation and update trigger (for example, which immutable enrollment/asset inputs are hashed and whether any successful reprovision rotates it) before implementation can claim stable target-generation semantics. The supplied 64-lowercase-hex representation and required presence are already fixed and are not open for debate.

## v2 normative closure — Sol review disposition

This additive correction controls any incomplete or conflicting v1 task wording while preserving ratified fresh-only fail-closed, exact-name grants, installer boundary, marker-last, uninstall/purge split, and corpus extend-only decisions. The preceding open question is retained as history and resolved here. No migration, shared-asset deletion, wildcard grant, native-user production credential, or bootstrap work is authorized.

### v2.1 Immutable assets, canonical GET comparison, and Option A

The listed bundle members MUST be byte-identical to their evidence artifacts. Parsed JSON equality is insufficient: reformatting, key reordering, or newline change invalidates the raw hashes and KAT.

| Logical path | Evidence artifact | Required raw SHA-256 |
|---|---|---|
| elastic/component-templates/logs-rigsignal.diagnosis-mappings.json | app-p0-2026-07-22/W1-component-template.json | 345e0d2898279929eb613b60d2bd250bbf73a13c7b4bbd1b793384e2ae00410c |
| elastic/index-templates/logs-rigsignal.diagnosis.json | app-p0-2026-07-22/W1-diagnosis-index-composition.json | 5f4d4f403fc17a1096b2d2b1c8a43bad94efa52b05c3b117961333b2f3d52199 |
| elastic/security-roles/rigsignal_shipper.json | app-p1-handshake-2026-07-23/rigsignal_shipper-role.json | 6eb0279c7e05b94bfd96083508a8c7e6ad5ca9cb65e531654bd6e0ae3eca7ed2 |

Reject duplicate JSON keys. Canonical JSON means RFC 8785/JCS. GET comparison projects only component_templates[0].component_template, index_templates[0].index_template, or the exact rigsignal_shipper role object, respectively; missing, plural, or wrong-name objects fail. JCS-serialize the projection and the matching request body and compare bytes. Server envelope/name/timestamp/order is not projected. Lifecycle role digest is SHA-256 of JCS role body, exactly 05b58b8369bc4212fcffa0ea81621ef10d6d57f1de464fbc3f562842a9cbafd7, distinct from the raw role hash.

Owner ratifies Option A, fixed W1 raw-content root. Manifest target_generation has scheme "rigsignal:target-generation:w1-assets:v1", algorithm "sha256", input_count 3, inputs (strictly UTF-8-path-sorted) of the three logical paths and exact raw hashes above, and value a7ed20a4b4bfe0b2e5597a065e8bdaa5161b0d962e1a502d3db3bbcc97e8ee7a. Recompute before any HTTP request:

    SHA256(b"rigsignal:target-generation:w1-assets:v1\0" || u32be(3)
     || for each sorted entry: u32be(path_len) || path_utf8 || sha256(raw_asset_bytes))

Offline tests recompute this KAT from the evidence-identical bytes and reject wrong count/order/path/member digest/manifest value with zero requests. Persist the same 32 bytes in protected state/capsule, render only 64 lowercase hex, and never learn it from Elasticsearch. Generation changes only after changed trio passes full barrier; same-bundle rerun retains it; role change rotates key and generation; template-only change rotates generation but retains a proved-valid key. Formatting-only changes rotate and exact byte reversion reuses old value. Identical trio on different clusters still needs separately pinned UUID.

### v2.2 Exact fresh fence, archive gate, and complete barrier

Existing stream is W1-compatible only if all proofs exist; otherwise it is pre-W1 and refuses before W1 PUT:

1. GET data stream logs-rigsignal.diagnosis-default names exactly one data stream and every backing index; a backing index, alias, wildcard, missing list, or second stream is not proof.
2. Protected committed v1 state belongs to this installation and has the same expected UUID plus a valid recomputed Option-A generation and JCS role digest. Its committed generation/role are trusted previous state and need not equal the incoming bundle.
3. For every backing index, GET mapping and flat settings; JCS-project every W1-owned path: mappings.properties.@timestamp, mappings.properties.event.properties.id, mappings.properties.host.properties.name, mappings.properties.observer.properties.name, mappings.dynamic, mappings.properties.rigsignal.properties.diagnosis.dynamic, the full recursive mappings.properties.rigsignal.properties.diagnosis properties tree, index.mapping.ignore_malformed, and index.failure_store.enabled; compare each to the same desired W1 _simulate_index projection. Current templates never retrofit old backing indices; any incompatible backing index refuses.
4. After their PUTs, the desired component, index, and role v2.1 canonical GET projections pass. This permits authorized role repair and compatible changed-trio transactions while the prior state/backing-index proofs still refuse incompatible indices.

Refusal/mismatch state deltas compare only owned resources: three W1 projections; exact data stream and each backing mapping/settings projection; rigsignal-bundle-meta; stored API-key IDs plus invalidated state; byte/hash/mode/owner/directory-entry inventories for state.json, credentials.toml, handshake.toml, shipping-policy-v1.toml. Pass is zero owned delta, or on failed rerun no marker write/advance.

Support only pairs (Elasticsearch 9.4.3, Kibana 9.4.3) and (9.4.4, 9.4.4); mixed pairs refuse. After Kibana ready, poll logs@mappings, logs@settings, ecs@mappings each second up to 60 seconds; logs@custom remains optional only under current ignore_missing rule. Timeout fails before W1 mutation.

Install and verify every existing manifest asset and preserve all established G4 assertions before credential release/marker: component/index templates, pipelines, transforms, dashboards, and W1 assets in dependency order. Any failure prevents release and marker. Shared-asset removal/replacement is future migration only and is not an implementation obligation.

Archive parsing rejects duplicate member paths including manifest, nonregular/symlink/hardlink/device members, absolute/traversal paths, duplicate JSON keys, malformed SHA/count types, unmanifested/missing members, and unknown roots. Members, manifest SHA mapping, and counts must be exact equal sets.

### v2.3 Installation interface and §15.3 capsule

install_assets.py requires --bundle PATH --endpoint HTTPS_URL --ca-file PATH --kibana-endpoint HTTPS_URL --kibana-ca-file PATH --admin-credentials-file PATH --agent-binary PATH --profile user; --enrollment-root PATH is test-only. Environment does not supply endpoints/CAs/credentials/profile. Invoking effective UID is installation principal. v1 shipping provisioning supports only user: its production enrollment root is XDG_STATE_HOME (or HOME/.local/state) rigsignal/enrollment and its sibling outbox, with explicit agent binary; --profile system is rejected as unsupported/broker-required because a system daemon must use a broker. Elasticsearch and Kibana endpoints are HTTPS and all installer/probe HTTP disables redirects.

Both CA files and the administrator credential are no-follow regular files, invoking-UID-owned and 0600 or stricter. Admin TOML uses existing elasticsearch api_key or username/password grammar, authenticates both Elasticsearch and Kibana, and is never copied into state/output. Committed root files are exactly credentials.toml, handshake.toml, shipping-policy-v1.toml, state.json. Root/candidate directory mode is 0700, each file 0600. Use directory-descriptor/no-follow traversal, same-directory 0600 temp, fsync/recheck, atomic rename, directory fsync, never truncate. Test-root tests prove all properties.

The capsule is shipping-policy-v1.toml, UTF-8 TOML with no unknown keys:

    ship_mode = "on"
    install_profile = "user"
    outbox_root = "/absolute/profile-specific/outbox"
    target_generation = "64-lowercase-hex"
    expected_cluster_uuid = "22-byte-base64url"

Bundled provisioning atomically publishes explicit ON after candidate verification with final credential/configuration release. Absence is user-managed OFF; no-ship/fixture/offline exclusions win first. After exclusions, capsule validation is required even when higher-tier values override individual fields. Corrupt/unreadable/wrong-owner/over-permissive/symlinked/unknown-key/invalid explicit ON fails closed before detector dispatch. It contains exactly the five §15.3 fields; shipping-policy-v1.toml itself provides the version.

Update handshake/outbox consumer to read capsule at tiers 3-4. Once present it replaces protected-config shipping table for expected UUID/target generation; flags and all six §16 environment overrides remain higher precedence. A generated --config …/handshake.toml resolves its sibling shipping-policy-v1.toml in the user profile enrollment root. Generated handshake.toml has endpoint/CA only. Tests cover valid parser, OFF absence, invalid ON failures, exclusions-first, tier replacement, that exact sibling lookup, and final zero-environment command with all six RIGSIGNAL handshake overrides unset, passing only config and dedicated credentials-file reference.

### v2.4 Crash-recoverable credential transaction

state.json is duplicate-key-rejecting JSON whose only allowed keys are version, phase, expected_cluster_uuid, target_generation, role_jcs_sha256, active_key_id, pending_revoke_ids, pending_mint_name, and candidate_key_id; all are required. version is integer 1; phase is one of committed, mint_intent, candidate_staged, candidate_verified; expected_cluster_uuid is a 22-character base64url string; target_generation and role_jcs_sha256 are 64-lowercase-hex strings; active_key_id, pending_mint_name, and candidate_key_id are string-or-null; pending_revoke_ids is an array of sorted, unique strings. Every key ID is at most 1024 bytes and pending_mint_name at most 255 bytes; unknown keys, wrong types, invalid nullability, duplicate IDs, or invalid encodings reject.

committed requires non-null active_key_id, null pending_mint_name/candidate_key_id, and empty pending_revoke_ids. mint_intent requires non-null pending_mint_name and null candidate_key_id. candidate_staged requires non-null pending_mint_name and candidate_key_id and retains the returned ID plus the candidate secret in its 0600 candidate credential file. candidate_verified has the same non-null candidate fields and is entered only after candidate handshake and exact-stream _create succeed. Before publication active_key_id remains the previous active ID (or null on first install); after transactional publication it is the candidate ID and the replaced ID is pending revocation.

On rerun, obtain/validate UUID with redirects disabled and compare to state before mint, credential/config/capsule write, marker write/advance. Mismatch refuses and never repins. Two-cluster test proves zero key/config/capsule/state/marker mutation. Initial bundled enrollment is the only accepted observation.

Recovery precedes normal work: mint_intent discovers candidates by deterministic name and revokes them; candidate_staged is verified or revoked; candidate_verified completes publication or revokes it. Already-invalidated confirmation is success. Every unpublished candidate is revoked and secret removed. Missing/revoked/unproved unchanged-digest active credential enters replacement, never silent success.

The sole normative publication sequence is Steps 1–11 in the acceptance oracle above; no second or alternate publication order is permitted.

Template-only change skips mint only after active proof and follows the applicable Steps 6–11. Role drift follows Steps 6–11 and rotates key. Same bundle with equal UUID/generation retains valid key/generation and cleans pending IDs before success.

| Fault injection/crash point | Required assertion after restart |
|---|---|
| before/after mint response | intent finds/revokes named candidate; no orphan; old committed credential works |
| candidate write or handshake/create verify | candidate revoked/secret removed; old credential/state/marker unchanged |
| each credentials/config/capsule publication | no partial visible file; recovery completes proved candidate or revokes; no marker write/advance |
| candidate/committed state publication | atomic old-or-new parseable state; active/pending IDs never lost |
| each revoke/confirmation | unresolved IDs remain; non-success; new active enrollment usable; no marker write/advance |

### v2.5 Purge, privilege proof, and immutable JCS bytes

uninstall.sh --purge requires --endpoint HTTPS_URL --ca-file PATH --admin-credentials-file PATH --enrollment-root PATH. It rejects absent/redirected/unprotected inputs and authenticates only protected administrator credential. It discovers every candidate using a persisted pending_mint_name, then revokes active_key_id ∪ pending_revoke_ids ∪ candidate_key_id ∪ those discovered candidates, confirms invalidated/already-invalidated for every ID, and only then deletes candidate and committed credential/config/capsule/state material. Failure retains every retry-critical local file/state and returns uninstall purge failed: shipper API key revocation:. Default uninstall still preserves shared W1 assets/data.

Role matrix has duplicate _create same ID -> 409 delivery proof and additionally shipper PUT exact-stream _doc existing-id -> 403 overwrite proof. Administrator pre-creates logs-rigsignal.diagnosis-other; shipper _create -> 403 proves authorization denial. Separate strict writes use unknown root, unknown rigsignal.diagnosis, malformed rigsignal.diagnosis.confidence; each rejects with no _ignored/failure-store artifact.

RFC 8785/JCS is sole DiagnosisEvent serialization. try_from_outcome returns ValidatedDiagnosisEvent with private immutable event and canonical_bytes Arc byte slice, exposes only immutable event() and canonical_bytes(), and has no mutable accessor. It calls serialize_diagnosis_event_jcs exactly once; future outbox persists/sends those bytes, never reserializes/recounts. Named saturate_event_byte_count(actual: u64)->u32 is called in production over-cap branch; synthetic 4294967296 returns 4294967295 without giant allocation.


### v2.6 Frozen §15.1 corpus extension before implementation

Before any validator implementation change, land and review a fixture-only checkpoint. It adds exactly the following files and MANIFEST rows; production fixture runner, never fixture-side serializer, calls production JCS helper.

| Files | Frozen input | Required expected sidecar |
|---|---|---|
| positive/24-event-bytes-exact-cap.input.json and .expected.json | Literal expansion of positive/01-diagnosis.input.json with only plain_language replaced by ASCII a repeated exactly 1,047,209 times; its RFC-8785 event is exactly 1,048,576 bytes. | Exactly {"result":"accepted","serialized_bytes":1048576}. |
| negative/24-event-bytes-one-over.input.json and .expected.json | Literal expansion of the same source with plain_language ASCII a repeated exactly 1,047,210 times; serialized length exactly 1,048,577. | Exactly {"variant":"EventBytesLimitExceeded","limit":1048576,"actual_saturated":1048577}. |
| positive/25-event-bytes-unicode-escape.input.json and .expected.json | Literal expansion of same base with plain_language equal to (U+00E9 followed by U+000A) repeated exactly 8,192 times, then ASCII a repeated exactly 1,014,441 times. Its source is 1,039,017 UTF-8 bytes while its JCS string is 1,047,209 bytes and event is 1,048,576 bytes. | Exactly {"result":"accepted","serialized_bytes":1048576,"source_plain_language_utf8_bytes":1039017}. |
| negative/26-event-bytes-saturation.input.json and .expected.json | Harness-only helper fixture, not allocated Outcome: {"synthetic_serialized_bytes_u64":4294967296}. | {"actual_saturated":4294967295,"allocation_bytes_max":1048576}. |

The input expansions/counts and expected-sidecar bytes above are frozen before code work; no later computation supplies a missing oracle. The checkpoint proves exact-cap live document avoids unrelated per-field failures. Existing fixture inputs/expected sidecars remain byte-identical; MANIFEST.md is intentionally sole pre-existing change. Baseline SHA-256 inventory of every other existing corpus file:

    0c3f9673c15514d10079d9a1a4afb6e8df680e33f5618f12392dec57a69d422e  contexts/diagnosis-finding.json
264d31de06772aa223c9aef3616dd2cc8a1362486243bbf55e35b4328ba82a8c  contexts/missing-detector-identity.json
b290b0219da8849a8ee444337fe0c340332a04f77732b2501528ba79f833f705  contexts/missing-detector-rule-version-identity.json
d17cbdc2c062490fe4c1123b2d222a0879e1de1a04feb64cbd9b35ff36678672  contexts/non-finding.json
a3c7f68a03b2aefc111c4f63ddebec68be93b0903bc25269a137e1201c99abcd  negative/03-empty-evidence.expected.json
1f3063b40192bc3d4cf73b5f6caff36b2ea91ab58253cc73d3010e86b571cee6  negative/03-empty-evidence.input.json
5825033101dafcbaf43f046960e94232acfc0cd9aa871a037e7f21435ffce253  negative/04-empty-plain-language.expected.json
ee6242bcaf1e09c3464fc0ce12bf146522b3d639600f2a394af1072e81f8d64b  negative/04-empty-plain-language.input.json
c3fec1b5e97c8351f7539555c176396202808892be8bb1125914adb01705857a  negative/05-missing-detector-contract-identity.expected.json
12fccb19bedcf7642818922e851162048802a5811fbbd7078e421f1cc922c48d  negative/05-missing-detector-contract-identity.input.json
c3fec1b5e97c8351f7539555c176396202808892be8bb1125914adb01705857a  negative/05b-missing-detector-contract-rule-version.expected.json
12fccb19bedcf7642818922e851162048802a5811fbbd7078e421f1cc922c48d  negative/05b-missing-detector-contract-rule-version.input.json
7e928b3f597916138733a26f9ea880f73607ed08d5547eeac904960e2708e646  negative/06-confidence-out-of-range.expected.json
66fa16f55a4d6aa82aec03d15f522d7e9f1c3be2259c129c554620abc5ccc0d3  negative/06-confidence-out-of-range.input.json
ed43a1fb8f726da5061ab79082ba35052bec34da54209c4ac756d04f69c51195  negative/07-limit-exceeded-array.expected.json
f20fa7441a5e51b37e5769ae609cec65e3bb470b146532103c2ecc4a7d096ca4  negative/07-limit-exceeded-array.input.json
276e6b1e15daa184969c8127848884a8c83181aee344e479ebaf8a64ce110f86  negative/08-detector-contract-mismatch.expected.json
5adfbce85a5019def0595b89ac77b404afb9816c2935362bedbcc0b22f5adaf0  negative/08-detector-contract-mismatch.input.json
9fe9ba158520d21f2d792627f2fdaf47d4ec2a42d3600081ac0bb106288b8594  negative/08b-detector-contract-rule-version-mismatch.expected.json
fe645aa6234f446b6d689337aaa7cb04fd6123ac96037ea2cd1b02807feb8418  negative/08b-detector-contract-rule-version-mismatch.input.json
2883f848a9be3609b45bf84239f761957eeab5565d3848f89ec17e705e6a012a  negative/09-unknown-outcome-serde.expected.json
2d91393ed14b692279500c0ec8a161744d9e0367eb4af5053744185bd8c20d9c  negative/09-unknown-outcome-serde.input.json
2ed27c1421e6928dbe13dbfdb5c59e1045b30341fe7ebe05700006bc5ac572c0  negative/10-schema-version-not-accepted.expected.json
6b6b90b709664e891766264bbd480dc80e128d92ff50013d8c763e6fa6c93ba4  negative/10-schema-version-not-accepted.input.json
7e928b3f597916138733a26f9ea880f73607ed08d5547eeac904960e2708e646  negative/13-confidence-just-below-zero.expected.json
04d00ae098359e328fba1272470a6a6b843540e4e6b6550535b32deda57090db  negative/13-confidence-just-below-zero.input.json
7e928b3f597916138733a26f9ea880f73607ed08d5547eeac904960e2708e646  negative/14-confidence-just-above-one.expected.json
0ff24da985fca524f8c8009de8044275fb6e331bc54907ed2ba93c6adf346e30  negative/14-confidence-just-above-one.input.json
ed43a1fb8f726da5061ab79082ba35052bec34da54209c4ac756d04f69c51195  negative/18-evidence-array-count-over-limit.expected.json
bc55b58d060669a0f292bc41bf745bce46975b6ed176ec2ed310d34df00c0d2b  negative/18-evidence-array-count-over-limit.input.json
2fe3164d9dbe54032ab6b79c06bf43278d4ba8f4ab94cb0674e1db3a3afd3404  negative/18-missing_evidence-array-count-over-limit.expected.json
48f20ae5d0c23e3c16d70e6081d048eac1aac6c41be5460fe29b15655bddd067  negative/18-missing_evidence-array-count-over-limit.input.json
5c23524512e37a607860fa1172f443b847112897acdf3eefaa61f335ab472579  negative/18-suggested_fixes-array-count-over-limit.expected.json
ffe9799b686033149981e9f66776bb5ffb505f2c5a6cf929362f3beb33deb4c2  negative/18-suggested_fixes-array-count-over-limit.input.json
3deb49010cd6b002600db57a77983ffba5bb56344577b2e0e9fe10bdbd6fee74  negative/18-supported_scope-array-count-over-limit.expected.json
2e8a712994e2314d7321a0f53188a7b3a31d47da15b6ab7d917c26dc5dd112b7  negative/18-supported_scope-array-count-over-limit.input.json
b773c547d8c080acf6256ab110c0b22dfc7a4cec45d8b786817ee0efb48c3c28  negative/20-evidence-element-over-limit.expected.json
3ff554ff508b0d66f9ba58dd4a3682bbc657b1c6d1c39a0c627ba9bd3ea2f879  negative/20-evidence-element-over-limit.input.json
c7598e69c5041b89c84e6a0bb3fdc220ddd7c86fc0e8bc2fef8a54d3f0e28e47  negative/20-missing_evidence-element-over-limit.expected.json
5ec76a14b408055a9b658b8244a6888fef7cd5ec94fbcd1be662c5d293ad93d3  negative/20-missing_evidence-element-over-limit.input.json
ff7fc5a1ca83380f75c9e190668e41a824cc9b003b0ba4ed2267eb55c1fc73b1  negative/20-suggested_fixes-element-over-limit.expected.json
b4cd13fa62d08f3622064419211ba925f1c654f55dd199917d144ed029a48024  negative/20-suggested_fixes-element-over-limit.input.json
af205f91f4b6479318396b370798472dfd53f170c76c789ded10c68ae40ae3c4  negative/20-supported_scope-element-over-limit.expected.json
21a85f8b186ec3576899243119c5a20dad7f9166701e1f56ba08536dc2b05129  negative/20-supported_scope-element-over-limit.input.json
3ec0fdad08627be73272b14c372e61c57b32b80655cdd6c0b448951383327a3e  negative/22-evidence-display-over-limit.expected.json
6fed2ae67b17c3851736f4c7a314b3e0b0a66aebfc3a280d543c632b4f791055  negative/22-evidence-display-over-limit.input.json
705932d950ad4188202ccf68384b587871f49c33794172cedfd55c751523abb6  negative/23-suggested-fixes-display-over-limit.expected.json
9fc76a3b31a8e6f0c1157143ddae2cc32bdadca8bccbdd21cfca4c03805baf53  negative/23-suggested-fixes-display-over-limit.input.json
98ff0097a36d5acc62555151075b4f60ab1eca2400a26851b75f034e73c81463  positive/01-diagnosis.expected.json
6e38ff2888cebc5b608e82997fe4ae2a802707228763a517425a0afb45f2ded3  positive/01-diagnosis.input.json
a50f90b55d198c6985bb86b857fc1d63d0948eac7196120967bb0bc3a111abe1  positive/01b-diagnosis-no-host.expected.json
efc2c64b0f45a9effe6f2a9bd5d53427b8ce4d1ffcabf3d4566fce2ebf0c6061  positive/01b-diagnosis-no-host.input.json
0312d38405bca86cb26f9c581d3c4711b965c50601a6b320b6aa78efea5ae215  positive/02-not-applicable.expected.json
12fccb19bedcf7642818922e851162048802a5811fbbd7078e421f1cc922c48d  positive/02-not-applicable.input.json
3846419159c97c848f4feee3c8f7acca5843555eb1659069cf57736b84ef281e  positive/11-confidence-zero.expected.json
2dcf7b82eaa19166ebdacd74aa6e3e4ceed3a9e912e6799a7e842caf2a7afc69  positive/11-confidence-zero.input.json
6111b7c5cd21566a44f7192c804929691db07eaf9ef6a6392eae1e8f18f9af5f  positive/12-confidence-one.expected.json
940c713515cc604ea6117af938c2f859ad7219a702f897a02c832d874a07d5b0  positive/12-confidence-one.input.json
f793601eda96c0bd64cb6479f50029c6c0b12d1e094f90cb7dc2b4ab75462a0e  positive/15-diagnosis-non-finding-conditional.expected.json
282461fe2d52b8ab8d3ff07f6c5a9a02f898d9c04a7dd798eabb836cbbad593c  positive/15-diagnosis-non-finding-conditional.input.json
a17fcf0a2f50e2d495e4f90ce263410edc183add6c62699a2facbccf60410f74  positive/16-schema-version-accepted.expected.json
61c778e3625d29ddd78f88d9a5e7532f21e964932b6005dcdd62ac86391eb870  positive/16-schema-version-accepted.input.json
fd341655f26d2e4e308a1646065a7f281b3a28e11173af32f19c83f68887f3ba  positive/17-all-array-counts-at-limit.expected.json
28273e73cf6d9f6226a3077f8a33ff4b23f57b4218fc4a8b7ef8b4ec66d46c64  positive/17-all-array-counts-at-limit.input.json
5b3fa50c4ed6c16a340bf8fb4559e48a6dcae58fdd7de894e6f75cef428f136f  positive/19-all-array-elements-at-limit.expected.json
4c5ed7c21a6ca553101db809c9464598342070ce91d80ebc87a182b2a9c342bd  positive/19-all-array-elements-at-limit.input.json
333a9bbd634a0aee73435216bb65eb4eb5ad12339c3da08e5528ba6160927438  positive/21-display-fields-at-limit.expected.json
1437a2deed266e2fb3df78bf59e77abe1a4f564635cfb4bd776447731299c8a8  positive/21-display-fields-at-limit.input.json

### v2.7 Self-contained matrix and evidence contract

Every leg is self-contained except explicit composite upgrade. fresh VERSION creates a new root/stack. idempotent-rerun VERSION runs fresh then same-bundle rerun in retained root. pre-w1-refusal VERSION creates own non-W1 stream/snapshot. uuid-mismatch VERSION provisions A then runs retained install against B. bytes-live VERSION creates own stack. stackupgrade 9.4.3 9.4.4 provisions 9.4.3, retains volumes, upgrades both services, reruns pre-existing asset-upgrade assertions/barrier, then does rollover before cleanup. Rollover is part of stackupgrade, not implicit dependency. Evidence records run root/composite parent.

Minimum commands:

    python3 -m pytest tools/tests/test_asset_tools.py
    cargo fmt --manifest-path src/Cargo.toml -- --check
    cargo test --manifest-path src/Cargo.toml --locked
    bash scripts/clean-stack/matrix.sh fresh 9.4.3
    bash scripts/clean-stack/matrix.sh fresh 9.4.4
    bash scripts/clean-stack/matrix.sh idempotent-rerun 9.4.3
    bash scripts/clean-stack/matrix.sh idempotent-rerun 9.4.4
    bash scripts/clean-stack/matrix.sh pre-w1-refusal 9.4.3
    bash scripts/clean-stack/matrix.sh pre-w1-refusal 9.4.4
    bash scripts/clean-stack/matrix.sh uuid-mismatch 9.4.3
    bash scripts/clean-stack/matrix.sh uuid-mismatch 9.4.4
    bash scripts/clean-stack/matrix.sh stackupgrade 9.4.3 9.4.4
    bash scripts/clean-stack/matrix.sh bytes-live 9.4.3
    bash scripts/clean-stack/matrix.sh bytes-live 9.4.4
    bash /home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-handshake-2026-07-23/wire-gate/run-wire-gate.sh <built-rigsignal-agent>

Credentials/passwords/authorization headers and raw installer bodies are forbidden in output/evidence. Wire gate remains external ephemeral evidence: unused native shipper user is existing gate defect/exemption, not production authorization. Under §16.2.17 only bounded 4 KiB allowlisted structurally redacted non-secret body evidence required by gate may be retained; ordinary installer/probe retains none.

### v2.8 Per-checkbox v2 conformance map

| New checkbox | Falsifiable assert | Verification | Pass |
|---|---|---|---|
| byte identity / Option A | Raw files, exact Option A tuple, and KAT exactly equal v2.1. | Offline before mocked HTTP. | Exact tuple/KAT pass; wrong count/order/path/member digest/manifest value and altered formatting reject with zero requests. |
| backing-index fence | Every backing actual projection and trusted state match. | Multi-backing/pre-W1. | Any missing/mismatch refuses zero owned delta. |
| redirects / UUID pin | Installer/probe never follow redirect or repin rerun. | Redirect server/two-cluster. | Redirect fails; mismatch changes no key/config/capsule/state/marker. |
| pairing / built-in poll | Only same pairs, bounded readiness. | Clock/poll plus live pairs. | Mixed/timeout pre-mutation fail; two pairs pass. |
| complete asset barrier | Existing assets/G4 precede marker. | Ordered mocks/legacy failure. | Observed install/verification request set equals the manifest set and every pre-existing G4 row passes; any failure causes no release/marker. |
| capsule / tier replacement | Versioned ON capsule replaces tier 3-4 shipping. | Parser/precedence tests. | Invalid ON fail-closed; zero-env handshake succeeds. |
| credential state | Active/pending/candidate/UUID/role/generation survive crash. | v2.4 fault rows. | No orphan/lost ID; marker last/no advance. |
| protected purge | Every active/pending ID admin-revoked first. | Protected admin mocks. | Confirm all before delete; failure retains state. |
| overwrite / other stream | Overwrite 403, existing other target denial. | Live role matrix. | PUT 403; duplicate 409; other 403. |
| immutable JCS bytes | One immutable retained canonical vector. | Rust API/static test. | No mutable/re-serialize; helper production-called. |
| fixture checkpoint | Exact four pairs before code; old inventory exact. | `fixture-only-§15.1-byte-cap-v1` checkpoint approval/inventory diff. | Recorded checkpoint approval precedes production diff; only named files/MANIFEST rows; all baseline hashes match. |
| executable matrix | State legs declare root/composite. | Command/evidence list. | Fresh x2, rerun x2, refusal x2, mismatch x2, upgrade-rollover, bytes x2 PASS. |
