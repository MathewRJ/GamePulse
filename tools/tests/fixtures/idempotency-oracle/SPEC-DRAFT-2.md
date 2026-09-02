# RigSignal 0.3.3 assets-install partial-apply idempotency — implementation specification (draft 2)

**Status:** revised design-gate draft; it authorizes no implementation.  It is a convergence design, not rollback, removal, a resume CLI, general drift detection, Fleet inventory restoration, journal relocation, a transition flag, release work, or Observer/cloud work.

## Sol REJECT discharge map

| Sol change | Discharged in |
|---:|---|
| 1 final-boundary races | §§4.4, 7.2, 8.2 |
| 2 full-flow bundle-meta | §§3.4, 5.4, 8.2 |
| 3 scope/cross-caller resume | §§3.2–3.3, 8.1 |
| 4 private v1 transition | §3.6, §8.3 |
| 5 incomplete-enrollment ordering | §5.5, §8.3 |
| 6 durable possible-mutation / exits | §§3.5, 6 |
| 7 migration-stable oracle | §4.2, §7 |
| 8 intent durability / same-UID wording | §2 |
| 9 exact parsed archive hash | §3.1, §8.4 |
| 10 canonical origin / lock domain | §3.2 |
| 11 complete flag table | §5.3 |
| 12 crash edges / tests | §§3.5, 8 |
| 13 count and grammars | §3.3 |
| 14 reopen gate after write choice | §7.4 |

## 1. Decision and authority invariant

The default-profile installer shall publish one protected `installing` record before its first default-profile remote mutation, persist object-granular progress, re-read after every uncertain mutation, and promote atomically to `installed` only after the required final verification.  The assets-only and full default-flow callers use this one machine.

An intent is **recovery context, transaction continuity, binding, and durable uncertainty evidence**.  It is never proof of ownership and never authorizes replacement of an already-present divergent Kibana object.  At the immediate write boundary a mutation is allowed only when:

1. the target is absent and the primitive is create-only;
2. the target semantically equals desired and needs no write; or
3. it is an ES target with a valid existing `managed_by == "rigsignal-asset-bundle"` stamp and the selected, otherwise-authorized ES reconciliation action applies.

Present divergent/unreadable Kibana targets (saved objects, space, and role) always refuse.  `--repair`, `--upgrade`, `--allow-downgrade`, a predecessor, and an intent do not weaken that rule.

### 2.1 Trust boundary and the honest strand cases

The protected local record rejects other-UID path substitution; it is not cryptographic provenance against the same UID or root.  A same-UID actor can create a schema-valid, exact-bound record from observable data and may be indistinguishable from a genuine record.  Therefore this specification calls only **detectably malformed, nonmatching, unsafe, or ambiguous** records invalid; it never claims to detect a valid same-UID forgery.  Such a record confers no divergent-object mutation authority under the invariant above.

Loss/corruption of intent does **not** by itself make convergence impossible: with an available exact bundle, all live targets absent or semantically exact can start a new transaction and converge using create-only/no-op operations.  The record is nevertheless a durability dependency for continuation, diagnosis, persisted uncertainty, predecessor context, and a safe binding across a partial operation.  The documented fail-closed/manual-remediation strands are:

| Strand | Result |
|---|---|
| migration/projection comparison cannot establish semantic equality | refuse; do not overwrite |
| foreign replacement or a divergent Kibana object | refuse; object owner remediation |
| corrupt, non-removable primary record | exit 3; repair local protected state manually |
| unavailable/non-exact bundle needed to validate/recover an active record | exit 3; supply the exact bundle or manual remediation |

Root or a credential holder is outside the provenance boundary.  The implementation must still fail closed on malformed/contradictory observations.

## 3. Bound transaction, canonical inputs, and shared lifecycle

### 3.1 Parse-and-hash one archive object

