#!/usr/bin/env bash
# Seed precisely the 39 Fleet-owned assets used by the coexistence gate.
set -euo pipefail
REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
: "${ES_URL:?ES_URL is required}"; : "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD is required}"
upgrade=0; [[ "${1:-}" != '--upgrade' ]] || upgrade=1
api() { curl --silent --show-error --fail --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" -H 'Content-Type: application/json' -X "$1" "${ES_URL}$2" "${@:3}"; }
meta='{"managed_by":"fleet","package":{"name":"rigsignal"}}'
[[ "$upgrade" == 0 ]] || meta='{"managed_by":"fleet","package":{"name":"rigsignal","version":"upgrade"}}'
components=(metrics-rigsignal.audio@package metrics-rigsignal.cpu@package metrics-rigsignal.ebpf@package metrics-rigsignal.ebpf_thread@package metrics-rigsignal.frame@package metrics-rigsignal.gpu@package metrics-rigsignal.memory@package metrics-rigsignal.network@package metrics-rigsignal.power@package metrics-rigsignal.session@package metrics-rigsignal.storage@package metrics-rigsignal.stream_client@package logs-rigsignal.events@package)
indexes=(logs-rigsignal.events metrics-rigsignal.audio metrics-rigsignal.cpu metrics-rigsignal.ebpf metrics-rigsignal.ebpf_thread metrics-rigsignal.frame metrics-rigsignal.gpu metrics-rigsignal.memory metrics-rigsignal.network metrics-rigsignal.power metrics-rigsignal.session metrics-rigsignal.storage metrics-rigsignal.stream_client)
pipelines=(logs-rigsignal.events-0.5.0 metrics-rigsignal.audio-0.5.0 metrics-rigsignal.cpu-0.5.0 metrics-rigsignal.ebpf-0.5.0 metrics-rigsignal.ebpf_thread-0.5.0 metrics-rigsignal.frame-0.5.0 metrics-rigsignal.gpu-0.5.0 metrics-rigsignal.memory-0.5.0 metrics-rigsignal.network-0.5.0 metrics-rigsignal.power-0.5.0 metrics-rigsignal.session-0.5.0 metrics-rigsignal.storage-0.5.0 metrics-rigsignal.stream_client-0.5.0)
for name in "${components[@]}"; do f="$REPO_ROOT/elastic/component-templates/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {_meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_component_template/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
for name in "${indexes[@]}"; do f="$REPO_ROOT/elastic/index-templates/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {composed_of: ((.composed_of // []) + [".fleet_globals-1", ".fleet_agent_id_verification-1"] | unique), _meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_index_template/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
for name in "${pipelines[@]}"; do f="$REPO_ROOT/elastic/pipelines/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {_meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_ingest/pipeline/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
