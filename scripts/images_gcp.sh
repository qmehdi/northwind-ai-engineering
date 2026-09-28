#!/usr/bin/env bash
# Build every service image for linux/amd64 and push to Artifact Registry.
# The repository is created here, not by Terraform: the session tier creates Cloud Run
# services from these images, so the push has to land before its apply. Terraform only
# looks the repository up; `make destroy-gcp TIER=session` deletes it.
# Each image is pushed under two tags: the git SHA (`-dirty` when the tree has changes;
# `latest` outside git), which scripts/deploy_gcp.sh defaults to as well so a Cloud Run
# revision names the code it runs, and `latest`, so an explicit NW_IMAGE_TAG=latest still works.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
REGION="${NW_GCP_RUN_REGION:-us-central1}"
image_tag() {
  if [ -n "${NW_IMAGE_TAG:-}" ]; then echo "$NW_IMAGE_TAG"; return; fi
  local sha
  if sha="$(git rev-parse --short HEAD 2>/dev/null)"; then
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then echo "$sha-dirty"; else echo "$sha"; fi
  else
    echo latest
  fi
}
TAG="$(image_tag)"
REG="$REGION-docker.pkg.dev/$PROJECT/northwind"
gcloud services enable artifactregistry.googleapis.com --project "$PROJECT" --quiet
gcloud artifacts repositories describe northwind --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create northwind --repository-format=docker --location "$REGION" --project "$PROJECT" --quiet
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
build() { # name app artifacts hf
  docker buildx build --platform linux/amd64 --build-arg "APP=$2" --build-arg "ARTIFACTS=$3" --build-arg "HF_MODELS=$4" \
    -t "$REG/nw-$1:$TAG" -t "$REG/nw-$1:latest" --push . ; }
build triage   nw.triage.service:app   "triage" 0
build semantic nw.semantic.service:app "semantic index" 1
build policy   nw.policy.service:app   "policy" 1
build agent    nw.agent.service:app    "triage semantic index policy" 1
build mcp      mcp                     "triage semantic index policy" 1
echo "pushed 5 images to $REG with tags $TAG and latest"
