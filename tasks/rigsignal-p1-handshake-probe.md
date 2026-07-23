# Task: rigsignal-p1-handshake-probe — ratified handshake probe

Session: `2026-07-23c-handshake-impl`. Work only in the supplied RigSignal worktree. Do not commit; the orchestrator commits after review. Do not bump versions.

## STM contract

Before work:

```bash
CHRONO_SESSION=2026-07-23c-handshake-impl STM_AGENT=codex@nuc \
  bash /home/dev/coding/Workflow/scripts/stm.sh recall
```

On completion and after each non-obvious decision/failure, save a concise STM entry with the same session and `STM_AGENT=codex@nuc`. If STM is unreachable, note that once in the final summary and proceed without retrying.

## Authority, selected decisions, and landing zone

This is the D6 implementation contract. The normative source is `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p0-2026-07-22/W1-DIAGNOSISEVENT-CONTRACT.md`: ratified §16 controls; §12.1, §14, and §15 provide its incorporated context. If this task conflicts with it, §16 wins and the implementer must report the conflict.

Use these selected forks only: §16.1.2b as amended (exact-name grants, no wildcards), §16.2.9b (template 404 = compatibility), §16.2.10a (413 = compatibility), §16.2.11a (pending later maps destination-blocked), §16.2.12a (display a validated observed UUID; never pin), and §16.3.2b (a leading-zero accepted version malforms the set).

Create `src/handshake.rs`, declared by `main.rs`. This is a distinct read-only protocol concern, so it must not grow `shipper.rs` or inherit delivery transport policy. `shipper::ping()` is prior art only: it returns `Ok` for all 4xx and is forbidden as classifier/client reuse. `main.rs`’s pre-`Config::load()` diagnose dispatch and its `unreachable!` guard are protected and must remain untouched: dispatch `HandshakeCheck` immediately after that protected diagnose block and **before** `Config::load()`. Use `DIAGNOSIS_SCHEMA_VERSION` from `src/detectors/contract/diagnosis_event.rs` as the sole compiled-version source; its existing membership helper does not replace the required accepted-set validation. The launcher owns status dispatch in `packaging/rigsignal-launcher.sh`; no launcher test convention exists, so add a focused POSIX shell test (for example `packaging/tests/test-rigsignal-launcher.sh`).

## Scope

In scope:

- `src/handshake.rs`: typed protocol model, input/preflight resolution, accepted-set validation/digest, output model, pure root/template/E3 classification, bounded HTTP parser, and unit tests.
- `src/main.rs`: top-level clap `handshake check` dispatch, without touching diagnose ordering.
- `src/config.rs` and example config only as needed for frozen `[shipping]` values.
- `src/Cargo.toml`/workspace-root `Cargo.lock` only if needed for a minimal SHA-256 implementation (`sha2` is acceptable) and target-Unix `libc`. `libc` is permitted solely behind `cfg(unix)` for `O_NOFOLLOW` open plus fstat-on-open-fd checks (regular file, `st_uid == geteuid()`, and `mode & 0o077 == 0`); it is not permission to add a heavyweight parser/security dependency or use libc for other work.
- Launcher and its new shell test.
- The evidence-stage role artifact specified below.

Out of scope: handshake cache/epochs; capsule implementation; durable outbox/drainer or durable-oracle §7 rows; Rust `rigsignal status`; enrollment/pinning/rebind; repo `elastic/` assets; and any provisioning/deployment. This is a one-shot read-only probe: no lease, cache/capsule/outbox mutation, enrollment, pinning, rebind, or durable recheck. It is **non-deployable** until the separate provisioning-order task lands the W1 assets; name that blocker in the final summary.

## Required implementation

### Types and request sequence

Implement:

- `ElasticsearchClusterUuid`: distinct ASCII grammar `[A-Za-z0-9_-]{22}`, exact byte comparison; never an RFC-4122 event UUID.
- `TargetGeneration([u8; 32])`: accepts exactly 64 lowercase hex and renders precisely 64 lowercase hex in every JSON/CLI use. Its derivation is deferred to outbox/provisioning work.
- Closed serializable enums: outcome `ready | pending_enrollment | failed`; reason `ready | pending_enrollment | local_config | connectivity | auth | destination | compatibility | unclassified_4xx`. Never emit a free-form cause/body.

