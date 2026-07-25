#!/usr/bin/env bash
# Seed precisely the 39 Fleet-owned assets used by the coexistence gate.
# The bundle-owned transform baseline is intentionally seeded locally by leg_b.
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
# Fleet component bodies are captured from the owner cluster.  They intentionally
# contribute mappings/settings beyond the bundle and make the dominance oracle a
# real gate rather than a contribution-free stub rehearsal.
meta='{"managed_by":"fleet"}'
[[ "$upgrade" == 0 ]] || meta='{"managed_by":"fleet","package":{"version":"round-18-upgrade"}}'
components=(metrics-rigsignal.audio@package metrics-rigsignal.cpu@package metrics-rigsignal.ebpf@package metrics-rigsignal.ebpf_thread@package metrics-rigsignal.frame@package metrics-rigsignal.gpu@package metrics-rigsignal.memory@package metrics-rigsignal.network@package metrics-rigsignal.power@package metrics-rigsignal.session@package metrics-rigsignal.storage@package metrics-rigsignal.stream_client@package logs-rigsignal.events@package)
indexes=(logs-rigsignal.events metrics-rigsignal.audio metrics-rigsignal.cpu metrics-rigsignal.ebpf metrics-rigsignal.ebpf_thread metrics-rigsignal.frame metrics-rigsignal.gpu metrics-rigsignal.memory metrics-rigsignal.network metrics-rigsignal.power metrics-rigsignal.session metrics-rigsignal.storage metrics-rigsignal.stream_client)
pipelines=(logs-rigsignal.events-0.5.0 metrics-rigsignal.audio-0.5.0 metrics-rigsignal.cpu-0.5.0 metrics-rigsignal.ebpf-0.5.0 metrics-rigsignal.ebpf_thread-0.5.0 metrics-rigsignal.frame-0.5.0 metrics-rigsignal.gpu-0.5.0 metrics-rigsignal.memory-0.5.0 metrics-rigsignal.network-0.5.0 metrics-rigsignal.power-0.5.0 metrics-rigsignal.session-0.5.0 metrics-rigsignal.storage-0.5.0 metrics-rigsignal.stream_client-0.5.0)
# The two Fleet verification component templates exist on any real Fleet-managed
# cluster; a vanilla stack lacks them and the index-template PUTs would 400.
for name in .fleet_globals-1 .fleet_agent_id_verification-1; do
  fixture="$REPO_ROOT/fixtures/fleet-owner-cluster/fleet_globals-1.json"
  [[ "$name" == .fleet_agent_id_verification-1 ]] && fixture="$REPO_ROOT/fixtures/fleet-owner-cluster/fleet_agent_id_verification-1.json"
  tmp="$(mktemp)"
  jq '.component_templates[0].component_template | del(.created_date_millis,.modified_date_millis)' "$fixture" >"$tmp"
  api PUT "/_component_template/$name" --data-binary "@$tmp" >/dev/null
  rm -f "$tmp"
done
for name in "${components[@]}"; do f="$REPO_ROOT/elastic/component-templates/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {_meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_component_template/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
# The owner-capture template bodies name four standard dependencies which a
# vanilla clean stack lacks.  They are only composition prerequisites here;
# the 13 target bodies below remain byte-for-byte owner captures.
for name in 'metrics@tsdb-settings' 'ecs@mappings' 'logs@mappings' 'logs@settings'; do
  api PUT "/_component_template/$name" --data-binary '{"template":{}}' >/dev/null
done
for name in "${indexes[@]}"; do
  fixture="$REPO_ROOT/fixtures/fleet-owner-cluster/live-$name.json"
  tmp="$(mktemp)"
  jq '.index_templates[0].index_template | del(.created_date_millis,.modified_date_millis)' "$fixture" >"$tmp"
  if [[ "$upgrade" == 1 ]]; then
    jq '. + {_meta: ((._meta // {}) + {package: (((._meta.package // {}) + {version:"round-18-upgrade"}))})} | .template.mappings._meta = ((.template.mappings._meta // {}) + {package: (((.template.mappings._meta.package // {}) + {version:"round-18-upgrade"}))})' "$tmp" >"$tmp.upgrade"
    mv "$tmp.upgrade" "$tmp"
  fi
  api PUT "/_index_template/$name" --data-binary "@$tmp" >/dev/null
  rm -f "$tmp"
done
for name in "${pipelines[@]}"; do f="$REPO_ROOT/elastic/pipelines/$name.json"; tmp="$(mktemp)"; jq --argjson meta "$meta" '. + {_meta: ((._meta // {}) + $meta)}' "$f" >"$tmp"; api PUT "/_ingest/pipeline/$name" --data-binary "@$tmp" >/dev/null; rm -f "$tmp"; done
