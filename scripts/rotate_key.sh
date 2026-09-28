#!/usr/bin/env bash
# Rotate the cohort API key on a deployed Session path and make every service pick it up.
#
#   TRACK=aws scripts/rotate_key.sh      (or NW_TRACK=aws; NW_STAGE=<word> for a staged deployment)
#   TRACK=gcp scripts/rotate_key.sh
#
# AWS: a new value becomes the secret's AWSCURRENT version. The functions read the secret
# once per cold start, so each one gets NW_KEY_ROTATED_AT bumped, a version published and
# the `live` alias moved to it at once, not through CodeDeploy: a 10 percent canary would
# refuse the new key on 90 percent of requests and the old key on the other 10, and there
# is no version of a key rotation that is right for a slice of traffic.
# GCP: a new Secret Manager version, then a new revision per service (the same env bump)
# reads `latest`; older enabled versions are disabled once every service serves again.
#
# Safe to rerun: every step is complete on its own, and a rerun rotates once more. The new
# key is printed once at the end and stored nowhere else. After a rotation
# `terraform output -raw api_key` (GCP) is stale; `aws secretsmanager get-secret-value`
# (AWS) reads the current one. Reference-stack runtimes read the secret at their own
# start, so restart them (AWS) or expect a new Agent Engine deploy (GCP) to pick it up.
set -euo pipefail
cd "$(dirname "$0")/.."
TRACK="${TRACK:-${NW_TRACK:-}}"
STAGE="${NW_STAGE:-}"
PREFIX="northwind${STAGE:+-$STAGE}"
SERVICES=(triage semantic policy agent)
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
new_key() { # 40 alphanumeric characters, the shape the deploy generated
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 20; else python3 -c 'import secrets; print(secrets.token_hex(20))'; fi
}
case "$TRACK" in
  aws)
    command -v jq >/dev/null || { echo "jq is required"; exit 2; }
    ARN="${NW_API_KEY_SECRET_ARN:-}"
    if [ -z "$ARN" ]; then
      [ -f deploy/aws/outputs.json ] || { echo "no deploy/aws/outputs.json; set NW_API_KEY_SECRET_ARN"; exit 2; }
      ARN="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(v["ApiKeySecretArn"] for k,v in d.items() if k.startswith(sys.argv[2]) and "ApiKeySecretArn" in v))' deploy/aws/outputs.json "$PREFIX-")"
    fi
    KEY="$(new_key)"
    aws secretsmanager put-secret-value --secret-id "$ARN" --secret-string "$KEY" >/dev/null
    echo "secret $ARN: new AWSCURRENT version"
    for s in "${SERVICES[@]}"; do
      fn="$PREFIX-$s"
      aws lambda wait function-updated --function-name "$fn"
      ENV="$(aws lambda get-function-configuration --function-name "$fn" --query 'Environment.Variables' | jq -c --arg t "$STAMP" '. + {NW_KEY_ROTATED_AT: $t}')"
      aws lambda update-function-configuration --function-name "$fn" --environment "{\"Variables\": $ENV}" >/dev/null
      aws lambda wait function-updated --function-name "$fn"
      v="$(aws lambda publish-version --function-name "$fn" --query Version --output text)"
      aws lambda update-alias --function-name "$fn" --name live --function-version "$v" >/dev/null
      echo "$fn: live -> version $v (reads the new key at its next cold start)"
    done ;;
  gcp)
    PROJECT="${NW_GCP_PROJECT:?set NW_GCP_PROJECT}"
    REGION="${NW_GCP_RUN_REGION:-us-central1}"
    SECRET="$PREFIX-api-key"
    KEY="$(new_key)"
    printf %s "$KEY" | gcloud secrets versions add "$SECRET" --project "$PROJECT" --data-file=- >/dev/null
    NEWEST="$(gcloud secrets versions list "$SECRET" --project "$PROJECT" --filter="state:enabled" --sort-by="~createTime" --limit=1 --format="value(name)")"
    echo "secret $SECRET: new version $NEWEST"
    for s in "${SERVICES[@]}" mcp; do
      svc="$PREFIX-$s"
      gcloud run services describe "$svc" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1 || continue
      gcloud run services update "$svc" --project "$PROJECT" --region "$REGION" --update-env-vars "NW_KEY_ROTATED_AT=$STAMP" --quiet >/dev/null
      echo "$svc: new revision reads secret version latest"
    done
    for v in $(gcloud secrets versions list "$SECRET" --project "$PROJECT" --filter="state:enabled" --format="value(name)"); do
      [ "$v" = "$NEWEST" ] && continue
      gcloud secrets versions disable "$v" --secret "$SECRET" --project "$PROJECT" --quiet >/dev/null && echo "secret version $v disabled"
    done ;;
  *) echo "usage: TRACK=aws|gcp scripts/rotate_key.sh"; exit 2 ;;
esac
echo "new API key (printed once, stored nowhere else):"
echo "$KEY"
