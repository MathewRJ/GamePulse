# Clean-stack compatibility spike

`spike.sh` is the TK-1 authoring harness for checking whether the canonical
`dashboards/v0.3.1/*.ndjson` files can be imported into an isolated Elasticsearch
and Kibana pair. It uses plain `docker run`, creates a unique network and container
names for each run, publishes random localhost ports by default, and removes only
the resources it created when it exits.

## Usage

Run this from the repository root on a host with Bash, Docker, curl, and jq:

```bash
scripts/clean-stack/spike.sh 9.4.3
scripts/clean-stack/spike.sh 9.4.3 9.4.3
scripts/clean-stack/spike.sh --dry-run 9.4.3
scripts/clean-stack/spike.sh --keep 9.4.3
```

Both version arguments must be exact `X.Y.Z` tags; `KB_VERSION` defaults to
`ES_VERSION`. `latest` is intentionally rejected. `--dry-run` prints the Docker
commands without creating anything. `--keep` leaves the uniquely named containers
and network in place for debugging; otherwise the exit trap removes them.

Set `CLEAN_STACK_ES_PORT` and/or `CLEAN_STACK_KB_PORT` to use fixed host ports.
Without them Docker assigns random localhost ports. `CLEAN_STACK_TIMEOUT_SECONDS`
changes the bounded per-service startup timeout (180 seconds by default).

Each non-dry run writes `spike-report-<ES_VERSION>.json` in the repository root.
It records the exact image tags and repository digests, timestamps, each saved-object
import result, and the result of an ES|QL query against a one-document probe index.
Import failures are findings: a completed boot and report exits successfully even
when an NDJSON file has saved-object errors. Boot, timeout, or unreachable-API
failures exit non-zero and print sanitized last-50-line container logs.

## Scope of the spike

This proves that a security-enabled single-node stack can boot at the selected tags,
that Kibana can reach it, that every canonical NDJSON file reaches Kibana's
saved-objects import API with `overwrite=true`, and that the ES|QL API path can run
a harmless query against a created probe index. It deliberately does not prove
dashboard rendering, data correctness, package installation, upgrade behavior, or
the clean-stack matrix that follows in TK-3.

The two endpoints to test are:

- minimum `9.4.3`, the production-proven endpoint;
- maximum, the newest GA 9.x version, resolved by the operator at run time.

TK-1 reports compatibility evidence for individual endpoints. Publishing a supported
version range is TK-4's job, not TK-1's.