Replace the `load_bundle(path)` then `bundle_sha256(path)` sequence.  Open the supplied archive once with `O_RDONLY|O_CLOEXEC|O_NOFOLLOW` where available; `fstat` it and reject non-regular files; stream that same opened descriptor through SHA-256 while parsing the tar stream (or first copy those exact descriptor bytes to a protected 0600 temporary file, fsync it, then parse and hash that file descriptor).  The `Bundle` returned to the transaction carries `archive_sha256` and immutable parsed bytes/provenance from this operation.  No later pathname re-open may contribute to `bundle_sha256`.  If descriptor stability cannot be provided on a supported platform, compare device/inode/size/mtime before and after parsing and refuse on change.  This eliminates the `load_bundle`/`bundle_sha256` path-swap TOCTOU.

### 3.2 Canonical origin and lock domain

`canonical_https_origin(value, flag)` shall parse rather than trim:

1. require scheme exactly `https` case-insensitively, no query, fragment, or nonempty path other than `/`;
2. reject any username/password/userinfo, missing host, invalid bracket syntax, invalid/non-numeric/out-of-range port, and a zone-bearing IPv6 literal;
3. for DNS names, apply UTS-46/IDNA ToASCII and lowercase the resulting ASCII hostname; for an IP literal, use the RFC 5952 compressed lowercase IPv6 form or canonical dotted-decimal IPv4;
4. reject an explicit port other than 1–65535; collapse omitted port and explicit `:443` to no port; retain another port as `:<decimal>`; and
5. return exactly `https://<canonical-host>[:port]`, with IPv6 bracketed.

Only this value is stored, compared, and printed in safe diagnostics.  It must not retain credentials or a spelling variant.

The lock domain is **one user-global default-profile lock**, independent of `--assets-marker`: `${XDG_STATE_HOME}/rigsignal/assets/assets-install.lock`, in a protected 0700 directory under the same ownership checks as the default marker.  It is an advisory exclusive OS lock, held for the complete invocation; it records only a redacted transaction diagnostic.  Explicit marker paths select a record location, not a lock domain.  This honestly guarantees serialization only for friendly default-profile RigSignal invocations by this UID using this implementation; it cannot serialize a malicious same-UID process, another user, root, or remote administrator.  A held lock is never broken automatically and returns exit 3.

Path-security checks (lexical path, no symlink, ownership/mode suitability) may precede locking.  The authoritative marker is read and fully validated **only after acquiring the global lock**, then re-read before every replacement; no pre-lock record content is used.

### 3.3 Identity, grammars, and immutable record

The source contains 29 source rows which deduplicate to **18 bundle-wide unique saved-object targets** (15 in `rigsignal`, 3 in `default`); no individual dashboard file has 18 members.  Together with one Kibana space, one Kibana role, and 46 ES logical targets, the expanded asset set is 66 targets.  Duplicate `(space,type,id)` definitions must have equal canonical desired semantics or local preflight refuses.

Use separate strict grammars:

* `cluster_uuid := [A-Za-z0-9_-]{22}` — Elasticsearch's URL-safe 22-character cluster UUID, not an RFC transaction UUID.
* `transaction_id := [0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}` — canonical generated UUIDv4, distinct from the cluster UUID grammar.
* `sha256 := [0-9a-f]{64}` and `source_commit := [0-9a-f]{40}`.

Both strict schemas reject unknown keys, duplicate remote keys, duplicate progress keys, noncanonical target ordering, invalid grammar, wrong cardinality, and a binding/digest differing from the exact loaded archive.  Canonical JSON is 0600; every replacement fsyncs file and containing directory and is read back.

Common immutable binding:

```json
{
  "schema_version": 2,
  "state": "installing | installed",
  "cluster_uuid": "<22-char ES cluster UUID>",
  "kibana_target": {"origin": "https://canonical-host[:port]", "spaces": ["default", "rigsignal"]},
  "ownership_profile": "default",
  "bundle_version": "0.3.3",
  "source_commit": "<40 hex>",
  "bundle_sha256": "<hash of exact parsed archive bytes>",
  "asset_set_sha256": "<canonical expanded 66-target semantic set>",
  "targets": ["<66 ordered typed remote keys plus desired semantic digest>"]
}
```