E1 is `GET /`. E2, after valid E1 only, is exactly:

```text
GET /_component_template/logs-rigsignal.diagnosis-mappings?filter_path=component_templates.name,component_templates.component_template._meta.accepted_schema_versions
```

E2’s `filter_path` is mandatory; parse only `component_templates[0].component_template._meta.accepted_schema_versions` and require exactly one entry named `logs-rigsignal.diagnosis-mappings`. After successful E2, selected §16.1.2b requires E3 `GET /logs-rigsignal.diagnosis-default/_mapping` (the exact stream name, never a wildcard). E3 is bounded but never retained or parsed as accepted-set data. E3's frozen shape rule: a `200` E3 body MUST parse (bounded) as a JSON object — `{}` included — whose member values are unconstrained beyond the bounded-scanner limits; a conforming body preserves the E2 result. Zero bytes, whitespace-only input, truncated JSON, a non-object root (array/string/number/literal), or any other syntax failure is `failed/compatibility`.

### Exhaustive classifier

Write pure stage-aware classifiers testable with synthetic status/body/transport observations. No unlisted observation may create an unclosed reason. Redirects are disabled and never followed. E2 must run after a valid root UUID even when pending; final pending requires successful member E2 and successful E3, and any listed failure wins.

Freeze this classifier precedence independently at each request stage, including compound observations. The classification point is the moment a status line is received: (1) a transport failure BEFORE any status line is received (DNS/TLS/connect/reset/deadline) is `connectivity`; (2) once a non-`200` status is received, its status row classifies IMMEDIATELY and irrevocably — the body is drained bounded (stopping silently at the cap or deadline), and nothing observed during the drain (cap reached, transport failure, any `Content-Encoding`, malformed content) changes the classification; (3) for a received `200`: first, a `Content-Encoding` other than absent/`identity` is `compatibility`; second, a transport failure or deadline while reading the body is `connectivity`; third, the decompressed cap exceeded while reading is `compatibility`; fourth, syntax/shape/parse failures are `compatibility`. Thus `401` with a malformed body is `auth`; `503` with an over-limit or encoded body is `connectivity` (its status row); `200` with an over-limit body is `compatibility`; `200` with a mid-body reset is `connectivity`. The bounded drain must never parse or retain the body.

| Stage observation | Result |
|---|---|
| Preflight malformed endpoint/generation/affinity/credential reference/CA | `failed/local_config` |
| Root 200 exactly one valid UUID matching expected | continue |
| Root 200 valid UUID with pending marker | continue pending |
| Root 200 missing/malformed UUID; root 1xx/2xx except 200; malformed/limited body; root 400 or 413 | `failed/compatibility` |
| Root UUID mismatch, 404, or 3xx | `failed/destination` |
| Root 401/403 | `failed/auth` |
| Root 408/429/5xx or DNS/TLS-peer/connect/reset/read/mid-body/deadline | `failed/connectivity` |
| Root other 4xx | `failed/unclassified_4xx` |
| Template 200 exact one name, valid member set, not pending | `ready/ready` |
| Template 200 valid member set, pending | `pending_enrollment/pending_enrollment` |
| Template wrong count/name, absent/malformed/empty/nonmember set; 1xx/2xx except 200; malformed/limited body; 400, 404, or 413 | `failed/compatibility` |
| Template 401/403 | `failed/auth` |
| Template 3xx | `failed/destination` |
| Template 408/429/5xx or transport/deadline | `failed/connectivity` |
| Template other 4xx | `failed/unclassified_4xx` |
| E3 200 with a valid JSON-object body (including `{}`) | preserve E2 result |
| E3 200 with a non-object root or any syntax failure (incl. zero bytes) | `failed/compatibility` |
| E3 1xx/2xx except 200, malformed/limited body, or 413 | `failed/compatibility` |
| E3 401/403 | `failed/auth` |
| E3 404 or 3xx | `failed/destination` |
| E3 408/429/5xx or transport/deadline | `failed/connectivity` |
| E3 other 4xx | `failed/unclassified_4xx` |

