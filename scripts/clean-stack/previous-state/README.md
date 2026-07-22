# Previous clean-stack asset state

This is **the 0.3.0-era production asset state, minimally adapted to boot on a
Fleet-free clean stack**. It is the documented baseline extracted from the
2026-07-22 production export: component templates, index templates, and ingest
pipelines are retained; export timestamps are removed; `.fleet_*` composed
components are removed; and the two cluster-local ILM references are replaced
with the corresponding stack-provided `logs@lifecycle` policy, as TK-2 does.

This is the honest “previous asset version” until 0.3.1 ships a real bundle
(Amendment 1 / Sol F5): upgrade testing must never install-current-twice.
