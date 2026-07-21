# D3 frozen expected-case matrix

This is a fixture oracle, derived from the §1 precedence order and the §3
pending-finding table, not from an implementation.  Unless stated otherwise,
the state has a learned baseline for `0000:03:00.0` = `[1002:7550]`, the
snapshot/header boot ID is paired with the compact ID in the named inventory,
and journal inputs are `short-iso-precise` text.  `pending` means the one
schema-v1 pending record.

| Fixture set / setup | Expected verdict | Exit | Expected state transition | Spec basis |
| --- | --- | ---: | --- | --- |
| Any valid snapshot set, no baseline | `baseline-required` | 0 | `pending` preserved | §1 row 2; §3 all other verdicts preserve |
| `real/capture-a` complete normalized capture, learned slot `0000:03:00.0`, no pending | `ok` | 0 | `pending` preserved | §1 row 8; §3 all other verdicts preserve |
| `real/capture-b` complete normalized capture, learned slot `0000:09:00.0`, no pending | `ok` | 0 | `pending` preserved | §1 row 8; §3 all other verdicts preserve |
| `real/legacy/{good-boot-kernel.log,boot-inventory.txt,healthy-slot.json}` with a converted authoritative healthy snapshot and learned baseline; inventory has no prior boot | `history-unavailable` | 0 | `pending` preserved | §1 row 7; §3 all other verdicts preserve |
| `synthetic/absent-gpu-pci-topology.txt` + `absent-gpu-current-journal.log` + `recovery-boot-inventory.txt` at compact `aaaaaaaa…` | `bus-absent` | 1 | create/replace `pending` with observation `aaaaaaaa…` | §1 row 4; §3 `bus-absent` |
| Same absent snapshot with prior journal omitted | `bus-absent` (reduced confidence) | 1 | create/replace `pending` | §1 row 4 (sysfs finding survives missing journal); §3 `bus-absent` |
| Absent snapshot's recorded pending + `recovery-healthy-pci-topology.txt` at later compact `bbbbbbbb…` | `recovered` | 0 | consume `pending` | §1 row 6; §3 `recovered` |
| Immediate rerun of the recovery fixture after consumption | `ok` | 0 | no `pending` created | §1 row 8; §3 all other verdicts preserve |
| `different-device-at-bdf-pci-topology.txt` | `hardware-changed` (replacement) | 1 | clear `pending` | §1 row 3(b); §3 `hardware-changed` |
| `unique-relocation-pci-topology.txt` | `hardware-changed` (unique relocation) | 1 | clear `pending` | §1 row 3(a); §3 `hardware-changed` |
| `duplicate-id-ambiguity-pci-topology.txt` | `hardware-changed` (ambiguity) | 1 | clear `pending` | §1 row 3(c); §3 `hardware-changed` |
| `multi-gpu-learn-pci-topology.txt` + `--learn-baseline` without `--slot` | no verdict (preflight/learn refusal) | 2 | unchanged | §1 preflight; §3 learn contract |
| Same multi-GPU fixture + `--learn-baseline --slot 0000:03:00.0` | learn confirmation | 0 | baseline created; `pending` absent | §1 row 1; §3 learn contract |
| Same multi-GPU fixture with existing learned explicit baseline | `ok` | 0 | `pending` preserved | §1 row 8; §3 all other verdicts preserve |
| `precursor-current-healthy-pci-topology.txt` + `precursor-prior-tail.log` + `precursor-boot-inventory.txt` | `precursor-warning` | 1 | `pending` preserved; never created | §1 row 5; §3 `precursor-warning` preservation |
| Same precursor pairing + `threshold-minus-one-prior-tail.log` | `ok` | 0 | `pending` preserved | §1 row 8; §1 precursor threshold; §3 all other verdicts preserve |
| Same precursor pairing + `cross-slot-prior-tail.log` | `ok` | 0 | `pending` preserved | §1 row 8; §1 precursor same-BDF rule; §3 all other verdicts preserve |
| Same precursor pairing + `outside-window-prior-tail.log` | `ok` | 0 | `pending` preserved | §1 row 8; §1 precursor 900-s window; §3 all other verdicts preserve |
| Same precursor pairing + `clean-tail-prior-tail.log` | `ok` | 0 | `pending` preserved | §1 row 8; §1 precursor terminal-failure rule; §3 all other verdicts preserve |
| Same precursor pairing + `truncated-tail-prior-tail.log` | `precursor-warning` (degraded tail confidence) | 1 | `pending` preserved; never created | §1 row 5; §1 truncated-tail terminal condition; §3 `precursor-warning` preservation |
| Healthy snapshot plus `absent-gpu-current-journal.log` for the same current boot | no verdict (inconsistent fixture preflight) | 2 | unchanged | §1 preflight fixture-consistency rule |
| Any otherwise-valid snapshot paired with `unpairable-boot-inventory.txt` | no verdict (boot-ID pairing preflight) | 2 | unchanged | §1 preflight boot-identity pairing rule |
| `malformed-pci-topology.txt` | no verdict (authoritative PCI preflight) | 2 | unchanged | §1 preflight PCI well-formedness rule |
| Valid learned-baseline set + omitted prior journal where no usable prior history remains | `history-unavailable` | 0 | `pending` preserved | §1 row 7; §3 all other verdicts preserve |
| Valid set + `--reset-baseline` (with or without an existing baseline) | reset confirmation | 0 | baseline and `pending` removed | §1 row 1; §3 reset contract |

Boot-ID pairing is deliberately represented twice: real inventories use compact 32-hex
IDs while topology headers use hyphenated UUIDs; the synthetic recovery and precursor
sets repeat that relationship.  All such pairs must normalize to lowercase 32-hex before
the row above is selected (§1 boot-ID normalization).

## Adjudication note (orchestrator, 2026-07-21f — matrix FROZEN as of this commit)

The `truncated-tail-prior-tail.log` row was flagged as interpretively ambiguous by its author.
RULING (vs spec L145-161): the row stands. The latch claim is supported by positive in-window
evidence (SMU flood ≥3 + reset attempts ≥2); truncation only weakens the no-recovery/shutdown
check → fire `precursor-warning` with DEGRADED confidence, and the truncation must appear in
`missing_evidence`. This is the spec's "lowers confidence" case — the forbidden case (truncation
alone supporting a latch claim) does not apply because the positive evidence is visible.
No other row may change without a new orchestrator ruling.
