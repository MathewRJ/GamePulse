#!/usr/bin/env python3
"""Generate the exhaustive flag/state oracle embedded in TEST-MANIFEST.md.

The absent ingest-pipeline/ES-role clean path is owner-ratified: GET absent,
PUT with a per-attempt ownership nonce, then the class-specific post-write
creation detector.  Detector-positive races halt in T-RECON-5/7; this table
models the detector-negative clean branch for every flag/state combination.
"""
from __future__ import annotations

import argparse
import hashlib
from itertools import product
from pathlib import Path


CALLERS = ("assets-only", "full-flow")
ASSET_OBLIGATIONS = "assets-66"
FULL_OBLIGATIONS = "assets-66+full-flow-step-11"

# ``predecessor`` means the record contains the byte-for-byte validated
# installed predecessor required by T-SM-11.  Keeping the full-flow variants
# separate makes the retained Step-11 obligation observable to assets-only.
RECORDS = (
    ("N", False, ASSET_OBLIGATIONS, False, False),
    ("I-assets-pm0", False, ASSET_OBLIGATIONS, False, True),
    ("I-assets-pm1", True, ASSET_OBLIGATIONS, False, True),
    ("I-full-pm0", False, FULL_OBLIGATIONS, False, True),
    ("I-full-pm1", True, FULL_OBLIGATIONS, False, True),
    ("I-with-valid-predecessor-assets-pm0", False, ASSET_OBLIGATIONS, True, True),
    ("I-with-valid-predecessor-assets-pm1", True, ASSET_OBLIGATIONS, True, True),
    ("I-with-valid-predecessor-full-pm0", False, FULL_OBLIGATIONS, True, True),
    ("I-with-valid-predecessor-full-pm1", True, FULL_OBLIGATIONS, True, True),
    ("S-current-assets", False, ASSET_OBLIGATIONS, False, False),
    ("S-current-full", False, FULL_OBLIGATIONS, False, False),
    ("S-prior-valid-direction", False, ASSET_OBLIGATIONS, True, False),
    ("S-prior-full-flow-installed", False, FULL_OBLIGATIONS, True, False),
)
LIVES = (
    "absent:guarded-class",
    "absent:pipeline-or-es-role",
    "exact",
    "es-stamped-divergent",
    "es-foreign-divergent",
    "kibana-divergent",
    "unreadable",
)
ASSETS_ONLY_META = "not-applicable"
RECONCILIATION_NOT_APPLICABLE = "not-applicable"
RECONCILIATION_COMPLETES = "all-reobserved-exact-or-absent-creatable"
RECONCILIATION_HALTS = "halt:divergent-unverifiable-detector-positive-or-unreachable"
FLAGS = (
    "none", "repair", "upgrade", "allow-downgrade",
    "repair+upgrade", "repair+allow-downgrade",
    "upgrade+allow-downgrade", "repair+upgrade+allow-downgrade",
)


def has_version_flag(flags: str) -> bool:
    return "upgrade" in flags or "allow-downgrade" in flags


def retained(record: str, possible_mutation: bool, obligations: str, active: bool,
             predecessor: bool) -> str:
    if not active:
        return record
    suffix = ";predecessor=valid" if predecessor else ""
    return f"I[{obligations}{suffix};pm={int(possible_mutation)}]"


def normal_done(caller: str, obligations: str) -> str:
    complete = obligations if caller == "assets-only" else FULL_OBLIGATIONS
    return f"S[{complete}]"


def target_policy(record: str, predecessor: bool, flags: str, live: str) -> tuple[int, int]:
    """Return this target's writes and exit after precedence preflights."""
    if live.startswith("absent:"):
        return 1, 0
    if live == "exact":
        return 0, 0
    if live == "es-stamped-divergent":
        permitted = flags in {"repair", "repair+upgrade", "repair+allow-downgrade"}
        permitted |= record == "N" and flags == "none"
        if predecessor and has_version_flag(flags):
            permitted = flags in {"upgrade", "allow-downgrade", "repair+upgrade", "repair+allow-downgrade"}
        if permitted:
            return 1, 0
    return 0, 3


def creatable_or_exact(live: str) -> bool:
    """Whether a recovery re-observation can proceed without a refusal."""
    return live.startswith("absent:") or live == "exact"


def reconciliation_outcomes(caller: str, possible_mutation: bool, obligations: str,
                            predecessor: bool, active: bool, live: str,
                            bundle_meta_live: str) -> tuple[str, ...]:
    """Return the feasible recovery outcomes for an installing pm=true record.

    A durable write-issued edge is not itself a terminal state.  On a fresh
    invocation the engine can clear it only after every required target is
    observed exact or absent on a proven creation path.  The halt row remains
    present even when the displayed target is clean because another required
    target can be divergent, unreachable, or detector-positive.
    """
    if not possible_mutation:
        return (RECONCILIATION_NOT_APPLICABLE,)
    outcomes = [RECONCILIATION_HALTS]
    all_displayed_targets_clean = creatable_or_exact(live)
    if caller == "full-flow":
        all_displayed_targets_clean &= creatable_or_exact(bundle_meta_live)
    # R-A3: an assets-only invocation cannot discharge an active Step-11
    # obligation, so it cannot complete reconciliation for that record.  The
    # R1 engine also retains a current-version transition predecessor until a
    # direction-selected transition can complete; its pm=true reread therefore
    # remains a halt rather than silently consuming that predecessor.
    if (all_displayed_targets_clean
            and not predecessor
            and not (caller == "assets-only" and active and obligations == FULL_OBLIGATIONS)):
        outcomes.insert(0, RECONCILIATION_COMPLETES)
    return tuple(outcomes)