The later durable mapping is documentation only: compatibility → `compatibility-blocked/compatibility`, auth → `auth-blocked/auth`, destination and selected pending → `destination-blocked/destination`, connectivity → `retry-wait/connectivity`, and unclassified 4xx → `retry-wait/unclassified-4xx`. Do not implement it.

### Accepted set and digest

A set is a nonempty JSON array of ≤256 strings. Every string is 1–10 ASCII decimal bytes, canonical `[1-9][0-9]*`, and numerically `1..=u32::MAX`. Reject nonstrings, empty arrays, non-ASCII/nondigits, zero, overflow, overlength, and under selected §16.3.2b any leading-zero spelling (`"01"`, `"001"`): the entire set is malformed and digest is null. Sort strings by UTF-8 bytes and deduplicate byte-identical values. Membership is the canonical decimal rendering of `DIAGNOSIS_SCHEMA_VERSION`.

Digest exactly:

```text
b"rigsignal:w1:accepted-schema-versions:digest:v1\0"
+ u32be(entry_count)
+ for each sorted unique entry: u32be(utf8_byte_length) + utf8_bytes
```

SHA-256 render is 64 lowercase hex. Required known-answer tests: `["1"]` → `e3109e79014641e8d92907f3030bcbc187e991df02b1ab0893e15578302c1d0a`; `["2","1","2"]` → `45051fcac43b37be314619cdfb3530ecd45c8a500b0521f29567857cd75b9df9`; `["01","1"]` and `["00"]` → malformed/null. Also prove reordering and duplicate insertion invariance. At the private digest-framing helper layer, retain the alternate-fork framing KAT `["01","1"]` → `ef6b086833d882eb0b66911d50184c24f593cae0b24acec0bba9f25166928dd6`; this proves framing only and does not admit the rejected §16.3.2a policy. Require grammar tests for 256/257 entries, `u32::MAX`/overflow, 10/11 digits, non-ASCII, nonstring, and lexicographic (not numeric) ordering.

### Input resolution and secrets

Resolve exactly endpoint, CA-file reference, expected UUID **or** pending marker (exactly one), target generation, and protected credential reference. Each non-secret input resolves first-match-wins: (1) explicit flag, (2) environment value, (3) protected config, (4) W1 §15.3 capsule. Tier 4 is empty until capsule implementation; once it exists, it replaces `[shipping]` at tiers 3–4 but never overrides flags/environment. An absent `--config` makes tier 3 empty: the handshake reader MUST NOT default-path search.

Exact flags: `--endpoint`, `--ca-file`, `--expected-cluster-uuid`, `--pending-enrollment`, `--target-generation`, `--credentials-file`, `--config`. Exact environment names: `RIGSIGNAL_ES_ENDPOINT`, `RIGSIGNAL_ES_CA_FILE`, `RIGSIGNAL_EXPECTED_CLUSTER_UUID`, `RIGSIGNAL_PENDING_ENROLLMENT` (literal `1`), `RIGSIGNAL_TARGET_GENERATION`, `RIGSIGNAL_ES_CREDENTIALS_FILE`. Retain existing `[elasticsearch]` `endpoint`/ `ca_cert`; freeze `[shipping]`: `expected_cluster_uuid` matching string, `pending_enrollment` boolean `true`, `target_generation` 64-lowercase-hex string.

The complete non-secret resolution table is:

| Input | Tier 1: handshake flag | Tier 2: exact environment | Tier 3: `--config` selected protected file only | Tier 4: capsule |
|---|---|---|---|---|
| endpoint | `--endpoint` | `RIGSIGNAL_ES_ENDPOINT` | `[elasticsearch].endpoint` | §15.3 value when implemented |
| CA reference | `--ca-file` | `RIGSIGNAL_ES_CA_FILE` | `[elasticsearch].ca_cert` | §15.3 value when implemented |
| affinity | `--expected-cluster-uuid` or `--pending-enrollment` | expected UUID env or pending env | `[shipping]` expected UUID or `pending_enrollment = true` | §15.3 value when implemented |
| target generation | `--target-generation` | `RIGSIGNAL_TARGET_GENERATION` | `[shipping].target_generation` | §15.3 value when implemented |
| credential reference/source | `--credentials-file` | `RIGSIGNAL_ES_CREDENTIALS_FILE` | inline `[elasticsearch]` credentials | §15.3 value when implemented |

