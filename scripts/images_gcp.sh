#!/usr/bin/env bash
# Build every service image for linux/amd64 and push to Artifact Registry.
# The repository is created here, not by Terraform: the session tier creates Cloud Run
# services from these images, so the push has to land before its apply. Terraform only
# looks the repository up; `make destroy-gcp TIER=session` deletes it.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
REGION="${NW_GCP_RUN_REGION:-us-central1}"
TAG="${NW_IMAGE_TAG:-latest}"
REG="$REGION-docker.pkg.dev/$PROJECT/northwind"
gcloud services enable artifactregistry.googleapis.com --project "$PROJECT" --quiet
gcloud artifacts repositories describe northwind --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create northwind --repository-format=docker --location "$REGION" --project "$PROJECT" --quiet
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
build() { # name app artifacts hf
  docker buildx build --platform linux/amd64 --build-arg "APP=$2" --build-arg "ARTIFACTS=$3" --build-arg "HF_MODELS=$4" -t "$REG/nw-$1:$TAG" --push . ; }
build triage   nw.triage.service:app   "triage" 0
build semantic nw.semantic.service:app "semantic index" 1
build policy   nw.policy.service:app   "policy" 1
build agent    nw.agent.service:app    "triage semantic index policy" 1
build mcp      mcp                     "triage semantic index policy" 1
echo "pushed 5 images to $REG:$TAG"
