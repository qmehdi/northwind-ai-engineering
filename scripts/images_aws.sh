#!/usr/bin/env bash
# AWS track, solo mode or any deploy without the delivery pipeline: build the course images and
# the training image from a laptop and push them to the stack's ECR repositories (`make images-aws`).
# The delivery pipeline's Build stage does the same in CodeBuild; this is the path when
# NW_CONNECTION_ARN was not set and the pipeline does not exist.
#   policy, agent  linux/arm64 (the Lambda and AgentCore run arm64), the delivery build's arguments
#   pipelines      linux/amd64 (SageMaker Processing runs amd64), the steps' extras, no artifact
# Each image is pushed under the git SHA (`-dirty` with changes; NW_IMAGE_TAG overrides) and
# `latest`; the stack's `PipelineImage` output names `nw-pipelines`' `latest`, which
# `make pipeline-upsert-aws` puts in the definition. The digests are printed at the end.
# IMAGES="pipelines" builds a subset. Run after `make deploy-aws` (the repositories come from it).
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_NAME="${NW_ENV:-}"
PREFIX="northwind${ENV_NAME:+-$ENV_NAME}"
REGION="${CDK_DEFAULT_REGION:-${NW_AWS_REGION:-us-east-1}}"
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
REG="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
image_tag() {
  if [ -n "${NW_IMAGE_TAG:-}" ]; then echo "$NW_IMAGE_TAG"; return; fi
  local sha
  if sha="$(git rev-parse --short=12 HEAD 2>/dev/null)"; then
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then echo "$sha-dirty"; else echo "$sha"; fi
  else
    echo latest
  fi
}
TAG="$(image_tag)"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
build() { # name platform build-args...
  local name="$1" platform="$2"; shift 2
  local repo="$REG/$PREFIX-$name"
  aws ecr describe-repositories --region "$REGION" --repository-names "$PREFIX-$name" >/dev/null \
    || { echo "no ECR repository $PREFIX-$name: run make deploy-aws first"; exit 1; }
  docker buildx build --platform "$platform" "$@" -t "$repo:$TAG" -t "$repo:latest" --push .
}
for name in ${IMAGES:-policy agent pipelines}; do
  case "$name" in
    policy) build policy linux/arm64 --build-arg APP=nw.policy.service:app --build-arg ARTIFACTS=policy \
              --build-arg HF_MODELS=1 --build-arg LAMBDA=1 --build-arg PORT=8000 ;;
    agent) build agent linux/arm64 --build-arg APP=nw.agent.service:app \
              --build-arg "ARTIFACTS=triage semantic index policy" --build-arg HF_MODELS=1 ;;
    pipelines) build pipelines linux/amd64 --build-arg APP=pipelines --build-arg ARTIFACTS= \
              --build-arg "EXTRAS=--extra dl --extra mlops --extra pipelines" ;;
    *) echo "unknown image $name; expected policy, agent or pipelines"; exit 2 ;;
  esac
done
echo "pushed ${IMAGES:-policy agent pipelines} to $REG/$PREFIX-* with tags $TAG and latest"
for name in ${IMAGES:-policy agent pipelines}; do
  digest="$(aws ecr describe-images --region "$REGION" --repository-name "$PREFIX-$name" --image-ids "imageTag=$TAG" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || echo '<digest unavailable>')"
  echo "$REG/$PREFIX-$name@$digest"
done
echo "pipelines image for the definition: $REG/$PREFIX-pipelines:latest (the PipelineImage output; NW_AWS_PIPELINE_IMAGE overrides)"
