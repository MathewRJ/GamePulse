# Assets install exit and refusal contract

`rigsignal assets install` is journal-free.  It never exposes an assets resume
command and never performs automatic rollback.  On an assets-only exit 4,
inspect the partial state and idempotently rerun `rigsignal assets install`.

| Exit | Meaning |
|---:|---|
| 0 | Success, including the post-write recheck. |
| 2 | Local input, usage, bundle, sidecar/version fence, credentials, CA/TLS, manifest/cardinality, or enrollment-preflight validation failure before a valid operation begins. |
| 3 | Safe pre-mutation refusal. |
| 4 | A PUT, mutating POST, or DELETE was issued; remote/recovery state can be partial. |

The sole 3/4 discriminator is the invocation-local `mutation_issued` tracker,
set immediately before each genuinely mutating request.  Once it is true, the
exit is always 4, including when a later exception is locally classified; exit
2 classification applies only while it remains false.  `FailureSite`, exception
type, journal intent, and write/proof state are diagnostic only and must not be
used to select 3 or 4.  Engine failures emit the raw stable message and
`RIGSIGNAL_FAILURE_SITE`; argparse and launcher-local validation have no engine
site.

| Emitted refusal/failure family | Exit | Operator action | FailureSite rule |
|---|---:|---|---|
| `agent_binary_unlaunchable`, `agent_version_unparseable`, `version_skew; *`, `admin_credential_api_key`, `install failed: bundle validation: *`, local `install failed: assets-only: *` | 2 | Correct the named local input, agent/version, credentials, CA, or bundle and rerun. | `preflight` for engine paths; none for launcher-local validation. |
| malformed or unverifiable remote assets ownership/presence GET response | 3 | Treat the remote response as unsafe; correct/reconcile the target and rerun. | `asset_apply`, before the first mutation. |
| argparse unknown/missing/invalid-choice | 2 | Correct CLI usage and rerun. | none. |
| `asset_conflict_unproven` | 3 | Prove/remove the foreign target through its owner path; do not overwrite it. | `asset_apply` or current pre-write site. |
| `cluster_health`, `ilm_delete_phase`, `profiles_composed_of`, `stream_composed_of`, `saved_object_topology_conflict: *`, `saved_object_topology_unverifiable: *` | 3 | Restore healthy compatible topology/composition and rerun. | `preflight` or `asset_apply`, before the write. |
| `assets_marker_upgrade_required`, `assets_marker_downgrade_required`, `adoption_required`, `migration_required`, `adoption_flag_stream_absent`, `adoption_flag_state_present`, `fleet_coexist_requires_full_flow` | 3 | Use the named upgrade, adoption, migration, or full-flow procedure. | `preflight`/`root_prepare`, before mutation. |
| `omitted_profile_on_coexist`, `ownership_profile_mismatch`, `ownership_table_version_mismatch` | 3 | Reinvoke with the recorded profile or use owner migration. | `preflight`/`root_prepare`. |
| `enrollment_remediation_required` | 3 | Perform owner/manual remediation; do not mutate this state. | `root_prepare`. |
| `enrollment ancestor is not protected: *`, `outbox preflight: *`, `enrollment preflight unavailable`, `atomic_publication_filesystem_unsupported`, `enrollment_publication_path_too_long`, `enrollment_parent_fsync_unsupported`, `local_transaction_storage_unavailable`, `enrollment_ca_path_invalid` | 2 before apply; 4 if incomplete-state recovery already issued a mutation | Fix the local enrollment filesystem/CA/preflight condition, then use full-flow recovery where applicable. | `root_prepare`/`candidate_stage`; exit derives only from `mutation_issued`. |
| `transaction_recovery_required`, `transaction_already_rolled_back`, `transaction_concurrent_drift`, `transaction_proof_ambiguous`, `transaction_proof_delete_not_authorized`, `rollback_source_mismatch*`, `rollback_source_unavailable; *`, `rollback_external_compatibility`, `m1_anchor_mismatch_break_glass` | 3 | Follow owner rollback/recovery or break-glass guidance. | rollback/current recovery site before reversal. |
| `transaction_journal_invalid`, `m1_anchor_absent`, `install failed: fleet stream verification: *` | 3 or 4 | Repair/reconcile and follow owner recovery. | 3 iff `mutation_issued` is false; otherwise 4. |
| `saved_object_id_regenerated: *`, `saved_object_id_regenerated_cleanup_failed: *`, `rollback_verify_failed` | 4 | Inspect and remediate dashboard/reversal partial state before rerun. | `asset_apply` or rollback/current recovery site. |
| `install failed: prerequisite: *`, `install failed: predecessor recheck: *`, `install failed: W1 asset verification: *`, `install failed: Kibana asset verification: *`, `install failed: shipper role verification: *`, `install failed: diagnosis stream verification: *`, `install failed: shipper credential verification: *`, `install failed: pre-publication fence: *`, `install failed: bundle marker: *`, `install failed: old shipper API key revocation: *`, `install failed: enrollment output: *`, `install refused: external asset compatibility: *` | 2, 3, or 4 | Correct the reported condition; for 4 inspect partial state and use the documented recovery/rerun route. | 2 only for underlying local validation before remote mutation; otherwise 3 iff `mutation_issued` is false, 4 iff true. |
| `install refused: existing diagnosis stream is not W1; migration is required` | 3 | Perform the documented migration. | `root_prepare`. |
| `install refused: <OwnershipTableError text>` | 2 | Correct local ownership-table/bundle input. | `preflight`. |

Full-flow enrollment recovery surfaces remain distinct: `clean` starts a new
install; `committed` is verify/no-op or explicit change; `incomplete` uses the
full-flow recovery/resume surface only; `rolled-back` needs owner confirmation
before a fresh install; `remediation` permits no mutation and requires manual
owner recovery.  No `assets resume` surface exists in 0.3.1.
