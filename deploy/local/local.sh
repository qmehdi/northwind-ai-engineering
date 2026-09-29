#!/bin/sh
# The Local track's operations, behind the Makefile's local-* targets (ADR 0011).
#   deploy/local/local.sh up | down | pull | status | wait | higher-up | bootstrap
# Environment: OBSERVABILITY=0 leaves the collector, Jaeger, Prometheus and Grafana out;
# GPU=1 adds docker-compose.gpu.yml and the gpu profile; NW_IMAGE_TAG tags the course images
# (default dev); NW_REGISTRY is the local registry (default localhost:5050).
set -eu
cd "$(dirname "$0")/../.."

FILES="-f docker-compose.yml"
PROFILES="--profile platform"
if [ "${OBSERVABILITY:-1}" = "1" ]; then PROFILES="$PROFILES --profile observability"; fi
if [ "${GPU:-0}" = "1" ]; then FILES="$FILES -f docker-compose.gpu.yml"; PROFILES="$PROFILES --profile gpu"; fi
# Images the course builds itself, pushed to the local registry so every service pulls a tag.
BUILT="triage semantic policy resolver agent agent-triage agent-policy agent-resolution mcp mlflow rustfs-init evidently drift-reports serving-stable serving-canary"

compose() { docker compose $FILES $PROFILES "$@"; }

wait_healthy() {
  # $1: seconds to wait; the rest: services that must report healthy
  budget=$1; shift
  for s in "$@"; do
    spent=0
    while :; do
      state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(compose ps -q "$s" 2>/dev/null | head -1)" 2>/dev/null || echo missing)
      case "$state" in
        healthy|running) printf '%-24s %s\n' "$s" "$state"; break ;;
      esac
      if [ "$spent" -ge "$budget" ]; then printf '%-24s %s (gave up after %ss)\n' "$s" "$state" "$budget"; break; fi
      sleep 5; spent=$((spent + 5))
    done
  done
}

status() {
  compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
  printf '\n%-16s ' "jaeger:"; curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:16686/ || echo down
  printf '%-16s ' "otel-collector:"; curl -sf -o /dev/null -w '%{http_code}\n' http://localhost:4318/ || echo "down (404 is up: the receiver answers on /v1/traces)"
  echo
  uv run python -m nw.platform.local status || true
}

case "${1:-}" in
  up)
    compose up -d registry
    wait_healthy 60 registry
    compose build triage                       # the serving image derives from it
    compose push triage
    compose build
    compose push $BUILT
    # The training image the Kubeflow DockerRunner starts for every step
    # (PIPELINE_RUNNER=docker; nw.pipelines.DEFAULT_IMAGE, override with NW_PIPELINE_IMAGE).
    docker build --build-arg APP=pipelines --build-arg ARTIFACTS= \
      --build-arg "EXTRAS=--extra dl --extra mlops --extra pipelines" -t nw-pipelines:latest .
    compose up -d
    echo; echo "waiting for the platform (the first start also pulls llama-guard3:1b and gpt-oss:20b in the background)"
    wait_healthy 240 postgres rustfs mlflow qdrant ollama litellm proxy evidently triage
    if [ -e artifacts/triage/latest ]; then
      echo; echo "registering artifacts/triage/latest as the live triage model"
      uv run python -m nw.platform.local bootstrap || echo "bootstrap failed; run make local-bootstrap after make train-triage"
    fi
    echo; status
    ;;
  down)
    compose --profile higher down
    ;;
  pull)
    compose run --rm ollama-pull
    if [ "${GPU:-0}" = "1" ]; then compose run --rm ollama-pull-120b; fi
    ;;
  status) status ;;
  wait) shift; wait_healthy "${1:-240}" postgres rustfs mlflow qdrant ollama litellm proxy evidently ;;
  higher-up)
    compose --profile higher up -d serving-stable-higher serving-canary-higher proxy-higher
    wait_healthy 60 proxy-higher
    echo "higher environment on :8105; promote with make local-promote"
    ;;
  bootstrap)
    uv run python -m nw.platform.local bootstrap
    ;;
  *)
    echo "usage: $0 up|down|pull|status|wait|higher-up|bootstrap"; exit 2 ;;
esac