def oracle(caller: str, record: str, possible_mutation: bool, obligations: str,
           predecessor: bool, active: bool, live: str, flags: str,
           bundle_meta_live: str, reconciliation_outcome: str) -> tuple[str, str, str]:
    """Return exact next-record, remote-write count, and process exit.

    This ordering mirrors ``run_default_asset_transaction``: durable
    uncertainty is authoritative before version-flag preflight; the latter is
    entirely pre-read.  Full flow observes Step 11 as its own live dimension.
    """
    current = retained(record, possible_mutation, obligations, active, predecessor)

    # Ratification-2 reconciliation: pm=true is a durable uncertainty edge,
    # not a blanket terminal result.  A complete exact/creatable reread clears
    # it, writes only the currently absent targets, and promotes.  Any failed
    # reread keeps the record and exits 4; it cannot escape through local-input
    # or ordinary-refusal exits.
    if possible_mutation:
        assert reconciliation_outcome != RECONCILIATION_NOT_APPLICABLE
        if reconciliation_outcome == RECONCILIATION_HALTS:
            return current, "0", "4"
        assert reconciliation_outcome == RECONCILIATION_COMPLETES
        assert creatable_or_exact(live)
        assert caller == "assets-only" or creatable_or_exact(bundle_meta_live)
        assert not predecessor
        assert not (caller == "assets-only" and active and obligations == FULL_OBLIGATIONS)
        ordinary_writes = int(live.startswith("absent:"))
        meta_writes = int(caller == "full-flow" and bundle_meta_live.startswith("absent:"))
        return normal_done(caller, obligations), str(ordinary_writes + meta_writes), "0"
    assert reconciliation_outcome == RECONCILIATION_NOT_APPLICABLE
    # Version direction is a local pre-read validation, except where the
    # record has its validated predecessor retained by T-SM-11.
    if has_version_flag(flags) and not predecessor:
        return current, "0", "2"
    if predecessor and not has_version_flag(flags):
        return current, "0", "3"

    # R-A3: assets-only cannot execute Step 11 or consume an active full-flow
    # obligation.  With pm=0 this is a no-write refusal; pm=1 returned above.
    if caller == "assets-only" and active and obligations == FULL_OBLIGATIONS:
        return current, "0", "3"
    # The engine's installed full-flow assets-only path returns before target
    # execution; it neither runs nor recreates Step 11.
    if caller == "assets-only" and record == "S-current-full":
        return record, "0", "0"

    asset_writes, asset_exit = target_policy(record, predecessor, flags, live)
    if caller == "assets-only":
        if asset_exit:
            return current, "0", str(asset_exit)
        return normal_done(caller, obligations), str(asset_writes), "0"

    # R-A4: bundle-meta is a distinct ES target dimension.  The complete
    # pre-write observation barrier (frozen T-RECON-2 / T-FLAG-2) classifies
    # EVERY target — Kibana and ES, including Step 11 — before the first
    # mutation, so a refusal-class observation in either dimension refuses
    # with zero writes.  No static live-state row can produce a mid-apply
    # refusal; write-then-halt outcomes exist only on crash/race paths owned
    # by T-SM and T-RECON-5/7.
    meta_writes, meta_exit = target_policy(record, predecessor, flags, bundle_meta_live)
    if meta_exit or asset_exit:
        return current, "0", "3"
    return normal_done(caller, obligations), str(meta_writes + asset_writes), "0"


def cases():
    """Yield the complete asymmetric caller × target-state product."""
    for caller, state, live, flags in product(CALLERS, RECORDS, LIVES, FLAGS):
        meta_states = (ASSETS_ONLY_META,) if caller == "assets-only" else LIVES
        for bundle_meta_live in meta_states:
            _record, possible_mutation, obligations, predecessor, active = state
            for reconciliation_outcome in reconciliation_outcomes(
                    caller, possible_mutation, obligations, predecessor, active, live, bundle_meta_live):
                yield caller, state, live, flags, bundle_meta_live, reconciliation_outcome


def evaluated_cases():
    for (caller, (record, pm, obligations, predecessor, active), live, flags, meta_live,
         reconciliation_outcome) in cases():
        outcome = oracle(caller, record, pm, obligations, predecessor, active, live, flags,
                         meta_live, reconciliation_outcome)
        yield (caller, record, pm, obligations, predecessor, active, live, flags, meta_live,
               reconciliation_outcome, outcome)


