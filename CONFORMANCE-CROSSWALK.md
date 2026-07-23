# Handshake-probe conformance crosswalk

This remaps every row in `tasks/rigsignal-p1-handshake-probe.md` to the
implemented test or artifact name. `Evidence-only` and `deferred` rows retain
their task-spec status; they are not claimed as implemented.

| Clause | Actual test or artifact |
|---|---|
| 16.0 | `handshake::tests::status_rows_are_closed`; this crosswalk |
| 16.1.1a | `handshake::tests::request_sequence_exact_paths_and_no_redirect_follow` |
| 16.1.1b | `handshake::tests::accepted_set_full_grammar_and_framing` |
| 16.1.1c | `handshake::tests::request_sequence_exact_paths_and_no_redirect_follow` |
| 16.1.2b | `handshake::tests::request_sequence_exact_paths_and_no_redirect_follow` |
| 16.1.3 | `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-handshake-2026-07-23/rigsignal_shipper-role.json` |
| 16.1.4 | `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-handshake-2026-07-23/rigsignal_shipper-role.json` |
| 16.1.5 | Evidence-only: Phase 5 orchestrator exact-role CAN artifact |
| 16.1.6 | Evidence-only: Phase 5 orchestrator exact-role CANNOT artifact |
| 16.2.1 | `handshake::tests::status_rows_are_closed` |
| 16.2.2 | `handshake::tests::pending_requires_successful_template_and_mapping` |
| 16.2.3 | `handshake::tests::per_stage_status_matrix_and_all_transport_failures` |
| 16.2.4 | `handshake::tests::per_stage_status_matrix_and_all_transport_failures` |
| 16.2.5 | `handshake::tests::request_sequence_exact_paths_and_no_redirect_follow` |
| 16.2.6 | `handshake::tests::request_sequence_exact_paths_and_no_redirect_follow` |
| 16.2.7 | `handshake::tests::compound_status_body_precedence_and_content_encoding` |
| 16.2.8 | `handshake::tests::pending_requires_successful_template_and_mapping` |
| 16.2.9b | `handshake::tests::per_stage_status_matrix_and_all_transport_failures` |
| 16.2.10a | `handshake::tests::per_stage_status_matrix_and_all_transport_failures` |
| 16.2.11a | Deferred: durable outbox/capsule task; probe behavior in `pending_requires_successful_template_and_mapping` |
| 16.2.12a | `handshake::tests::report_nullability_and_golden_json_lines` |
| 16.2.13a | `handshake::tests::endpoint_affinity_and_secret_resolution_rules` |
| 16.2.13b | `tests::handshake_clap_surface_is_subcommand_scoped`; `tests::handshake_runtime_guard_rejects_root_flag_before_subcommand` |
| 16.2.13c | `handshake::tests::legacy_env_ignored` |
| 16.2.13d | `handshake::tests::endpoint_affinity_and_secret_resolution_rules` |
| 16.2.13e | `handshake::tests::endpoint_affinity_and_secret_resolution_rules` |
| 16.2.13f | `handshake::tests::endpoint_affinity_and_secret_resolution_rules` |
| 16.2.13g | `handshake::tests::protected_source_metadata_negatives`; CA ruling in `ProcessEnvironment::read_public` |
| 16.2.13h | `handshake::tests::endpoint_affinity_and_secret_resolution_rules`; `protected_source_metadata_negatives` |
| 16.2.13i | `handshake::tests::report_nullability_and_golden_json_lines` |
| 16.2.14 | `handshake::tests::malformed_ca_is_local_with_all_nullable_fields_null`; `endpoint_affinity_and_secret_resolution_rules` |
| 16.2.15 | `handshake::tests::shared_deadline_slow_body`; `e1_consumes_budget_for_e2`; `mid_body_deadline` |
| 16.2.16a | `handshake::tests::real_read_body_enforces_the_cap`; `compound_status_body_precedence_and_content_encoding` |
| 16.2.16b | `handshake::tests::scanner_boundaries_duplicates_and_chunks` |
| 16.2.16c | `handshake::tests::scanner_boundaries_duplicates_and_chunks` |
| 16.2.16d | `handshake::tests::scanner_boundaries_duplicates_and_chunks` |
| 16.2.16e | `handshake::tests::compound_status_body_precedence_and_content_encoding`; `mid_body_deadline` |
| 16.2.17 | `handshake::tests::endpoint_affinity_and_secret_resolution_rules` |
| 16.3.1 | `handshake::tests::accepted_set_vectors_and_boundaries`; `accepted_set_full_grammar_and_framing` |
| 16.3.2b | `handshake::tests::accepted_set_vectors_and_boundaries`; `accepted_set_full_grammar_and_framing` |
| 16.4.1 | `handshake::tests::uuid_and_generation_grammar` |
| 16.4.2a | `handshake::tests::uuid_and_generation_grammar` |
| 16.4.2b | `handshake::tests::uuid_and_generation_grammar` |
| 16.4.2c | Deferred: outbox/provisioning task |
| 16.4.3 | Deferred: outbox/capsule task |
| 16.4.4 | `handshake::tests::uuid_and_generation_grammar`; `endpoint_affinity_and_secret_resolution_rules` |
| 16.5.1 | `tests::handshake_clap_surface_is_subcommand_scoped` |
| 16.5.2 | `tests::handshake_clap_surface_is_subcommand_scoped` (and protected dispatch in `main.rs`) |
| 16.5.3 | `packaging/tests/test-rigsignal-launcher.sh` |
| 16.5.4 | Deferred: durable outbox/recheck task |
| 16.5.5 | Deferred: durable outbox/recheck task |
| 16.5.6 | `packaging/tests/test-rigsignal-launcher.sh` |
| 16.5.7 | `handshake::tests::report_nullability_and_golden_json_lines`; `malformed_ca_is_local_with_all_nullable_fields_null` |
| 16.5.8 | `handshake::tests::classified_exit_map_is_closed` |
| 16.6.1 | This crosswalk and task-spec authority statement |
