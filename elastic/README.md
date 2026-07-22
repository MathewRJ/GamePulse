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

The diagnostic-results (diagnose verdict) assets are **not yet part of this
bundle**, pending the results-to-Kibana design task.