def assert_consistency(rows: list[tuple]) -> None:
    # The original 5,824-row asymmetric product remains intact.  Each pm=true
    # row retains its halt outcome; 168 additionally feasible clean rereads
    # receive their explicit promotion outcome.
    expected = len(RECORDS) * len(LIVES) * len(FLAGS) * (1 + len(LIVES)) + 168
    assert len(rows) == expected
    dimensions = {(caller, record, live, flags, meta, reconciliation)
                  for (caller, record, _pm, _obligations, _predecessor, _active, live, flags,
                       meta, reconciliation, _outcome) in rows}
    assert len(dimensions) == expected
    for (caller, _record, pm, _obligations, _predecessor, _active, _live, _flags, _meta,
         reconciliation, outcome) in rows:
        if pm:
            assert outcome[2] not in {"2", "3"}
            assert (outcome[2] == "4") == (reconciliation == RECONCILIATION_HALTS)
            if reconciliation == RECONCILIATION_COMPLETES:
                assert outcome[2] == "0"
        else:
            assert reconciliation == RECONCILIATION_NOT_APPLICABLE
        assert (caller == "assets-only") == (_meta == ASSETS_ONLY_META)
    # The only allowed exception is the earlier durable-uncertainty halt.
    for (_caller, _record, pm, _obligations, predecessor, _active, _live, flags, _meta,
         _reconciliation, outcome) in rows:
        if not pm and has_version_flag(flags) and not predecessor:
            assert outcome[2] == "2"
    for (caller, record, pm, obligations, predecessor, active, _live, flags, meta,
         reconciliation, outcome) in rows:
        if (caller == "assets-only" and active and obligations == FULL_OBLIGATIONS
                and (pm or predecessor or not has_version_flag(flags))):
            assert meta == ASSETS_ONLY_META and outcome[1:] == (
                "0", "4" if pm and reconciliation == RECONCILIATION_HALTS else "3")
    full_meta = {meta for caller, *_unused, meta, _reconciliation, _outcome in rows
                 if caller == "full-flow"}
    assert full_meta == set(LIVES)
    # Frozen T-RECON-2 / T-FLAG-2 barrier invariant: a refusal-class live
    # state in EITHER dimension (never-authorized foreign/Kibana divergence or
    # unverifiable) forbids every remote write in that invocation.
    refusal_class = {"kibana-divergent", "es-foreign-divergent", "unreadable"}
    for (_caller, _record, pm, _obligations, _predecessor, _active, live, _flags, meta,
         reconciliation, outcome) in rows:
        if (live in refusal_class or meta in refusal_class) and not (
                pm and reconciliation == RECONCILIATION_COMPLETES):
            assert outcome[1] == "0", (live, meta, outcome)


def render() -> str:
    rows = list(evaluated_cases())
    assert_consistency(rows)
    rendered_rows = [
        f"| {caller} | {record} | {live} | {meta_live} | {flags} | {reconciliation} | {outcome[0]} | {outcome[1]} | {outcome[2]} |"
        for (caller, record, _pm, _obligations, _predecessor, _active, live, flags, meta_live,
             reconciliation, outcome) in rows
    ]
    row_digest = hashlib.sha256("\n".join(rendered_rows).encode("utf-8")).hexdigest()
    lines = [
        "<!-- BEGIN GENERATED FLAG-STATE TABLE -->",
        "**Generator:** `python3 gen_flag_table.py --manifest TEST-MANIFEST.md`.  "
        "**Cross product:** base 13 record states × 7 ordinary live states × 8 flag sets × "
        "(1 assets-only Step-11 state + 7 full-flow Step-11 states) = 5,824 rows, plus 168 feasible "
        f"pm=true clean-reconciliation outcomes = **{len(rows)} rows**.  "
        "For pm=true, `all-reobserved-exact-or-absent-creatable` clears durable uncertainty and promotes; "
        "the halt outcome retains it at exit 4. Pipeline/ES-role absence is clean only on the detector-negative branch; "
        "T-RECON-5/7 own detector-positive halts.",
        "",
        "| Caller | Record state | Ordinary live state | Bundle-meta live state | Flags | Reconciliation outcome | Expected next record | Remote writes | Exit |",
        "|---|---|---|---|---|---|---|---:|---:|",
        *rendered_rows,
        "",
        f"**Generated row count:** `{len(rows)}`.  **Table data rows SHA-256:** `{row_digest}`.",
        "<!-- END GENERATED FLAG-STATE TABLE -->",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, help="replace the generated block in this manifest")
    args = parser.parse_args()
    rendered = render()
    if args.manifest is None:
        print(rendered)
        return 0
    text = args.manifest.read_text(encoding="utf-8")
    begin = "<!-- BEGIN GENERATED FLAG-STATE TABLE -->"
    end = "<!-- END GENERATED FLAG-STATE TABLE -->"
    if begin not in text or end not in text:
        raise SystemExit("generated-table markers are missing from manifest")
    prefix, rest = text.split(begin, 1)
    _old, suffix = rest.split(end, 1)
    args.manifest.write_text(prefix + rendered + suffix, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