For endpoint, CA, and generation, the first present cell wins and must validate; lower cells are ignored. For credentials, a winning dedicated-file reference wins over inline credentials even when invalid; no present dedicated reference means the protected inline source may be selected. The capsule column is empty now and replaces `[shipping]` values at tiers 3–4 when implemented. No cell implies a default search.

Credentials come only from the protected `--config`-selected config’s `[elasticsearch]` `api_key` or `username`+`password`, or a dedicated credentials file with that same table. Dedicated file wins even when invalid; within the winning source api key wins. Empty or partial selected basic credentials, empty api keys, unknown dedicated-file credential keys, and every malformed winning credential source are `local_config`, never a fall-through to another source. Credential material is forbidden in argv, environment value, stdout/stderr/JSON, tracing, and logs; only its non-secret file reference may appear.

Use a handshake-specific protected-config reader, never `Config::load()`. It accepts only `--config` at the handshake subcommand and reads only the frozen handshake keys. It ignores legacy `RIGSIGNAL_CONFIG`, `ES_API_KEY`, `ES_URL`, and `ES_CA_CERT` completely. Define clap as `rigsignal-agent handshake check [HANDSHAKE-ONLY OPTIONS]`: `HandshakeCheck` owns the seven exact flags above, so valid placement is after `handshake check`; reject root telemetry/options (for example `--dry-run`, telemetry endpoint/options and legacy config options) combined with `handshake check` via clap conflicts/subcommand scoping as a usage error outside `10..=16`. Tests must prove legacy flags/environment are rejected or ignored, respectively, and cannot affect resolution.

Endpoint grammar is frozen: the resolved endpoint MUST parse as an absolute URL with scheme exactly `http` or `https`, a nonempty host, an optional port, and a path that is empty or exactly `/` (the trailing slash is normalized away); any query string, fragment, userinfo (a forbidden credential channel), other scheme, or nonempty path is `failed/local_config` — reject, never normalize beyond the single trailing slash. Requests are composed by exact concatenation of the normalized origin with the fixed request paths (`/` for E1; the exact E2 path with its mandatory `filter_path`; `/logs-rigsignal.diagnosis-default/_mapping` for E3); no other rewriting is permitted.

Resolve expected UUID/pending enrollment as one sum-typed affinity input, not as two independently resolved values. Apply this full truth table tier by tier; `present` includes empty or malformed text and is validated at the winning tier:

| First tier containing an affinity alternative | Result |
|---|---|
| neither alternative at a tier | inspect the next lower tier |
| exactly one expected UUID, valid | select expected UUID; lower tiers ignored |
| exactly one `--pending-enrollment` flag / config boolean `true` / env literal `1` | select pending; lower tiers ignored |
| both alternatives at the same tier | `failed/local_config`; lower tiers ignored |
| present expected UUID invalid/empty, pending config value invalid, or pending env value other than literal `1` | `failed/local_config`; never fall through |
| no alternative in any tier | `failed/local_config` |

For every other input, a present empty/malformed high-tier value is likewise `local_config`, never fall-through. A dedicated credential reference is considered present at its winning tier even if the file is invalid, and then wins over inline credentials.

On Unix, every credential source is atomically opened with `O_NOFOLLOW` and then fstat-checked on that opened fd: it must be a regular file, have `st_uid == geteuid()`, and `(mode & 0o077) == 0`. Missing, non-regular, unreadable, malformed, wrong-owner, over-permissive, or symlinked is `local_config`. Extract the metadata predicate as a pure validator with injectable metadata so wrong-owner behavior is testable without `chown`. On non-Unix, protected credential/config use fails closed as `local_config` before any networking; cfg gates must make Windows all-target builds compile. CA missing/unreadable/malformed is `local_config`; after valid CA read, TLS peer/DNS/connect/reset/read/deadline failure is connectivity.

### HTTP policy and bounded parsing