`installing` additionally has `transaction_id`, `created_at`, `predecessor` (complete validated installed binding or null), `caller_obligations`, `progress`, and transaction-level `possible_mutation`.  `installed` has `completed_transaction_id`, `completed_at`, and `verified_target_set_sha256`, and has neither progress nor mutation authority.

### 3.4 One transaction, caller obligations, and the full-flow marker

`caller_obligations` is a persisted set, initially containing the caller that first publishes intent: `assets-only` or `full-flow`.  It records required completion work, not exclusive ownership.  All active transactions require `assets-66`; a full-flow invocation also adds `full-flow-step-11`.  The machine is shared as follows:

| Existing record / invocation | Required behavior |
|---|---|
| no record / assets-only | create intent with `{assets-66}`; verify 66; promote |
| no record / full-flow | create intent with `{assets-66, full-flow-step-11}` before first default-profile asset mutation; do not promote until Step 11 is exact |
| installing from full-flow / assets-only rerun | exact binding resumes 66 targets but **cannot** promote; retain `full-flow-step-11`, report `partial-remote-possible`, and exit 3 only if the assets-only command cannot safely execute the remaining Step 11. It must not silently drop the obligation. A later full-flow rerun performs Step 11 and promotes. |
| installing from assets-only / full-flow rerun | exact binding is reused; atomically add `full-flow-step-11` before first full-flow remote mutation; complete assets plus Step 11, then promote |
| installed / either matching caller | full semantic verification; ordinary rerun recreates missing targets only where absent/create-only and all required obligations for that invocation are met; divergence still refuses |

Step 11 is the ES `component_templates/rigsignal-bundle-meta` target.  It is a first-class persisted transaction obligation with its own remote key and progress (`planned`, `write-issued`, `verified`) whenever `full-flow-step-11` exists.  Its desired body is the actual `marker_body(bundle, ownership_profile, applied_owned_assets, verified_external_assets)` result.  Before its PUT, persist `write-issued` and `possible_mutation=true`; PUT/response loss is unknown outcome; GET and the ES semantic projection must match before `verified`.  A missing, malformed, foreign, or divergent bundle-meta follows the ES predicate below; it is not inferred from local intent.  Full flow never promotes while this obligation is pending, even if all 66 asset targets are exact.

### 3.5 Durable states, atomic replacements, and crash matrix

`progress[target]` is `planned | write-issued | verified`.  Before **any** HTTP mutation, atomically persist that target as `write-issued` and `possible_mutation=true`; this covers a fsynced write-issued record even when HTTP dispatch never begins.  `possible_mutation` remains true for the rest of that transaction and is copied into recovery output; it is cleared only by promotion after the complete final verification.  It is transaction-level because a later failure may occur on another target and because the current invocation's in-memory `MutationTracker` cannot describe a prior invocation.

Atomic write uses a uniquely named hidden `.rigsignal-*` temporary file: write + fsync, `os.replace`, validate protection, directory fsync, then read-back.  On a caught failure, attempt to unlink only the created temp name; after SIGKILL hidden residue is harmless, ignored by record discovery, and cleaned only after validating that it is an owned regular temp in the selected protected directory.  Never treat temp residue as a record.

