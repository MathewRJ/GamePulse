# DiagnosisEvent v1 frozen fixture corpus

Status: **FROZEN — reviewer approval recorded 2026-07-22; all six MANIFEST
adjudications were accepted as drafted.**
This corpus is the P1 oracle.  Implementations consume it; they must not rewrite it.

## Layout and fixture schema

- `positive/` contains accepted primary and boundary cases.  Primary cases have an
  exact `.expected.json` envelope; accepted cap/helper boundaries use an exact
  result sidecar of `{"result":"accepted",...}` or the JSON literal `true`.
- `negative/` contains validator rejections plus the serde and helper boundaries.
  A validator error sidecar is the frozen fixture notation
  `{"variant":"Name",...payload}`; it asserts the named Rust variant and every
  payload member exactly.  It is fixture notation, not a required serde format for
  `ValidationError`.
- `*.input.json` for validator and serde cases is the raw `Outcome` JSON wire
  representation emitted by `src/detectors/contract.rs`: diagnosis is untagged
  (no `outcome` member); not-applicable has
  `"outcome":"not-applicable"`.  These files deliberately do not invent a tagged
  enum representation.
- `contexts/*.json` is the fixture adapter for injected `EventContext`.
  `diagnosis-finding.json` and `non-finding.json` have the same fixed ID,
  enqueue time, local host, detector contract, and input mode; their sole
  difference is the required in-process disposition.  The missing-identity context
  is the intentional case-3 override.
- Helper input is an object with `compiled_schema_version` and
  `accepted_schema_versions`; it has no `Outcome` or `EventContext`.
- Cases 17, 19, and 21 additionally assert `evidence_display_length` and
  `suggested_fixes_display_length` in their accepted sidecars.  Each is the
  integer Unicode scalar-value character count of the fully rendered display
  string (including ordinal prefixes and newline separators), not the display
  content or its UTF-8 byte count.

## Canonical serialization

