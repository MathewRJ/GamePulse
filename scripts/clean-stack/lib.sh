#!/usr/bin/env bash
# Shared lifecycle helpers for isolated Elasticsearch/Kibana clean-stack runs.

set -euo pipefail

CS_DRY_RUN="${CS_DRY_RUN:-0}"
CS_KEEP="${CS_KEEP:-0}"
CS_TIMEOUT_SECONDS="${CLEAN_STACK_TIMEOUT_SECONDS:-180}"
CS_CURL_TIMEOUT_SECONDS="${CLEAN_STACK_CURL_TIMEOUT_SECONDS:-15}"
CS_BIND_ADDRESS="${CLEAN_STACK_BIND_ADDRESS:-127.0.0.1}"
CS_NETWORK_CREATED=0
CS_ES_CREATED=0
CS_KB_CREATED=0
CS_ES_VOLUME_CREATED=0
CS_KB_VOLUME_CREATED=0
CS_TLS_DIR=''
CS_CA_FILE=''

cs_usage_error() {
  printf 'error: %s\n' "$1" >&2
  return 1
}

cs_require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || {
      cs_usage_error "required command not found: $tool"
      return 1
    }
  done
}

cs_new_suffix() {
  printf '%s-%s-%s' "${BASHPID}" "${RANDOM}" "${RANDOM}"
}

cs_init_names() {
  CS_SUFFIX="$1"
  CS_NETWORK="rigsignal-clean-stack-${CS_SUFFIX}"
  CS_ES_CONTAINER="rigsignal-es-${CS_SUFFIX}"
  CS_KB_CONTAINER="rigsignal-kibana-${CS_SUFFIX}"
  CS_ES_DATA_VOLUME="rigsignal-es-data-${CS_SUFFIX}"
  CS_KB_DATA_VOLUME="rigsignal-kibana-data-${CS_SUFFIX}"
}

cs_set_tls_paths() {
  local run_dir="$1"

  CS_TLS_DIR="${run_dir}/tls"
  CS_CA_FILE="${CS_TLS_DIR}/ca.pem"
  export CS_TLS_DIR CS_CA_FILE
}