Build a handshake-specific reqwest client with `Policy::none()` redirects. Set one absolute monotonic 10-second deadline immediately after successful preflight. Each E1/E2/E3 operation is its complete send **plus bounded body read** future, wrapped in `timeout_at(the_same_absolute_deadline, ...)`; it covers DNS, TLS, headers, and streamed body, and no client timeout may reset the budget. Request `Accept-Encoding: identity`, do not enable reqwest compression features, and — on `200` responses only, per the frozen classifier precedence — treat a received `Content-Encoding` other than absent or `identity` as `failed/compatibility` before JSON parsing (on non-`200` responses the status row already classified and encoding is irrelevant). Stream the identity response with a 65,536-byte cap before parsing; never use unbounded `text()` or `json()`.

Before `serde_json` deserialization, implement a small in-module bounded scanner, not a heavyweight parser dependency. It must enforce nesting ≤32, JSON token count ≤4,096, and decoded string length ≤4 KiB including escape handling; document its accounting beside the code, then deserialize only the checked buffer into minimal response shapes. Scanner semantics are frozen: a token is each `{`, `}`, `[`, `]`, `:`, `,`, complete string, complete number, or complete literal (`true`, `false`, `null`); root containers begin at depth 1 (so 32 is accepted and 33 rejected); decoded string length is UTF-8 bytes; simple escapes count as their one decoded byte; `\uXXXX` counts the UTF-8 bytes of its decoded scalar; a valid surrogate pair is one scalar and counts its UTF-8 bytes; unpaired/malformed escapes are malformed. Protected-key duplicate semantics are frozen: the protected keys are, for E1, `cluster_uuid` in the root object; for E2, `component_templates` in the root object, and `name`, `component_template`, `_meta`, and `accepted_schema_versions` each within its expected containing object on the parsed path. A duplicate occurrence of any protected key within its containing object is malformed (`compatibility` at that stage) — never last-wins or first-wins. Duplicates of non-protected keys are permitted and their values skipped (still counted against scanner limits). E3 has no protected keys (object-root check only). All limits are inclusive. Require 32/33-depth, 4,096/4,097-token, 4,096/4,097-decoded-byte string, escaped-string, valid surrogate-pair, malformed-escape, 65,536/65,537-byte, and arbitrary chunk-boundary tests. Malformed/over-limit body is stage-local compatibility. Probe bodies and authorization are neither persisted nor logged; ordinary output carries none, and sentinel credential/body tests must prove neither appears in stdout, stderr, nor captured logs.

### CLI and launcher

Add a top-level clap arm so the direct command is exactly `rigsignal-agent handshake check`, never under `diagnose`. Each classified result prints exactly one stdout JSON object with every field:

```text
probe_schema_version: 1
diagnosis_schema_version: DIAGNOSIS_SCHEMA_VERSION
outcome, reason: closed domains
failed_stage: none | local | root_info | template_read | mapping_read
target_generation: valid 64-lowercase hex after preflight, else null
observed_cluster_uuid: validated root UUID under selected display fork, else null
accepted_set_digest: non-null only after valid E2 parsing
```

Module surface is prescribed: typed `CheckArgs` feeds `run_check(...) -> Result<ClassifiedProbe, InternalError>`; `ClassifiedProbe` owns the exact exit code and serialized object. Give `run_check` injected `Environment`, `Clock`/absolute-deadline, and `Transport` seams: `Transport` is a trait with a reqwest implementation and deterministic test implementation returning status/headers/chunked body/transport observations. Convert every listed URL/config/file/CA/credential/header/preflight failure to a sanitized `ClassifiedProbe`; only broken invariants or output-I/O are `InternalError`. `main` serializes a classified report exactly once, appends exactly one trailing LF to the fixture object, and returns its exit code.

Exit map: 0 ready; 10 pending enrollment; 11 local config; 12 connectivity; 13 auth; 14 destination; 15 compatibility; 16 unclassified 4xx. Usage/internal exits remain outside 10–16. Appendix A’s eight §16.5 canonical fixture objects plus exactly one trailing LF are byte-for-byte golden tests. Add a state/nullability matrix proving root UUID mismatch retains observed UUID; structurally valid nonmember E2 retains digest; wrong E2 name/count has no digest; every E3 failure retains validated UUID and E2 digest; and every E3 failure has `failed_stage: mapping_read`.

