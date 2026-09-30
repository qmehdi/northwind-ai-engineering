#!/usr/bin/env bash
# Build every service image for linux/amd64 and push to Artifact Registry.
# The repository (named after the environment, default `northwind`) is created here when it
# does not exist yet, because the platform's Cloud Run services and Agent Engine need the
# images before `terraform apply` can create them; scripts/deploy_gcp.sh then adopts the
# repository into Terraform state. Each image is pushed under one immutable tag, the git SHA
# (with a hash of the diff when the tree has changes), which scripts/deploy_gcp.sh computes the
# same way so a revision names the code it runs; `latest` is never pushed. An image whose tag
# already exists is not rebuilt. The digests are printed at the end: Cloud Deploy releases
# promote by digest, never by tag (ADR 0012). Live images come from Cloud Build, signed
# (`make release-gcp`); these laptop builds serve the tenants' services.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
REGION="${NW_GCP_RUN_REGION:-us-central1}"
ENVIRONMENT="${NW_ENVIRONMENT:-northwind}"
# The image tag: the git SHA, plus a hash of the diff for a dirty tree, so a tag always names one
# build (the repository's tags are immutable). Never `latest`; outside git, set NW_IMAGE_TAG.
image_tag() {
  if [ -n "${NW_IMAGE_TAG:-}" ]; then echo "$NW_IMAGE_TAG"; return; fi
  local sha
  if sha="$(git rev-parse --short HEAD 2>/dev/null)"; then
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
      echo "$sha-dirty-$( (git diff HEAD; git ls-files --others --exclude-standard) | shasum | cut -c1-8)"
    else echo "$sha"; fi
  else
    echo "not a git checkout: set NW_IMAGE_TAG" >&2; exit 2
  fi
}
TAG="$(image_tag)"
# The full commit the image is built from: the Dockerfile keeps it as NW_IMAGE_GIT_SHA, the lineage
# a registered model and /version report (nw.platform.lineage).
GIT_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
REG="$REGION-docker.pkg.dev/$PROJECT/$ENVIRONMENT"
gcloud services enable artifactregistry.googleapis.com --project "$PROJECT" --quiet
gcloud artifacts repositories describe "$ENVIRONMENT" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$ENVIRONMENT" --repository-format=docker --immutable-tags --location "$REGION" --project "$PROJECT" --quiet
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
build() { # name app artifacts hf [extras]; extras default to the Dockerfile's
  if gcloud artifacts docker images describe "$REG/nw-$1:$TAG" --project "$PROJECT" >/dev/null 2>&1; then
    echo "nw-$1:$TAG exists (tags are immutable), not rebuilt"; return; fi
  local extras=()
  if [ -n "${5:-}" ]; then extras=(--build-arg "EXTRAS=$5"); fi
  docker buildx build --platform linux/amd64 --build-arg "GIT_SHA=$GIT_SHA" --build-arg "APP=$2" --build-arg "ARTIFACTS=$3" --build-arg "HF_MODELS=$4" \
    ${extras[@]+"${extras[@]}"} -t "$REG/nw-$1:$TAG" --push . ; }
build triage    nw.triage.service:app   "triage" 0
build semantic  nw.semantic.service:app "semantic index" 1
build policy    nw.policy.service:app   "policy" 1
build agent     nw.agent.service:app    "triage semantic index policy" 1
build mcp       mcp                     "triage semantic index policy" 1
# The training image every Vertex pipeline step runs in: no artifact baked in, the steps' extras.
# scripts/deploy_gcp.sh compiles the pipelines against nw-pipelines:$TAG.
build pipelines pipelines               "" 0 "--extra dl --extra mlops --extra pipelines"
echo "6 images in $REG under the tag $TAG"
for s in triage semantic policy agent mcp pipelines; do
  echo "nw-$s@$(gcloud artifacts docker images describe "$REG/nw-$s:$TAG" --project "$PROJECT" --format='value(image_summary.digest)' 2>/dev/null || echo '<digest unavailable>')"
done
