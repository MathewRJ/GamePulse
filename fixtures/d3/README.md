# D3 fixtures

These normalized UTF-8 captures are the offline oracle for `diagnose gpu-boot`.
`real/` contains redacted captures taken on 2026-07-21; `synthetic/` contains
deliberately minimal edge cases. `MANIFEST.md` records provenance and hashes,
and `EXPECTED.md` freezes the verdict/state expectations.

Use them only with explicit replay mode and a temporary state file, for example:

```bash
rigsignal-agent diagnose gpu-boot --offline \
  --pci-snapshot fixtures/d3/real/capture-a/pci-topology.txt \
  --boot-list fixtures/d3/real/capture-a/boot-inventory.txt \
  --journal-prior-tail fixtures/d3/real/capture-a/journal-previous-1-tail.log \
  --state-file /tmp/d3-state.json --learn-baseline --slot 0000:03:00.0
```

The redactions remove host/user identifiers and sensitive machine details.
They preserve boot IDs where required for pairing and normalized PCI/journal
facts needed by the detector.