| Durable point / crash or race edge | Resumption or result |
|---|---|
| lock acquired, before intent publication | no record: release lock; zero remote mutation; retry starts new transaction |
| temp fsynced before replace; replace before directory fsync | old valid record or new complete record only; retry reads authoritative record under lock |
| any progress-record replacement (before/after replace/dir fsync) | old or new complete progress; re-read live target, never trust progress as ownership |
| intent fsynced, before first mutation | exact invocation reuses `installing` and begins guarded apply |
| `write-issued` fsynced, HTTP never starts | re-read: absent can receive create-only attempt; exact becomes verified; no blind retry claim |
| HTTP success response lost / process killed during any mutation | unknown outcome; re-read exact/absent/divergent under predicate |
| guarded create returns 409 | immediately re-read: exact => verified; divergent/unreadable => exit 3/no overwrite |
| exact GET then crash before `verified` persistence | re-read and mark verified only if still exact |
| verification succeeds then foreign replacement before promotion | final reverify observes divergence and refuses; no promotion |
| crash midway through 66-target final reverify | remain installing; repeat entire final reverify before promotion |
| installed → installing replacement for repair/transition | predecessor + new installing is either old installed or full new installing; no partial grammar |
| full-flow Step 11: before write-issued, response success/loss, verified before promotion | obligation remains pending until exact GET; response loss is unknown outcome; promotion remains prohibited |
| installed promotion succeeds while lock is held | record is installed; release lock normally; rerun verifies/no-ops or permitted absent creation |
| SIGKILL/signal exit rather than injected exception | process status is signal-derived; no synthetic 2/4 promise for that killed process; later resumption applies this matrix. Injected test exceptions must use the documented engine 2/4 contract. |

### 3.6 Primary/new path, legacy and private v1 matrix

The primary is the protected private default leaf; the legacy old default path remains non-migrated.  Explicit `--assets-marker` never consults the old default path.  A new primary wins over any legacy residue.

| Private primary | Old default | Action | Remote writes |
|---|---|---|---|
| absent | absent | v2 transaction allowed after preflight | intent only |
| absent | valid or malformed | refuse legacy-only; do not read/migrate/delete it | none |
| valid v2 installed/installing | any | primary wins; report old as residue | normal v2 predicate |
| valid schema-v1 at primary | any | under global lock parse with an isolated strict v1 reader; if valid and exact current bundle identity set, atomically replace it with a v2 **installed** record only after full 66-target semantic verification (and Step 11 verification if full-flow); otherwise refuse with manual recovery token | zero until verification proves all exact; then local replacement only |
| malformed v1 or ambiguous v1/v2 keys | any | `assets_transaction_invalid`; do not reinterpret, overwrite, or delete | none |
| malformed/unsafe v2 primary | any | `assets_transaction_invalid`; old cannot rescue it | none |

The v1 reader accepts exactly the historical schema keys and values, requires protected regular-file properties, rejects unknown/duplicate/missing identities, wrong source commit/version, and cannot create an installing state.  No automatic cross-version consumption occurs.

## 4. Desired-state oracle and guarded write mechanism

### 4.1 Per-class observation predicate

All reads immediately preceding writes and all verification reads use the selected adapter.  A remote read error, malformed response, or ambiguous resolution is refusal, never absence.

| Live state | ES target (including bundle-meta) | Kibana saved object / space / role |
|---|---|---|
| absent/404 | create-only allowed | create-only allowed |
| semantic exact | no-op; verify | no-op; verify |
| present, divergent, valid RigSignal stamp | ES reconciliation only if table §5.3 selects it | n/a |
| present, divergent, no/malformed/foreign stamp | refuse | refuse |
| unreadable/unverifiable | refuse | refuse |

For a create attempt, 409 is a race signal, not success: re-read as above.  The final boundary is therefore **not** claimed to prevent a remote administrator from changing an object after the final GET or after promotion; no documented Kibana per-object optimistic precondition exists.  The honest guarantee is: before every installer-issued write, it has just observed absence/exact/qualified ES ownership; it never intentionally uses an overwrite primitive on a present divergent Kibana object; it promotes only after an immediate full reverify while holding the local lock.  Post-promotion external changes are outside this mechanism and ordinary reruns observe/refuse them.

### 4.2 Migration-stable saved-object semantic oracle

For a desired bundle line, define `SO_desired(space,type,submitted_id, attributes, references)` by canonical JSON (JCS) of:

```json
{"space":"<canonical target space>","type":"<type>","attributes":<attributes>,"references":<references-or-[]>}
```

