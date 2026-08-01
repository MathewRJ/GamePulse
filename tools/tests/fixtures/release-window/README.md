# Release-window fixture policy

`test_verify_release_window.sh` creates the recorded-draft shape and all 15
synthetic assets in a private temporary directory. The payloads, their seven
two-space SHA-256 sidecars, root manifest, API asset list, owner snapshot,
mock installer, and native stand-in are generated from real bytes on every
test run so the expected digests cannot be hand-maintained.

Oracle 2 is deliberately **S8c live-validated deferred** in this repository:
there is no genuine GitHub Sigstore attestation bundle plus trusted root that
can be recorded in this sandbox. Fixture mode therefore never invokes `gh`
for attestation and never uses a mock that could claim a cryptographic pass.
The script's live path invokes `gh attestation verify` for each of the 15
assets and requires a nonempty JSON array. S8c must add an authentic recorded
bundle/root and exercise pass, changed-byte, wrong-ref, and wrong-repository
offline rejects.