Expected envelopes are compared after [RFC 8785 JSON Canonicalization Scheme
(JCS)](https://www.rfc-editor.org/rfc/rfc8785): UTF-8, recursively lexicographically
sorted object keys, ECMAScript-compatible shortest round-trippable JSON numbers, and
no insignificant whitespace.  The terminal LF in repository JSON files is not part
of the canonical byte sequence.  Input and sidecar JSON files are also stored in JCS
form for stable review, but only expected envelopes require byte-identical output
comparison.

## Fixed EventContext constants

| Field | Fixed value |
| --- | --- |
| `event_id` | `01890f3e-7b64-7cc7-8a3d-5e6f708192a3` (UUIDv7-shaped) |
| `enqueue_timestamp` | `2026-07-22T12:34:56.789012Z` |
| `local_host_name` | `Fixture-LOCAL.EXAMPLE` |
| `detector_contract.detector_id` | `D6` |
| `detector_contract.rule_version` | `d6.2` |
| `detector_contract.error_prefix` | `D6` |
| `input_mode` | `fixture` |

The diagnosis primary input timestamp is the separately fixed
`2026-07-22T10:11:12.131415Z`.  As required by §13.4, it wins for diagnosis;
the context enqueue timestamp wins for not-applicable.

## Case inventory

Context abbreviations: **DF** = `contexts/diagnosis-finding.json`; **NF** =
`contexts/non-finding.json`; **MI** = `contexts/missing-detector-identity.json`;
**MR** = `contexts/missing-detector-rule-version-identity.json`; **—** = no
context.  Every row's expected sidecar shares its input basename with
`.expected.json`.  The inventory contains 36 fixture cases.

| ID | Boundary | Input files | Context | Expected result | Contract basis |
| --- | --- | --- | --- | --- | --- |
| 01 diagnosis | validator | `positive/01-diagnosis.input.json` | DF | exact golden envelope | §§3–5, 13.2, 13.4 |
| 01b diagnosis without host | validator | `positive/01b-diagnosis-no-host.input.json` | DF | exact golden envelope | §§3–5, 13.2, 13.4 |
| 02 not-applicable | validator | `positive/02-not-applicable.input.json` | NF | exact golden envelope | §§3–5, 13.2, 13.4 |
| 03 empty evidence (case 1) | validator | `negative/03-empty-evidence.input.json` | DF | `EmptyEvidence` | §12.2 case 1; §13.1 |
| 04 empty plain language (case 2) | validator | `negative/04-empty-plain-language.input.json` | NF | `EmptyPlainLanguage` | §12.2 case 2; §13.1 |
| 05 missing identity (case 3) | validator | `negative/05-missing-detector-contract-identity.input.json` | MI | `MissingDetectorContractIdentity` | §12.2 case 3; §13.1, §13.4 |
| 05b missing rule-version identity | validator | `negative/05b-missing-detector-contract-rule-version.input.json` | MR | `MissingDetectorContractIdentity` | §13.1, §13.4 |
| 06 confidence 1.4 (case 4) | validator | `negative/06-confidence-out-of-range.input.json` | DF | `ConfidenceOutOfRange` | §12.2 case 4; §13.1 |
| 07 evidence 51 elements (case 5) | validator | `negative/07-limit-exceeded-array.input.json` | DF | `LimitExceeded { field: "evidence", limit: 50 }` | §7; §12.2 case 5; §13.1, §13.3 |
| 08 carried detector-id mismatch | validator | `negative/08-detector-contract-mismatch.input.json` | DF | `DetectorContractMismatch { field: "detector_id", expected: "D6", actual: "D6-mismatch" }` | §13.1, §13.4 |
| 08b carried rule-version mismatch | validator | `negative/08b-detector-contract-rule-version-mismatch.input.json` | DF | `DetectorContractMismatch { field: "rule_version", expected: "d6.2", actual: "d6.2-mismatch" }` | §13.1, §13.4 |
| 09 unknown outcome (case 6) | serde | `negative/09-unknown-outcome-serde.input.json` | — | serde rejection into `Outcome` | §12.2 case 6; §13.1 |
| 10 version 1 absent (case 7) | helper | `negative/10-schema-version-not-accepted.input.json` | — | `false` | §12.1–12.2 case 7; §13.1, §13.4 |
| 11 confidence 0 | validator | `positive/11-confidence-zero.input.json` | DF | exact golden envelope | §4; §13.5 |
| 12 confidence 1 | validator | `positive/12-confidence-one.input.json` | DF | exact golden envelope | §4; §13.5 |
| 13 confidence just below 0 | validator | `negative/13-confidence-just-below-zero.input.json` | DF | `ConfidenceOutOfRange` | §4; §13.5 |
| 14 confidence just above 1 | validator | `negative/14-confidence-just-above-one.input.json` | DF | `ConfidenceOutOfRange` | §4; §13.5 |
| 15 non-finding conditional fields | validator | `positive/15-diagnosis-non-finding-conditional.input.json` | NF | exact golden envelope | §§4.2–4.3, §5, §13.5 |
| 16 version 1 present | helper | `positive/16-schema-version-accepted.input.json` | — | `true` | §12.1; §13.1 |
| 17 all four array counts = 50 | validator | `positive/17-all-array-counts-at-limit.input.json` | DF | accepted; display lengths 590/590 | §7; §13.3, §13.5 |
| 18a evidence count = 51 | validator | `negative/18-evidence-array-count-over-limit.input.json` | DF | `LimitExceeded { evidence, 50 }` | §7; §13.3, §13.5 |
| 18b suggested fixes count = 51 | validator | `negative/18-suggested_fixes-array-count-over-limit.input.json` | DF | `LimitExceeded { suggested_fixes, 50 }` | §7; §13.3, §13.5 |
| 18c supported scope count = 51 | validator | `negative/18-supported_scope-array-count-over-limit.input.json` | DF | `LimitExceeded { supported_scope, 50 }` | §7; §13.3, §13.5 |
| 18d missing evidence count = 51 | validator | `negative/18-missing_evidence-array-count-over-limit.input.json` | DF | `LimitExceeded { missing_evidence, 50 }` | §7; §13.3, §13.5 |
| 19 all four element lengths = 4096 | validator | `positive/19-all-array-elements-at-limit.input.json` | DF | accepted; display lengths 4099/4099 | §7; §13.3, §13.5 |
| 20a evidence element = 4097 | validator | `negative/20-evidence-element-over-limit.input.json` | DF | `LimitExceeded { evidence, 4096 }` | §7; §13.3, §13.5 |
| 20b suggested-fixes element = 4097 | validator | `negative/20-suggested_fixes-element-over-limit.input.json` | DF | `LimitExceeded { suggested_fixes, 4096 }` | §7; §13.3, §13.5 |
| 20c supported-scope element = 4097 | validator | `negative/20-supported_scope-element-over-limit.input.json` | DF | `LimitExceeded { supported_scope, 4096 }` | §7; §13.3, §13.5 |
| 20d missing-evidence element = 4097 | validator | `negative/20-missing_evidence-element-over-limit.input.json` | DF | `LimitExceeded { missing_evidence, 4096 }` | §7; §13.3, §13.5 |
| 21 both display strings = 8192 | validator | `positive/21-display-fields-at-limit.input.json` | DF | accepted; display lengths 8192/8192 | §5, §7; §13.3, §13.5 |
| 22 evidence display = 8193 | validator | `negative/22-evidence-display-over-limit.input.json` | DF | `LimitExceeded { evidence_display, 8192 }` | §5, §7; §13.3, §13.5 |
| 23 suggested-fixes display = 8193 | validator | `negative/23-suggested-fixes-display-over-limit.input.json` | DF | `LimitExceeded { suggested_fixes_display, 8192 }` | §5, §7; §13.3, §13.5 |
| 24 exact serialized event byte cap | validator | `positive/24-event-bytes-exact-cap.input.json` | DF | accepted; serialized bytes 1048576 | §15.1 |
| 24 one byte over serialized event cap | validator | `negative/24-event-bytes-one-over.input.json` | DF | `EventBytesLimitExceeded { limit: 1048576, actual_saturated: 1048577 }`; serialized bytes 1048577 | §15.1 |
| 25 Unicode and JSON-escape serialized byte cap | validator | `positive/25-event-bytes-unicode-escape.input.json` | DF | accepted; serialized bytes 1048576; source plain-language UTF-8 bytes 1039018 | §15.1 |
| 26 serialized-byte saturation helper | helper | `negative/26-event-bytes-saturation.input.json` | — | `actual_saturated: 4294967295`; allocation bytes max 1048576 | §15.1 |

Case 01 proves source-host lowercasing and ordered evidence/fix displays.  Case 01b
proves the diagnosis-arm `None` host fallback and lowercasing.  Case 02 proves
fallback-host lowercasing, the injected timestamp, and diagnosis-only field
omission.  Case 15 proves that a diagnosis retains `suggested_fixes: []` and
`missing_evidence: []`, while omitting `suggested_fixes_display`.

## Required unit tests not JSON-representable

- Construct `Outcome::Diagnosis` values at confidence `NaN`, `+∞`, and `-∞`;
  each must produce `ConfidenceOutOfRange`.  JSON cannot express these IEEE-754
  values.
- Round-trip a constructed valid diagnosis and a constructed valid not-applicable
  through the raw CLI JSON forms, and assert both branches are exhaustive.  This is
  required alongside case 09 by §13.1; the current source only derives
  `Serialize`, so the eventual deserialization support is itself subject to the
  adjudication below.
- Invoke the validator with the actual in-process `Disposition` from each branch,
  not a value reconstructed from the raw JSON.  The context adapter's disposition
  field is test-fixture metadata only.

## Adjudications required

Reviewer approval on 2026-07-22 accepted adjudications 1–6 as drafted.

1. **Raw `Outcome` deserialization is not currently implemented.**  The checked
   source derives `Serialize`, but not `Deserialize`, for `Outcome`,
   `Diagnosis`, and `NotApplicable`.  Case 09 and the §13.1 valid-round-trip
   requirement therefore require a future deserialization representation.  This
   corpus freezes the only defensible target as the existing emitted wire shapes
   (untagged diagnosis; literal `"not-applicable"` not-applicable), not an invented
   tagged wrapper.  Citation: CODE `src/detectors/contract.rs:21–41,112–139`;
   §4, §13.1.
2. **Disposition cannot be recovered from raw CLI JSON.**  It is `#[serde(skip)]`
   on both source structs, while §13.4 requires it in every envelope.  The context
   files use `in_process_disposition` solely to let JSON fixtures stand for the
   signal that must actually be threaded from `Outcome::disposition()`; this
   extends the enumerated §13.2 context fields and needs owner approval of that test
   adapter shape.  Citation: CODE `contract.rs:39–40,118–119,141–147`; §13.2,
   §13.4.
3. **`EventContext` and `InputMode` have no implemented serialized shape.**
   This corpus chooses lower-snake-case context keys and the string `"fixture"`.
   Only the resulting envelope path `rigsignal.diagnosis.input_mode` is mapped;
   reviewer must ratify this test-side context schema, not treat it as a change to
   the raw CLI contract.  Citation: §§3.2–3.3, §13.2, §13.4.
4. **`ValidationError` has no implementation or serde contract yet.**  The
   `{"variant":...}` sidecars freeze discriminant and payload equality without
   assuming a Rust enum JSON encoding.  Reviewer must ratify that harness notation.
   Citation: §13.1.
5. **Positive envelope optional-root policy.**  The component template maps
   `observer.name` but neither §4 nor §13.4 requires it.  Golden envelopes omit
   it (and all other unrelated ECS fields), so byte comparison is against the
   minimal required envelope.  Citation: §3.1, §4.1, component template.
6. **Boundary-success cap cases intentionally assert acceptance, not a second
   enormous golden envelope.**  They still force the exact source strings/counts
   through validation; exact envelope byte comparison is frozen for the primary
   positive branches and confidence/conditional semantics.  Reviewer must confirm
   this is sufficient for §13.5's boundary coverage.  Citation: §13.5.