For GET/resolve output define `SO_live` from the resolved destination's target space, type, attributes, and references.  Exclude `updated_at`, `version`, `coreMigrationVersion`, `typeMigrationVersion`, and all server/import metadata (`managed`, namespaces, originId, score, meta, outcome/alias fields).  Normalize omitted references to `[]`; preserve array order and all semantic attribute values unless the probes below establish a documented/observed deterministic transformation, in which case normalize that named transformation symmetrically and record it in the adapter version.  The desired archive retains migration fields for import compatibility but they are **not** equality inputs.

There is no Saved Object server provenance field usable as RigSignal ownership proof.  Exact semantic equality is a convergence/no-op proof only.  `managed` and `originId` are not authority.

Generic saved-object CRUD, `_bulk_get`, resolve, and bulk resolve are deprecated in 9.4 documentation.  Place every such call, response parse, semantic projection, `destinationId` mapping, and fallback behind one `SavedObjectAdapter`; callers cannot call the endpoint directly.  Preserve a returned `destinationId` as submitted `(space,type,id)` → actual physical identity and verify the returned/resolved graph; do not assume references or physical IDs stayed submitted.  A destination remap that cannot be represented by the selected adapter/oracle is refusal, not a hidden success.

Lost response after import/create is **unknown outcome**, never a safe failure.  Re-read/resolve and compare `SO_live`; only absence may receive another create-only attempt, exact becomes verified, and mismatch refuses.

## 5. Apply flow, flags, and enrollment ordering

### 5.1 Object-granular execution

Build the canonical 66-target inventory before intent publication.  The 18 saved objects are classified individually; a dashboard source file is complete only when every member is exact.  Mixed exact/absent members create only the absent members; any divergent/unreadable member refuses.  `_dashboard_present()` and dashboard-file presence must not decide action or promotion.

Before each mutation persist `write-issued`/`possible_mutation`; apply one selected guarded primitive; then GET/resolve semantic verification and persist `verified`.  Direct per-object create (or another candidate) is permitted only after §7 gate evidence proves its behavior for every shipped type.  `_import?overwrite=true` is not a recovery primitive unless the re-opened gate explicitly approves it; it has no documented optimistic concurrency protection.

### 5.2 Full final verification

Promotion requires each required target state `verified`, an ordered full 66-target re-read under §4.1/§4.2, matching `verified_target_set_sha256`, and, if `full-flow-step-11` is present, exact verified bundle-meta.  On any failure leave `installing`; do not infer install success from source-dashboard completion or an old marker.

### 5.3 Complete version × record × live × flag table

Flags are a set; evaluate all supplied flags together.  Conflicting/meaningless combinations (for example upgrade plus allow-downgrade, or a version flag without a valid predecessor/version transition) are local input refusal (2) before remote mutation.  `repair` never grants authority.

| Record version/state | Live state | no flags | `repair` | valid `upgrade` / `allow-downgrade` | combinations |
|---|---|---|---|---|---|
| none or exact-bound installing, target absent | all classes | create-only | create-only | create-only only after transition validation | same, unless invalid combo |
| none or exact-bound installing, target exact | all classes | no-op/verify | no-op/verify | no-op/verify | no-op/verify |
| none/installing, ES stamped divergent | ES | refuse (same version or no valid transition) | reconcile only when repair's existing ES policy selects it | reconcile only if predecessor/version direction is valid | require every selected condition; no implicit OR |
| none/installing, ES un/foreign-stamped divergent | ES | refuse | refuse | refuse | refuse |
| none/installing, Kibana divergent | Kibana | refuse | refuse | refuse | refuse |
| installed current, target absent | all classes | **create-only recreate** and full reverify; ordinary installed rerun does not leave missing targets absent | create-only recreate | create-only recreate | same unless invalid combo |
| installed current, target exact | all classes | no-op | no-op | no-op | no-op |
| installed current, ES stamped divergent | ES | refuse | reconcile if existing repair policy selects it | transition only if valid predecessor/direction; same-version version flags refuse | all selected predicates required |
| installed current, Kibana divergent | Kibana | refuse | refuse | refuse | refuse |
| installed prior version, any target | all classes | exit 3: no automatic cross-version consumption | exit 3 unless an exact current v2 transaction was already established | only create/no-op/qualified ES reconcile after explicit valid predecessor + direction; divergent Kibana still refuses | invalid/multiple contradictory flags => 2 |