Make the minimal launcher guard: plain `rigsignal status` with no status args remains unchanged. Only `rigsignal status handshake recheck` with zero generation args or one valid 64-lowercase-hex generation is accepted; until durable support exists it must fail nonzero with “not yet available”. Every other status argument sequence, including malformed generation, fails nonzero and never falls through to ordinary status. The shell test table must cover plain-status equivalence; valid zero/one generation forms; uppercase, short, long, and nonhex generation; missing `recheck`; extra arguments; and unrelated status arguments, and prove each rejected form never invokes `cmd_status`.

### Role-evidence handoff

Write this production role JSON to `/home/dev/coding/Workflow/projects/Workflow/evidence/app-p1-handshake-2026-07-23/rigsignal_shipper-role.json`, not to repo `elastic/`:

```json
{
  "cluster": ["monitor"],
  "indices": [{
    "names": ["logs-rigsignal.diagnosis-default"],
    "privileges": ["view_index_metadata", "create_doc"]
  }]
}
```

No wildcard, `manage_index_templates`, document read/search/update/delete/overwrite, template modification, or other-stream write is permitted. The live exact-role CAN/CANNOT gate is **Phase 5, orchestrator-run, outside this task**. Its required evidence is an ephemeral ES 9.4.4 harness/artifact recording the exact role hash, image identity, E1/E2/E3 statuses, successful `create_doc`, and denials for overwrite/update/delete/get/search, other-stream write, and template mutation, without credentials or authorization headers. Implementer completion is **not** acceptance: merge is contingent on the orchestrator wire gate passing under the exact-name role.

## Conformance map

Every atomic clause has one row. `implement now` is executable work in this task; `evidence only — Phase 5 wire gate (orchestrator)` is a merge-contingent but out-of-task live proof; `deferred` is intentionally not implemented here.

