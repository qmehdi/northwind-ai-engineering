#!/usr/bin/env bash
# AWS track, solo mode or any deploy without the delivery pipeline: build the course images and
# the training image from a laptop and push them to the stack's ECR repositories (`make images-aws`).
# The delivery pipeline's Build stage does the same in CodeBuild; this is the path when
# NW_CONNECTION_ARN was not set and the pipeline does not exist.
#   policy, agent  linux/arm64 (the Lambda and AgentCore run arm64), the delivery build's arguments
#   pipelines      linux/amd64 (SageMaker Processing runs amd64), the steps' extras, no artifact
# Each image is pushed under the git SHA (`-dirty-<time>` with changes, because the repositories
# are tag-immutable; NW_IMAGE_TAG overrides); nothing is pushed as `latest`. The training
# image's digest is written to the SSM parameter `/<environment>/images/pipelines`, the one the
# stack's `PipelineImage` output names and `make pipeline-upsert-aws` reads. With
# NW_PROMOTE_IMAGES=1 the policy and agent digests go to `/<environment>/images/policy|agent`
# too, and the next `make deploy-aws` serves them (the delivery pipeline's Deploy stage does the
# same with a canary). When the `notation` CLI with the AWS Signer plugin is installed, every
# digest is signed with the stack's profile (output `SigningProfileArn`), which the Deploy stage
# verifies. IMAGES="pipelines" builds a subset. Run after `make deploy-aws`.
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
    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then echo "$sha-dirty-$(date +%s)"; else echo "$sha"; fi
  else
    echo latest
  fi
}
TAG="$(image_tag)"
# The full commit the image is built from: the Dockerfile keeps it as NW_IMAGE_GIT_SHA, the lineage
# a registered model and /version report (nw.platform.lineage). Empty outside git.
GIT_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
build() { # name platform build-args...
  local name="$1" platform="$2"; shift 2
  local repo="$REG/$PREFIX-$name"
  aws ecr describe-repositories --region "$REGION" --repository-names "$PREFIX-$name" >/dev/null \
    || { echo "no ECR repository $PREFIX-$name: run make deploy-aws first"; exit 1; }
  docker buildx build --platform "$platform" "$@" --build-arg "GIT_SHA=$GIT_SHA" -t "$repo:$TAG" --push .
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
echo "pushed ${IMAGES:-policy agent pipelines} to $REG/$PREFIX-* with tag $TAG"
PROFILE="$(python3 -c 'import json,sys
try:
    o=json.load(open(sys.argv[1])); o=next(v for k,v in o.items() if k.startswith("northwind")); print(o.get("SigningProfileArn",""))
except Exception:
    print("")' deploy/aws/outputs.json)"
for name in ${IMAGES:-policy agent pipelines}; do
  digest="$(aws ecr describe-images --region "$REGION" --repository-name "$PREFIX-$name" --image-ids "imageTag=$TAG" --query 'imageDetails[0].imageDigest' --output text)"
  ref="$REG/$PREFIX-$name@$digest"
  echo "$ref"
  if [ -n "$PROFILE" ] && command -v notation >/dev/null; then
    notation sign --plugin com.amazonaws.signer.notation.plugin --id "$PROFILE" "$ref" >/dev/null && echo "  signed with $PROFILE"
  fi
  if [ "$name" = pipelines ] || [ "${NW_PROMOTE_IMAGES:-0}" = 1 ]; then
    aws ssm put-parameter --region "$REGION" --name "/$PREFIX/images/$name" --type String --overwrite --value "$ref" >/dev/null
    echo "  recorded in /$PREFIX/images/$name"
  fi
done