This table explicitly covers stamped ES divergence without predecessor, same-version stamped divergence without repair, exact ES under repair, same-version upgrade/downgrade, all flags together, installed-old with changed Kibana object, and installed-exact with missing member.

### 5.4 Bundle-meta and flag symmetry

The bundle-meta target uses the ES rows in §5.3, but full-flow has an additional truth condition: its body must correspond to the exact current applied/verified asset sets.  An installed rerun missing it recreates it create-only; an exact one is no-op; a divergent marker needs a valid ES stamp and a table-authorized reconciliation, otherwise refusal.  Its response-loss path is retained as `write-issued`/unknown outcome.

### 5.5 Incomplete-enrollment recovery ordering

Do not change enrollment credential lifecycle.  During incomplete-enrollment recovery, first recover/validate the existing enrollment key/candidate state using its established protected ordering; failure there is classified through the existing `MutationTracker`.  Only after that step has established the exact authorization and target binding may the shared default-asset transaction acquire/use the lock and read/write the asset record.  Conversely, an existing valid `installing` asset record is not discarded because enrollment recovery is incomplete; it is retained for later exact resumption.  Thus the ordering is: protected key recovery/validation → canonical remote binding → global assets lock → authoritative asset-record read/reuse/create → remote asset work.  Valid, invalid, and unavailable asset records are tested separately; a key-recovery failure after prior asset mutation returns 4 because persisted `possible_mutation` and invocation `MutationTracker` both prevent downgrade.

## 6. Exit, diagnostic, and launcher contract

Persist `possible_mutation` before dispatch as §3.5 requires.  For the current invocation use `MutationTracker`; final exit is mutation-possible if either tracker is true or the locked active record says `possible_mutation=true`.

| Condition | Token | Engine / launcher exit |
|---|---|---:|
| local input/path/lock publication failure with neither persisted nor issued possible mutation | `assets_transaction_local_preflight` | 2 / 2 |
| malformed, nonmatching, unsafe, ambiguous, held-lock, unavailable-exact-bundle, or unresolved obligation | `install refused: assets_transaction_invalid|active_or_mismatch|ambiguous` | 3 / 3 |
| pre-write remote conflict/read refusal and no possible mutation | `assets_transaction_remote_conflict` | 3 / 3 |
| any issued/persisted possible mutation, unknown outcome, verification/promotion failure, or later refusal | `partial-remote-possible` plus safe transaction token | 4 for the failing invocation; a later no-write refusal remains 3 but emits the same recovery state | 
| complete required verification and promotion | normal success | 0 / 0 |

For a later rerun that refuses without issuing a new write but sees an `installing` record with `possible_mutation=true`, exit 3 and emit `RIGSIGNAL_RECOVERY_STATE partial-remote-possible transaction=<redacted token>`; do not falsely describe the remote state as clean.  Never print credentials, origins, response bodies, or full transaction UUIDs.  Test direct engine and `rigsignal-launcher.sh` `wait` passthrough for 2/3/4.

## 7. Mandatory 9.4.x API probes and gate

The following isolated live probes use a real release-format RigSignal bundle against clean, separately namespaced Kibana 9.4.3 and 9.4.4 stacks.  Execute every probe for each shipped saved-object type: `tag`, `index-pattern`, `dashboard`, `visualization`, and `search`; use an object with a nonempty reference graph where the type supports it.  Save sanitized requests/responses, physical IDs, resolved IDs, and `SO_live` projections as evidence.

