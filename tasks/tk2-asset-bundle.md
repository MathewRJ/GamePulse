# TK-2 — Versioned Fleet-free asset bundle (turnkey-readiness slice)

Context: STRATEGY-2026H2.md Amendment 1; Sol finding F1 (pilot-readiness spar 2026-07-22):
the release ships binaries only; `elastic/component-templates/` does not exist in the repo;
`tools/deploy_component_templates.py` silently skips missing templates. The production
cluster is the only complete source of the working asset definitions. The orchestrator has
exported them for you (you have NO network/cluster access — work only from these files):
`tools/asset-export-2026-07-22/{index-templates,component-templates,pipelines,transforms}.json`
(raw API responses, unsanitized). Do NOT commit that directory; it is input only.

## Deliverables

1. **Canonical asset tree `elastic/`** (committed, one asset per file, `json.dumps(...,
   indent=2, sort_keys=True)` deterministic formatting):
   - `elastic/index-templates/<name>.json`
   - `elastic/component-templates/<name>.json`
   - `elastic/pipelines/<name>.json`
   - `elastic/transforms/<name>.json`
   Sanitization rules (document every rule applied in `elastic/README.md`):
   - EXCLUDE `rigsignal-archive-plain` (cluster-local pre-migration archive template).
   - EXCLUDE superseded pipeline versions: keep, per stream, ONLY the pipeline actually
     referenced by that stream's index template `default_pipeline` setting (e.g. drop
     `metrics-rigsignal.audio-0.3.1` if the template references `-0.5.0`). Cross-check
     every kept template's `default_pipeline` resolves to a bundled pipeline — assert this
     in the build tool.
   - REPLACE cluster-local ILM references: `keep-forever-rollover` and
     `logs-rigsignal-stream-30d` → stock managed defaults (`logs@lifecycle` for logs
     templates; metrics templates that reference no policy stay as-is). Rationale: the
     bundle must not impose the owner's retention policy on other stacks; stock defaults
     have no delete phase.
   - STRIP volatile/server-generated fields: `created_date_millis`, `modified_date_millis`,
     any `version` fields ES injects, `installed_*` metadata. Keep `_meta` but normalize:
     set `_meta.managed_by: "rigsignal-asset-bundle"`.
2. **`tools/build_asset_bundle.py`** — assembles `dist/rigsignal-assets-<version>.tar.gz`:
   - version from `--version` arg or parsed from Cargo.toml `[package] version`.
   - `manifest.json` inside the tarball: bundle version, git commit (`--source-commit` arg,
     orchestrator supplies), per-file sha256, counts by asset type, list of included
     dashboard files.
   - Includes `dashboards/v0.3.1/*.ndjson` verbatim.
   - Build FAILS (exit 1) if: any template's `default_pipeline` is not in the bundle; any
     referenced component template (composed_of, ignoring @custom/stock `*@*` names) is
     not in the bundle; the elastic/ tree contains a file not matching the naming scheme;
     dashboards glob matches zero files.
3. **`tools/install_assets.py`** — replaces the silent-skip deploy tools (do not delete
   them; add a deprecation note at the top of each old tool's docstring):
   - Input: a bundle tarball (`--bundle`) or the repo tree (`--from-source`).
   - Auth/endpoints via env: `RIGSIGNAL_ES_URL`, `RIGSIGNAL_KB_URL`, `RIGSIGNAL_ES_AUTH`
     (either `user:pass` or `ApiKey <key>`). Never log credential values.
   - Installs in dependency order: component templates → index templates → pipelines →
     transform (create or update; do NOT start it) → Kibana saved objects via
     `_import?overwrite=true` (multipart NDJSON upload).
   - **No-skip assertion**: after install, re-read every asset from the cluster and verify
     presence; final line `installed X/X assets` — any shortfall or any per-asset error is
     exit 1 with a per-asset failure table. NEVER continue past a missing input file.
   - Writes a version marker: component template `rigsignal-bundle-meta` with
     `_meta: {bundle_version, source_commit, installed_at_field: "set by server"}` (no
     template body side effects: empty `template: {}`).
   - `--dry-run`: lists every asset + target API path, no network calls.
   - Idempotent: re-running the same bundle succeeds and changes nothing semantically.
4. **`elastic/README.md`** — what the tree is, sanitization rules applied, how to rebuild
   the export (one-liner pointing at tools/install_assets.py + the export provenance),
   bundle build + install usage, and the explicit statement that diagnostic-results
   (diagnose verdict) assets are NOT yet part of the bundle (pending the results→Kibana
   design task).

## Acceptance criteria (binary)

- AC1: `python3 -m py_compile` clean on both tools; no new dependencies beyond stdlib.
- AC2: `build_asset_bundle.py` run from a clean checkout produces a tarball whose
  manifest counts equal the file counts in `elastic/` + dashboards; sha256s verify.
- AC3: every kept index template's `default_pipeline` resolves inside the bundle
  (asserted by the build, proven by building).
- AC4: `install_assets.py --dry-run --from-source` lists every asset in the tree with
  zero omissions (count printed == manifest count).
- AC5: cross-check table in your final summary: for each of the 17 exported index
  templates → included/excluded + why; for each of the 16 pipelines → kept/dropped + why.
- AC6: no raw-export directory committed; no secrets anywhere; old deploy tools untouched
  except deprecation notes.

## Constraints

- New/changed files: `elastic/**`, `tools/build_asset_bundle.py`, `tools/install_assets.py`,
  deprecation docstring lines in the three old deploy tools, `elastic/README.md`. Nothing else.
- Commit on this branch (codex/tk2-bundle), conventional message.
- Final message: condensed summary — AC status, the AC5 cross-check table, deviations. No file dumps.
