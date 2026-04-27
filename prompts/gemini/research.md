# Gemini delegation prompts — GamePulse

Patterns for delegating bulk-read and research tasks to Gemini CLI. Gemini
runs against your free Google account — no cost — so anything that
involves reading more than five files or fetching a non-trivial amount of
web content should be delegated rather than burning Claude Code tokens.

These are not agent definitions. They are reusable prompts that the
`docs-writer`, `integration-auditor`, `architect`, and `security-auditor`
agents call out to via `gemini -p "..."`.

---

## Pattern 1 — Field description audit

```
gemini -p "Read every file matching data_stream/*/fields/*.yml in the current
working directory. For each field across all files, output JSON with shape:
{
  results: [
    {
      file: '<relative path>',
      field: '<full dotted name>',
      type: '<es type>',
      has_description: <bool>,
      description: '<text or empty>',
      time_series_metric: '<gauge|counter|null>',
      unit: '<unit or null>',
      dimension: <bool>
    }
  ]
}
Return only the JSON. No commentary."
```

---

## Pattern 2 — Recent elastic/integrations PR survey

Used by `integration-auditor` to compare GamePulse against current
acceptance patterns.

```
gemini -p "Fetch https://github.com/elastic/integrations/pulls?q=is%3Apr+is%3Amerged
and identify the 5 most recently merged integration PRs. For each, fetch the
PR description and the changed files list. Output JSON:
{
  prs: [
    {
      number: <int>,
      title: '<title>',
      url: '<url>',
      merged_at: '<iso>',
      categories_in_manifest: [<strings>],
      data_stream_count: <int>,
      has_dashboards: <bool>,
      has_pipeline_tests: <bool>,
      has_system_tests: <bool>,
      readme_sections: [<strings>]
    }
  ]
}
Return only the JSON."
```

---

## Pattern 3 — Cargo dependency provenance check

Used by `security-auditor` for supply-chain review.

```
gemini -p "Read Cargo.lock in the current working directory. For each direct
dependency that is NOT one of the top 100 most-downloaded crates on
crates.io, output JSON:
{
  flagged: [
    {
      crate: '<name>',
      version: '<version>',
      source: '<registry url or git url>',
      first_published: '<iso>',
      last_updated: '<iso>',
      downloads_total: <int>,
      maintainers: [<strings>],
      known_cves: [<strings>]
    }
  ]
}
Use crates.io and rustsec.org for lookups. Return only the JSON."
```

---

## Pattern 4 — Documentation completeness sweep

Used by `docs-writer`.

```
gemini -p "Read docs/README.md and every fields.yml in data_stream/.
Identify every field documented in fields.yml that is NOT mentioned in
docs/README.md. Identify every concept in docs/README.md that is NOT
backed by at least one field. Output JSON:
{
  undocumented_fields: [<dotted-name>],
  unbacked_concepts: [{concept: '<text>', readme_section: '<heading>'}]
}
Return only the JSON."
```

---

## Pattern 5 — Dashboard reference check

Used by `dashboard-designer` and `integration-auditor`.

```
gemini -p "Read every JSON in kibana/dashboard/. For each panel, extract:
panel title, panel type, data view title, every field referenced in the
query or visualization config. Cross-reference field references against
the fields.yml files in data_stream/. Output JSON:
{
  dashboards: [
    {
      file: '<path>',
      panels: [
        {
          title: '<text>',
          type: '<lens|...>',
          data_view: '<title>',
          fields_referenced: [<dotted-names>],
          fields_missing_from_mapping: [<dotted-names>]
        }
      ]
    }
  ]
}
Return only the JSON."
```