| §16 clause | Label and obligation | Named test/evidence artifact |
|---|---|---|
| 16.0 | implement now — W1 §16 and selected forks control. | `handshake::tests::contract_selected_forks` and final conformance summary. |
| 16.1.1a | implement now — E1 then E2. | `handshake::tests::request_sequence_e1_e2_e3`. |
| 16.1.1b | implement now — exact E2 source/name. | `handshake::tests::template_exact_name_and_shape`. |
| 16.1.1c | implement now — mandatory exact `filter_path`. | `handshake::tests::e2_path_and_filter_path_exact`. |
| 16.1.2b | implement now — exact-name E3 after successful E2. | `handshake::tests::e3_only_after_successful_e2`. |
| 16.1.3 | implement now — role excludes `manage_index_templates`. | `rigsignal_shipper-role.json` structural assertion. |
| 16.1.4 | implement now — exact stream `create_doc` role entry. | `rigsignal_shipper-role.json` structural assertion. |
| 16.1.5 | evidence only — Phase 5 wire gate (orchestrator) — selected reads and create CAN succeed. | Phase 5 exact-role CAN artifact (role hash/image/statuses/create result). |
| 16.1.6 | evidence only — Phase 5 wire gate (orchestrator) — prohibited operations CANNOT succeed. | Phase 5 exact-role CANNOT artifact (overwrite/update/delete/get/search/other-stream/template denials). |
| 16.2.1 | implement now — inherited closed mappings. | `handshake::tests::closed_reason_matrix`. |
| 16.2.2 | implement now — no TOFU; pending remains blocked in documented mapping. | `handshake::tests::pending_never_pins_or_delivers`. |
| 16.2.3 | implement now — root 404 mapping. | `handshake::tests::root_status_matrix`. |
| 16.2.4 | implement now — stage-specific classifier, including E3. | `handshake::tests::per_stage_status_matrix`. |
| 16.2.5 | implement now — redirects disabled/not followed and received 3xx classified. | `handshake::tests::redirect_not_followed`. |
| 16.2.6 | implement now — root before template, including pending. | `handshake::tests::request_sequence_e1_e2_e3`. |
| 16.2.7 | implement now — complete shape checks/closed reasons. | `handshake::tests::classifier_is_exhaustive`. |
| 16.2.8 | implement now — final pending requires successful member E2 and E3. | `handshake::tests::pending_requires_e2_and_e3`. |
| 16.2.9b | implement now — template 404 is compatibility. | `handshake::tests::template_status_matrix`. |
| 16.2.10a | implement now — all stage 413 values are compatibility. | `handshake::tests::status_413_all_stages`. |
| 16.2.11a | deferred — durable pending mapping is outbox work; probe emits pending only. | `handshake::tests::pending_probe_result`; outbox/capsule task. |
| 16.2.12a | implement now — validated observed UUID is displayed, never pinned. | `handshake::tests::uuid_retention_nullability`. |
| 16.2.13a | implement now — only five input categories. | `handshake::tests::resolution_truth_table`. |
| 16.2.13b | implement now — exact handshake-only flags; legacy/root flags rejected. | `main::tests::handshake_clap_surface`. |
| 16.2.13c | implement now — exact environments; legacy environments ignored. | `handshake::tests::legacy_env_ignored`. |
| 16.2.13d | implement now — frozen config keys/types. | `handshake::tests::protected_config_schema`. |
| 16.2.13e | implement now — four-tier first-match-wins/capsule replacement. | `handshake::tests::resolution_truth_table`. |
| 16.2.13f | implement now — sum-typed, exactly-one affinity. | `handshake::tests::affinity_tier_truth_table`. |
| 16.2.13g | implement now — protected-source rules/fail closed. | `handshake::tests::protected_source_negatives`. |
| 16.2.13h | implement now — dedicated-file/api-key ties and invalid-winner behavior. | `handshake::tests::credential_winner_truth_table`. |
| 16.2.13i | implement now — compiled schema source only. | `handshake::tests::compiled_schema_version_only`. |
| 16.2.14 | implement now — no credential leakage; CA/config vs peer failure distinction. | `handshake::tests::sentinel_secret_not_emitted`. |
| 16.2.15 | implement now — one ten-second absolute deadline. | `handshake::tests::shared_deadline_slow_body`. |
| 16.2.16a | implement now — 65,536-byte body cap/content-encoding rejection. | `handshake::tests::body_cap_boundaries_and_content_encoding`. |
| 16.2.16b | implement now — depth ≤32. | `handshake::tests::scanner_depth_boundaries`. |
| 16.2.16c | implement now — tokens ≤4,096. | `handshake::tests::scanner_token_boundaries`. |
| 16.2.16d | implement now — decoded strings ≤4 KiB. | `handshake::tests::scanner_string_boundaries_and_escapes`. |
| 16.2.16e | implement now — malformed/over-limit `200` bodies compatibility; frozen compound precedence. | `handshake::tests::compound_status_body_precedence`. |
| 16.2.17 | implement now — no body/authorization retention or logs. | `handshake::tests::sentinel_body_and_auth_not_retained`. |
| 16.3.1 | implement now — grammar/sort/dedup/framing/SHA/membership. | `handshake::tests::accepted_set_vectors_and_boundaries`. |
| 16.3.2b | implement now — leading zero malforms whole set. | `handshake::tests::leading_zero_malformed`; `digest_framing_alternate_kat`. |
| 16.4.1 | implement now — target generation is 32 octets. | `handshake::tests::target_generation_parse`. |
| 16.4.2a | implement now — `[u8; 32]` representation. | `handshake::tests::target_generation_representation`. |
| 16.4.2b | implement now — 64 lowercase-hex rendering. | `handshake::tests::target_generation_render`. |
| 16.4.2c | deferred — derivation belongs to outbox/provisioning. | outbox/provisioning task. |
| 16.4.3 | deferred — outbox/capsule task — cluster-UUID type migration. | outbox/capsule task. |
| 16.4.4 | implement now — handshake root/expected/JSON use distinct ES UUID. | `handshake::tests::elasticsearch_uuid_parse_and_mismatch`. |
| 16.5.1 | implement now — exact `handshake check` command. | `main::tests::handshake_clap_surface`. |
| 16.5.2 | implement now — one-shot read-only, not diagnose. | `main::tests::diagnose_order_and_handshake_dispatch`. |
| 16.5.3 | implement now — launcher accepts bounded future surface. | `packaging/tests/test-rigsignal-launcher.sh`. |
| 16.5.4 | deferred — healthy-connectivity/lease-mediated durable mechanism. | durable outbox/recheck task. |
| 16.5.5 | deferred — durable mechanism implementation; probe remains non-durable. | durable outbox/recheck task; `main::tests::handshake_read_only_scope`. |
| 16.5.6 | implement now — launcher guard and usage exit reservation. | `packaging/tests/test-rigsignal-launcher.sh`. |
| 16.5.7 | implement now — stable JSON keys/domains/nullability. | `handshake::tests::appendix_a_golden_and_nullability_matrix`. |
| 16.5.8 | implement now — closed classified exits. | `handshake::tests::classified_exit_map`. |
| 16.6.1 | implement now — W1/incorporated-source precedence. | `handshake::tests::contract_selected_forks` and final conformance summary. |