cs_prepare_tls() {
  local run_dir="$1"

  cs_require_tools openssl
  cs_set_tls_paths "$run_dir"
  umask 077
  [[ -d "$run_dir" ]] || { cs_usage_error "TLS run directory does not exist: $run_dir"; return 1; }
  mkdir -m 700 "$CS_TLS_DIR"

  # The one server certificate is presented by both HTTP endpoints.  Including
  # the ephemeral container names lets Kibana verify its in-network ES URL.
  openssl req -x509 -new -nodes -newkey rsa:2048 \
    -keyout "${CS_TLS_DIR}/ca.key" \
    -out "$CS_CA_FILE" \
    -days 1 \
    -subj '/CN=RigSignal Clean Stack CA' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' >/dev/null 2>&1
  openssl req -new -nodes -newkey rsa:2048 \
    -keyout "${CS_TLS_DIR}/server.key" \
    -out "${CS_TLS_DIR}/server.csr" \
    -subj '/CN=localhost' \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,DNS:${CS_ES_CONTAINER},DNS:${CS_KB_CONTAINER}" >/dev/null 2>&1
  openssl x509 -req \
    -in "${CS_TLS_DIR}/server.csr" \
    -CA "$CS_CA_FILE" \
    -CAkey "${CS_TLS_DIR}/ca.key" \
    -CAcreateserial \
    -out "${CS_TLS_DIR}/server.pem" \
    -days 1 \
    -sha256 \
    -copy_extensions copy >/dev/null 2>&1
  chmod 600 "$CS_TLS_DIR"/*

  # curl is used throughout the harness, including a few direct calls in the
  # matrix assertions.  Set its CA input once rather than weakening any call.
  export CURL_CA_BUNDLE="$CS_CA_FILE"
}

cs_print_command() {
  local argument
  printf 'docker'
  for argument in "$@"; do
    printf ' %q' "$argument"
  done
  printf '\n'
}

cs_docker() {
  if [[ "$CS_DRY_RUN" == '1' ]]; then
    cs_print_command "$@"
    return 0
  fi
  docker "$@"
}

cs_docker_quiet() {
  if [[ "$CS_DRY_RUN" == '1' ]]; then
    cs_print_command "$@"
    return 0
  fi
  docker "$@" >/dev/null
}

cs_port_mapping() {
  local host_port="$1"
  local container_port="$2"
  if [[ -n "$host_port" ]]; then
    printf '%s:%s:%s' "$CS_BIND_ADDRESS" "$host_port" "$container_port"
  else
    printf '%s::%s' "$CS_BIND_ADDRESS" "$container_port"
  fi
}

cs_create_network() {
  cs_docker_quiet network create "$CS_NETWORK"
  CS_NETWORK_CREATED=1
}

cs_start_elasticsearch() {
  local image="$1"
  local port_mapping="$2"

  cs_docker_quiet run --detach \
    --name "$CS_ES_CONTAINER" \
    --network "$CS_NETWORK" \
    --publish "$port_mapping" \
    --volume "${CS_TLS_DIR}:/usr/share/elasticsearch/config/certs:ro" \
    --env 'discovery.type=single-node' \
    --env 'xpack.security.enabled=true' \
    --env 'xpack.security.http.ssl.enabled=true' \
    --env 'xpack.security.http.ssl.key=/usr/share/elasticsearch/config/certs/server.key' \
    --env 'xpack.security.http.ssl.certificate=/usr/share/elasticsearch/config/certs/server.pem' \
    --env 'xpack.security.http.ssl.certificate_authorities=/usr/share/elasticsearch/config/certs/ca.pem' \
    --env ELASTIC_PASSWORD \
    "$image"
  CS_ES_CREATED=1
}

cs_create_named_volumes() {
  cs_docker_quiet volume create "$CS_ES_DATA_VOLUME"
  CS_ES_VOLUME_CREATED=1
  cs_docker_quiet volume create "$CS_KB_DATA_VOLUME"
  CS_KB_VOLUME_CREATED=1
}

cs_start_elasticsearch_with_volume() {
  local image="$1"
  local port_mapping="$2"

  cs_docker_quiet run --detach \
    --name "$CS_ES_CONTAINER" \
    --network "$CS_NETWORK" \
    --publish "$port_mapping" \
    --volume "${CS_ES_DATA_VOLUME}:/usr/share/elasticsearch/data" \
    --volume "${CS_TLS_DIR}:/usr/share/elasticsearch/config/certs:ro" \
    --env 'discovery.type=single-node' \
    --env 'xpack.security.enabled=true' \
    --env 'xpack.security.http.ssl.enabled=true' \
    --env 'xpack.security.http.ssl.key=/usr/share/elasticsearch/config/certs/server.key' \
    --env 'xpack.security.http.ssl.certificate=/usr/share/elasticsearch/config/certs/server.pem' \
    --env 'xpack.security.http.ssl.certificate_authorities=/usr/share/elasticsearch/config/certs/ca.pem' \
    --env ELASTIC_PASSWORD \
    "$image"
  CS_ES_CREATED=1
}

cs_start_kibana() {
  local image="$1"
  local port_mapping="$2"

  cs_docker_quiet run --detach \
    --name "$CS_KB_CONTAINER" \
    --network "$CS_NETWORK" \
    --publish "$port_mapping" \
    --volume "${CS_TLS_DIR}:/usr/share/kibana/config/certs:ro" \
    --env 'SERVER_HOST=0.0.0.0' \
    --env 'SERVER_SSL_ENABLED=true' \
    --env 'SERVER_SSL_CERTIFICATE=/usr/share/kibana/config/certs/server.pem' \
    --env 'SERVER_SSL_KEY=/usr/share/kibana/config/certs/server.key' \
    --env "ELASTICSEARCH_HOSTS=[\"https://${CS_ES_CONTAINER}:9200\"]" \
    --env 'ELASTICSEARCH_SSL_CERTIFICATEAUTHORITIES=["/usr/share/kibana/config/certs/ca.pem"]' \
    --env 'ELASTICSEARCH_USERNAME=kibana_system' \
    --env ELASTICSEARCH_PASSWORD \
    "$image"
  CS_KB_CREATED=1
}

cs_start_kibana_with_volume() {
  local image="$1"
  local port_mapping="$2"

  cs_docker_quiet run --detach \
    --name "$CS_KB_CONTAINER" \
    --network "$CS_NETWORK" \
    --publish "$port_mapping" \
    --volume "${CS_KB_DATA_VOLUME}:/usr/share/kibana/data" \
    --volume "${CS_TLS_DIR}:/usr/share/kibana/config/certs:ro" \
    --env 'SERVER_HOST=0.0.0.0' \
    --env 'SERVER_SSL_ENABLED=true' \
    --env 'SERVER_SSL_CERTIFICATE=/usr/share/kibana/config/certs/server.pem' \
    --env 'SERVER_SSL_KEY=/usr/share/kibana/config/certs/server.key' \
    --env "ELASTICSEARCH_HOSTS=[\"https://${CS_ES_CONTAINER}:9200\"]" \
    --env 'ELASTICSEARCH_SSL_CERTIFICATEAUTHORITIES=["/usr/share/kibana/config/certs/ca.pem"]' \
    --env 'ELASTICSEARCH_USERNAME=kibana_system' \
    --env ELASTICSEARCH_PASSWORD \
    "$image"
  CS_KB_CREATED=1
}

cs_published_port() {
  local container="$1"
  local container_port="$2"
  docker inspect --format "{{(index (index .NetworkSettings.Ports \"${container_port}\") 0).HostPort}}" "$container"
}

cs_repo_digests_json() {
  local image="$1"
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image" \
    | jq -Rsc 'split("\n") | map(select(length > 0))'
}

cs_http_to_file() {
  local output_file="$1"
  shift
  curl --silent --show-error \
    --connect-timeout 3 \
    --max-time "$CS_CURL_TIMEOUT_SECONDS" \
    --output "$output_file" \
    --write-out '%{http_code}' \
    "$@"
}

cs_status_is_success() {
  [[ "$1" =~ ^2[0-9][0-9]$ ]]
}

cs_wait_for_elasticsearch() {
  local base_url="$1"
  local username="$2"
  local password="$3"
  local response_file="$4"
  local started_at="$SECONDS"
  local body

  while (( SECONDS - started_at < CS_TIMEOUT_SECONDS )); do
    if body="$(curl --silent --show-error --connect-timeout 3 \
      --max-time "$CS_CURL_TIMEOUT_SECONDS" \
      --config <(printf 'user = "%s:%s"\n' "$username" "$password") \
      "${base_url}/_cluster/health?wait_for_status=yellow&timeout=5s" 2>/dev/null)" \
      && jq -e '(.status == "green") or (.status == "yellow")' <<<"$body" >/dev/null 2>&1; then
      printf '%s\n' "$body" >"$response_file"
      return 0
    fi
    sleep 1
  done

  return 1
}

cs_wait_for_kibana() {
  local base_url="$1"
  local username="$2"
  local password="$3"
  local response_file="$4"
  local started_at="$SECONDS"
  local body

  while (( SECONDS - started_at < CS_TIMEOUT_SECONDS )); do
    if body="$(curl --silent --show-error --connect-timeout 3 \
      --max-time "$CS_CURL_TIMEOUT_SECONDS" \
      --config <(printf 'user = "%s:%s"\n' "$username" "$password") \
      --header 'kbn-xsrf: clean-stack-spike' \
      "${base_url}/api/status" 2>/dev/null)" \
      && jq -e '.status.overall.level == "available"' <<<"$body" >/dev/null 2>&1; then
      printf '%s\n' "$body" >"$response_file"
      return 0
    fi
    sleep 1
  done

  return 1
}

cs_sanitize_line() {
  local line="$1"
  if [[ -n "${ELASTIC_PASSWORD:-}" ]]; then
    line="${line//"${ELASTIC_PASSWORD}"/[REDACTED]}"
  fi
  if [[ -n "${ELASTICSEARCH_PASSWORD:-}" ]]; then
    line="${line//"${ELASTICSEARCH_PASSWORD}"/[REDACTED]}"
  fi
  printf '%s\n' "$line"
}

cs_dump_logs() {
  local container
  for container in "$@"; do
    printf '%s\n' "--- last 50 log lines: ${container} ---" >&2
    while IFS= read -r line; do
      cs_sanitize_line "$line" >&2
    done < <(docker logs --tail 50 "$container" 2>&1 || true)
  done
}

cs_timeout_with_logs() {
  local component="$1"
  shift
  printf 'error: timed out waiting for %s after %ss\n' "$component" "$CS_TIMEOUT_SECONDS" >&2
  cs_dump_logs "$@"
  return 1
}

cs_cleanup() {
  local exit_status="$?"

  if [[ "$CS_KEEP" == '1' ]]; then
    printf 'Keeping clean-stack resources (requested with --keep): %s, %s, %s, %s, %s\n' \
      "$CS_ES_CONTAINER" "$CS_KB_CONTAINER" "$CS_NETWORK" "$CS_ES_DATA_VOLUME" "$CS_KB_DATA_VOLUME" >&2
    return "$exit_status"
  fi

  if [[ "$CS_KB_CREATED" == '1' ]]; then
    if [[ "$CS_DRY_RUN" == '1' ]]; then
      cs_docker_quiet rm --force "$CS_KB_CONTAINER"
    else
      cs_docker_quiet rm --force "$CS_KB_CONTAINER" >/dev/null 2>&1 || true
    fi
  fi
  if [[ "$CS_ES_CREATED" == '1' ]]; then
    if [[ "$CS_DRY_RUN" == '1' ]]; then
      cs_docker_quiet rm --force "$CS_ES_CONTAINER"
    else
      cs_docker_quiet rm --force "$CS_ES_CONTAINER" >/dev/null 2>&1 || true
    fi
  fi
  if [[ "$CS_NETWORK_CREATED" == '1' ]]; then
    if [[ "$CS_DRY_RUN" == '1' ]]; then
      cs_docker_quiet network rm "$CS_NETWORK"
    else
      cs_docker_quiet network rm "$CS_NETWORK" >/dev/null 2>&1 || true
    fi
  fi
  if [[ "$CS_KB_VOLUME_CREATED" == '1' ]]; then
    if [[ "$CS_DRY_RUN" == '1' ]]; then
      cs_docker_quiet volume rm "$CS_KB_DATA_VOLUME"
    else
      cs_docker_quiet volume rm "$CS_KB_DATA_VOLUME" >/dev/null 2>&1 || true
    fi
  fi
  if [[ "$CS_ES_VOLUME_CREATED" == '1' ]]; then
    if [[ "$CS_DRY_RUN" == '1' ]]; then
      cs_docker_quiet volume rm "$CS_ES_DATA_VOLUME"
    else
      cs_docker_quiet volume rm "$CS_ES_DATA_VOLUME" >/dev/null 2>&1 || true
    fi
  fi

  return "$exit_status"
}
