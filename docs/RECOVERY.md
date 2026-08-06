# Asset installation recovery

This procedure is for the operator running `rigsignal assets install`.

## What exit 4 means

Exit 4 means that remote state may be partial.  The current invocation may have
issued a mutating request, or a protected active transaction record from an
earlier invocation records `possible_mutation=true`.  Therefore a later run can
return exit 4 even when it issues no request.  It remains exit 4 until complete
re-observation and reconciliation clear the durable uncertainty.

For a pipeline or Elasticsearch role, a detected concurrent overwrite is also
an exit-4 halt.  The installer retains the transaction record and diagnostic
evidence, makes no further writes after detection, and does not delete, PUT, or
auto-restore the object.

## Operator recovery path

1. Preserve the installer output, `RIGSIGNAL_FAILURE_SITE`, and the protected
   transaction record.  Do not delete the record to force a different exit.
2. Inspect the named remote object and coordinate with its owner.  In particular,
   treat a pipeline/role detector result as possible concurrent ownership, not
   permission for the installer to overwrite it.
3. If the object must be restored, perform that restoration manually under the
   operator's change-control process after preserving the diagnostic evidence.
   The installer has no automatic restoration path.
4. Re-run the same command after the remote state is exact or has been manually
   reconciled.  A successful complete verification clears the uncertainty; an
   unresolved record continues to return exit 4.

`--repair` can reconcile only a proven RigSignal-owned Elasticsearch object.
It cannot rewrite a present divergent Kibana saved object, space, or role.
For a divergent Kibana object, delete it in Kibana and then rerun the installer
so that its guarded create path can establish the object again.
