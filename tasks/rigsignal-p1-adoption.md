# Amendment 7 / A4 — Explicit installer migration-adoption path

**STATUS: RATIFIED — rev 2 + confirmation fixes (2026-07-24, session
2026-07-24c-installer-adoption).** In force as the normative source for the installer
migration-adoption path until folded into the target contract files.

- **Rev 1** (2026-07-24): initial draft from the informal-plan Sol spar.
- **Rev 2** (2026-07-24): both review gates ran against rev 1 and returned:
  - **Overseer: APPROVE WITH NOTES** (three notes folded in below — lifecycle-resolution
    wording, fresh-install regression leg, upgrade/rollover + tightened rerun-count leg).
  - **Sol cross-check: REWORK**, with six exact required edits (verdict, findings, and edits
    preserved at `sol-crosscheck-rev1-verdict.md` in this directory), applied verbatim below.
    All cited `file:line` anchors re-verified against source before use.
- **Rev 2 confirmation** (2026-07-24): Sol confirmation pass on rev 2 — six edits present
  without material drift, no regression; two mechanical fixes required (leg-9 version-pair
  sentence, this citation) and applied. Verdict preserved at `sol-confirm-rev2-verdict.md`.
  - No conflict was found between the two gates' notes; they compose directly.
- **Session:** `2026-07-24c-installer-adoption`
- **What this amends:**
  - `tasks/rigsignal-p1-provisioning-order.md` — carves a bounded exception into the ratified
    "fresh-install-only" scope (lines 31, 39) and the v2.2 exact-fence rule (lines 250–259).
    This document is the normative source until folded into that file's own v2.x numbering.
  - `W1-DIAGNOSISEVENT-CONTRACT.md` — next amendment in sequence after Amendment 6 (§16). This
    is **Amendment 7**.
  - `W2-SPACE-ROLE-CONTRACT.md` — next amendment in sequence after Amendment A3. This is
    **Amendment A4**, a **clarification, not a reversal**: A3.2 ("hits = exactly the
    installer's self-proof docs") already matches the owner's retain-proofs decision. A4
    fixes the **stored live-gate runner**
    (`app-p1-w2-subset-2026-07-24/live-gate/scripts/run-w2-live-gate.sh:363-366`), which still
    asserts `hitcount == 0` — a stale implementation, not a contract requirement.

Everything below is normative "MUST" text pending ratification.

---

## 1. Scope of the exception

§2 of `rigsignal-p1-provisioning-order.md` excludes "migrating or repairing a pre-existing
non-W1 diagnosis stream" from the installer's fresh-install-only scope. This amendment does
**not** repeal that exclusion. It adds one narrow, explicit, one-shot exception: adoption of a
stream that already passes the §4 compatibility predicate but has no local committed
enrollment state. Non-passing streams remain out of scope and require external migration (as
already demonstrated for the owner cluster, `AMENDMENT-M1.md`) before the installer touches
them at all.

## 2. The `--adopt-existing-w1-stream` flag

1. The flag MUST be one-shot: consumed for a single invocation, never persisted, never
   inferred from state or environment.
2. **[Sol exact edit 1]** The complete refusal/adoption decision is this matrix; no case
   outside it is permitted:

   | Local condition | Remote stream | Flag absent | Flag present |
   |---|---|---|---|
   | `state.json` absent; no owned enrollment/staging artifacts | absent | ordinary fresh install | refuse `adoption_flag_stream_absent` |
   | same clean root | shared W1 predicate passes | refuse `adoption_required` | adoption path |
   | same clean root | shared W1 predicate fails | refuse `migration_required` | refuse `migration_required` |
   | valid committed state | any | ordinary rerun rules | refuse `adoption_flag_state_present` |
   | valid incomplete state | any | ordinary recovery, then re-evaluate without adoption | refuse `adoption_flag_state_present` **before recovery** |
   | malformed/mismatched state, orphaned committed file, `candidate/`, or sibling publication stage | any | refuse `enrollment_remediation_required` | same refusal |

3. Every refusal row returns nonzero with the exact stable error code shown and permits
   read-only inspection only. It creates no enrollment root or staging directory, performs no
   recovery or key invalidation, mutates no owned local or remote resource, and writes or
   advances no marker.

## 3. Adoption semantics — fence-only, no new phase

1. Explicit adoption satisfies **only** the pre-PUT existing-stream compatibility fence
   currently implemented as `existing_stream_is_compatible()` / `fence()`
   (`tools/install_assets.py:1132`, `:1161`). It does not create a new `state.json` phase and
   does not synthesize a pre-key "adopted" committed state.
2. **[Sol exact edit 2]** Adoption changes only Step 4's local committed-state ownership
   conjunct. In the single clean-root/compatible-stream/flag-present matrix cell, the shared
   §4 remote predicate substitutes for that conjunct. Steps 1–3 and 5–11, including existing
   `mint_intent`, `candidate_staged`, `candidate_verified`, publication, handshake,
   committed-state, recovery, and marker ordering, remain byte-for-byte normative. No
   `adopted` phase or state is introduced.
3. `state.json`'s `committed` phase invariant is unchanged: it MUST still require a non-null
   `active_key_id` (`tools/install_assets.py:547`; normative text at
   `rigsignal-p1-provisioning-order.md:287`). Adoption supplies no shortcut around minting a
   real key through the real transaction.
4. Concretely: with `prior=None` (no local state, the exact post-migration owner condition),
   `existing_stream_is_compatible()` currently returns `False` unconditionally because it
   requires `state is not None and state["phase"] == "committed"`
   (`tools/install_assets.py:1147`). The implementation MUST add a narrowly scoped adoption
   branch to that function (or its caller) that, only in the §2.2 matrix cell where the flag
   is present and the remote stream passes the shared §4 predicate, substitutes that predicate
   for the state-derived ownership check. It MUST NOT weaken the check on any other matrix
   row.

## 4. Shape-verification extension (shared predicate)

**[Sol exact edit 3]** Replaces the adoption-only framing of rev 1 in full:

1. The §4 compatibility predicate is shared by explicit adoption **and** ordinary
   committed-state reruns. For every backing index, its enumerated W1-owned projection is
   compared by exact JCS equality; subset matching is forbidden, and the full recursive
   `rigsignal.diagnosis.properties` member is one equality operand.
2. `GET _data_stream` must resolve exactly one named stream and a nonempty set of distinct
   `(index_name,index_uuid)` pairs. `failure_store.enabled` at the data-stream level must
   exist, be Boolean, and equal `false`.
3. For every captured pair, `_settings` must report the same `index.uuid`; the backing
   mapping/settings projection must equal canonical W1; the data-stream and per-index
   `ilm_policy` must equal the lifecycle name derived from the immutable shipped W1 template;
   `_ilm/explain` must report that exact index as managed by that policy; and the resolved
   policy must contain no `delete` phase. Missing, malformed, contradictory, or
   UUID-mismatched evidence refuses.

   **Clarification (overseer note 1):** the W1 index template declares no explicit
   `index.lifecycle.name` field. "The lifecycle name derived from the immutable shipped W1
   template" is the template composition's inherited default, `logs@lifecycle` — this
   predicate pins the **resolved** policy, never a literal declared-name field (none exists).
   Evidenced live at `legacy-migration-2026-07-24b/live/vf-ilm-explain.json`
   (`"policy":"logs@lifecycle","managed":true`) and `.../live/pf-ilm-policies.json`
   (`logs@lifecycle.policy.phases` = `{hot: rollover}` only — no `delete` key).
4. Exactness is over the enumerated owned projection, not the complete effective mapping.
   Canonical composed fields such as the built-in `data_stream` root are not foreign fields.
   This amendment adds no whole-root namespace prohibition. (Confirmed against
   `legacy-migration-2026-07-24b/live/vf-mapping.json`, which carries a `data_stream` mapping
   root untouched by `owned_mapping_projection()`, `tools/install_assets.py:1049`.)
5. **Historical document semantics are out of scope for the installer.** Shape verification
   proves mapping/settings/lifecycle compatibility, not that every historical document's
   `outcome`/`disposition`/`input_mode` values are contract-valid — plain `keyword` mappings
   cannot enforce that (`W1-DIAGNOSISEVENT-CONTRACT.md:588` matrix). For the owner path,
   external attestation already exists and is sufficient: M1's amendment (`AMENDMENT-M1.md`)
   plus the live verify row set (`legacy-migration-2026-07-24b/live/verdict-rows.tsv`, H_new
   hash/backing-UUID matches). The installer MUST NOT audit historical document contents as an
   adoption precondition, and MUST NOT embed any owner-specific document hash, ID, or UUID in
   generic installer code.

## 5. Probe/proof documents

1. `candidate_document()` (`tools/install_assets.py:1235`) is contract-invalid as written
   (`outcome:"finding"` is not `diagnosis|not_applicable`; `evidence` empty while the implied
   outcome requires non-empty; `host.name`/`evidence_display` absent; diagnosis-conditional
   `falsifier`/`supported_scope`/`nearest_alternative` absent; `suggested_fixes`/
   `missing_evidence` absent — required fields per `W1-DIAGNOSISEVENT-CONTRACT.md:191-224`).
   This amendment requires a full replacement, not a two-field patch.
2. **[Sol exact edit 4]** Runtime substitution is permitted **only** for `@timestamp`, the
   lowercase runtime `host.name`, and `event.id`, generated from the frozen fixture
   `fixtures/diagnosis_event/v1/positive/15-diagnosis-non-finding-conditional.{input,expected}.json`
   (`MANIFEST.md` row "15 non-finding conditional fields"). Every other fixture field is
   verbatim, **including `detector_id:"D6"` and `input_mode:"fixture"`** — also
   `disposition:"non_finding"`, `verdict`, `confidence`, `confidence_basis`, `falsifier`,
   `supported_scope`, `nearest_alternative`, `evidence`, `suggested_fixes:[]`,
   `missing_evidence:[]`. No new fixture and no ad hoc envelope construction.
3. The write request `_id` MUST equal `_source.event.id` MUST equal a fresh
   `provision-<fresh-id>` value, unique per accepted-write attempt (not merely per
   invocation): if one invocation performs multiple accepted-write attempts, each attempt
   gets its own distinct ID. This closes the collision defect at
   `tools/install_assets.py:1505` (fixed `"active-proof"` string) precisely, rather than by
   varying only `event.id` while an Elasticsearch `_id` stays fixed.
4. **Proofs are retained** (owner decision, consistent with Amendment A3.2). Accepted proofs
   are never deleted or overwritten.
5. **[Sol exact edit 4, negative-probe oracles]** Negative probes MUST assert exactly these
   status/error-class pairs, on both supported versions, and MUST include the
   already-ratified unknown-field-inside-`rigsignal.diagnosis` case (rev 1 named only the
   unknown-root case):
   - unknown root field: `400`, `strict_dynamic_mapping_exception`;
   - unknown `rigsignal.diagnosis` field: `400`, `strict_dynamic_mapping_exception`;
   - string-valued `confidence`: `400`, `document_parsing_exception`, caused by
     `number_format_exception`.

   A generic `status >= 400` (current pattern, `tools/install_assets.py:1283/1292`) is
   insufficient — it would let 401/403/409/500 falsely stand in for a mapping-rejection proof.

   The accepted-write proof additionally requires `refresh=wait_for`, an exact-ID refetch
   returning exactly one hit, JCS equality of that hit's `_source` with the submitted
   canonical envelope, and `_ignored` absent from hit metadata — the exact discipline M1
   required for its own re-ingest refetches (`AMENDMENT-M1.md` M1.2).
6. **[Sol exact edit 5 — TOCTOU, replaces rev 1's "after all asset PUTs"]** After Steps 7–8
   complete successfully and immediately before Step 9 atomic publication, rerun the complete
   shared compatibility predicate, canonical asset/role GET comparisons, and simulation. The
   backing `(index_name,index_uuid)` set must equal the pre-PUT snapshot exactly. Any drift
   fails closed; a newly minted candidate is invalidated or left in recoverable state, and no
   credential/configuration publication, committed state, or marker occurs.

## 6. Live-gate obligations

Legs 1–8 and 10 run independently on both supported version pairs, (ES 9.4.3, Kibana 9.4.3)
and (ES 9.4.4, Kibana 9.4.4) (`tools/install_assets.py:1009`). Leg 9 runs once across the
specified 9.4.3→9.4.4 transition.

1. **Refusal, no flag:** §2.2 matrix row 2, flag absent → refuse `adoption_required`, zero
   owned mutation.
2. **Adoption success:** §2.2 matrix row 2, flag present → both M1 documents, their canonical
   hashes/IDs, and the backing-index UUID are preserved unchanged; no recreate, no rollover.
3. **Rerun leg, ≥2 clean reruns [overseer note 3]:** after one adoption, run N ≥ 2 subsequent
   clean reruns without the flag. Exact count formula: adoption plus N reruns MUST add exactly
   `1 + N` distinct retained self-proofs — one per invocation — and MUST leave both M1
   documents unchanged and never counted among the proofs.
4. **Shape-negative matrix, run WITH the flag [Sol exact edit 6]:** one divergent backing
   index among several; `confidence:float`; missing or extra `rigsignal.diagnosis` mapping;
   non-strict `dynamic`; `ignore_malformed:true`; data-stream-level failure store enabled;
   wrong/missing lifecycle name; and (added per Sol) correct `logs@lifecycle` name whose
   resolved policy contains a `delete` phase. Each MUST refuse `migration_required` before any
   W1 PUT.
5. **Flag-misuse matrix [Sol exact edit 6]:** every non-adoption-path row of the §2.2 matrix
   with the flag present — stream absent; valid committed state; valid incomplete state;
   malformed/mismatched state; orphaned committed file; `candidate/` present; sibling
   publication stage present. Each refuses per its matrix cell, zero owned mutation.
6. **Probe crash/retry + TOCTOU drift [Sol exact edit 6]:** simulated failure mid-probe leaves
   no stray committed key and no silently-abandoned candidate; additionally, inject drift
   after Step 8 and before Step 9 to prove the §5.6 TOCTOU fence fails closed.
7. **Full M1-shape verification leg:** a stream shaped exactly like the owner's post-M1 stream
   (per `legacy-migration-2026-07-24b/live/verdict-rows.tsv` and `AMENDMENT-M1.md`) passes
   adoption end-to-end, including the §4.2–§4.3 lifecycle and data-stream-level
   failure-store checks.
8. **Fresh-install regression [overseer note 2]:** clean cluster, no pre-existing stream, flag
   omitted → ordinary fresh install passes end-to-end through the strengthened §4/§5
   predicate and probe machinery unchanged in outcome (proves the hardening adds no
   regression to the non-adoption path).
9. **Upgrade/rollover leg [overseer note 3a]:** adopt on (ES 9.4.3, Kibana 9.4.3); upgrade the
   stack to (9.4.4, 9.4.4); force/observe a rollover. The shared §4 predicate still passes
   against the new backing index, and the shape invariant holds: the new backing index's UUID
   is captured and verified, and the prior backing index's M1 documents/hashes remain intact.
10. **T18 correction [Sol exact edit 6]:** assert the **exact expected proof-ID set and
    canonical proof sources**, not unfiltered total hits. On the standard fresh-plus-rerun W2
    gate that set has size two; on the owner/M1 gate the legacy M1 documents remain separately
    present and are **not** counted as proofs. Corrects `run-w2-live-gate.sh:363-366`.

---

## Ratification

- [x] Overseer gate — APPROVE WITH NOTES (2026-07-24, notes folded into rev 2; verdict in
      session record)
- [x] Sol cross-check — REWORK applied verbatim in rev 2; rev-2 confirmation pass found six
      edits present without material drift, two mechanical fixes applied
      (`sol-crosscheck-rev1-verdict.md`, `sol-confirm-rev2-verdict.md`)
- [x] Owner ratification — the two substantive forks (explicit one-shot flag vs auto-adoption;
      retain proofs + T18 fix vs delete + A3.2 amendment) were decided directly by the owner
      during 2026-07-24c session planning; ratified per the owner-approved session plan

Until all three are checked, this document has no normative force and MUST NOT be cited as
ratified contract text.
