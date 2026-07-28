# RigSignal asset bundle

This directory is the canonical, Fleet-free RigSignal Elasticsearch asset bundle.
Each JSON file is a request body for the asset named by its filename; files are
formatted with `json.dumps(..., indent=2, sort_keys=True)`.

The assets were derived from the production-cluster API export at
`tools/asset-export-2026-07-22/` on 2026-07-22.  That directory is input-only
and deliberately is not committed.  To recreate the installed assets from this
tree, build a bundle and install it with:

```sh
python3 tools/build_asset_bundle.py --source-commit "$(git rev-parse HEAD)"
python3 tools/install_assets.py --bundle dist/rigsignal-assets-<version>.tar.gz
```

Alternatively, install the checked-out tree directly:

```sh
python3 tools/install_assets.py --from-source
```

`install_assets.py` reads `RIGSIGNAL_ES_URL`, `RIGSIGNAL_KB_URL`, and
`RIGSIGNAL_ES_AUTH` (`user:pass` or `ApiKey <key>`).  Use `--dry-run` to list
the exact target API paths without making network calls.

## Sanitization applied

- Excluded `rigsignal-archive-plain`, the cluster-local pre-migration archive
  template.
- Kept only the pipeline revisions referenced by the retained streams:
  `metrics-rigsignal.audio-0.3.1` and `metrics-rigsignal.ebpf-0.4.0` were
  removed in favor of their `0.5.0` revisions.  The build verifies every
  `default_pipeline` reference in a retained template resolves to this tree.
- Replaced the owner-local lifecycle policies `keep-forever-rollover` and
  `logs-rigsignal-stream-30d` with `logs@lifecycle` on log templates.  Metrics
  templates without an explicit policy remain unchanged.  Stock lifecycle
  defaults impose no bundle-specific delete phase.
- Removed server-generated timestamps (`created_date_millis`,
  `modified_date_millis`), server-injected root `version` fields, installed
  metadata, and transform response-only fields (`id`, `authorization`, and
  `create_time`).
- Preserved existing `_meta` and normalized `_meta.managed_by` to
  `rigsignal-asset-bundle`.
- Removed Fleet-internal component references (`.fleet_globals-1`,
  `.fleet_agent_id_verification-1`) from `composed_of`: they exist only on
  Fleet-managed clusters and make a clean Fleet-free stack reject the template
  (proven live against a fresh 9.4.4 container, 2026-07-22).
- Transform updates: `_transform/_update` rejects the immutable `pivot` field,
  so the installer strips it when updating an existing transform. A changed
  `pivot` therefore does NOT propagate via re-install — that is a breaking
  change requiring delete + recreate, by design.

The diagnostic-results (diagnose verdict) assets are **not yet part of this
bundle**, pending the results-to-Kibana design task.

## Saved-object topology preflight — refusals and operator response

Before any mutation, the installer enumerates every space and refuses rather
than risk Kibana regenerating a bundle object's id (which would leave objects
this installer cannot verify or roll back). The preflight runs for **every**
ownership profile.

| Refusal | Meaning | Operator response |
|---|---|---|
| `saved_object_topology_conflict: <type>/<id>: literal_id_exists_elsewhere space=<s>` | A bundle id already exists in another space; importing would regenerate ids. | Resolve or remove that object; its disposition is a judgment call the installer will not make for you. |
| `saved_object_topology_conflict: <type>/<id>: alias_match space=<s>` | A legacy-url-alias references a bundle id. | Remove the alias (or the object that owns it), then re-run. |
| `saved_object_topology_conflict: <type>/<id>: target_origin_derivative physical_id=<uuid> …` | An origin-derivative of a bundle id sits in the target space — typically an earlier run whose response was lost. | Run the printed `RIGSIGNAL_REMEDIATION` payload (below), then re-run. |
| `saved_object_topology_unverifiable: …` | The topology could not be proven complete (non-superuser credential, unreadable space list, malformed/paginated `_find`). | Re-run with a `superuser` credential and a reachable Kibana; never treat as clean. |
| `saved_object_id_regenerated` / `…_cleanup_failed` | Kibana regenerated an id despite the preflight (a race), or the targeted cleanup of the regenerated copy failed. | For `_cleanup_failed`, delete the named `(type, destinationId, space)` by hand, then re-run. |

**Two-part remediation.** When a refusal names both a target-space UUID orphan
and a foreign literal object, BOTH must be addressed, in order: (1) resolve or
remove every named foreign literal object by hand — no command is printed for
these on purpose; (2) execute every printed `RIGSIGNAL_REMEDIATION` line, which
is a single-line JSON payload (`method`, pre-encoded `path`, `headers`) meant to
be parsed and replayed verbatim through an authenticated client, never
hand-retyped; (3) only then re-run the installer. Deleting the UUID alone will
refuse again on the untouched foreign object.

**Credential requirement (0.3.1+).** The preflight proves complete space
visibility via `GET /_security/_authenticate` and requires the built-in
`superuser` role. An equivalently-privileged *custom* role is refused
`saved_object_topology_unverifiable: privilege_unverified` — this is a
deliberate fail-closed change; a custom admin role that worked previously must
now be swapped for a `superuser` credential for install/rollback.
