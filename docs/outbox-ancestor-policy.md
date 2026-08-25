# Outbox ancestor policy

The outbox terminal object is subject to the strict
`_enrollment_parent_safe` predicate: inspect it with `lstat` without following
links; allow it to be absent; otherwise require a real directory owned by the
effective user and not group- or other-writable.

Its ancestor chain is covered by `check_install_root_ancestors`.  At every
installer call site, that enrollment-root ancestor check runs over the same
component chain immediately before the outbox check.

`check_outbox_root` deliberately retains its delegated ancestor check as
defence-in-depth.  Under the required ordering it is expected to be
unreachable-failing; the ordering is pinned by an installer unit test.

For the exit contract, see the `outbox preflight:` row in
[`assets-install-exit-contract.md`](assets-install-exit-contract.md); this
policy does not duplicate that contract.

The ruling record is maintained in the Workflow hub at
`tasks/frame-arc-2026-08-25/F6-DELTA-RULING-2026-08-25.md`.
