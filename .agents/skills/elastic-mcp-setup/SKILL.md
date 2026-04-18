# Elastic Agent Builder MCP Server — Setup Skill

## What it is

The Elastic Agent Builder MCP server exposes Elasticsearch and Kibana capabilities over the Model Context Protocol. When wired into Claude Code or claude.ai, it enables live ES|QL queries, index inspection, and field validation directly inside a conversation — without copying curl commands back and forth. For GamePulse this means you can validate that a field exists and has the right type before building a dashboard panel, and inspect live session documents during debugging.

## When to use it

- ES|QL field validation before a dashboard build (confirm the field exists, check `.keyword` vs `text`, verify cardinality)
- Live inspection of index state during debugging (check what documents landed after a collection run)
- Ad-hoc aggregations to sanity-check pipeline output

## When NOT to use it

- Workflow automation — use scripts in `tools/` for anything that needs to run unattended
- Data ingestion or writes — the API key below is read-only by design
- As a dependency for any skill, test, or CI step — it is developer-side tooling only

---

## Step 1 — Create a read-only API key

Run this against your Elasticsearch endpoint (substitute `$ES_URL` and your admin credentials):

```bash
curl -X POST "$ES_URL/_security/api_key" \
  -H "Content-Type: application/json" \
  -H "Authorization: ApiKey $ES_API_KEY" \
  -d '{
    "name": "gamepulse-mcp-readonly",
    "role_descriptors": {
      "gamepulse_mcp_reader": {
        "cluster": [],
        "indices": [
          {
            "names": ["metrics-gamepulse.*", "logs-gamepulse.*"],
            "privileges": ["read", "view_index_metadata"]
          }
        ],
        "applications": [
          {
            "application": "kibana-.kibana",
            "privileges": [
              "feature_agentBuilder.read",
              "feature_actions.read"
            ],
            "resources": ["*"]
          }
        ]
      }
    }
  }'
```

The response includes an `encoded` field — that is the base64 API key. Copy it; it is shown only once.

---

## Step 2 — Wire into Claude Code

1. Copy the template to a live config:
   ```bash
   cp .mcp.json.example .mcp.json
   ```
   `.mcp.json` is gitignored — do not commit it.

2. Set the environment variable:
   ```bash
   export GAMEPULSE_MCP_API_KEY="<encoded value from Step 1>"
   ```
   Add this to your shell profile or a `.env` file that is also gitignored.

3. Restart Claude Code. The `elastic-agent-builder` server will appear in the MCP server list.

---

## Step 3 — Wire into claude.ai

In your claude.ai project settings, add the same MCP server entry from `.mcp.json.example`, substituting the real `KIBANA_URL` and `AUTH_HEADER` values. Use the same API key.

---

## Verification

After restarting, ask Claude Code to run an ES|QL query against a GamePulse index:

```esql
FROM metrics-gamepulse.cpu-*
| LIMIT 1
| KEEP @timestamp, host.name, cpu.usage_percent
```

If the MCP server is connected, results come back inline. If not, check `GAMEPULSE_MCP_API_KEY` is exported in the shell that launched Claude Code.