1. **PROBE-1 literal create:** `POST /s/{space}/api/saved_objects/{type}/{id}?overwrite=false` with the bundle attributes/references. Pass only if 201/accepted behavior creates exactly the submitted identity and GET yields `SO_live == SO_desired`; fail if it overwrites, remaps without explicit mapping, or cannot represent references.
2. **PROBE-2 conflict guard:** repeat the exact call from PROBE-1, then repeat with a deliberately divergent body and `overwrite=false`. Pass only if existing-object calls yield a detectable conflict (normally 409) and GET proves the original semantic object unchanged; fail on overwrite/merge/ambiguous success.
3. **PROBE-3 unknown outcome:** dispatch the PROBE-1 create through a controlled response-drop proxy, then `GET /s/{space}/api/saved_objects/{type}/{id}` and `GET /s/{space}/api/saved_objects/resolve/{type}/{id}`. Pass only if the adapter can classify exact vs absent vs divergent and preserve any actual destination; fail if it must blind-retry or cannot determine the destination.
4. **PROBE-4 import/destination map:** `POST /s/{space}/api/saved_objects/_import?overwrite=false` with one NDJSON object, then inspect `successResults.destinationId` when present and GET/resolve both submitted and destination IDs. Pass only if the adapter retains a submitted→actual mapping and verifies the semantic graph; fail on unexpected copy or unresolved reference rewrite.
5. **PROBE-5 semantic metadata:** create/import, GET, then perform an otherwise identical supported operation or migration-triggering restart. Pass only if attributes/references/space/type compare equal after excluding `updated_at`, `version`, `coreMigrationVersion`, and `typeMigrationVersion`; fail if another changing field is required for semantic equality or desired data changes.
6. **PROBE-6 reference preservation:** create every duplicate/linked bundle object using the candidate primitive, then GET/resolve each object and inspect each reference `(type,id,name)`. Pass only if `SO_live` graph equals desired or the adapter has an explicit, verified destination map that rewrites every affected reference; fail on dangling/implicit rewrite.
7. **PROBE-7 9.4.3→9.4.4 upgrade:** on 9.4.3 create all five types, record projections/mapping, upgrade the same stack in place to 9.4.4, then GET/resolve every object. Pass only if the oracle remains equal and no unexpected physical copies exist; fail otherwise.
8. **PROBE-8 full bundle replay:** on each version, execute the candidate primitive for all 29 source rows and re-run it. Pass only if there are exactly 18 unique progress targets, exactly one create attempt per deduplicated identity, no unexpected physical IDs, and semantic equality after the second run.

The mechanism choice (per-object create, import, or another adapter-backed primitive) is blocked until all applicable probes pass.  The selected adapter must use no API outside the probed call set.  **Immediately after choosing it, reopen the architecture/security design gate**: review its exact request semantics, response-loss behavior, 409 behavior, destination mapping, migration oracle, authorization surface, and final-boundary claim.  A later mechanism change reopens the gate again.  If no candidate passes, 0.3.3 does not implement Kibana partial recovery; it fails closed.

## 8. Tests and acceptance matrix (tests first)

### 8.1 Located authority and cross-caller tests

1. Full default path with foreign/unstamped ES target: exit 3, transport-wide zero-write sentinel.
2. Crafted structurally valid predecessor with each transition flag: no Kibana/unstamped ES overwrite.
3. Partial dashboard with foreign root plus missing member: object-complete refusal; no import/PUT.
4. Older valid record plus changed unstampable Kibana target: no flag and every valid flag combination refuse.
5. Interrupted full flow (before/within Step 11) resumed by assets-only: it retains bundle-meta obligation, cannot promote/drop it; later full-flow completes it.
6. Interrupted assets-only resumed by full flow: one transaction ID, full-flow obligation added durably before its mutation, final promotion only after Step 11.

### 8.2 Required exhaustive matrix

