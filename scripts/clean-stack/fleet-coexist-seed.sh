#!/usr/bin/env bash
# Seed precisely the 39 Fleet-owned assets used by the coexistence gate.
set -euo pipefail
REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
: "${ES_URL:?ES_URL is required}"; : "${ELASTIC_PASSWORD:?ELASTIC_PASSWORD is required}"
upgrade=0; [[ "${1:-}" != '--upgrade' ]] || upgrade=1
api() {
  local method="$1" path="$2" out
  if ! out="$(curl --silent --show-error --fail-with-body --max-redirs 0 --user "elastic:$ELASTIC_PASSWORD" -H 'Content-Type: application/json' -X "$method" "${ES_URL}${path}" "${@:3}" 2>&1)"; then
    printf 'seed FAILED %s %s: %s\n' "$method" "$path" "$out" >&2
    return 22
  fi
  printf '%s' "$out"
}
# Ratified owner-cluster shape (amendment §1/§1.1): Fleet copies differ from the
# bundle body ONLY in _meta.managed_by. Stamping more (e.g. _meta.package) would
# seed drift the ratified projection is required to refuse. --upgrade therefore
# rehearses a Fleet REINSTALL (byte-identical rewrite), not a body-changing upgrade.
meta='{"managed_by":"fleet"}'
[[ "$upgrade" == 0 ]] || meta='{"managed_by":"fleet"}'
components=(metrics-rigsignal.audio@package metrics-rigsignal.cpu@package metrics-rigsignal.ebpf@package metrics-rigsignal.ebpf_thread@package metrics-rigsignal.frame@package metrics-rigsignal.gpu@package metrics-rigsignal.memory@package metrics-rigsignal.network@package metrics-rigsignal.power@package metrics-rigsignal.session@package metrics-rigsignal.storage@package metrics-rigsignal.stream_client@package logs-rigsignal.events@package)
indexes=(logs-rigsignal.events metrics-rigsignal.audio metrics-rigsignal.cpu metrics-rigsignal.ebpf metrics-rigsignal.ebpf_thread metrics-rigsignal.frame metrics-rigsignal.gpu metrics-rigsignal.memory metrics-rigsignal.network metrics-rigsignal.power metrics-rigsignal.session metrics-rigsignal.storage metrics-rigsignal.stream_client)
pipelines=(logs-rigsignal.events-0.5.0 metrics-rigsignal.audio-0.5.0 metrics-rigsignal.cpu-0.5.0 metrics-rigsignal.ebpf-0.5.0 metrics-rigsignal.ebpf_thread-0.5.0 metrics-rigsignal.frame-0.5.0 metrics-rigsignal.gpu-0.5.0 metrics-rigsignal.memory-0.5.0 metrics-rigsignal.network-0.5.0 metrics-rigsignal.power-0.5.0 metrics-rigsignal.session-0.5.0 metrics-rigsignal.storage-0.5.0 metrics-rigsignal.stream_client-0.5.0)
# The two Fleet verification component templates exist on any real Fleet-managed
# cluster; a vanilla stack lacks them and the index-template PUTs would 400.
for name in .fleet_globals-1 .fleet_agent_id_verification-1; do api PUT "/_component_template/$name" --data-binary '{"template":{"mappings":{"_meta":{}}},"_meta":{"managed":true,"managed_by":"fleet"}}' >/dev/null; done
for name in "${components[@]}"; do f="$REPO_ROOT/elastic/component-templates/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {_meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_component_template/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
for name in "${indexes[@]}"; do f="$REPO_ROOT/elastic/index-templates/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {composed_of: ((.composed_of // []) + [".fleet_globals-1", ".fleet_agent_id_verification-1"]), _meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_index_template/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
for name in "${pipelines[@]}"; do f="$REPO_ROOT/elastic/pipelines/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {_meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_ingest/pipeline/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