## Hazards, verification, acceptance

Hazards to address: do not reuse 4xx-tolerant `ping()`; do not move diagnose dispatch; do not call `Config::load()` for handshake; reject root telemetry flags; disable reqwest redirects; enforce a shared absolute deadline rather than resettable client timeout; bound JSON explicitly because serde_json has no built-in depth/token caps; leave commits to the orchestrator; and cfg-gate POSIX protection so Windows all-target checks pass.

Classifier coverage is explicit, at **each** root/template/E3 stage where the row applies: 100, 199, 200, 201, 299, 300, 399, 400, 401, 403, 404, 408, 413, 429, one other 4xx representative, 500, and 599, plus DNS, TLS-peer, connect, reset, read, incomplete/mid-body read, deadline, cap-exceeded, malformed body, and unsupported content-encoding observations. Include the compound `401`-malformed, `503`-over-limit, and `200`-over-limit cases. Transport-seam request tests must prove request order, exact paths, E2 `filter_path`, redirects not followed, E1 consuming nearly all budget, slow body timeout, encoded-response rejection, and E3 absent unless E2 succeeds.

Run the exact CI commands from `.github/workflows/ci.yml`:

```bash
cargo fmt --manifest-path src/Cargo.toml -- --check
cargo check --manifest-path src/Cargo.toml --locked
cargo clippy --manifest-path src/Cargo.toml --locked --all-targets -- -D warnings
cargo test --manifest-path src/Cargo.toml --locked
cargo check --manifest-path src/Cargo.toml --locked --features ebpf
cargo clippy --manifest-path src/Cargo.toml --locked --all-targets --features ebpf -- -D warnings
bash packaging/tests/test-rigsignal-launcher.sh
```

The Linux commands above are local verification. Both-OS CI must run `cargo check --manifest-path src/Cargo.toml --locked`, `cargo clippy --manifest-path src/Cargo.toml --locked --all-targets -- -D warnings`, and `cargo test --manifest-path src/Cargo.toml --locked`; Linux additionally runs the two `--features ebpf` commands above and smoke CI runs `cargo build --manifest-path src/Cargo.toml --locked` then `bash scripts/smoke-test.sh ./target/debug/rigsignal-agent`. Windows additionally runs `cargo build --release --manifest-path src/Cargo.toml --locked`. Windows result is **pending CI** at implementer handoff and must be reported as such.

Implementer acceptance requires the listed checks green; every status/transport/preflight row above; all §16.3 vectors/reorder/duplicates/alternate framing and grammar boundaries; protected-source missing/nonregular/unreadable/malformed/wrong-mode/wrong-owner/symlink negatives; all scanner/body boundaries; all Appendix A JSON+single-LF goldens and nullability transitions; sentinel no-secret/no-body/no-auth retention tests; and launcher table tests. Live §16.1.5/§16.1.6 proof is **not** an implementer acceptance leg: merge remains contingent on the Phase 5 orchestrator exact-role wire gate passing. The final summary must map every conformance clause to its named implementation/test/evidence artifact, identify Windows as pending CI until it reports, and call out the provisioning-order blocker.