| Case | Required assertion |
|---|---|
| private schema-v1 valid migration and malformed-v1 refusal | valid current v1 gets only local v2 installed replacement after full exact verify; malformed has zero remote writes |
| bundle-meta foreign, exact, malformed, Step-11 lost response | exact no-op; allowable absent create; foreign/malformed refusal; loss is unknown outcome and re-read |
| incomplete enrollment with valid/invalid/unavailable asset record | ordering §5.5, record retained, correct 2/3/4 classification |
| resumed promotion failure after only a prior invocation mutation | later refusal exit 3 plus `partial-remote-possible`, never exit 2; eventual exact rerun converges |
| per-class final-boundary race | component/index template, pipeline, ES role, transform, space, Kibana role, and each of tag/index-pattern/dashboard/visualization/search: replace after pre-write GET and after verify; no unauthorized overwrite/promotion |
| create semantics | `createOnly=true` where applicable, `overwrite=false`, 409 then exact/divergent re-read for every target class |
| migration and reference equality | PROBE-1–8 evidence on 9.4.3/9.4.4 including in-place upgrade |
| duplicate expansion | 29 source rows → exactly 18 saved-object progress targets and exactly one create per duplicate identity |
| archive path swap | swap pathname during parse/hash; digest always exact parsed bytes or invocation refuses; no intent mismatch |
| lock domain | concurrent calls with two explicit marker paths serialize on one user-global lock; binding mismatch never replaces active intent |
| canonical origin | userinfo rejected; DNS case/IDNA/default-port normalize; IPv6 canonicalizes; invalid port rejected |
| all flag combinations | every subset/invalid combination against absent/exact/stamped-divergent/unstamped-divergent ES and Kibana states matches §5.3 |
| installed rerun missing target | ordinary rerun create-only recreates absent target; divergent one refuses |
| no-record recovery | exact/absent partial set converges; migrated/oracle-divergent and foreign replacement refuse |
| corrupt/lost intent | exact object, migrated object, and foreign replacement separately prove honest recovery/refusal behavior |
| remote-read failures | both assets-only and full-flow classify GET/projection/resolve failures as refusal with no write |
| token redaction | `partial-remote-possible` includes safe/redacted token only, no endpoint/credential/body |

### 8.3 Crash and signal tests

Inject each §3.5 row: after lock; after temp fsync; after replace before dir fsync; during every progress replacement; after write-issued before dispatch; after response loss; 409 exact/divergent; GET exact before verified; verified then foreign replacement; each final-reverify target boundary; installed→installing replacement; all Step-11 boundaries; installed promotion while lock held; and hidden temp residue.  Separately use a real subprocess SIGKILL to prove only later resumption is specified, and injected exceptions to prove direct engine and launcher 2/4 behavior.  A test-only shared hook after each target verification and intra-dashboard member requires explicit unsafe test injection and loopback-only endpoint.

### 8.4 Isolated live recovery gate

Use a unique Compose project, ports, volumes, and run directory; no broad `down`/`prune` and no cloud-migration stack.  Against a release-format bundle: crash after one saved-object verification (expect first engine exit 4 and a protected installing record); independently inspect partial objects and progress; rerun to exit 0; independently semantically verify all 66 targets and Step 11 where full flow; repeat mid-dashboard member; assert no unexpected IDs.  Run exactly `python3 -m unittest discover -s tools/tests`, `python3 tools/tests/test_assets_only.py -v`, relevant launcher/packaging tests, and this gate.

## 9. Implementation boundaries and acceptance gate

Implementation may touch the marker/asset planner, one shared default transaction helper, the isolated saved-object adapter, assets-only/full-flow routing, tests, clean-stack recovery gate, and user documentation.  It may not add Fleet inventory, observed-hash ownership/general drift detection, a new asset journal, automatic cross-version intent consumption, enrollment credential lifecycle changes, a broader concurrency claim, worktree pruning, or release operations.

Before code, ratify §§1–7 and the candidate API evidence.  Before merge, the four historical abuse tests and all §8 tests pass; the live gate passes; the reopened architecture/security gate accepts the selected write mechanism; and reviewers confirm that no intent, predecessor, or flag path overwrites a foreign Kibana object.

SPEC-DRAFT-2-COMPLETE
