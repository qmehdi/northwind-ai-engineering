#!/usr/bin/env bash
# AWS: synth, review, deploy. TIER=session|reference. Never deploys behind a failed synth test.
set -euo pipefail
cd "$(dirname "$0")/.."
TIER="${TIER:-session}"
ACTION="${1:-deploy}"
export JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-${NW_AWS_REGION:-us-east-1}}"
CTX=(-c "tier=$TIER" -c "alertEmail=${NW_ALERT_EMAIL:-}" -c "budgetUsd=${NW_BUDGET_USD:-100}")
cd deploy/aws
case "$ACTION" in
  synth)   .venv/bin/python -m pytest -q tests && npx cdk synth "${CTX[@]}" -q ;;
  diff)    npx cdk diff "${CTX[@]}" ;;
  deploy)
    .venv/bin/python -m pytest -q tests
    echo "Read deploy/COSTS.md. Deploying tier=$TIER to account $CDK_DEFAULT_ACCOUNT in $CDK_DEFAULT_REGION."
    npx cdk bootstrap "aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION" -q
    npx cdk deploy "${CTX[@]}" --require-approval broadening --outputs-file outputs.json
    ;;
  destroy) npx cdk destroy "${CTX[@]}" --force ;;
  stop)
    # App Runner has no scale-to-zero; pausing a service still bills provisioned memory.
    # Stop means pause every northwind service; start resumes them.
    for arn in $(aws apprunner list-services --query "ServiceSummaryList[?starts_with(ServiceName,'northwind-')].ServiceArn" --output text); do
      aws apprunner pause-service --service-arn "$arn" >/dev/null && echo "paused $arn"; done ;;
  start)
    for arn in $(aws apprunner list-services --query "ServiceSummaryList[?starts_with(ServiceName,'northwind-')].ServiceArn" --output text); do
      aws apprunner resume-service --service-arn "$arn" >/dev/null && echo "resumed $arn"; done ;;
  *) echo "usage: deploy_aws.sh synth|diff|deploy|stop|start|destroy"; exit 2 ;;
esac
