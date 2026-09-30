#!/usr/bin/env bash
# Build every course image for linux/amd64 and push it to the platform's Azure Container
# Registry (deploy/azure/outputs.json names it; run `make deploy-azure` first, the apps start on a
# placeholder image). Each image is pushed under two tags: the git SHA (`-dirty` when the tree
# has changes; `latest` outside git), which scripts/deploy_azure.sh defaults to as well, and
# `latest`, which nw/platform/azure.py falls back to for the serving images. The digests are
# printed at the end: `make release-azure` promotes by digest, never by tag (ADR 0012).
#
#   nw-triage, nw-semantic   the serving images of the online endpoints (Azure ML custom
#                            container: the runtime answers /health and /predict on
#                            AIP_HTTP_PORT, which the deployment sets)
#   nw-policy, nw-agent      the Container Apps services (and the Foundry hosted agent image)
#   nw-mcp                   the MCP server, an internal Container App
#   nw-pipelines             the image every Azure ML pipeline step and the schedule run in
#
# Every pushed digest is signed with cosign and the platform's Key Vault key
# (`azurekms://<vault>.vault.azure.net/<NW_AZURE_SIGNING_KEY>`, signing needs Key Vault Crypto
# User on the key: the builder identity, or the platform owner); `make release-azure` verifies
# the signature before it creates a revision. The signature goes to the registry next to the
# image, not to the public transparency log (the image names stay private). NW_SIGN=0 skips
# signing (a laptop without cosign); such an image cannot be released.
#
# IMAGES="policy agent" limits the build to those names.
set -euo pipefail
cd "$(dirname "$0")/.."
PARAMS_PY=(python3 deploy/azure/scripts/azure_params.py)
ACR="${NW_AZURE_ACR:-$("${PARAMS_PY[@]}" get NW_AZURE_ACR)}"
[ -n "$ACR" ] || { echo "no registry: run make deploy-azure first (or set NW_AZURE_ACR)"; exit 1; }
REG="$ACR.azurecr.io"
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
# The full commit the image is built from: the Dockerfile keeps it as NW_IMAGE_GIT_SHA, the lineage
# a registered model and /version report (nw.platform.lineage).
GIT_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
WANTED="${IMAGES:-triage semantic policy agent mcp pipelines}"
SIGN="${NW_SIGN:-1}"
if [ "$SIGN" = "1" ] && ! command -v cosign >/dev/null 2>&1; then
  echo "cosign is not installed (brew install cosign, or github.com/sigstore/cosign releases);" >&2
  echo "NW_SIGN=0 pushes unsigned images, which make release-azure refuses" >&2
  exit 1
fi
az acr login -n "$ACR"
build() { # name app artifacts hf [extras]; extras default to the Dockerfile's
  case " $WANTED " in *" $1 "*) ;; *) return 0 ;; esac
  local extras=()
  if [ -n "${5:-}" ]; then extras=(--build-arg "EXTRAS=$5"); fi
  docker buildx build --platform linux/amd64 --build-arg "GIT_SHA=$GIT_SHA" --build-arg "APP=$2" --build-arg "ARTIFACTS=$3" --build-arg "HF_MODELS=$4" \
    ${extras[@]+"${extras[@]}"} -t "$REG/nw-$1:$TAG" -t "$REG/nw-$1:latest" --push .
}
build triage    nw.triage.service:app   "triage" 0
build semantic  nw.semantic.service:app "semantic index" 1
build policy    nw.policy.service:app   "policy" 1
build agent     nw.agent.service:app    "triage semantic index policy" 1
build mcp       mcp                     "triage semantic index policy" 1
# The pipelines image carries the Azure ML SDK so a scheduled job can submit the pipeline.
PIPELINE_EXTRAS="--extra dl --extra mlops --extra pipelines"
if grep -q '^platform-azure' pyproject.toml; then PIPELINE_EXTRAS="$PIPELINE_EXTRAS --extra platform-azure"; fi
build pipelines pipelines               "" 0 "$PIPELINE_EXTRAS"
echo "pushed to $REG with tags $TAG and latest: $WANTED"
VAULT="${NW_AZURE_KEY_VAULT:-$("${PARAMS_PY[@]}" get NW_AZURE_KEY_VAULT)}"
KEY="${NW_AZURE_SIGNING_KEY:-$("${PARAMS_PY[@]}" get NW_AZURE_SIGNING_KEY)}"
for s in $WANTED; do
  DIGEST="$(az acr repository show -n "$ACR" --image "nw-$s:$TAG" --query digest -o tsv)"
  echo "nw-$s@$DIGEST"
  if [ "$SIGN" = "1" ]; then
    AZURE_AUTH_METHOD=cli cosign sign --yes --tlog-upload=false \
      --key "azurekms://$VAULT.vault.azure.net/$KEY" "$REG/nw-$s@$DIGEST" >/dev/null
    echo "  signed with azurekms://$VAULT.vault.azure.net/$KEY"
  fi
done
echo "Next: make deploy-azure (the apps move from the placeholder to tag $TAG) or make release-azure (the live canary)."
