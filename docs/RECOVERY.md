# Asset installation recovery

If an asset installation reports a partial remote state, retain the protected
transaction record and rerun the same installer only after the remote object
state has been inspected.

`--repair` can reconcile only a proven RigSignal-owned Elasticsearch object.
It cannot rewrite a present divergent Kibana saved object, space, or role.
For a divergent Kibana object, delete it in Kibana and then rerun the installer
so that its guarded create path can establish the object again.
